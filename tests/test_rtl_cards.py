"""Right-to-left cards (founder, 2026-10-16, on the Arabic Circle lesson):
"shift the bullet points to the right so the right to left works as
intended … this rule should apply for all languages [written right to
left]; bullet points, if any, should be right to left."

What is pinned:
  * mirror_scene_for_rtl mirrors the WRITTEN content — every x to
    WORLD_W - x, left anchors to right anchors, shapes' points, ellipse
    centres and arrow ends — and leaves the avatars and the caption stream
    (ids starting with ``__``) where the founder approved them;
  * build_whiteboard_scene(direction="rtl") puts the heading's points at the
    right edge with their dash bullets to the RIGHT of each line; the quiz
    card too; "ltr" is unchanged;
  * the maths cards (concept, recap, closing, hook) mirror for an RTL lesson
    language (shared.languages.is_rtl) and not for English;
  * the composer passes the lesson direction to the whiteboard fallback.

Run: .venv/Scripts/python.exe -m pytest tests/test_rtl_cards.py -q -p no:cacheprovider
"""
from __future__ import annotations

import re

from spike.scene_engine.schema import WORLD_W
from spike.scene_engine.whiteboard import (STUDENT_ID, TEACHER_ID, build_whiteboard_scene,
                                           mirror_scene_for_rtl, narration_stream, student_element,
                                           teacher_element)

SEG = {
    "segment_id": "s002", "type": "explore",
    "slide_heading": "المحيط والمساحة للدائرة",
    "slide_points": ["نصف القطر r يساوي نصف القطر d", "المحيط C يساوي باي مضروبا في القطر",
                     "المساحة A تساوي باي مضروبا في مربع نصف القطر"],
    "text": "في الدوائر نسمي ذلك المحيط.",
    "dialogue": [{"who": "teacher", "line": "في الدوائر نسمي ذلك المحيط."}],
}


def _by_id(scene: dict) -> dict:
    return {e["id"]: e for e in scene["elements"]}


class TestMirror:
    def test_written_content_mirrors_and_speakers_stay(self):
        scene = {"id": "x", "elements": [
            {"id": "t", "type": "text", "text": "a", "at": [60, 44], "anchor": "lt"},
            {"id": "c", "type": "text", "text": "b", "at": [640, 90], "anchor": "mt"},
            {"id": "d", "type": "shape", "shape": "line", "points": [[64, 150], [92, 146]]},
            {"id": "e", "type": "shape", "shape": "ellipse", "center": [300, 300], "rx": 20, "ry": 10},
            {"id": "a", "type": "arrow", "tail": {"el": "t", "edge": "right", "dx": 6.0}, "head": [500, 200]},
            teacher_element(), student_element(),
            {"id": "__nb_s002_0", "type": "shape", "shape": "path", "points": [[100, 100], [200, 100]]},
        ], "actions": []}
        m = mirror_scene_for_rtl(scene)
        b = _by_id(m)
        assert b["t"]["at"] == [WORLD_W - 60, 44] and b["t"]["anchor"] == "rt"
        assert b["c"]["at"] == [WORLD_W - 640, 90] and b["c"]["anchor"] == "mt", "a centred element stays centred"
        assert b["d"]["points"] == [[WORLD_W - 64, 150], [WORLD_W - 92, 146]]
        assert b["e"]["center"] == [WORLD_W - 300, 300]
        assert b["a"]["tail"] == {"el": "t", "edge": "left", "dx": -6.0} and b["a"]["head"] == [WORLD_W - 500, 200]
        assert b[TEACHER_ID]["at"] == teacher_element()["at"] and b[STUDENT_ID]["at"] == student_element()["at"]
        assert b["__nb_s002_0"]["points"] == [[100, 100], [200, 100]], "a caption bubble is not board content"
        # pure: the source is untouched
        assert scene["elements"][0]["at"] == [60, 44] and scene["elements"][0]["anchor"] == "lt"
        assert mirror_scene_for_rtl(mirror_scene_for_rtl(scene))["elements"][0]["at"] == [60, 44], "an involution"


class TestWhiteboardCard:
    def test_an_rtl_card_writes_its_points_at_the_right_edge_with_the_dash_to_the_right(self):
        ltr = build_whiteboard_scene(SEG, avatars={"teacher": "avatar_teacher"}, sketches=False, direction="ltr")
        rtl = build_whiteboard_scene(SEG, avatars={"teacher": "avatar_teacher"}, sketches=False, direction="rtl")
        l, r = _by_id(ltr), _by_id(rtl)
        for i in range(3):
            lp, rp = l[f"wb_p{i}"], r[f"wb_p{i}"]
            assert lp["anchor"] == "lt" and rp["anchor"] == "rt"
            assert rp["at"] == [WORLD_W - lp["at"][0], lp["at"][1]]
            # the dash sits to the RIGHT of the line's right edge
            dash_x = max(pt[0] for pt in r[f"wb_d{i}"]["points"])
            assert dash_x > rp["at"][0], (dash_x, rp["at"])
            ldash_x = min(pt[0] for pt in l[f"wb_d{i}"]["points"])
            assert ldash_x < lp["at"][0]
        assert r["wb_h"]["anchor"] == "mt" and r["wb_h"]["at"][0] == l["wb_h"]["at"][0], "the centred heading stays"
        assert r[TEACHER_ID]["at"] == l[TEACHER_ID]["at"], "the teacher keeps her corner"
        assert [a["target"] for a in rtl["actions"]] == [a["target"] for a in ltr["actions"]], "timing is untouched"

    def test_the_default_direction_is_unchanged(self):
        assert build_whiteboard_scene(SEG, sketches=False) == build_whiteboard_scene(SEG, sketches=False, direction="ltr")

    def test_the_quiz_card_mirrors_too(self):
        seg = {**SEG, "slide_visual": {"kind": "quiz", "caption": "ما نصف القطر؟", "options": ["٣", "٦", "٩"]}}
        ltr = build_whiteboard_scene(seg, sketches=False, direction="ltr")
        rtl = build_whiteboard_scene(seg, sketches=False, direction="rtl")
        l, r = _by_id(ltr), _by_id(rtl)
        assert r["wb_q"]["anchor"] == "rt" and r["wb_q"]["at"][0] == WORLD_W - l["wb_q"]["at"][0]
        opts = [k for k in l if re.match(r"wb_o\d", k)]
        assert opts, "the quiz has options"
        for k in opts:
            assert r[k]["at"][0] == WORLD_W - l[k]["at"][0]

    def test_the_caption_stream_added_later_is_not_moved(self):
        rtl = build_whiteboard_scene(SEG, sketches=False, direction="rtl")
        els, _ = narration_stream(SEG["text"], uid="s002")
        assert all(str(e["id"]).startswith("__") for e in els), "stream ids are overlays"
        again = mirror_scene_for_rtl({**rtl, "elements": rtl["elements"] + els})
        tail = [e for e in again["elements"] if str(e["id"]).startswith("__nb_")]
        assert [e.get("at") for e in tail] == [e.get("at") for e in els]


class TestMathsCards:
    def _lesson(self, topic: str, points: list[str]):
        from maths.schema import Lesson, MethodCard
        return Lesson(topic=topic, method=MethodCard(steps=["انقل الثوابت", "اقسم على المعامل"]),
                      concept_points=points, misconceptions=points)

    def test_arabic_concept_and_recap_cards_put_heading_points_and_dashes_at_the_right_and_the_card_at_the_left(self):
        from maths.board import CARD_X, concept_segment, recap_segment
        ar = self._lesson("المحيط والمساحة للدائرة", ["نصف القطر r يساوي نصف القطر d", "المحيط C يساوي باي مضروبا في القطر"])
        en = self._lesson("Circumference and area", ["The radius r is half the diameter d", "C equals pi times d"])
        for build in (concept_segment, recap_segment):
            a, e = _by_id(build(ar, "s002", lang="ar")["scene"]), _by_id(build(en, "s002", lang="en")["scene"])
            assert e["wb_h"]["anchor"] == "lt" and e["wb_h"]["at"][0] == 60
            assert a["wb_h"]["anchor"] == "rt" and a["wb_h"]["at"][0] == WORLD_W - 60
            for i in range(2):
                assert e[f"wb_p{i}"]["anchor"] == "lt" and a[f"wb_p{i}"]["anchor"] == "rt"
                # x mirrors; y is measured from each heading's own height, so it differs by language
                assert a[f"wb_p{i}"]["at"][0] == WORLD_W - e[f"wb_p{i}"]["at"][0]
                assert max(pt[0] for pt in a[f"wb_d{i}"]["points"]) > a[f"wb_p{i}"]["at"][0], "dash to the right of the line"
                assert a[f"wb_p{i}"].get("fixed") is True, "the pin survives the mirror"
            # the method card crosses to the left and its title anchors right-to-left
            assert e["card_title"]["at"][0] == CARD_X and e["card_title"]["anchor"] == "lt"
            assert a["card_title"]["at"][0] == WORLD_W - CARD_X and a["card_title"]["anchor"] == "rt"
            assert max(pt[0] for pt in a["card_box"]["points"]) < WORLD_W / 2

    def test_closing_and_hook_cards_follow_and_english_is_untouched(self):
        from maths.board import closing_segment, hook_segment
        ar = self._lesson("المحيط والمساحة للدائرة", [])
        en = self._lesson("Circumference and area", [])
        a, e = _by_id(closing_segment(ar, "s9", lang="ar")["scene"]), _by_id(closing_segment(en, "s9", lang="en")["scene"])
        assert a["wb_h"]["anchor"] == "rt" and e["wb_h"]["anchor"] == "lt"
        ha, he = hook_segment(ar, "s001", None, lang="ar")["scene"], hook_segment(en, "s001", None, lang="en")["scene"]
        assert _by_id(ha)["wb_h"]["anchor"] == "mt" and _by_id(he)["wb_h"]["anchor"] == "mt"
        # English: not a single x moved
        assert e["card_title"]["anchor"] == "lt"

    def test_every_rtl_card_still_parses_and_compiles(self):
        from maths.board import compile_lesson
        from spike.scene_engine.director import parse_scene_response
        from spike.scene_engine.render import SceneRenderer
        ar = self._lesson("المحيط والمساحة للدائرة", ["نصف القطر r يساوي نصف القطر d"])
        for s in compile_lesson(ar, language="ar")[1:]:
            sc = parse_scene_response(s["scene"], s["text"])
            assert sc is not None, s["segment_id"]
            SceneRenderer(sc).compile(20.0)


def test_the_composer_hands_the_lesson_direction_to_the_whiteboard_fallback():
    from pathlib import Path
    src = Path("agent6_animation/video_composer.py").read_text(encoding="utf-8")
    # the fallback is built from the DISPLAYED segment (view_seg: a Jawi
    # lesson's display copy, catalogue.jawi) — every call keeps the direction
    calls = re.findall(r"build_whiteboard_scene\((?:script_seg|view_seg)[^)]*\)", src)
    assert len(calls) == 3, calls
    assert calls and all("direction=direction" in c for c in calls), calls
