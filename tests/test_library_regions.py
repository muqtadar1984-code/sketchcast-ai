"""A PNG's named parts are an ASSET, not a per-deploy expense.

An SVG gets its parts free from its <g id> groups. A PNG has to buy them: one
paid vision call locates the named structures so the engine can point at them.
That asymmetry is fine. What was not fine is where the answer lived.

Measured on prod 2026-09-05: 217 approved non-avatar PNG rows in the library,
and group_count = 0 on every single one — the annotation was cached ONLY under
CACHE_DIR, which is inside the Railway worker container with no volume mounted.
Every redeploy wiped it, so every library PNG re-bought the same vision call,
forever, and 378 more diagrams were about to land on the same arrangement.

So the annotation now travels with the row. Two write paths meet here:

  * at PUBLISH, for an asset being born — which is what makes the 378 free;
  * at record_vision, for the 217 that already exist and whose row can only be
    reached by an update.

and one read path: hydrate seeds the local meta.json, and the renderer's own
cache guard then skips the call it used to pay.

No test here may make a live call. The library is offline (conftest patches
_sb) and every vision pass is a stub — a real annotate_regions reaching the
network would be the failure this file exists to prevent.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

import shared.visual_library as vl
from spike.scene_engine import raster_assets as ra


# The same document tests/test_visual_library_formats.py publishes: it clears
# the strict publish contract, so a refusal here is about this change.
SVG_DOC = """<svg viewBox="0 0 800 600">
<g id="outline"><path d="M 10 10 L 790 10 L 790 590 L 10 590 Z" stroke="black" fill="none"/></g>
<g id="nucleus"><path d="M 300 300 C 340 260, 380 300, 340 340 Q 310 360, 300 300 Z" stroke="black" fill="none"/></g>
</svg>"""

# The engine's own tail: this is the form part_names_from_prompt treats as
# authoritative, so the names below are exactly what the renderer will ask for.
PROMPT = ("A plant cell in cross-section. Name the layer groups exactly: "
          "nucleus, membrane")


def _png_bytes(width: int = 640, height: int = 480) -> bytes:
    """A real PNG — Pillow has to be able to open it and _finish has to be
    able to walk its alpha, so a fake header will not do."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ImageDraw.Draw(img).rectangle([10, 10, width - 10, height - 10],
                                  outline=(0, 0, 0, 255), width=3)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _fake_sb(seen: dict, *, columns: set[str] | None = None,
             rows: list[dict] | None = None, blow_up: str | None = None):
    """A Supabase double that refuses unknown columns the way PostgREST does
    (one message, naming the first), and records every write."""
    known = columns
    store = list(rows or [])

    class Storage:
        def from_(self, _bucket):
            return self

        def upload(self, path, fh, _opts):
            seen.setdefault("uploaded", []).append(path)

        def download(self, path):
            return seen["objects"][path]

    class Query:
        def __init__(self):
            self._filters: list[tuple] = []
            self._pending: dict | None = None

        def select(self, *_a, **_k):
            return self

        def eq(self, col, val):
            self._filters.append((col, val))
            return self

        def neq(self, *_a):
            return self

        def or_(self, *_a):
            return self

        def order(self, *_a, **_k):
            return self

        def range(self, *_a):
            return self

        def limit(self, _n):
            return self

        def execute(self):
            out = [r for r in store
                   if all(str(r.get(c)) == str(v) for c, v in self._filters)]
            if self._pending is not None:
                # An UPDATE is aimed by the filters chained AFTER it, so the
                # aim can only be known here — which is also why the suite
                # could not see it: recording the payload at .update() time
                # meant deleting the .eq("id", ...) from record_vision left
                # every test green, while against real PostgREST an
                # unfiltered PATCH stamps every row in visual_assets.
                seen.setdefault("updated", []).append(dict(self._pending))
                seen.setdefault("update_targets", []).append(
                    [str(r.get("id")) for r in out])
                for r in out:
                    r.update(self._pending)
                self._pending = None
            return type("R", (), {"data": out})()

        def _check(self, payload):
            if known is None:
                return
            unknown = sorted(set(payload) - known)
            if unknown:
                seen.setdefault("refused", []).append(unknown)
                raise RuntimeError(
                    "{'code': 'PGRST204', 'message': \"Could not find the "
                    f"'{unknown[0]}' column of 'visual_assets' in the schema "
                    "cache\"}")

        def insert(self, row):
            self._check(row)
            seen.setdefault("inserted", []).append(dict(row))
            store.append(dict(row))
            return self

        def update(self, payload):
            if blow_up:
                raise RuntimeError(blow_up)
            self._check(payload)
            self._pending = dict(payload)
            return self

    class SB:
        storage = Storage()

        def table(self, _name):
            return Query()

    return SB()


@pytest.fixture
def library(tmp_path, monkeypatch):
    """An isolated local index; the remote is added per test."""
    monkeypatch.setattr(vl, "LIBRARY_DIR", tmp_path / "idx")
    monkeypatch.setattr(vl, "DECISION_LOG", tmp_path / "decisions.jsonl")
    monkeypatch.setattr(vl, "_sb", lambda: None)
    return vl


class TestAPublishedPngCarriesWhatItsVisionCallLearned:
    """The half that makes the 378 commissioned diagrams free: they are
    annotated on the way in, so the payload is already in hand at publish."""

    def _publish(self, tmp_path, monkeypatch, metadata):
        seen: dict = {}
        monkeypatch.setattr(vl, "_sb", lambda: _fake_sb(seen))
        png = tmp_path / "asset.png"
        png.write_bytes(_png_bytes(640, 480))
        assert vl.publish_generated("plant_cell", PROMPT, png, metadata)
        return seen["inserted"][0]

    def test_the_row_holds_the_boxes_the_dimensions_and_the_parts(
            self, library, tmp_path, monkeypatch):
        row = self._publish(tmp_path, monkeypatch, {
            "provenance": "generated", "baked_text": False,
            "regions": {"nucleus": [[10, 20, 30, 40]], "membrane": []},
            "annotated_for": ["nucleus", "membrane"]})

        assert row["vision"] == {
            "regions": {"nucleus": [[10.0, 20.0, 30.0, 40.0]], "membrane": []},
            "annotated_for": ["nucleus", "membrane"],
            "baked_text": False, "w": 640, "h": 480}
        # The parts land in the SAME columns an SVG's groups do, so a lesson
        # asks one question and never branches on format.
        assert row["group_ids"] == ["nucleus"]
        assert row["group_count"] == 1

    def test_a_part_vision_could_not_see_is_answered_but_not_promised(
            self, library, tmp_path, monkeypatch):
        """'membrane' was asked for and came back with no box. It must stay in
        annotated_for — or every later lesson re-asks the same unanswerable
        question forever — and it must stay OUT of group_ids, because
        row_has_parts is a promise the renderer then has to keep."""
        row = self._publish(tmp_path, monkeypatch, {
            "regions": {"nucleus": [[1, 2, 3, 4]], "membrane": []},
            "annotated_for": ["nucleus", "membrane"]})

        assert "membrane" in row["vision"]["annotated_for"]
        assert vl.row_has_parts(row, ["nucleus"])
        assert not vl.row_has_parts(row, ["membrane"])

    def test_the_dimensions_are_measured_off_the_bytes_being_uploaded(
            self, library, tmp_path, monkeypatch):
        """A box is pixel coordinates: it means nothing without the image size
        it was measured against, and a size carried along in metadata can
        drift from the bytes. This one cannot — it is read from the IHDR of
        the exact object that goes to storage."""
        row = self._publish(tmp_path, monkeypatch, {
            "regions": {"nucleus": [[1, 2, 3, 4]]},
            "annotated_for": ["nucleus"],
            "vision": {"regions": {"nucleus": [[1, 2, 3, 4]]},
                       "annotated_for": ["nucleus"], "w": 99, "h": 99}})

        assert (row["vision"]["w"], row["vision"]["h"]) == (640, 480)

    def test_an_asset_nobody_asked_a_question_about_carries_no_payload(
            self, library, tmp_path, monkeypatch):
        """'{}' is the column default and means 'not annotated', which is true
        and re-askable. A document full of empty fields would instead claim
        'asked, and found nothing'. Omitting the key also spares one refused
        insert per publish until 0105 is applied."""
        row = self._publish(tmp_path, monkeypatch, {"provenance": "generated"})

        assert "vision" not in row
        assert row["group_ids"] == [] and row["group_count"] == 0


class TestAnSvgStillTakesItsPartsFromItsMarkup:
    def test_group_ids_come_from_the_validator_not_from_vision(
            self, library, tmp_path, monkeypatch):
        """The SVG tier is vision-free by design. Even handed a metadata dict
        full of regions, an SVG's parts must be the exact <g id>s the publish
        validator read out of the document."""
        seen: dict = {}
        monkeypatch.setattr(vl, "_sb", lambda: _fake_sb(seen))
        svg = tmp_path / "asset.svg"
        svg.write_bytes(SVG_DOC.encode("utf-8"))

        assert vl.publish_generated("chloroplast", "A chloroplast", svg, {
            "regions": {"nonsense": [[1, 2, 3, 4]]},
            "annotated_for": ["nonsense"]})

        row = seen["inserted"][0]
        assert row["group_ids"] == ["outline", "nucleus"]
        assert row["group_count"] == 2
        assert "vision" not in row


class TestTheReadPathDoesNotReAskWhatTheRowAlreadyKnows:
    """hydrate writes the matched ROW into meta.json, whose annotation lives
    under 'vision'. raster_assets has always read top-level annotated_for, so
    a hydrated file looked un-annotated and its cache guard bought vision
    again — for a picture the library already knew, on every fresh container."""

    def _hydrated(self, tmp_path, monkeypatch, vision):
        data = _png_bytes(640, 480)
        seen: dict = {"objects": {"generated/plant_cell/abc.png": data}}
        row = {"id": "row-1", "asset_key": "plant_cell",
               "canonical_key": "plant_cell", "asset_type": "visual",
               "asset_format": "png", "status": "approved",
               "description": PROMPT, "storage_path":
               "generated/plant_cell/abc.png", "group_ids": [],
               "group_count": 0, "vision": vision}
        monkeypatch.setattr(vl, "_sb", lambda: _fake_sb(seen, rows=[row]))
        monkeypatch.setattr(vl, "find", lambda *a, **k: dict(row, match_score=1.0))
        cache = tmp_path / "cache"
        assert vl.hydrate("plant_cell", PROMPT, cache, asset_format="png")
        return cache, seen

    def test_a_hydrated_png_binds_without_a_single_vision_call(
            self, library, tmp_path, monkeypatch):
        cache, _ = self._hydrated(tmp_path, monkeypatch, {
            "regions": {"nucleus": [[10, 20, 30, 40]], "membrane": []},
            "annotated_for": ["nucleus", "membrane"],
            "baked_text": False, "w": 640, "h": 480})
        calls: list = []
        monkeypatch.setattr(ra, "annotate_regions",
                            lambda *a, **k: calls.append(a) or
                            {"regions": {}, "has_text": False, "text_boxes": []})

        asset = ra._get_raster_asset("plant_cell", PROMPT, cache,
                                     allow_generate=False)

        assert calls == [], "the row already answered this question"
        assert asset is not None
        assert asset.regions["nucleus"] == [[10, 20, 30, 40]]

    def test_a_row_that_admits_it_has_baked_text_is_still_rescanned(
            self, library, tmp_path, monkeypatch):
        """The one case where re-buying the call IS the point: scrubbing needs
        text_boxes, which the payload does not carry. Seeding here would serve
        a picture with words on it, flagged, instead of cleaning it."""
        cache, _ = self._hydrated(tmp_path, monkeypatch, {
            "regions": {"nucleus": [[10, 20, 30, 40]]},
            "annotated_for": ["nucleus", "membrane"],
            "baked_text": True, "w": 640, "h": 480})
        calls: list = []
        monkeypatch.setattr(
            ra, "annotate_regions",
            lambda ink, names: calls.append(list(names)) or
            {"regions": {}, "has_text": True, "text_boxes": [[0, 0, 5, 5]]})

        ra._get_raster_asset("plant_cell", PROMPT, cache, allow_generate=False)

        assert calls == [["nucleus", "membrane"]]

    def test_boxes_measured_on_a_different_size_are_refused_not_rescaled(
            self, library, tmp_path, monkeypatch):
        """This is why w/h are inside the payload. A row whose boxes belong to
        other bytes must be DETECTED, not believed: drawing those coordinates
        on this image would put every label somewhere plausible and wrong."""
        cache, _ = self._hydrated(tmp_path, monkeypatch, {
            "regions": {"nucleus": [[10, 20, 30, 40]]},
            "annotated_for": ["nucleus", "membrane"],
            "baked_text": False, "w": 100, "h": 100})
        calls: list = []
        monkeypatch.setattr(
            ra, "annotate_regions",
            lambda ink, names: calls.append(list(names)) or
            {"regions": {"nucleus": [[1, 1, 2, 2]]}, "has_text": False,
             "text_boxes": []})

        asset = ra._get_raster_asset("plant_cell", PROMPT, cache,
                                     allow_generate=False)

        assert calls == [["nucleus", "membrane"]]
        assert asset.regions["nucleus"] == [[1, 1, 2, 2]]


class TestTheAnnotationAccumulatesInsteadOfThrashing:
    """The old guard compared annotated_for to the wanted set for EQUALITY, so
    a second lesson wanting a different part of the same picture re-annotated
    it and overwrote what the first lesson had learned. Across many lessons
    one asset ping-ponged between name sets instead of converging.

    Accumulating is safe here and nowhere else: these are pixel boxes on ONE
    unchanging image. scrub_all_text only zeroes alpha, and nothing in this
    path resizes or re-crops, so a box measured on an earlier pass is still
    true on a later one."""

    def _cached(self, tmp_path, md):
        cache = tmp_path / "cache"
        d = cache / ra.canonical_key("plant_cell")
        d.mkdir(parents=True)
        (d / "asset.png").write_bytes(_png_bytes(640, 480))
        (d / "meta.json").write_text(json.dumps(md), encoding="utf-8")
        return cache

    def test_only_the_genuinely_new_part_is_bought(
            self, library, tmp_path, monkeypatch):
        cache = self._cached(tmp_path, {
            "provenance": "generated", "baked_text": False,
            "annotated_for": ["nucleus"],
            "regions": {"nucleus": [[10, 20, 30, 40]]}})
        asked: list = []
        monkeypatch.setattr(
            ra, "annotate_regions",
            lambda ink, names: asked.append(list(names)) or
            {"regions": {"membrane": [[1, 2, 3, 4]]}, "has_text": False,
             "text_boxes": []})

        asset = ra._get_raster_asset("plant_cell", PROMPT, cache,
                                     allow_generate=False)

        assert asked == [["membrane"]], "nucleus was already answered"
        assert asset.regions == {"nucleus": [[10, 20, 30, 40]],
                                 "membrane": [[1, 2, 3, 4]]}
        md = json.loads((cache / ra.canonical_key("plant_cell")
                         / "meta.json").read_text(encoding="utf-8"))
        assert md["annotated_for"] == ["nucleus", "membrane"]

    def test_a_narrower_request_does_not_re_ask_a_wider_answer(
            self, library, tmp_path, monkeypatch):
        """The case the equality guard got exactly backwards: everything this
        lesson wants has already been asked, and one of the answers was
        'cannot see it'. Re-asking would buy the same 'no' again."""
        cache = self._cached(tmp_path, {
            "provenance": "generated", "baked_text": False,
            "annotated_for": ["nucleus", "membrane", "vacuole"],
            "regions": {"nucleus": [[10, 20, 30, 40]]}})
        calls: list = []
        monkeypatch.setattr(ra, "annotate_regions",
                            lambda *a, **k: calls.append(a) or
                            {"regions": {}, "has_text": False, "text_boxes": []})

        asset = ra._get_raster_asset("plant_cell", PROMPT, cache,
                                     allow_generate=False)

        assert calls == []
        assert asset.regions == {"nucleus": [[10, 20, 30, 40]]}


class TestTheRowLearnsFromTheRenderThatBoundIt:
    """The half that converges the 217 rows that already exist. Their row was
    written before the column did, so publishing cannot reach them — only an
    update can. One vision call per asset EVER, instead of one per deploy."""

    def _bind(self, tmp_path, monkeypatch, seen, annotate):
        data = _png_bytes(640, 480)
        seen["objects"] = {"generated/plant_cell/abc.png": data}
        row = {"id": "row-1", "asset_key": "plant_cell",
               "canonical_key": "plant_cell", "asset_type": "visual",
               "asset_format": "png", "status": "approved",
               "description": PROMPT,
               "storage_path": "generated/plant_cell/abc.png"}
        sb = _fake_sb(seen, rows=[row])
        monkeypatch.setattr(vl, "_sb", lambda: sb)
        monkeypatch.setattr(vl, "find", lambda *a, **k: dict(row, match_score=1.0))
        cache = tmp_path / "cache"
        assert vl.hydrate("plant_cell", PROMPT, cache, asset_format="png")
        monkeypatch.setattr(ra, "annotate_regions", annotate)
        return ra._get_raster_asset("plant_cell", PROMPT, cache,
                                    allow_generate=False), cache

    def test_a_fresh_annotation_is_written_back_to_the_row_it_came_from(
            self, library, tmp_path, monkeypatch):
        seen: dict = {}
        self._bind(tmp_path, monkeypatch, seen, lambda ink, names: {
            "regions": {"nucleus": [[10, 20, 30, 40]], "membrane": []},
            "has_text": False, "text_boxes": []})

        assert len(seen["updated"]) == 1
        update = seen["updated"][0]
        assert update["vision"]["regions"]["nucleus"] == [[10.0, 20.0, 30.0, 40.0]]
        assert update["vision"]["annotated_for"] == ["nucleus", "membrane"]
        assert (update["vision"]["w"], update["vision"]["h"]) == (640, 480)
        assert update["group_ids"] == ["nucleus"] and update["group_count"] == 1

    def test_the_row_is_told_what_the_STORED_bytes_show_not_the_scrubbed_copy(
            self, library, tmp_path, monkeypatch):
        """scrub_all_text zeroes alpha on the LOCAL file only. The boxes stay
        valid for both copies, but the object in storage still carries the
        words, so the row must not be told the asset is clean."""
        seen: dict = {}
        _, cache = self._bind(tmp_path, monkeypatch, seen, lambda ink, names: {
            "regions": {"nucleus": [[10, 20, 30, 40]]}, "has_text": True,
            "text_boxes": [[0, 0, 5, 5]]})

        assert seen["updated"][0]["vision"]["baked_text"] is True
        md = json.loads((cache / ra.canonical_key("plant_cell")
                         / "meta.json").read_text(encoding="utf-8"))
        assert md["baked_text"] is False, "the local copy was scrubbed"

    def test_a_locally_generated_asset_writes_to_no_row(
            self, library, tmp_path, monkeypatch):
        """There is no row yet — publish will make one. An update aimed at an
        id nobody has would either fail or, worse, hit somebody else's."""
        seen: dict = {}
        monkeypatch.setattr(vl, "_sb", lambda: _fake_sb(seen))
        cache = tmp_path / "cache"
        d = cache / ra.canonical_key("plant_cell")
        d.mkdir(parents=True)
        (d / "asset.png").write_bytes(_png_bytes(640, 480))
        (d / "meta.json").write_text(json.dumps({"provenance": "generated"}),
                                     encoding="utf-8")
        monkeypatch.setattr(ra, "annotate_regions", lambda ink, names: {
            "regions": {"nucleus": [[1, 2, 3, 4]]}, "has_text": False,
            "text_boxes": []})

        ra._get_raster_asset("plant_cell", PROMPT, cache, allow_generate=False)

        assert "updated" not in seen
        md = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        assert md["vision"]["w"] == 640


class TestRecordVisionNeverKillsARender:
    """A render must not fail because a cost optimisation could not write.
    The worst case is the next worker paying what this one just paid — which
    is precisely today's behaviour, so there is nothing to lose by failing."""

    PAYLOAD = {"regions": {"nucleus": [[1, 2, 3, 4]], "membrane": []},
               "annotated_for": ["nucleus", "membrane"],
               "baked_text": False, "w": 640, "h": 480}

    # The live column list, measured read-only on 2026-09-05 with 0104 applied
    # (version 20260905054301) and 0105 not.
    PROD_0104 = {"id", "asset_key", "canonical_key", "asset_type", "role",
                 "description", "curriculum", "subject", "grade", "age_band",
                 "topic", "concepts", "status", "provenance", "source",
                 "storage_path", "content_hash", "quality", "usage_count",
                 "last_used_at", "created_at", "updated_at", "asset_format",
                 "group_ids", "group_count"}

    def test_a_database_without_0105_returns_false_and_raises_nothing(
            self, library, monkeypatch):
        seen: dict = {}
        monkeypatch.setattr(vl, "_sb",
                            lambda: _fake_sb(seen, columns=self.PROD_0104))

        assert vl.record_vision("row-1", self.PAYLOAD) is False

        assert seen["refused"] == [["vision"]]
        # …but the parts still land: group_ids/group_count are 0104 columns,
        # they are live today, and they are what row_has_parts answers from.
        assert seen["updated"] == [{"group_ids": ["nucleus"], "group_count": 1}]

    def test_it_lands_the_whole_payload_once_the_column_exists(
            self, library, monkeypatch):
        seen: dict = {}
        monkeypatch.setattr(
            vl, "_sb",
            lambda: _fake_sb(seen, columns=self.PROD_0104 | {"vision"}))

        assert vl.record_vision("row-1", self.PAYLOAD) is True
        assert seen["updated"][0]["vision"] == self.PAYLOAD

    def test_a_failure_that_is_not_a_schema_miss_is_survived_too(
            self, library, monkeypatch):
        seen: dict = {}
        monkeypatch.setattr(
            vl, "_sb",
            lambda: _fake_sb(seen, blow_up="connection reset by peer"))

        assert vl.record_vision("row-1", self.PAYLOAD) is False

    def test_an_offline_library_is_simply_a_no(self, library):
        assert vl.record_vision("row-1", self.PAYLOAD) is False
        assert vl.record_vision("", self.PAYLOAD) is False
        assert vl.record_vision("row-1", {}) is False


class TestTheMigrationIsSafeToRunOnProduction:
    """The founder applies this by hand against 230 live rows; the file has to
    be readable as a promise, not just as DDL. Sibling of the 0104 class in
    tests/test_visual_library.py."""

    def _sql(self) -> str:
        p = (Path(__file__).resolve().parents[1] / "database"
             / "0106_visual_assets_vision.sql")
        assert p.exists(), "the prod migration is missing"
        return p.read_text(encoding="utf-8")

    def test_the_structural_statement_is_idempotent(self):
        sql = self._sql().lower()
        assert ("add column if not exists vision jsonb not null "
                "default '{}'::jsonb") in sql
        body = sql.split("begin;", 1)[1]
        assert body.count("alter table") == 1, "one column, one ALTER"

    def test_it_touches_nothing_that_already_exists(self):
        """vision is a NEW axis. No existing row, column, constraint or stored
        object may move — including 0104's, which IS applied to prod."""
        import re
        body = self._sql().lower().split("begin;", 1)[1]
        stripped = "\n".join(line.split("--", 1)[0]
                             for line in body.splitlines())
        stripped = re.sub(r"comment on\b.*?';", "", stripped, flags=re.S)
        assert "comment on" not in stripped
        for forbidden in ("asset_type", "asset_format", "group_ids",
                          "group_count", "drop column", "storage.objects",
                          "storage_path", "create index"):
            assert forbidden not in stripped, forbidden

    def test_it_says_who_applies_it(self):
        assert "NOT APPLIED BY ANY AGENT" in self._sql()

    def test_the_checked_in_ddl_carries_the_same_column(self):
        ddl = (Path(__file__).resolve().parents[1] / "database"
               / "visual_asset_library.sql").read_text(encoding="utf-8").lower()
        assert "vision jsonb not null default '{}'::jsonb" in ddl


class TestTheRowIsWidenedByABind_NeverNarrowedByOne:
    """record_vision REPLACES the columns it writes, so what it sends has to
    be the union of the row and the container. The accumulation used to live
    only in the container's meta.json, and three reachable paths hand it a
    baseline NARROWER than the row: a row flagged baked_text (which
    _lift_library_vision deliberately will not seed from), a local meta that
    already carried its own older annotated_for, and — every single bind while
    0105 is unapplied, which is prod today — no stored payload to seed from at
    all. Each of those rewrote the row as the one lesson that touched it last.
    """

    PROD_0104 = TestRecordVisionNeverKillsARender.PROD_0104

    def _row(self, **over):
        row = {"id": "row-1", "asset_key": "plant_cell",
               "canonical_key": "plant_cell", "asset_type": "visual",
               "asset_format": "png", "status": "approved",
               "storage_path": "generated/plant_cell/abc.png",
               "group_ids": [], "group_count": 0}
        row.update(over)
        return row

    def test_three_lessons_worth_of_boxes_survive_a_one_part_bind(
            self, library, monkeypatch):
        """The row knows stoma, cuticle and xylem. A lesson naming only stoma
        must not cost the other two — they were paid for."""
        stored = vl.vision_payload(
            {"stoma": [[1, 1, 2, 2]], "cuticle": [[3, 3, 4, 4]],
             "xylem": [[5, 5, 6, 6]]},
            ["stoma", "cuticle", "xylem"], False, 640, 480)
        rows = [self._row(vision=stored,
                          group_ids=["stoma", "cuticle", "xylem"],
                          group_count=3)]
        seen: dict = {}
        monkeypatch.setattr(vl, "_sb", lambda: _fake_sb(
            seen, columns=self.PROD_0104 | {"vision"}, rows=rows))

        assert vl.record_vision("row-1", vl.vision_payload(
            {"stoma": [[9, 9, 10, 10]]}, ["stoma"], False, 640, 480))

        after = rows[0]["vision"]
        assert set(after["regions"]) == {"stoma", "cuticle", "xylem"}
        # the fresher measurement of the part this lesson asked about wins…
        assert after["regions"]["stoma"] == [[9.0, 9.0, 10.0, 10.0]]
        # …and the parts it did not ask about are untouched
        assert after["regions"]["xylem"] == [[5.0, 5.0, 6.0, 6.0]]
        assert sorted(rows[0]["group_ids"]) == ["cuticle", "stoma", "xylem"]
        assert rows[0]["group_count"] == 3

    def test_a_pass_that_cannot_see_a_part_does_not_erase_the_pass_that_could(
            self, library, monkeypatch):
        """Vision under-reports stochastically — the module scrubs and rescans
        for exactly that reason. An empty answer must mark the part ANSWERED
        (or it is re-asked forever) without deleting a box already bought."""
        stored = vl.vision_payload({"nucleus": [[1, 1, 2, 2]]},
                                   ["nucleus"], False, 640, 480)
        rows = [self._row(vision=stored, group_ids=["nucleus"], group_count=1)]
        seen: dict = {}
        monkeypatch.setattr(vl, "_sb", lambda: _fake_sb(
            seen, columns=self.PROD_0104 | {"vision"}, rows=rows))

        vl.record_vision("row-1", vl.vision_payload(
            {"nucleus": [], "membrane": [[7, 7, 8, 8]]},
            ["nucleus", "membrane"], False, 640, 480))

        assert rows[0]["vision"]["regions"]["nucleus"] == [[1.0, 1.0, 2.0, 2.0]]
        assert sorted(rows[0]["group_ids"]) == ["membrane", "nucleus"]

    def test_baked_text_latches_true_on_the_row(self, library, monkeypatch):
        """scrub_all_text cleans the LOCAL copy only. Once the stored object is
        known to carry words, no later pass over a scrubbed file may tell the
        row otherwise — the read path skips the rescan for a row saying False.
        """
        stored = vl.vision_payload({"nucleus": [[1, 1, 2, 2]]}, ["nucleus"],
                                   True, 640, 480)
        rows = [self._row(vision=stored)]
        seen: dict = {}
        monkeypatch.setattr(vl, "_sb", lambda: _fake_sb(
            seen, columns=self.PROD_0104 | {"vision"}, rows=rows))

        vl.record_vision("row-1", vl.vision_payload(
            {"membrane": [[7, 7, 8, 8]]}, ["membrane"], False, 640, 480))

        assert rows[0]["vision"]["baked_text"] is True

    def test_boxes_for_another_size_replace_rather_than_mix(
            self, library, monkeypatch):
        """Regions are pixel coordinates. Two payloads that disagree about the
        dimensions they were measured on describe different images, so the one
        measured against the bytes in hand wins outright — and the group names
        go with it, because group_ids is a promise the renderer has to keep."""
        stored = vl.vision_payload({"nucleus": [[1, 1, 2, 2]]}, ["nucleus"],
                                   False, 100, 100)
        rows = [self._row(vision=stored, group_ids=["nucleus"], group_count=1)]
        seen: dict = {}
        monkeypatch.setattr(vl, "_sb", lambda: _fake_sb(
            seen, columns=self.PROD_0104 | {"vision"}, rows=rows))

        vl.record_vision("row-1", vl.vision_payload(
            {"membrane": [[7, 7, 8, 8]]}, ["membrane"], False, 640, 480))

        assert set(rows[0]["vision"]["regions"]) == {"membrane"}
        assert rows[0]["group_ids"] == ["membrane"]

    def test_the_parts_still_accumulate_while_0105_is_unapplied(
            self, library, monkeypatch):
        """Prod as measured: 0104 applied, 0105 not. There is no payload to
        merge against, so group_ids is the ONLY thing a bind leaves behind —
        and it was being rewritten from scratch on every one of them."""
        rows = [self._row(group_ids=["nucleus"], group_count=1)]
        seen: dict = {}
        monkeypatch.setattr(vl, "_sb", lambda: _fake_sb(
            seen, columns=self.PROD_0104, rows=rows))

        assert vl.record_vision("row-1", vl.vision_payload(
            {"membrane": [[7, 7, 8, 8]]}, ["membrane"], False, 640, 480)) is False

        assert rows[0]["group_ids"] == ["nucleus", "membrane"]
        assert rows[0]["group_count"] == 2

    def test_the_update_is_aimed_at_one_row_and_not_at_the_table(
            self, library, monkeypatch):
        """An unfiltered PATCH is not a no-op in PostgREST — it stamps every
        row in visual_assets with this asset's boxes."""
        rows = [self._row(), self._row(id="row-2", asset_key="volcano",
                                       canonical_key="volcano")]
        seen: dict = {}
        monkeypatch.setattr(vl, "_sb", lambda: _fake_sb(
            seen, columns=self.PROD_0104 | {"vision"}, rows=rows))

        vl.record_vision("row-1", vl.vision_payload(
            {"nucleus": [[1, 2, 3, 4]]}, ["nucleus"], False, 640, 480))

        assert seen["update_targets"] == [["row-1"]]
        assert rows[1]["group_ids"] == [] and "vision" not in rows[1]


class TestTheRendererSeesThisPromptsPartsAndNoOthers:
    """`regions` is not only a box cache — it is the NAMESPACE render.py
    matches layer names against, and match_layer_ids' second rung is bare
    substring containment. That rung is safe only while the keys are the parts
    of the prompt being rendered, which the old replace-everything code kept
    by accident. Accumulating across lessons widens what the fallback can hit,
    and a label then anchors confidently on a foreign part."""

    def _cached(self, tmp_path, md):
        cache = tmp_path / "cache"
        d = cache / ra.canonical_key("plant_cell")
        d.mkdir(parents=True)
        (d / "asset.png").write_bytes(_png_bytes(640, 480))
        (d / "meta.json").write_text(json.dumps(md), encoding="utf-8")
        return cache

    def test_another_lessons_nuclear_membrane_cannot_answer_for_membrane(
            self, library, tmp_path, monkeypatch):
        from spike.scene_engine.vector_assets import match_layer_ids
        cache = self._cached(tmp_path, {
            "provenance": "generated", "baked_text": False,
            "annotated_for": ["nucleus", "nuclear membrane"],
            "regions": {"nucleus": [[1, 1, 2, 2]],
                        "nuclear membrane": [[3, 3, 4, 4]]}})
        monkeypatch.setattr(ra, "annotate_regions", lambda ink, names: {
            # vision boxes cytoplasm and cannot see the plasma membrane
            "regions": {"cytoplasm": [[5, 5, 6, 6]]}, "has_text": False,
            "text_boxes": []})

        asset = ra._get_raster_asset(
            "plant_cell",
            "A plant cell. Name the layer groups exactly: membrane, cytoplasm",
            cache, allow_generate=False)

        assert set(asset.regions) == {"cytoplasm"}
        # …so the leader line for 'membrane' is UNRESOLVED — render.py draws
        # to the element edge and logs it, which reads as an unlabelled part.
        assert match_layer_ids(list(asset.regions), ["membrane"]) == []
        # and the cache still holds everything, so nothing is re-bought
        md = json.loads((cache / ra.canonical_key("plant_cell")
                         / "meta.json").read_text(encoding="utf-8"))
        assert set(md["regions"]) == {"nucleus", "nuclear membrane",
                                      "cytoplasm"}

    def test_a_key_the_model_named_its_own_way_still_reaches_the_renderer(
            self, library, tmp_path, monkeypatch):
        """'vacuole' was asked; the model wrote back 'sap vacuole'. The old
        code handed that key over, render.py resolves it by containment, and
        narrowing must not take it away."""
        cache = self._cached(tmp_path, {
            "provenance": "generated", "baked_text": False,
            "annotated_for": ["vacuole"],
            "regions": {"sap vacuole": [[1, 1, 2, 2]]}})
        monkeypatch.setattr(ra, "annotate_regions", lambda ink, names: {
            "regions": {}, "has_text": False, "text_boxes": []})

        asset = ra._get_raster_asset(
            "plant_cell",
            "A plant cell. Name the layer groups exactly: vacuole",
            cache, allow_generate=False)

        assert asset.regions == {"sap vacuole": [[1, 1, 2, 2]]}


class TestASpellingVariantIsNotAFreshQuestion:
    """Every other name comparison in the engine goes through partnames —
    annotate_regions files keys under norm_part, match_layer_ids falls back to
    resolve_part. A verbatim guard buys the call again for a row that already
    answers, and writes a second spelling of one part into annotated_for and
    group_ids."""

    def _cached(self, tmp_path, md):
        cache = tmp_path / "cache"
        d = cache / ra.canonical_key("plant_cell")
        d.mkdir(parents=True)
        (d / "asset.png").write_bytes(_png_bytes(640, 480))
        (d / "meta.json").write_text(json.dumps(md), encoding="utf-8")
        return cache

    @pytest.mark.parametrize("stored,wanted", [
        ("chloroplasts", "chloroplast"),     # Latin/English plural
        ("chloroplasts", "Chloroplasts"),    # case
        ("cell wall", "cell_wall"),          # separator style is model whim
        ("nuclei", "nucleus"),
    ])
    def test_a_plural_or_re_separated_name_costs_nothing(
            self, library, tmp_path, monkeypatch, stored, wanted):
        cache = self._cached(tmp_path, {
            "provenance": "generated", "baked_text": False,
            "annotated_for": [stored],
            "regions": {stored: [[1, 1, 2, 2]]}})
        calls: list = []
        monkeypatch.setattr(ra, "annotate_regions",
                            lambda ink, names: calls.append(list(names)) or
                            {"regions": {}, "has_text": False,
                             "text_boxes": []})

        asset = ra._get_raster_asset(
            "plant_cell",
            "A plant cell. Name the layer groups exactly: " + wanted,
            cache, allow_generate=False)

        assert calls == [], "the row already holds a box for this part"
        # and the key is still handed over, because render.py's own matcher
        # resolves the two spellings to one part
        assert asset.regions == {stored: [[1, 1, 2, 2]]}
        md = json.loads((cache / ra.canonical_key("plant_cell")
                         / "meta.json").read_text(encoding="utf-8"))
        assert md["annotated_for"] == [stored], "no duplicate spelling"

    def test_a_containment_neighbour_is_still_a_real_question(
            self, library, tmp_path, monkeypatch):
        """Tolerance stops at exact/plural. 'membrane' is NOT answered by
        having asked about 'nuclear membrane' — treating it as answered would
        mean never buying a box for the part the lesson actually names."""
        cache = self._cached(tmp_path, {
            "provenance": "generated", "baked_text": False,
            "annotated_for": ["nuclear membrane"],
            "regions": {"nuclear membrane": [[1, 1, 2, 2]]}})
        calls: list = []
        monkeypatch.setattr(ra, "annotate_regions",
                            lambda ink, names: calls.append(list(names)) or
                            {"regions": {"membrane": [[3, 3, 4, 4]]},
                             "has_text": False, "text_boxes": []})

        ra._get_raster_asset(
            "plant_cell",
            "A plant cell. Name the layer groups exactly: membrane",
            cache, allow_generate=False)

        assert calls == [["membrane"]]


class TestASecondPassOverScrubbedPixelsCannotDeclareTheRowClean:
    """scrub_all_text writes the cleaned pixels to the LOCAL asset.png and
    never re-uploads. From the second pass onward `ink` is clean while the
    stored object still carries the words, and the flag the row is told is
    load-bearing on the read side: False means seed and never rescan."""

    def test_the_flag_latches_for_the_rest_of_the_container(
            self, library, tmp_path, monkeypatch):
        seen: dict = {"objects": {}}
        data = _png_bytes(640, 480)
        seen["objects"]["generated/plant_cell/abc.png"] = data
        row = {"id": "row-1", "asset_key": "plant_cell",
               "canonical_key": "plant_cell", "asset_type": "visual",
               "asset_format": "png", "status": "approved",
               "description": PROMPT, "group_ids": [], "group_count": 0,
               "storage_path": "generated/plant_cell/abc.png"}
        sb = _fake_sb(seen, rows=[row])
        monkeypatch.setattr(vl, "_sb", lambda: sb)
        monkeypatch.setattr(vl, "find", lambda *a, **k: dict(row,
                                                             match_score=1.0))
        cache = tmp_path / "cache"
        assert vl.hydrate("plant_cell", PROMPT, cache, asset_format="png")

        # pass 1: the downloaded bytes carry a word; it is scrubbed locally
        monkeypatch.setattr(ra, "annotate_regions", lambda ink, names: {
            "regions": {"nucleus": [[10, 20, 30, 40]]}, "has_text": True,
            "text_boxes": [[0, 0, 5, 5]]})
        ra._get_raster_asset("plant_cell", PROMPT, cache, allow_generate=False)
        assert seen["updated"][0]["vision"]["baked_text"] is True

        # pass 2: a different lesson, a new part name, and `ink` is now the
        # SCRUBBED local copy — so vision honestly reports no text
        monkeypatch.setattr(ra, "annotate_regions", lambda ink, names: {
            "regions": {"vacuole": [[1, 2, 3, 4]]}, "has_text": False,
            "text_boxes": []})
        ra._get_raster_asset(
            "plant_cell",
            "A plant cell. Name the layer groups exactly: vacuole",
            cache, allow_generate=False)

        assert seen["updated"][-1]["vision"]["baked_text"] is True, (
            "the object in storage still has the word on it")
        assert row["vision"]["baked_text"] is True

    def test_the_renderer_reports_the_latched_flag_not_this_passs_reading(
            self, library, tmp_path, monkeypatch):
        """Pinned at the renderer's own end of the pipe, because the row's
        merge latches too and would hide a regression here — and it cannot
        help at all while 0105 is unapplied, when there is no stored payload
        to latch against."""
        sent: list = []
        monkeypatch.setattr(vl, "record_vision",
                            lambda asset_id, payload: sent.append(payload)
                            or True)
        cache = tmp_path / "cache"
        d = cache / ra.canonical_key("plant_cell")
        d.mkdir(parents=True)
        (d / "asset.png").write_bytes(_png_bytes(640, 480))
        (d / "meta.json").write_text(json.dumps(
            {"provenance": "visual_library", "library_asset_id": "row-1"}),
            encoding="utf-8")

        monkeypatch.setattr(ra, "annotate_regions", lambda ink, names: {
            "regions": {"nucleus": [[10, 20, 30, 40]]}, "has_text": True,
            "text_boxes": [[0, 0, 5, 5]]})
        ra._get_raster_asset("plant_cell", PROMPT, cache, allow_generate=False)

        monkeypatch.setattr(ra, "annotate_regions", lambda ink, names: {
            "regions": {"vacuole": [[1, 2, 3, 4]]}, "has_text": False,
            "text_boxes": []})
        ra._get_raster_asset(
            "plant_cell",
            "A plant cell. Name the layer groups exactly: vacuole",
            cache, allow_generate=False)

        assert [p["baked_text"] for p in sent] == [True, True]


class TestTheFilesThatTwoWritersShareGoInAtomically:
    """asset_lock is a threading.RLock: it serialises the parent's render
    threads and nothing else. With RENDER_PROCESSES > 0 a spawned segment
    child re-binds against the same CACHE_DIR, and register_local's lock is
    per-KEY, so two publishes of different assets are genuinely concurrent in
    the local index."""

    def test_meta_json_is_never_written_through_a_truncating_write(self):
        src = (Path(__file__).resolve().parents[1] / "spike" / "scene_engine"
               / "raster_assets.py").read_text(encoding="utf-8")
        assert "meta.write_text(" not in src

    def test_the_cache_path_routes_meta_through_the_atomic_writer(
            self, library, tmp_path, monkeypatch):
        wrote: list = []
        real = vl.write_json_atomic
        monkeypatch.setattr(vl, "write_json_atomic",
                            lambda path, payload: wrote.append(Path(path).name)
                            or real(path, payload))
        cache = tmp_path / "cache"
        d = cache / ra.canonical_key("plant_cell")
        d.mkdir(parents=True)
        (d / "asset.png").write_bytes(_png_bytes(640, 480))
        (d / "meta.json").write_text(json.dumps(
            {"provenance": "generated"}), encoding="utf-8")
        monkeypatch.setattr(ra, "annotate_regions", lambda ink, names: {
            "regions": {"nucleus": [[1, 2, 3, 4]]}, "has_text": False,
            "text_boxes": []})

        ra._get_raster_asset("plant_cell", PROMPT, cache, allow_generate=False)

        assert wrote == ["meta.json"]

    def test_two_index_writers_never_share_a_scratch_file(
            self, library, tmp_path, monkeypatch):
        """One fixed index.json.tmp meant two publishing threads opened the
        same file with truncation: whoever renamed first published half of the
        other's document, and whoever renamed second lost its row to a
        FileNotFoundError raised out through publish_generated."""
        names: list = []
        real = Path.write_bytes

        def spy(self, data):
            names.append(self.name)
            return real(self, data)

        monkeypatch.setattr(Path, "write_bytes", spy)
        vl._write_local_index([{"asset_key": "plant_cell"}])
        vl._write_local_index([{"asset_key": "volcano"}])

        scratch = [n for n in names if n.endswith(".part")]
        assert len(scratch) == 2, "the index did not go through a scratch file"
        assert scratch[0] != scratch[1], "two writers shared one scratch name"


class TestHydrateNormalisesWhatTheRowHappensToHold:
    def test_string_coordinates_from_a_row_reach_the_renderer_as_numbers(
            self, library, tmp_path, monkeypatch):
        """hydrate spreads the whole row into meta.json, so the raw `vision`
        value lands there on its own; the explicit row_vision() call is what
        makes the shape trustworthy. jsonb round-trips whatever was written,
        and the renderer does arithmetic on these boxes."""
        data = _png_bytes(640, 480)
        seen: dict = {"objects": {"generated/plant_cell/abc.png": data}}
        row = {"id": "row-1", "asset_key": "plant_cell",
               "canonical_key": "plant_cell", "asset_type": "visual",
               "asset_format": "png", "status": "approved",
               "description": PROMPT,
               "storage_path": "generated/plant_cell/abc.png",
               "group_ids": [], "group_count": 0,
               "vision": {"regions": {"nucleus": [["10", "20", "30", "40"]]},
                          "annotated_for": ["nucleus", "membrane"],
                          "baked_text": False, "w": "640", "h": "480"}}
        monkeypatch.setattr(vl, "_sb", lambda: _fake_sb(seen, rows=[row]))
        monkeypatch.setattr(vl, "find", lambda *a, **k: dict(row,
                                                             match_score=1.0))
        cache = tmp_path / "cache"
        assert vl.hydrate("plant_cell", PROMPT, cache, asset_format="png")
        monkeypatch.setattr(ra, "annotate_regions", lambda ink, names: {
            "regions": {}, "has_text": False, "text_boxes": []})

        asset = ra._get_raster_asset("plant_cell", PROMPT, cache,
                                     allow_generate=False)

        assert asset.regions["nucleus"] == [[10.0, 20.0, 30.0, 40.0]]
        assert all(isinstance(v, float)
                   for v in asset.regions["nucleus"][0])
