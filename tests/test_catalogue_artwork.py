"""The article's own approved artwork has to reach the article's own video.

Measured on the live Cells kit (2026-09-07): two approved, labelled figures
(``animal_cell``, ``plant_cell``), a video that used neither, and five
invented scene keys that all resolved ``local_cache`` / ``generated``. The
cause was in the loader — an article becomes a chapter with ``images: []`` and
its figures survive only as "Figure: …" caption lines — so the pipeline never
learned the artwork existed.

Everything here CALLS things: ``tests.catalogue_fakes.FakeSB`` (with its
storage shim) for the database, the REAL
``agent5_slides.figures.attach_figures_to_segments`` for placement, and the
real catalogue branch of ``worker.process`` for the end-to-end. No model, no
network, no live Supabase.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

import pytest

from agent5_slides.figures import attach_figures_to_segments, load_chapter_figures
from catalogue.artwork import (ARTWORK_STATUSES, artwork_label, artwork_words,
                               load_article_artwork, ready_figures)
from shared.visual_library import BUCKET
from tests.catalogue_fakes import FakeSB
from tests.test_catalogue_kit import _job, _presentation_fakes, _sb, _worker_env

ARTICLE_ID = "art-1"

# Two figures with artwork and one still draft — the exact shape the pilot
# article had, plus the spec/labels a real row carries.
PLANT = {
    "id": "f1", "article_id": ARTICLE_ID, "figure_key": "plant_cell", "sort": 0,
    "caption": "A plant cell in cross-section", "status": "rendered",
    "visual_asset_id": "va-plant",
    "spec": {"subject": "a plant cell", "parts": ["cell wall", "chloroplast", "vacuole"],
             "style": "whiteboard diagram", "notes": ""},
    "labels": [{"group_id": "wall", "label": "cell wall"},
               {"group_id": "chloroplast", "label": "chloroplast"},
               {"group_id": None, "label": "vacuole"}],
}
ANIMAL = {
    "id": "f2", "article_id": ARTICLE_ID, "figure_key": "animal_cell", "sort": 1,
    "caption": "An animal cell", "status": "approved", "visual_asset_id": "va-animal",
    "spec": {"subject": "an animal cell", "parts": ["nucleus", "cytoplasm"],
             "style": "whiteboard diagram", "notes": ""},
    "labels": [{"group_id": "nucleus", "label": "nucleus"},
               {"group_id": "cytoplasm", "label": "cytoplasm"}],
}
DRAFT = {
    "id": "f3", "article_id": ARTICLE_ID, "figure_key": "levels_of_organisation", "sort": 2,
    "caption": "Levels of organisation", "status": "draft", "visual_asset_id": None,
    "spec": {"subject": "levels of organisation", "parts": ["cell", "tissue"],
             "style": "whiteboard diagram", "notes": ""},
    "labels": [],
}

PLANT_ASSET = {"id": "va-plant", "asset_key": "plant_cell", "asset_format": "png",
               "status": "approved", "group_ids": ["wall", "chloroplast"],
               "storage_path": "generated/plant/aaaa.png"}
ANIMAL_ASSET = {"id": "va-animal", "asset_key": "animal_cell", "asset_format": "png",
                "status": "approved", "group_ids": ["nucleus", "cytoplasm"],
                "storage_path": "generated/animal/bbbb.png"}


def _png(color) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGBA", (16, 16), color).save(buf, format="PNG")
    return buf.getvalue()


PLANT_BYTES = _png((0, 128, 0, 255))
ANIMAL_BYTES = _png((128, 0, 0, 255))


def _store(*, figures=(PLANT, ANIMAL, DRAFT), assets=(PLANT_ASSET, ANIMAL_ASSET),
           files=True) -> FakeSB:
    sb = FakeSB()
    sb.tables["article_figures"] = [dict(f) for f in figures]
    sb.tables["visual_assets"] = [dict(a) for a in assets]
    if files:
        sb.files[(BUCKET, PLANT_ASSET["storage_path"])] = PLANT_BYTES
        sb.files[(BUCKET, ANIMAL_ASSET["storage_path"])] = ANIMAL_BYTES
    return sb


def _segments(headings) -> list[dict]:
    return [{"segment_id": f"s{i}", "slide_heading": h, "text": h,
             "slide_points": ["a point", "another point"],
             "slide_visual": {"kind": "bullets"}}
            for i, h in enumerate(headings, start=1)]


# ── the pure parts ─────────────────────────────────────────────────────


class TestTheFigureIsDescribedByWhatIsInIt:
    def test_the_label_is_the_subject_not_the_whole_caption(self):
        assert artwork_label(PLANT) == "a plant cell"
        assert artwork_label({**PLANT, "spec": {}}) == "A plant cell in cross-section"
        assert artwork_label({"figure_key": "root_hair_cell"}) == "root hair cell"

    def test_the_words_carry_the_part_names(self):
        """The part names are the strongest matching signal there is: a slide
        about the cell wall and a figure labelled 'cell wall' are one board."""
        words = artwork_words(PLANT, PLANT_ASSET)
        assert {"plant", "cell", "wall", "chloroplast", "vacuole"} <= words
        # The caption too, through the same crude singulariser the book path
        # uses on both sides of the comparison ("cross" -> "cros").
        from agent5_slides.figures import _words
        assert _words("A plant cell in cross-section") <= words

    def test_the_assets_own_group_ids_count_as_parts(self):
        """What the artwork was FOUND to carry, not only what its spec asked
        for: a spec part the asset lacks is a label with a null group id."""
        thin = {"caption": "", "spec": {}, "labels": []}
        assert artwork_words(thin, {"group_ids": ["magma_chamber"]}) >= {"magma", "chamber"}
        assert artwork_words(thin, None) == set()

    def test_only_a_figure_with_an_asset_is_offered_and_the_order_is_stable(self):
        assert [f["id"] for f in ready_figures([ANIMAL, PLANT, DRAFT])] == ["f1", "f2"]
        assert set(ARTWORK_STATUSES) == {"rendered", "approved"}
        assert ready_figures([{**PLANT, "visual_asset_id": None}]) == []
        assert ready_figures(None) == []


# ── the download ───────────────────────────────────────────────────────


class TestTheArtworkIsDownloadedIntoTheJob:
    def test_every_approved_figure_becomes_an_attachable_dict(self, tmp_path):
        sb = _store()
        figs = load_article_artwork(sb, ARTICLE_ID, tmp_path / "artwork")

        assert [f["label"] for f in figs] == ["a plant cell", "an animal cell"]
        assert [Path(f["src"]).read_bytes() for f in figs] == [PLANT_BYTES, ANIMAL_BYTES]
        assert all(Path(f["src"]).parent == tmp_path / "artwork" for f in figs)
        assert figs[0]["caption"] == "A plant cell in cross-section"
        # OUR artwork: no source to credit, and a fabricated one would be worse
        assert [f["attribution"] for f in figs] == ["", ""]
        assert {"plant", "wall", "chloroplast"} <= figs[0]["words"]
        assert sb.downloads == [(BUCKET, PLANT_ASSET["storage_path"]),
                                (BUCKET, ANIMAL_ASSET["storage_path"])]

    def test_a_draft_figure_is_skipped_and_the_log_says_how_many(self, tmp_path, caplog):
        sb = _store()
        with caplog.at_level(logging.INFO, logger="worker.catalogue.artwork"):
            figs = load_article_artwork(sb, ARTICLE_ID, tmp_path / "artwork")
        assert len(figs) == 2, "the draft one has no asset to download"
        assert any("1 of 3 figure(s)" in r.getMessage() for r in caplog.records), \
            [r.getMessage() for r in caplog.records]

    def test_a_missing_storage_object_is_skipped_not_raised(self, tmp_path, caplog):
        sb = _store(files=False)
        sb.files[(BUCKET, ANIMAL_ASSET["storage_path"])] = ANIMAL_BYTES
        with caplog.at_level(logging.WARNING, logger="worker.catalogue.artwork"):
            figs = load_article_artwork(sb, ARTICLE_ID, tmp_path / "artwork")
        assert [f["label"] for f in figs] == ["an animal cell"]
        assert any("could not be downloaded" in r.getMessage() for r in caplog.records)

    def test_an_asset_row_that_has_gone_is_skipped(self, tmp_path, caplog):
        sb = _store(assets=(ANIMAL_ASSET,))
        with caplog.at_level(logging.WARNING, logger="worker.catalogue.artwork"):
            figs = load_article_artwork(sb, ARTICLE_ID, tmp_path / "artwork")
        assert [f["label"] for f in figs] == ["an animal cell"]
        assert any("not in the library" in r.getMessage() for r in caplog.records)

    def test_an_svg_asset_is_skipped_because_the_slide_pastes_raster(self, tmp_path, caplog):
        """agent5_slides.slide_builder._paste_figure opens the src with PIL. An
        SVG returns no elements there, so the segment falls back to bullets
        having spent a figure slot on nothing — better to not offer it."""
        svg = {**PLANT_ASSET, "asset_format": "svg", "storage_path": "generated/plant/aaaa.svg"}
        sb = _store(assets=(svg, ANIMAL_ASSET), files=False)
        sb.files[(BUCKET, svg["storage_path"])] = b"<svg/>"
        sb.files[(BUCKET, ANIMAL_ASSET["storage_path"])] = ANIMAL_BYTES
        with caplog.at_level(logging.WARNING, logger="worker.catalogue.artwork"):
            figs = load_article_artwork(sb, ARTICLE_ID, tmp_path / "artwork")
        assert [f["label"] for f in figs] == ["an animal cell"]
        assert any("pastes raster only" in r.getMessage() for r in caplog.records)

    def test_an_article_with_no_artwork_yet_is_not_an_error(self, tmp_path):
        sb = _store(figures=(DRAFT,), assets=())
        assert load_article_artwork(sb, ARTICLE_ID, tmp_path / "artwork") == []
        assert sb.downloads == []
        assert load_article_artwork(sb, "", tmp_path / "artwork") == []


# ── the placement ──────────────────────────────────────────────────────


class TestTheArtworkReachesTheExistingPlacementMechanism:
    def test_the_figures_land_on_the_segments_they_describe(self, tmp_path):
        """The real attach_figures_to_segments, keyword matcher (client=None),
        over the real downloaded files."""
        sb = _store()
        figs = load_article_artwork(sb, ARTICLE_ID, tmp_path / "artwork")
        segments = _segments(["What a cell is", "Inside a plant cell",
                              "Inside an animal cell", "Why it matters"])
        placed = attach_figures_to_segments(segments, figs, set(), None)

        assert placed == 2, "two figures, four open slots, cap of two"
        visuals = {s["slide_heading"]: s["slide_visual"] for s in segments}
        assert visuals["Inside a plant cell"]["src"] == figs[0]["src"]
        assert visuals["Inside an animal cell"]["src"] == figs[1]["src"]
        assert visuals["Inside a plant cell"]["kind"] == "figure"
        assert visuals["Inside a plant cell"]["attribution"] == ""
        assert segments[1]["slide_points"] == [], "the figure replaces the bullets"
        assert visuals["What a cell is"] == {"kind": "bullets"}

    def test_the_reviewed_artwork_wins_the_slot_over_an_invented_asset(self, tmp_path):
        """The cap is half a part's open slots. When both kinds of figure
        exist the article's is offered FIRST, so it is the one that fits."""
        sb = _store(figures=(PLANT,))
        artwork = load_article_artwork(sb, ARTICLE_ID, tmp_path / "artwork")
        crop = tmp_path / "crop.png"
        crop.write_bytes(ANIMAL_BYTES)
        from agent5_slides.figures import _words
        book_figure = {"src": str(crop), "caption": "A plant cell", "label": "Fig 1.2",
                       "attribution": "Fig 1.2", "words": _words("A plant cell")}

        segments = _segments(["Inside a plant cell", "Why it matters"])
        placed = attach_figures_to_segments(segments, artwork + [book_figure], set(), None)

        assert placed == 1, "cap = max(1, 2 // 2)"
        assert segments[0]["slide_visual"]["src"] == artwork[0]["src"]

    def test_the_book_path_still_builds_the_shape_it_always_did(self, tmp_path, monkeypatch):
        """The catalogue must not have changed the crops: a book figure still
        prints its own label as the attribution, and its words still come from
        caption + label only."""
        from agent1_ingestion import figure_detector

        def _crop(_pdf, _page, _bbox, dest):
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            Path(dest).write_bytes(ANIMAL_BYTES)
            return str(dest)

        monkeypatch.setattr(figure_detector, "crop_figure", _crop)
        book = {"chapters": [{"num": 3, "figures": [
            {"page": 12, "bbox": [0, 0, 1, 1], "caption": "A palisade cell", "label": "Fig 3.1"}]}]}
        figs = load_chapter_figures(book, 3, "book.pdf", tmp_path / "figures")

        assert len(figs) == 1
        assert figs[0]["attribution"] == "Fig 3.1", "the printed label, never a fabricated source"
        assert figs[0]["caption"] == "A palisade cell"
        assert "palisade" in figs[0]["words"]
        assert set(figs[0]) == {"src", "caption", "label", "attribution", "words"}, \
            "the same contract the catalogue path now fills"


# ── end to end, through the worker's catalogue branch ──────────────────


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("TTS_PREMIUM_PROVIDER", "ELEVENLABS_ENABLED", "ELEVENLABS_API_KEY",
              "GOOGLE_TTS_ENABLED", "GOOGLE_APPLICATION_CREDENTIALS",
              "GOOGLE_APPLICATION_CREDENTIALS_JSON", "VERTEX_PROJECT_ID", "VIDEO_ENGINE",
              "SEMANTIC_PLAN", "FEATURE_TEXTBOOK_FIGURES", "FEATURE_CHAPTER_HEAL",
              "DECK_IN_PRESENTATION", "CATALOGUE_PART_TARGET_MIN", "CATALOGUE_WINDOW_UTC",
              "SUPPORT_AGENT_ENABLED", "TTS_PREMIUM_CANARY_OWNERS"):
        monkeypatch.delenv(k, raising=False)


def _slide_recorder(monkeypatch) -> list[list[dict]]:
    """Capture each part's segments AS THE SLIDE BUILDER RECEIVES THEM — after
    the figures were attached and before the job's tmp dir is swept."""
    seen: list[list[dict]] = []

    class _Dump:
        def __init__(self, d):
            self._d = d

        def model_dump(self):
            return self._d

    def fake_slides(script_data, branding=None, direction="ltr", build_deck=True, **kw):
        segs = script_data["episodes"][0]["segments"]
        seen.append([{**s, "_src_exists": Path((s.get("slide_visual") or {}).get("src", "")).exists()
                      if isinstance(s.get("slide_visual"), dict) else False} for s in segs])
        return _Dump({"segments": [{"segment_id": s["segment_id"], "slide_image_path": None}
                                   for s in segs], "deck_path": None})

    monkeypatch.setattr("agent5_slides.slide_generator.generate_episode_slides", fake_slides)
    return seen


class TestTheCatalogueVideoUsesTheArticlesArtwork:
    def _prepared(self, monkeypatch, tmp_path):
        sb = _sb(statuses={"gen-p": "processing"})
        # the pilot's shape: one rendered figure with an asset, one still draft
        sb.tables["article_figures"] = [dict(PLANT), dict(DRAFT)]
        sb.tables["visual_assets"] = [dict(PLANT_ASSET)]
        sb.files[(BUCKET, PLANT_ASSET["storage_path"])] = PLANT_BYTES
        sb.tables["jobs"] = [_job("job-p", "gen-p", "presentation")]
        process, uploads, _ = _worker_env(monkeypatch, sb)
        _presentation_fakes(monkeypatch, tmp_path)
        seen = _slide_recorder(monkeypatch)
        return sb, process, seen

    def test_the_figure_is_attached_although_the_textbook_flag_is_off(self, monkeypatch, tmp_path):
        """FEATURE_TEXTBOOK_FIGURES guards cropping someone else's book. This
        artwork is ours and always applies."""
        import os

        sb, process, seen = self._prepared(monkeypatch, tmp_path)
        assert not os.getenv("FEATURE_TEXTBOOK_FIGURES")
        monkeypatch.setattr("agent5_slides.figures.load_chapter_figures",
                            lambda *a, **k: pytest.fail("a kit has no PDF to crop"))

        process.process_generation(sb, sb.tables["jobs"][0], "gen-p")

        placed = [s for part in seen for s in part
                  if isinstance(s.get("slide_visual"), dict)
                  and s["slide_visual"].get("kind") == "figure"]
        assert placed, "the approved plant-cell figure never reached a slide"
        assert all(s["_src_exists"] for s in placed), "the file was gone by slide time"
        assert all("artwork" in s["slide_visual"]["src"] for s in placed)
        assert all(s["slide_visual"]["attribution"] == "" for s in placed)
        assert sb.downloads == [(BUCKET, PLANT_ASSET["storage_path"])], \
            "downloaded once for the whole lesson, not once per part"

    def test_a_kit_whose_artwork_cannot_be_read_still_renders(self, monkeypatch, tmp_path):
        """A missing figure is a quality fact, never a failed kit."""
        sb, process, seen = self._prepared(monkeypatch, tmp_path)
        sb.files.clear()

        process.process_generation(sb, sb.tables["jobs"][0], "gen-p")

        assert seen and not any(isinstance(s.get("slide_visual"), dict)
                                and s["slide_visual"].get("kind") == "figure"
                                for part in seen for s in part)
        assert next(g for g in sb.tables["generations"] if g["id"] == "gen-p")["status"] == "done"
