"""Board colour, phase 1 (spike/scene_engine/colour.py): a second accent the
engine assigns from what it knows — leader arrows, relation arrows, equation
rows — behind FEATURE_BOARD_COLOUR. The flag OFF is the benchmark: the
compiled plan must be byte-identical to today's, so a rollback is unsetting
one variable.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spike.scene_engine import colour
from spike.scene_engine.continuity import compile_plan, parse_visual_plan
from spike.scene_engine.paper import PALETTE, role_color
from spike.scene_engine.schema import ArrowElement, TextElement
from spike.scene_engine.semantic import adapt_semantic_plan

NARR = {
    "s001": "Here is a right-angled triangle. The hypotenuse is the longest side.",
    "s002": "Water flows from the outer bank to the inner bank.",
}


def _plan():
    return {"chapters": [{
        "id": "chapter_1", "concept": "triangle_sides", "transition": "continue",
        "assets": {"triangle": "A right-angled triangle, the hypotenuse on the right, the base at the bottom"},
        "semantic_regions": ["hypotenuse", "base"],
        "elements": [
            {"id": "tri", "type": "illustration", "asset": "triangle", "role": "root_visual"},
            {"id": "lbl_hyp", "type": "text", "text": "Hypotenuse", "role": "label"},
        ],
        "steps": [
            {"segment": 1, "decision": "EXTEND", "reason": "introduce the shape",
             "actions": [
                 {"verb": "DRAW", "target": {"element": "tri"}, "cue": "a right-angled triangle"},
                 {"verb": "WRITE", "target": {"element": "lbl_hyp"}, "cue": "The hypotenuse"},
                 # a leader: the label for this region exists
                 {"verb": "ARROW", "target": {"asset": "triangle", "region": "hypotenuse"}, "cue": "the longest side"}]},
            {"segment": 2, "decision": "EXTEND", "reason": "a relation",
             "actions": [
                 # a relation: no label names this region
                 {"verb": "ARROW", "target": {"asset": "triangle", "region": "base"}, "cue": "outer bank"}]},
        ],
    }]}


def _equation_plan():
    return {"chapters": [{
        "id": "chapter_1", "concept": "respiration_equation", "transition": "clear_and_redraw",
        "assets": {}, "semantic_regions": [],
        "elements": [
            {"id": "lhs", "type": "text", "text": "Glucose + Oxygen", "role": "label"},
            {"id": "arrow", "type": "text", "text": "→", "role": "label"},
            {"id": "rhs", "type": "text", "text": "Carbon dioxide + Water + Energy", "role": "label"},
        ],
        "steps": [{"segment": 1, "decision": "EXTEND", "reason": "the word equation",
                   "actions": [
                       {"verb": "WRITE", "target": {"element": "lhs"}, "cue": "right-angled"},
                       {"verb": "WRITE", "target": {"element": "arrow"}, "cue": "right-angled"},
                       {"verb": "WRITE", "target": {"element": "rhs"}, "cue": "right-angled"}]}],
    }]}


def _arrows(plan):
    return {e["id"]: e for c in plan["chapters"] for e in c["elements"] if e.get("type") == "arrow"}


def _texts(plan):
    return {e["id"]: e for c in plan["chapters"] for e in c["elements"] if e.get("type") == "text"}


@pytest.fixture
def off(monkeypatch):
    monkeypatch.delenv(colour.FLAG, raising=False)


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv(colour.FLAG, "1")


class TestTheBenchmarkIsUntouched:
    def test_the_flag_reads_off_by_default(self, off):
        assert colour.enabled() is False
        assert colour.arrow_colour(leader=True) is None
        assert colour.arrow_colour(leader=False) is None

    def test_no_colour_key_is_written_with_the_flag_off(self, off):
        plan, issues = adapt_semantic_plan(_plan(), NARR, strict=True)
        assert issues == []
        arrows = _arrows(plan)
        assert set(arrows) == {"arr_hypotenuse", "arr_base"}
        assert all("color" not in a for a in arrows.values()), "the benchmark carries no colour key"
        eq, _ = adapt_semantic_plan(_equation_plan(), NARR, strict=False)
        assert all("color" not in t for t in _texts(eq).values())

    def test_the_adapter_output_is_identical_with_the_flag_off_or_unset(self, monkeypatch):
        monkeypatch.delenv(colour.FLAG, raising=False)
        a, _ = adapt_semantic_plan(_plan(), NARR, strict=True)
        monkeypatch.setenv(colour.FLAG, "0")
        b, _ = adapt_semantic_plan(_plan(), NARR, strict=True)
        assert a == b

    def test_the_schema_default_is_still_ink(self, off):
        a = ArrowElement(id="a", tail=[0, 0], head=[10, 10])
        assert a.color == "ink"
        assert TextElement(id="t", text="x", at=[0, 0]).color == "ink"


class TestTheRoles:
    def test_the_palette_carries_the_second_accent(self):
        for role in ("accent2", "accent2_bright", "accent2_mist"):
            assert role in PALETTE, role
        assert role_color("accent2") == PALETTE["accent2"] != PALETTE["accent"]
        # a scene style's accent override never touches the second accent
        assert role_color("accent2", style_accent=(1, 2, 3)) == PALETTE["accent2"]
        assert role_color("accent", style_accent=(1, 2, 3)) == (1, 2, 3)

    def test_the_schema_accepts_the_role_everywhere_a_role_goes(self):
        assert ArrowElement(id="a", tail=[0, 0], head=[10, 10], color="accent2").color == "accent2"
        assert TextElement(id="t", text="x", at=[0, 0], color="accent2").color == "accent2"

    def test_a_leader_is_accent_and_a_relation_is_accent2(self, on):
        plan, issues = adapt_semantic_plan(_plan(), NARR, strict=True)
        assert issues == []
        arrows = _arrows(plan)
        assert arrows["arr_hypotenuse"]["color"] == "accent", "tail is the label: a leader"
        assert arrows["arr_base"]["color"] == "accent2", "tail is the margin: a relation"

    def test_an_equation_row_reads_accent_ink_accent2(self, on):
        plan, _ = adapt_semantic_plan(_equation_plan(), NARR, strict=False)
        t = _texts(plan)
        assert (t["lhs"]["color"], t["arrow"]["color"], t["rhs"]["color"]) == ("accent", "ink", "accent2")

    def test_a_row_without_a_sign_is_left_alone(self, on):
        texts = {"a": {"text": "Nucleus"}, "b": {"text": "Cell wall"}, "c": {"text": "Vacuole"}}
        assert colour.colour_equation_row(["a", "b", "c"], texts) is False
        assert all("color" not in v for v in texts.values())
        # a sign at an end is not an equation either
        texts = {"a": {"text": "="}, "b": {"text": "x"}, "c": {"text": "y"}}
        assert colour.colour_equation_row(["a", "b", "c"], texts) is False

    def test_the_roles_survive_parsing_and_compiling(self, on):
        plan, _ = adapt_semantic_plan(_plan(), NARR, strict=True)
        vp = parse_visual_plan(copy.deepcopy(plan))
        assert vp is not None
        cols = {e["id"]: e.get("color") for c in vp.chapters for e in c.elements if e.get("type") == "arrow"}
        assert cols == {"arr_hypotenuse": "accent", "arr_base": "accent2"}
        scenes, _, report = compile_plan(vp, NARR)
        seen = {e["id"]: e.get("color") for sc in scenes.values() for e in sc["elements"] if e.get("type") == "arrow"}
        assert seen.get("arr_base") == "accent2" and seen.get("arr_hypotenuse") == "accent"

    def test_a_synthesised_leader_takes_the_leader_colour(self, on):
        """continuity draws a leader for a label the director left bare —
        it annotates, so it is a leader."""
        raw = _plan()
        raw["chapters"][0]["steps"][0]["actions"] = [
            a for a in raw["chapters"][0]["steps"][0]["actions"] if a["verb"] != "ARROW"]
        raw["chapters"][0]["steps"] = raw["chapters"][0]["steps"][:1]
        plan, _ = adapt_semantic_plan(raw, NARR, strict=True)
        vp = parse_visual_plan(plan)
        scenes, _, report = compile_plan(vp, NARR)
        synth = [e for sc in scenes.values() for e in sc["elements"] if e["id"].startswith("arr_auto_")]
        assert synth, "\n".join(report)
        assert {e.get("color") for e in synth} == {"accent"}
