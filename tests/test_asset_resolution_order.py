"""Which picture answers a request, and in what order it is looked for.

Two faults, measured on the live Cells kit (2026-09-07), both in the LADDER
rather than in the library's matching:

1. A LOCAL CACHE ENTRY BEAT THE LIBRARY. The order was cache → library →
   generate, so a directory an earlier AI generation wrote answered first and
   the library was never consulted. Five diagrams resolved
   ``outcome: local_cache`` with ``asset_provenance: "generated"`` and
   ``library_asset_id: null`` while ``visual_assets`` held an approved,
   reviewed row for every one of them. (The avatars DID come from the library
   on the same run — they have no cache entry on a fresh container — so the
   library path itself was working.)

2. TWO SPELLINGS OF ONE PICTURE WERE TWO ASSETS. Part 1 asked
   ``levels_of_organization`` and cached it; part 2 asked
   ``levels_of_organisation``, folded to a different canonical key, found
   nothing, was deferred by the image rate limiter and shipped a scene with no
   diagram at all.

Nothing here reaches the network: the library is a fake client with a storage
shim, and the generator is never allowed to run (``allow_generate=False``), so
a test that accidentally fell through to it would fail rather than spend an
image call.
"""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path

import pytest

import shared.visual_library as vl
import shared.visual_library_integration as vli
from shared.asset_keys import canonical_key, spelling_variants
from spike.scene_engine import raster_assets as ra

PROMPT = "A whiteboard diagram of the levels of organisation in a body"


def _png(color) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGBA", (16, 16), color).save(buf, format="PNG")
    return buf.getvalue()


CACHED_BYTES = _png((10, 10, 10, 255))     # what a past generation left behind
LIBRARY_BYTES = _png((200, 30, 30, 255))   # the approved, reviewed asset


def _cache_entry(root: Path, key: str, provenance: str,
                 data: bytes = CACHED_BYTES) -> Path:
    d = root / canonical_key(key)
    d.mkdir(parents=True, exist_ok=True)
    (d / "asset.png").write_bytes(data)
    (d / "meta.json").write_text(json.dumps({
        "key": key, "prompt": PROMPT, "provenance": provenance,
        "regions": {}, "annotated_for": [], "baked_text": False,
    }), encoding="utf-8")
    return d


def _library(monkeypatch, tmp_path, rows, *, data: bytes = LIBRARY_BYTES) -> list:
    """A fake Supabase holding `rows`, over an EMPTY local index. Returns the
    list of storage paths downloaded, so a test can prove no read happened."""
    monkeypatch.setattr(vl, "LIBRARY_DIR", tmp_path / "index")
    monkeypatch.delenv("VISUAL_LIBRARY_MIN_SCORE", raising=False)
    monkeypatch.delenv("LIBRARY_OVER_GENERATED_CACHE", raising=False)
    downloads: list[str] = []

    class Storage:
        def from_(self, _bucket):
            return self

        def download(self, path):
            downloads.append(path)
            return data

    class Table:
        def select(self, *_a, **_k):
            return self

        def eq(self, *_a, **_k):
            return self

        def neq(self, *_a, **_k):
            return self

        def or_(self, *_a, **_k):
            return self

        def limit(self, *_a, **_k):
            return self

        def order(self, *_a, **_k):
            return self

        def range(self, *_a, **_k):
            return self

        def execute(self):
            return type("R", (), {"data": [dict(r) for r in rows]})()

    class SB:
        storage = Storage()

        def table(self, _name):
            return Table()

    monkeypatch.setattr(vl, "_sb", lambda: SB())
    return downloads


LEVELS_ROW = {
    "id": "va-levels",
    "asset_key": "levels_organization",
    "canonical_key": "levels_organization",
    "description": ("An educational diagram of the levels of organisation in a "
                    "body, from cell to tissue to organ to system"),
    "subject": "biology", "grade": "k12", "curriculum": "generic",
    "topic": "levels of organisation", "concepts": [], "status": "approved",
    "asset_type": "visual", "asset_format": "png",
    "storage_path": "generated/levels_organization/abcd.png",
}


def _decisions(caplog) -> list[dict]:
    """Every VISUAL_LIBRARY_DECISION row the wrapper emitted, read the way
    scripts/visual_library_report.py reads the Railway log stream."""
    out = []
    marker = vl.DECISION_PREFIX + " "
    for record in caplog.records:
        msg = record.getMessage()
        i = msg.find(marker)
        if i >= 0:
            out.append(json.loads(msg[i + len(marker):]))
    return out


def _resolve(key: str, cache: Path, caplog):
    """One resolution through the REAL wrapper, generation forbidden."""
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="shared.visual_library"):
        asset = ra.get_raster_asset(key, PROMPT, cache, False)
    rows = _decisions(caplog)
    assert len(rows) == 1, rows
    return asset, rows[0]


# ── A2: the library outranks a GENERATED cache entry ───────────────────


class TestTheLibraryOutranksAGeneratedCacheEntry:
    KEY = "levels_of_organization"

    def test_an_approved_asset_replaces_a_generated_cache_entry(self, tmp_path, monkeypatch, caplog):
        cache = tmp_path / "cache"
        entry = _cache_entry(cache, self.KEY, "generated")
        downloads = _library(monkeypatch, tmp_path, [LEVELS_ROW])

        asset, row = _resolve(self.KEY, cache, caplog)

        assert asset is not None
        assert (entry / "asset.png").read_bytes() == LIBRARY_BYTES, \
            "the cache still holds the picture nobody reviewed"
        meta = json.loads((entry / "meta.json").read_text(encoding="utf-8"))
        assert meta["provenance"] == "visual_library"
        assert meta["library_asset_id"] == "va-levels"
        assert downloads == [LEVELS_ROW["storage_path"]]

    def test_the_decision_line_says_the_library_won_over_the_cache(self, tmp_path, monkeypatch, caplog):
        """One row per request is the only record anyone judges reuse by, so
        overruling a cache entry has to be visible in it — and countable
        separately from an ordinary library hit."""
        cache = tmp_path / "cache"
        _cache_entry(cache, self.KEY, "generated")
        _library(monkeypatch, tmp_path, [LEVELS_ROW])

        _asset, row = _resolve(self.KEY, cache, caplog)

        assert row["outcome"] == "library_over_generated_cache"
        assert row["library_hit"] is True and row["library_discarded"] is None
        assert row["asset_provenance"] == "visual_library"
        assert row["library_asset_id"] == "va-levels"
        assert row["matched_id"] == "va-levels" and row["match_score"] > 0
        assert row["ai_generated"] is False and row["published"] is False

    def test_a_cache_entry_the_library_already_gave_is_not_looked_up_again(self, tmp_path, monkeypatch, caplog):
        cache = tmp_path / "cache"
        _cache_entry(cache, self.KEY, "visual_library")
        downloads = _library(monkeypatch, tmp_path, [LEVELS_ROW])

        _asset, row = _resolve(self.KEY, cache, caplog)

        assert downloads == [], "the fast path must cost no lookup at all"
        assert row["outcome"] == "local_cache"
        assert row["match_source"] == "none", "nothing was scored"

    def test_a_key_the_library_has_nothing_for_keeps_its_cache(self, tmp_path, monkeypatch, caplog):
        cache = tmp_path / "cache"
        entry = _cache_entry(cache, self.KEY, "generated")
        downloads = _library(monkeypatch, tmp_path, [])

        _asset, row = _resolve(self.KEY, cache, caplog)

        assert (entry / "asset.png").read_bytes() == CACHED_BYTES
        assert downloads == [] and row["outcome"] == "local_cache"

    def test_the_flag_restores_the_old_cache_first_order(self, tmp_path, monkeypatch, caplog):
        cache = tmp_path / "cache"
        entry = _cache_entry(cache, self.KEY, "generated")
        downloads = _library(monkeypatch, tmp_path, [LEVELS_ROW])
        monkeypatch.setenv("LIBRARY_OVER_GENERATED_CACHE", "0")

        _asset, row = _resolve(self.KEY, cache, caplog)

        assert vli.library_over_generated_cache_enabled() is False
        assert (entry / "asset.png").read_bytes() == CACHED_BYTES
        assert downloads == [] and row["outcome"] == "local_cache"

    def test_the_flag_is_on_unless_it_is_turned_off(self, monkeypatch):
        monkeypatch.delenv("LIBRARY_OVER_GENERATED_CACHE", raising=False)
        assert vli.library_over_generated_cache_enabled() is True
        for off in ("0", "false", "no", "off", "OFF"):
            monkeypatch.setenv("LIBRARY_OVER_GENERATED_CACHE", off)
            assert vli.library_over_generated_cache_enabled() is False, off

    def test_this_workers_own_cache_index_is_not_mistaken_for_the_library(self, tmp_path, monkeypatch, caplog):
        """`find` also sees the local index, and the bootstrap registers every
        generated cache entry into it — with no `id`, because it is not a
        durable row. Refreshing a file FROM ITSELF would only relabel a
        generated picture as a library hit."""
        cache = tmp_path / "cache"
        entry = _cache_entry(cache, self.KEY, "generated")
        downloads = _library(monkeypatch, tmp_path, [])
        vl.register_local({k: v for k, v in LEVELS_ROW.items()
                           if k not in ("id", "storage_path")}
                          | {"local_cache_path": str(entry / "asset.png")})

        _asset, row = _resolve(self.KEY, cache, caplog)

        assert (entry / "asset.png").read_bytes() == CACHED_BYTES
        assert downloads == [] and row["outcome"] == "local_cache"

    def test_a_replace_hydration_overwrites_where_an_ordinary_one_does_not(self, tmp_path, monkeypatch):
        """The one behaviour `replace` adds, at the level it was added."""
        cache = tmp_path / "cache"
        _cache_entry(cache, self.KEY, "generated")
        _library(monkeypatch, tmp_path, [LEVELS_ROW])
        target = cache / canonical_key(self.KEY) / "asset.png"

        assert vl.hydrate(self.KEY, PROMPT, cache, None, asset_format="png") is not None
        assert target.read_bytes() == CACHED_BYTES, "cached wins by default"
        assert vl.hydrate(self.KEY, PROMPT, cache, None, asset_format="png",
                          replace=True) is not None
        assert target.read_bytes() == LIBRARY_BYTES

    def test_a_prefound_row_is_not_scored_a_second_time(self, tmp_path, monkeypatch):
        """Every lookup is a paged read of the whole approved table; a caller
        that already scored the library must not pay for the same scan twice."""
        cache = tmp_path / "cache"
        _library(monkeypatch, tmp_path, [LEVELS_ROW])
        monkeypatch.setattr(vl, "find", lambda *a, **k: pytest.fail("re-scanned"))

        assert vl.hydrate(self.KEY, PROMPT, cache, None, asset_format="png",
                          hit=dict(LEVELS_ROW)) is not None
        assert (cache / canonical_key(self.KEY) / "asset.png").read_bytes() == LIBRARY_BYTES


# ── A3: one picture, spelled two ways ──────────────────────────────────


PAIRS = [
    ("levels_of_organisation", "levels_of_organization"),
    ("photosynthesis_analyse", "photosynthesis_analyze"),
    ("colour_wheel", "color_wheel"),
    ("centre_of_mass", "center_of_mass"),
    ("muscle_fibre", "muscle_fiber"),
    ("haemoglobin", "hemoglobin"),
    ("oesophagus", "esophagus"),
]


class TestOneWordWrittenTwoWays:
    @pytest.mark.parametrize("british,american", PAIRS)
    def test_each_pair_folds_in_both_directions(self, british, american):
        assert spelling_variants(british) == [british, american]
        assert spelling_variants(american) == [american, british]

    @pytest.mark.parametrize("key", [
        "electron_shell", "mitochondrion", "hemisphere", "electron",
        "red_blood_cell", "volcano_cross_section", "avatar_female_teacher", "",
    ])
    def test_a_word_the_list_does_not_cover_is_returned_unchanged(self, key):
        """A general transform would rewrite words it should not: 'hem' ->
        'haem' anywhere turns hemisphere into haemisphere, and 'e' -> 'oe' at
        the start turns electron into oelectron."""
        assert spelling_variants(key) == [key]

    def test_the_stored_key_is_never_rewritten(self):
        """canonical_key is pinned byte-for-byte against the app and 684
        published assets are filed under it. The alias is a LOOKUP step and
        must not change what anything is stored as."""
        assert canonical_key("levels_of_organisation") == "levels_organisation"
        assert canonical_key("levels_of_organization") == "levels_organization"
        assert canonical_key("haemoglobin") == "haemoglobin"

    def test_the_cache_of_one_spelling_answers_the_other(self, tmp_path, monkeypatch, caplog):
        """Part 1 cached `levels_of_organization`; part 2 must not pay for
        `levels_of_organisation` all over again."""
        cache = tmp_path / "cache"
        entry = _cache_entry(cache, "levels_of_organization", "generated")
        downloads = _library(monkeypatch, tmp_path, [])

        asset, row = _resolve("levels_of_organisation", cache, caplog)

        assert asset is not None, "generation was forbidden; this came from the cache"
        assert row["outcome"] == "local_cache"
        assert Path(row["asset_used"]) == entry / "asset.png"
        assert downloads == []
        assert not (cache / canonical_key("levels_of_organisation")).exists(), \
            "no second directory for the same picture"

    def test_a_miss_still_writes_under_the_key_it_was_asked_for(self, tmp_path):
        """The alias only ever RETURNS a directory that already has an asset;
        nothing is ever stored under someone else's spelling."""
        cache = tmp_path / "cache"
        assert ra.cache_dir_for("levels_of_organisation", cache) == \
            cache / canonical_key("levels_of_organisation")
        _cache_entry(cache, "levels_of_organization", "generated")
        assert ra.cache_dir_for("levels_of_organisation", cache) == \
            cache / canonical_key("levels_of_organization")
        assert ra.cache_dir_for("levels_of_organization", cache) == \
            cache / canonical_key("levels_of_organization")

    @pytest.mark.parametrize("stored,requested", [
        ("oesophagus", "esophagus"), ("esophagus", "oesophagus")])
    def test_the_library_serves_the_other_spelling(self, tmp_path, monkeypatch,
                                                   stored, requested):
        """Measured: the guard scores `esophagus` against a stored
        `oesophagus` at 0.00 and refuses it, so without the retry the picture
        is generated a second time."""
        _library(monkeypatch, tmp_path, [])
        vl.register_local({
            "asset_key": stored, "canonical_key": canonical_key(stored),
            "description": "The muscular tube that carries food down to the stomach",
            "subject": "biology", "grade": "k12", "curriculum": "generic",
            "topic": stored, "concepts": [], "status": "approved",
            "asset_type": "visual", "asset_format": "png",
            "local_cache_path": "/tmp/o.png"})
        prompt = "The muscular tube that carries food down to the stomach"

        assert vl._find_one(requested, prompt) is None, "the primary lookup misses"
        hit = vl.find(requested, prompt)
        assert hit is not None and hit["asset_key"] == stored

    def test_a_refresh_of_an_aliased_entry_binds_the_file_it_wrote(self, tmp_path, monkeypatch, caplog):
        """The two fixes meet here: the generated entry is under the OTHER
        spelling, and hydrate files its replacement under the requested key.
        The row must name the file the renderer actually binds, not the stale
        directory the lookup started from."""
        cache = tmp_path / "cache"
        _cache_entry(cache, "levels_of_organization", "generated")
        _library(monkeypatch, tmp_path, [LEVELS_ROW])

        asset, row = _resolve("levels_of_organisation", cache, caplog)

        assert asset is not None
        assert row["outcome"] == "library_over_generated_cache"
        landed = cache / canonical_key("levels_of_organisation") / "asset.png"
        assert Path(row["asset_used"]) == landed
        assert landed.read_bytes() == LIBRARY_BYTES
        assert row["asset_provenance"] == "visual_library"

    def test_the_retry_does_not_double_the_refusal_log(self, tmp_path, monkeypatch, caplog):
        """The guard was made quiet on purpose (760 lines for a 40-asset
        lesson); a spelling retry must not put the same refusal back twice."""
        _library(monkeypatch, tmp_path, [])
        vl.register_local({
            "asset_key": "catalyst_energy_profile",
            "canonical_key": "catalyst_energy_profile",
            "description": ("An energy profile of a reaction with and without a "
                            "catalyst, showing the activation energy"),
            "subject": "chemistry", "grade": "k12", "curriculum": "generic",
            "topic": "energy profile", "concepts": [], "status": "approved",
            "asset_type": "visual", "local_cache_path": "/tmp/c.png"})
        with caplog.at_level(logging.INFO, logger="shared.visual_library"):
            assert vl.find("neutralisation_energy_profile",
                           "An energy profile of a reaction with and without a "
                           "catalyst, showing the activation energy") is None
        refusals = [r.getMessage() for r in caplog.records if "refused" in r.getMessage()]
        assert len(refusals) == 1, refusals
