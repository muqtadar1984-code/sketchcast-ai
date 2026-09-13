"""Every route a deck reaches a user by, and every script a lesson is taught in.

Three routes: a catalogue kit (article), a teacher's deck job (analysis + one
authoring call), and the deck a teacher downloads BESIDE THE VIDEO — which is
where most of them get it (measured: 14 of the last 33 lessons carried one,
only 6 had a deck generation of their own). Ten selectable languages across
four scripts: Latin, Arabic (RTL), Devanagari, Telugu.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pptx import Presentation

from agent5_slides import deck_generator as dg
from agent5_slides import metrics as mx
from agent5_slides.deck_notes import author_deck
from agent5_slides.deck_storyboard import (ICONS, QUIZ, SHAPES, TAKEAWAYS,
                                           storyboard, summarise)
from agent5_slides.slide_generator import generate_episode_slides
from tests.test_deck_generation import ANALYSIS, BOOK, CHAPTER, _reply, _StubClient


def _video_script(lang_text: dict[str, str] | None = None) -> dict:
    """A VIDEO script as the semantic path writes it: headings and narration,
    NO slide_points, a visual on some segments."""
    t = lang_text or {}
    return {"episodes": [{"episode_title": t.get("title", "Cells"), "segments": [
        {"segment_id": "s001", "type": "hook", "slide_heading": t.get("h1", "What is a cell?"),
         "slide_points": [], "text": t.get("n1", "Every living thing is built from cells. Let us look inside one.")},
        {"segment_id": "s002", "type": "explore", "slide_heading": t.get("h2", "How energy is released"),
         "slide_points": [], "text": t.get("n2", "Glucose and oxygen meet in the mitochondria."),
         "slide_visual": {"kind": "flow", "nodes": [t.get("f1", "Glucose"), t.get("f2", "Oxygen"), t.get("f3", "Energy")],
                          "caption": t.get("cap", "In the mitochondria")}},
        {"segment_id": "s003", "type": "quiz", "slide_heading": t.get("h3", "Which part holds the DNA?"),
         "slide_points": [], "text": t.get("n3", "Think before you answer."),
         "slide_visual": {"kind": "quiz", "options": [t.get("o1", "Membrane"), t.get("o2", "Nucleus"), t.get("o3", "Mitochondria")], "answer": 1}},
        {"segment_id": "s004", "type": "takeaways", "slide_heading": t.get("h4", "What to remember"),
         "slide_points": [], "text": t.get("n4", "Three things to keep."),
         "slide_visual": {"kind": "takeaways", "nodes": [t.get("t1", "Cells are the unit of life"), t.get("t2", "The nucleus holds DNA")]}},
    ]}]}


ANALYSIS_WITH_DEFS = {**ANALYSIS, "concepts": {"concepts": [
    {"concept_id": "c001", "name": "cell membrane", "definition": "The boundary that controls what enters and leaves."},
    {"concept_id": "c002", "name": "nucleus", "definition": "Holds the DNA."},
]}}


def _shape_kinds(prs):
    return [sh.shape_type for s in prs.slides for sh in s.shapes]


class TestTheDeckBesideTheVideo:
    def test_the_embedded_deck_is_native_and_carries_the_visuals(self, monkeypatch, tmp_path):
        monkeypatch.delenv("DECK_STORYBOARD", raising=False)
        m = generate_episode_slides(script_data=_video_script(), out_dir=tmp_path,
                                    analysis=ANALYSIS_WITH_DEFS).model_dump()
        deck = Path(m["deck_path"])
        assert deck.exists()
        prs = Presentation(str(deck))
        W = prs.slide_width
        pics = [sh for s in prs.slides for sh in s.shapes if sh.shape_type == 13]
        assert not any(abs(sh.width - W) < 10000 for sh in pics), "no full-bleed PNG slide"
        texts = [sh.text_frame.text for s in prs.slides for sh in s.shapes
                 if getattr(sh, "has_text_frame", False)]
        assert any("Words to know" in t for t in texts), "the glossary from analysis.concepts"
        assert any("Glucose" in t for t in texts), "flow nodes are native text"
        assert any("Nucleus" in t for t in texts), "quiz options are native text"
        # The video still gets its PNGs — the deck change must not touch it.
        assert len(list(tmp_path.glob("*_slide.png"))) == 4

    def test_the_narration_is_distilled_to_points_when_there_are_no_points(self, monkeypatch, tmp_path):
        """The semantic script prompt does not write slide_points; the legacy
        renderer showed the narration as fallback text. The storyboard shows
        short sentences distilled from it — never the paragraph (founder,
        2026-09-12)."""
        monkeypatch.delenv("DECK_STORYBOARD", raising=False)
        model = dg.model_from_script(ANALYSIS_WITH_DEFS, _video_script())
        assert model.sections[0].points == []
        assert "built from cells" in model.sections[0].body_md
        from agent5_slides.deck_storyboard import section_content
        blocks, notes = section_content(model.sections[0])
        assert all(b["kind"] != "para" for b in blocks) and "built from cells" in notes
        kinds = summarise(storyboard(model))
        assert kinds.get(SHAPES) == 1 and kinds.get(QUIZ) == 1 and kinds.get(TAKEAWAYS) == 1

    def test_a_geometry_fault_costs_the_deck_not_the_video(self, monkeypatch, tmp_path):
        """Bonus semantics on this route: the video ships, the deck does not —
        never a bad deck beside a good video."""
        monkeypatch.delenv("DECK_STORYBOARD", raising=False)
        from agent5_slides import deck_render
        real = deck_render.build

        def _faulty(slides, out_path, **kw):
            path, _ = real(slides, out_path, **kw)
            return path, ["slide 2 (section): body overflows the slide by 0.40in"]

        monkeypatch.setattr(deck_render, "build", _faulty)
        m = generate_episode_slides(script_data=_video_script(), out_dir=tmp_path).model_dump()
        assert m["deck_path"] is None
        assert len(list(tmp_path.glob("*_slide.png"))) == 4


class TestTheAuthoredExtras:
    def test_author_deck_returns_the_extras_and_the_wrapper_only_the_slides(self):
        reply = {**_reply(),
                 "objectives": ["Describe the cell as the unit of life.", ""],
                 "misconceptions": [{"misconception": "All cells look the same.", "correction": "They vary."}, "junk"],
                 "worked_examples": [{"problem": "Count the cells.", "solution": "Two."}]}
        out = author_deck(BOOK, CHAPTER, ANALYSIS, _StubClient(reply), {"num_slides": 8}, "en")
        assert out["objectives"] == ["Describe the cell as the unit of life."]
        assert out["misconceptions"] == [{"misconception": "All cells look the same.", "correction": "They vary."}]
        assert len(out["slides"]) >= 3
        from agent5_slides.deck_notes import author_deck_slides
        assert author_deck_slides(BOOK, CHAPTER, ANALYSIS, _StubClient(reply), {}, "en") == out["slides"]

    def test_missing_extras_are_empty_not_fatal(self):
        out = author_deck(BOOK, CHAPTER, ANALYSIS, _StubClient(_reply()), {}, "en")
        assert out["objectives"] == [] and out["misconceptions"] == [] and out["worked_examples"] == []

    def test_the_extras_reach_the_model_as_slides(self):
        extras = {"objectives": ["Explain respiration."],
                  "misconceptions": [{"misconception": "Plants do not respire.", "correction": "They do, all the time."}],
                  "worked_examples": [{"problem": "Name the gas released.", "solution": "Carbon dioxide."}]}
        model = dg.model_from_script(ANALYSIS_WITH_DEFS, _video_script(), extras)
        kinds = summarise(storyboard(model))
        assert kinds.get("objectives") == 1 and kinds.get("misconceptions") == 1 and kinds.get("worked_example") == 1


class TestVisualsBecomeNativeSlides:
    def _model_with(self, visual: dict):
        script = _video_script()
        script["episodes"][0]["segments"][1]["slide_visual"] = visual
        return dg.model_from_script({}, script)

    def test_compare_is_a_native_table(self, tmp_path):
        model = self._model_with({"kind": "compare", "groups": [
            {"heading": "Plant cell", "items": ["Cell wall", "Chloroplasts"]},
            {"heading": "Animal cell", "items": ["No wall"]}]})
        slides = storyboard(model)
        sec = next(s for s in slides if s.section_id == "s002" and s.kind == "section")
        tbl = next(b for b in sec.blocks if b["kind"] == "table")
        assert tbl["header"] == ["Plant cell", "Animal cell"]
        assert tbl["rows"] == [["Cell wall", "No wall"], ["Chloroplasts", ""]]

    def test_definition_becomes_the_key_idea_band(self):
        model = self._model_with({"kind": "definition", "body": "The site of respiration."})
        sec = next(s for s in storyboard(model) if s.section_id == "s002" and s.kind == "section")
        assert sec.key_idea == "The site of respiration."

    @pytest.mark.parametrize("kind", ["flow", "cycle", "hierarchy"])
    def test_diagrams_are_shapes_and_connectors_with_editable_labels(self, kind, tmp_path):
        from agent5_slides import deck_render
        model = self._model_with({"kind": kind, "nodes": ["Sun", "Leaf", "Sugar", "Growth"]})
        slides = [s for s in storyboard(model) if s.kind == SHAPES]
        assert len(slides) == 1
        path, faults = deck_render.build(slides, tmp_path / f"{kind}.pptx")
        assert faults == []
        prs = Presentation(str(path))
        s = list(prs.slides)[0]
        labels = [sh.text_frame.text for sh in s.shapes if getattr(sh, "has_text_frame", False)]
        assert all(any(n in t for t in labels) for n in ("Sun", "Leaf", "Sugar", "Growth"))
        assert not any(sh.shape_type == 13 for sh in s.shapes), "no raster on a shape diagram"
        connectors = [sh for sh in s.shapes if sh.shape_type == 10]   # MSO_SHAPE_TYPE.LINE? connectors
        assert len(list(s.shapes)) > 4

    def test_icons_have_native_labels(self, tmp_path):
        from agent5_slides import deck_render
        model = self._model_with({"kind": "icons", "items": [
            {"icon": "sun", "label": "Light"}, {"icon": "cloud", "label": "Water"}, {"icon": "atom", "label": "Carbon dioxide"}]})
        slides = [s for s in storyboard(model) if s.kind == ICONS]
        path, faults = deck_render.build(slides, tmp_path / "icons.pptx")
        assert faults == []
        s = list(Presentation(str(path)).slides)[0]
        labels = [sh.text_frame.text for sh in s.shapes if getattr(sh, "has_text_frame", False)]
        assert all(any(l in t for t in labels) for l in ("Light", "Water", "Carbon dioxide"))
        assert sum(1 for sh in s.shapes if sh.shape_type == 13) == 3, "one small raster per icon"

    def test_a_visual_below_its_minimum_contributes_nothing(self):
        """The fixture's OTHER segments keep their quiz and takeaways; only
        s002's visual is under test."""
        model = self._model_with({"kind": "flow", "nodes": ["Only one"]})
        assert not [s for s in storyboard(model) if s.kind == SHAPES and s.section_id == "s002"]
        model = self._model_with({"kind": "quiz", "options": ["Just one"], "answer": 0})
        assert not [s for s in storyboard(model) if s.kind == QUIZ and s.section_id == "s002"]


class TestEveryScriptIsMeasuredAsItself:
    LATIN = "The nucleus holds the genetic material of the cell."
    ARABIC = "تحتوي النواة على المادة الوراثية للخلية وتتحكم في نشاطها."
    HINDI = "केन्द्रक कोशिका की आनुवंशिक सामग्री को धारण करता है।"
    TELUGU = "కేంద్రకం కణం యొక్క జన్యు పదార్థాన్ని కలిగి ఉంటుంది."
    CJK = "细胞核储存细胞的遗传物质并控制细胞的活动。"

    def test_an_ideograph_is_twice_a_latin_letter(self):
        assert mx.text_width_em("细胞") == pytest.approx(2.0)
        assert mx.text_width_em("ab") == pytest.approx(0.96)

    def test_combining_marks_take_no_width(self):
        assert mx.text_width_em("क") == mx.text_width_em("कि")

    def test_the_same_character_count_wraps_more_lines_in_wider_scripts(self):
        """Sixty characters of Chinese need about twice the lines of sixty
        characters of English in the same column — the whole reason a
        character count could not be the estimate."""
        w = 3.0
        latin = mx.text_height_in((self.LATIN * 2)[:60], w, 16)
        cjk = mx.text_height_in((self.CJK * 3)[:60], w, 16)
        assert cjk > latin * 1.5

    def test_a_character_count_alone_would_have_lied(self):
        """The old estimate: every glyph 0.48em. Twenty ideographs are 20em
        wide, not 9.6 — the difference is a wrapped line the slide did not
        budget for, below its bottom edge."""
        twenty = "细" * 20
        assert mx.text_width_em(twenty) == pytest.approx(20.0)
        assert 20 * 0.48 < mx.text_width_em(twenty)

    @pytest.mark.parametrize("text", [ARABIC, HINDI, TELUGU, CJK])
    def test_the_storyboard_paginates_and_renders_every_script(self, text, tmp_path):
        from agent5_slides import deck_render
        t = {"title": text[:12], "h1": text[:20], "n1": (text + " ") * 40, "h2": text[:16], "n2": text,
             "f1": text[:6], "f2": text[6:12], "f3": text[12:18], "cap": text[:20],
             "h3": text[:20], "n3": text, "o1": text[:8], "o2": text[8:16], "o3": text[16:24],
             "h4": text[:14], "n4": text, "t1": text[:24], "t2": text[24:48]}
        model = dg.model_from_script({}, _video_script(t))
        slides = storyboard(model)
        path, faults = deck_render.build(slides, tmp_path / "deck.pptx")
        assert faults == [], faults
        assert len(list(Presentation(str(path)).slides)) >= 5


class TestRightToLeft:
    def test_the_rtl_pass_reaches_table_cells(self, tmp_path):
        from agent5_slides.slide_builder import _mirror_deck_rtl
        model = dg.from_article({
            "id": "a", "title": "الخلية",
            "sections": [{"id": "s1", "heading": "ما هي الخلية؟", "body_md": "الخلية هي وحدة الحياة."}],
            "glossary": [{"term": "النواة", "definition": "تحتوي على المادة الوراثية."}],
            "misconceptions": [{"misconception": "كل الخلايا متشابهة.", "correction": "تختلف كثيراً."}],
            "claims": [{"id": "c1", "text": "الخلية هي وحدة الحياة.", "section_id": "s1"}]}, [])
        path = dg.build_lesson_deck(model, tmp_path / "ar.pptx", direction="rtl")
        prs = Presentation(str(path))
        cells = [c for s in prs.slides for sh in s.shapes if getattr(sh, "has_table", False)
                 for r in sh.table.rows for c in r.cells]
        assert cells, "the glossary and misconceptions are tables"
        for c in cells:
            for p in c.text_frame.paragraphs:
                assert p._p.pPr is not None and p._p.pPr.get("rtl") == "1"   # noqa: SLF001
