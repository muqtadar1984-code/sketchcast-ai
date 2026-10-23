"""Pin cards (catalogue/pinterest/templates.py): exact size, safe zones,
measured text that refuses to overflow, right-aligned RTL, and Devanagari
refused where shaping is required and missing."""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from catalogue.pinterest import templates as T


def _fig(size=(800, 700)):
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    d.ellipse([60, 60, size[0] - 60, size[1] - 60], outline="black", width=8)
    return img


POINTS = ["Describe the parts of a plant cell", "Compare plant and animal cells", "Explain the cell wall"]


@pytest.mark.parametrize("template,inputs", [
    ("explainer", T.Inputs("Plant and animal cells", POINTS, subject="biology", figure=_fig())),
    ("explainer", T.Inputs("Solving linear equations", POINTS, subject="algebra")),
    ("diagram", T.Inputs("A plant cell", labels=["cell wall", "nucleus"], subject="biology", figure=_fig())),
    ("worksheet", T.Inputs("Plant cells", subject="Biology", preview=Image.new("RGB", (640, 360), "white"))),
])
def test_every_layout_is_exactly_1000_by_1500_with_the_safe_zones(template, inputs):
    img, meta = T.render(template, inputs, "en")
    assert img.size == (1000, 1500) and (meta["width"], meta["height"]) == (1000, 1500)
    assert not any(b["overflow"] for b in meta["text_boxes"])
    assert meta["min_contrast"] >= 4.5
    assert meta["dominant_color"].startswith("#") and len(meta["dominant_color"]) == 7
    # the title sits in the top 12 %, the brand strip in the bottom 10 %
    title = next(b for b in meta["text_boxes"] if b["name"] == "title")
    assert title["used"][3] <= T.TITLE_BAND[3]
    for b in meta["text_boxes"]:
        if b["name"] not in ("brand",):
            assert b["used"][3] <= T.BRAND_STRIP[1], b
    # the bottom-right 120 px square is a single flat colour: nothing drawn there
    corner = img.crop(T.CLEAR_BOX)
    assert len(corner.getcolors(10_000)) == 1
    brand = next(b for b in meta["text_boxes"] if b["name"] == "brand")
    assert brand["used"][2] <= T.CLEAR_BOX[0]


def test_the_diagram_reports_its_coverage():
    _img, meta = T.render("diagram", T.Inputs("A cell", labels=["nucleus"], subject="biology", figure=_fig()))
    assert meta["diagram_coverage"] >= 0.35
    _img, meta = T.render("explainer", T.Inputs("A cell", POINTS, subject="biology", figure=_fig()))
    assert meta["diagram_coverage"] is None


def test_text_that_cannot_fit_at_the_floor_is_refused():
    long_title = " ".join(["Photosynthesis"] * 30)
    with pytest.raises(T.TextDoesNotFit):
        T.render("explainer", T.Inputs(long_title, POINTS, subject="biology"))
    many = [" ".join(["chlorophyll absorbs light energy"] * 6)] * 5
    with pytest.raises(T.TextDoesNotFit):
        T.render("explainer", T.Inputs("Cells", many, subject="biology"))


def test_a_diagram_without_its_artwork_or_parts_is_never_drawn():
    with pytest.raises(T.TextDoesNotFit):
        T.render("diagram", T.Inputs("A cell", labels=["nucleus"], subject="biology"))
    with pytest.raises(T.TextDoesNotFit):
        T.render("diagram", T.Inputs("A cell", labels=[], subject="biology", figure=_fig()))


def test_a_text_only_explainer_needs_three_points():
    with pytest.raises(T.TextDoesNotFit):
        T.render("explainer", T.Inputs("Equations", POINTS[:2], subject="algebra"))


def test_arabic_is_right_aligned():
    title = "الخلايا النباتية والحيوانية"
    pts = ["صف أجزاء الخلية النباتية", "قارن بين الخلايا", "اشرح جدار الخلية"]
    _img, meta = T.render("explainer", T.Inputs(title, pts, subject="biology"), "ar")
    assert meta["rtl"] is True
    for name in ("title", "points"):
        b = next(x for x in meta["text_boxes"] if x["name"] == name)
        right_gap = b["box"][2] - b["used"][2]
        left_gap = b["used"][0] - b["box"][0]
        assert right_gap < left_gap, f"{name} hugs the right edge"
    _img, meta = T.render("explainer", T.Inputs("Plant cells", POINTS, subject="biology"), "en")
    b = next(x for x in meta["text_boxes"] if x["name"] == "title")
    assert b["used"][0] - b["box"][0] < b["box"][2] - b["used"][2], "English hugs the left edge"


DEVA = "पादप और जंतु कोशिकाएँ"


def test_devanagari_is_refused_when_shaping_is_required_and_missing(monkeypatch):
    from agent5_slides import slide_builder
    from shared import pillow_raqm

    monkeypatch.setattr(slide_builder, "_RAQM", False)
    monkeypatch.setenv("TEXT_SHAPING_REQUIRED", "1")
    with pytest.raises(pillow_raqm.ShapingUnavailable):
        T.render("explainer", T.Inputs(DEVA, [DEVA, DEVA, DEVA], subject="biology"), "hi")


def test_devanagari_is_drawn_where_shaping_is_not_required(monkeypatch):
    monkeypatch.setenv("TEXT_SHAPING_REQUIRED", "0")
    img, meta = T.render("explainer", T.Inputs(DEVA, [DEVA, DEVA, DEVA], subject="biology"), "hi")
    assert img.size == (1000, 1500) and not any(b["overflow"] for b in meta["text_boxes"])


@pytest.mark.skipif(not __import__("PIL.features", fromlist=["check"]).check("raqm"), reason="no RAQM here")
def test_devanagari_is_drawn_with_raqm(monkeypatch):
    monkeypatch.setenv("TEXT_SHAPING_REQUIRED", "1")
    img, _meta = T.render("explainer", T.Inputs(DEVA, [DEVA, DEVA, DEVA], subject="biology"), "hi")
    assert img.size == (1000, 1500)


def test_contrast_is_wcag():
    assert T.contrast((255, 255, 255), (0, 0, 0)) == 21.0
    for colour in T.SUBJECT_COLOURS.values():
        assert T.contrast((255, 255, 255), colour) >= 4.5
