"""ONE library, two formats.

hydrate/publish carried a `png_path` and wrote `asset.png`. Making SVG a
first-class asset must not fork the library in two, must not add a parallel
store, and must never rasterise an SVG to fit the older path: the markup IS
the asset. So both functions carry bytes plus a format, and the format decides
the filename and the content type — never the identity.

No Supabase, no model call.
"""

from __future__ import annotations

import json

SVG_DOC = """<svg viewBox="0 0 800 600">
<g id="outline"><path d="M 10 10 L 790 10 L 790 590 L 10 590 Z" stroke="black" fill="none"/></g>
<g id="nucleus"><path d="M 300 300 C 340 260, 380 300, 340 340 Q 310 360, 300 300 Z" stroke="black" fill="none"/></g>
</svg>"""


class TestStorageIsFormatAgnostic:
    def _recorder(self, monkeypatch, tmp_path):
        import shared.visual_library as vl
        monkeypatch.setattr(vl, "LIBRARY_DIR", tmp_path / "idx")
        seen: dict = {}

        class Storage:
            def from_(self, bucket):
                seen["bucket"] = bucket
                return self

            def upload(self, path, fh, opts):
                seen["path"] = path
                seen["opts"] = opts
                seen["bytes"] = fh.read()

        class Table:
            def select(self, *a, **k):
                return self

            def eq(self, *a):
                return self

            def limit(self, n):
                return self

            def order(self, *a, **k):
                return self

            def execute(self):
                return type("R", (), {"data": []})()

            def insert(self, row):
                seen["row"] = row
                return self

        class SB:
            storage = Storage()

            def table(self, _n):
                return Table()

        monkeypatch.setattr(vl, "_sb", lambda: SB())
        return vl, seen

    def test_an_svg_is_stored_as_markup_in_the_same_bucket(
            self, tmp_path, monkeypatch):
        vl, seen = self._recorder(monkeypatch, tmp_path)
        svg = tmp_path / "asset.svg"
        svg.write_bytes(SVG_DOC.encode("utf-8"))
        assert vl.publish_generated("chloroplast", "A chloroplast", svg)
        assert seen["bucket"] == vl.BUCKET, "one library, one bucket"
        assert seen["path"].startswith("generated/chloroplast/")
        assert seen["path"].endswith(".svg")
        assert seen["opts"]["content-type"] == "image/svg+xml"
        assert seen["bytes"] == SVG_DOC.encode("utf-8"), \
            "the markup is the canonical asset; it is never rasterised"
        assert seen["row"]["asset_format"] == "svg"

    def test_a_png_is_stored_exactly_as_it_was_before(
            self, tmp_path, monkeypatch):
        vl, seen = self._recorder(monkeypatch, tmp_path)
        png = tmp_path / "asset.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        assert vl.publish_generated("volcano", "A volcano", png)
        assert seen["path"].endswith(".png")
        assert seen["opts"]["content-type"] == "image/png"
        assert seen["row"]["asset_format"] == "png"
        assert seen["row"]["group_ids"] == [] and seen["row"]["group_count"] == 0

    def test_the_format_can_be_stated_rather_than_guessed(
            self, tmp_path, monkeypatch):
        vl, seen = self._recorder(monkeypatch, tmp_path)
        odd = tmp_path / "asset.bin"
        odd.write_bytes(SVG_DOC.encode("utf-8"))
        assert vl.publish_generated("chloroplast", "A chloroplast", odd,
                                    asset_format="svg")
        assert seen["path"].endswith(".svg")

    def test_the_group_ids_ride_on_the_row(self, tmp_path, monkeypatch):
        """So the library can answer "does this asset contain the part the
        lesson wants to label?" without downloading it."""
        vl, seen = self._recorder(monkeypatch, tmp_path)
        svg = tmp_path / "asset.svg"
        svg.write_bytes(SVG_DOC.encode("utf-8"))
        vl.publish_generated("chloroplast", "A chloroplast", svg)
        assert seen["row"]["group_ids"] == ["outline", "nucleus"]
        assert seen["row"]["group_count"] == 2
        assert vl._local_candidates()[0]["group_ids"] == ["outline", "nucleus"]


class TestPublishIsTheStrictGate:
    """Every publisher goes through publish_generated, including the one-shot
    migration script, so the contract cannot be entered by another door."""

    def test_an_svg_that_breaks_the_contract_is_refused(
            self, tmp_path, monkeypatch, caplog):
        import logging
        import shared.visual_library as vl
        monkeypatch.setattr(vl, "LIBRARY_DIR", tmp_path / "idx")
        monkeypatch.setattr(vl, "_sb", lambda: None)
        svg = tmp_path / "asset.svg"
        svg.write_bytes(SVG_DOC.replace(
            "</svg>", "<text>nucleus</text></svg>").encode("utf-8"))
        with caplog.at_level(logging.WARNING, logger="shared.visual_library"):
            assert vl.publish_generated("chloroplast", "A chloroplast",
                                        svg) is False
        assert vl._local_candidates() == [], "not even registered locally"
        assert "breaks the asset contract" in caplog.text
        assert "text" in caplog.text, "the log has to say WHAT was wrong"

    def test_a_valid_svg_is_published(self, tmp_path, monkeypatch):
        import shared.visual_library as vl
        monkeypatch.setattr(vl, "LIBRARY_DIR", tmp_path / "idx")
        monkeypatch.setattr(vl, "_sb", lambda: None)
        svg = tmp_path / "asset.svg"
        svg.write_bytes(SVG_DOC.encode("utf-8"))
        assert vl.publish_generated("chloroplast", "A chloroplast", svg) is True

    def test_a_png_is_not_run_through_the_svg_gate(self, tmp_path, monkeypatch):
        """The raster tier has its own validation and must be unaffected."""
        import shared.visual_library as vl
        monkeypatch.setattr(vl, "LIBRARY_DIR", tmp_path / "idx")
        monkeypatch.setattr(vl, "_sb", lambda: None)
        png = tmp_path / "asset.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"<text>not xml at all")
        assert vl.publish_generated("volcano", "A volcano", png) is True


class TestFindAndHydrateRespectTheFormat:
    SVG_ROW = {
        "id": "svg-1", "asset_key": "chloroplast_structure",
        "canonical_key": "chloroplast_structure",
        "description": "A chloroplast with thylakoids and internal grana",
        "subject": "biology", "grade": "k12", "curriculum": "generic",
        "topic": "chloroplast", "concepts": [], "status": "approved",
        "asset_type": "visual", "asset_format": "svg",
        "group_ids": ["outline", "grana"], "group_count": 2,
        "storage_path": "generated/chloroplast_structure/aa.svg",
    }
    PNG_ROW = {
        "id": "png-1", "asset_key": "chloroplast_photo",
        "canonical_key": "chloroplast_photo",
        "description": "A chloroplast with thylakoids and internal grana",
        "subject": "biology", "grade": "k12", "curriculum": "generic",
        "topic": "chloroplast", "concepts": [], "status": "approved",
        "asset_type": "visual", "asset_format": "png",
        "storage_path": "generated/chloroplast/bb.png",
    }

    def _library(self, monkeypatch, tmp_path, *rows, payload=b"x"):
        import shared.visual_library as vl
        monkeypatch.setattr(vl, "LIBRARY_DIR", tmp_path / "idx")
        monkeypatch.delenv("VISUAL_LIBRARY_MIN_SCORE", raising=False)
        for r in rows:
            vl.register_local(r)

        class Storage:
            def from_(self, _b):
                return self

            def download(self, _p):
                return payload

        class SB:
            storage = Storage()

            def table(self, _n):
                raise RuntimeError("remote search is not part of this test")

        monkeypatch.setattr(vl, "_sb", lambda: SB())
        return vl

    def test_the_svg_tier_only_sees_svg_rows(self, tmp_path, monkeypatch):
        vl = self._library(monkeypatch, tmp_path, self.SVG_ROW, self.PNG_ROW)
        prompt = "A chloroplast with thylakoids and internal grana"
        # both rows are about the same thing and both pass the key guard;
        # the FORMAT is what decides which one each tier may be served
        assert vl.find("chloroplast_diagram", prompt,
                       asset_format="svg")["id"] == "svg-1"
        assert vl.find("chloroplast_diagram", prompt,
                       asset_format="png")["id"] == "png-1"

    def test_an_unfiltered_search_still_sees_both(self, tmp_path, monkeypatch):
        vl = self._library(monkeypatch, tmp_path, self.SVG_ROW, self.PNG_ROW)
        prompt = "A chloroplast with thylakoids and internal grana"
        assert vl.find("chloroplast_diagram", prompt) is not None

    def test_hydrating_an_svg_writes_markup_where_the_svg_tier_reads(
            self, tmp_path, monkeypatch):
        from spike.scene_engine.svg_assets import svg_cache_dir
        vl = self._library(monkeypatch, tmp_path, self.SVG_ROW,
                           payload=SVG_DOC.encode("utf-8"))
        cache = tmp_path / "cache"
        hit = vl.hydrate("chloroplast_diagram",
                         "A chloroplast with thylakoids and internal grana",
                         cache, asset_format="svg")
        assert hit is not None
        landed = svg_cache_dir(cache, "chloroplast_diagram") / "asset.svg"
        assert landed.exists(), [str(p) for p in cache.rglob("*")]
        assert landed.read_bytes() == SVG_DOC.encode("utf-8")
        assert not list(cache.rglob("asset.png")), "nothing was rasterised"

    def test_it_lands_under_the_REQUESTED_key_not_the_matched_one(
            self, tmp_path, monkeypatch):
        """The PNG hydration bug, one format later: a match that cannot be
        delivered is an expensive way to agree with yourself."""
        from spike.scene_engine.svg_assets import svg_cache_dir
        vl = self._library(monkeypatch, tmp_path, self.SVG_ROW,
                           payload=SVG_DOC.encode("utf-8"))
        cache = tmp_path / "cache"
        requested = "chloroplast_diagram"
        matched = "chloroplast_structure"
        assert vl.canonical_key(requested) != vl.canonical_key(matched)
        assert vl.hydrate(requested, "A chloroplast with thylakoids and "
                          "internal grana", cache,
                          asset_format="svg") is not None
        assert (svg_cache_dir(cache, requested) / "asset.svg").exists()
        assert not (svg_cache_dir(cache, matched) / "asset.svg").exists()

    def test_the_hydrated_copy_is_marked_as_a_library_hit(
            self, tmp_path, monkeypatch):
        from spike.scene_engine.svg_assets import svg_cache_dir
        vl = self._library(monkeypatch, tmp_path, self.SVG_ROW,
                           payload=SVG_DOC.encode("utf-8"))
        cache = tmp_path / "cache"
        vl.hydrate("chloroplast_diagram", "A chloroplast with thylakoids "
                   "and internal grana", cache, asset_format="svg")
        meta = json.loads((svg_cache_dir(cache, "chloroplast_diagram")
                           / "meta.json")
                          .read_text(encoding="utf-8"))
        assert meta["provenance"] == "visual_library", \
            "or the integration layer re-publishes what it just reused"
        assert meta["library_asset_id"] == "svg-1"
        assert meta["asset_format"] == "svg"
        assert meta["group_ids"] == ["outline", "grana"]

    def test_the_two_modules_agree_on_where_an_svg_lives(self, tmp_path):
        """One fold, asked of the module that owns it. Two copies of this
        answer is the bug that made every *_cell library hit a paid
        regeneration."""
        import shared.visual_library as vl
        from spike.scene_engine.svg_assets import svg_cache_dir
        for key in ("chloroplast", "Ciliated Cells Diagram", "figure_3"):
            assert vl._local_asset_path(tmp_path, key, "svg") == \
                svg_cache_dir(tmp_path, key) / "asset.svg", key
            assert vl._local_asset_path(tmp_path, key, "png") == \
                tmp_path / vl.canonical_key(key) / "asset.png", key
