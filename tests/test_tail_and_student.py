"""The two defects the first catalogue kit shipped with (Food Chains,
2026-09-20), pinned:

  1. the narration ended while the board was still drawing — the clip was
     sized to the ANIMATION, and nothing after the fit-to-audio compression
     was bounded by the voice;
  2. a student avatar sat on every scene and never spoke — seated by the
     style label, voiced only by lines that never came.
"""

from __future__ import annotations

import pytest

from spike.scene_engine.schema import Scene
from spike.scene_engine.timing import (TAIL_SLACK_SECS, TimedAction,
                                       animation_end, compile_timeline,
                                       fit_to_audio, take_tail_fits)


def _scene(actions, **over) -> Scene:
    data = {
        "id": "t", "narration": "the wall blocks larger particles today",
        "elements": [
            {"id": "box", "type": "shape", "shape": "path",
             "points": [(100, 100), (300, 100), (300, 300)]},
            {"id": "lbl", "type": "text", "text": "Wall", "at": (400, 120)},
        ],
        "actions": actions,
    }
    data.update(over)
    return Scene.model_validate(data)


class TestTheBoardNeverOutlastsTheVoice:
    def test_a_cued_tail_is_warped_inside_the_audio(self):
        # compile_timeline's compression cannot move a cue: a draw anchored
        # at 90% of a 6 s card plus its own duration ends past the audio.
        s = _scene([
            {"verb": "draw", "target": "box", "duration": 1.0},
            {"verb": "draw", "target": "box", "at": {"frac": 0.9}, "duration": 4.0},
        ])
        tl = compile_timeline(s, 6.0)
        assert animation_end(tl) > 6.0, "the premise: the anchor overruns"
        fitted = fit_to_audio(tl, 6.0, s.min_hold)
        assert animation_end(fitted) <= 6.0 - s.min_hold + 1e-6
        fits = take_tail_fits()
        assert len(fits) == 1 and "x0." in fits[0]

    def test_a_board_that_already_fits_is_untouched(self):
        s = _scene([{"verb": "draw", "target": "box", "duration": 1.0}])
        tl = compile_timeline(s, 20.0)
        assert fit_to_audio(tl, 20.0, s.min_hold) == tl
        assert take_tail_fits() == []

    def test_a_silent_scene_is_paced_by_its_animation(self):
        s = _scene([{"verb": "draw", "target": "box", "duration": 5.0}])
        tl = compile_timeline(s, 0.0)
        assert fit_to_audio(tl, 0.0, s.min_hold) == tl

    def test_captions_stay_glued_to_the_voice(self):
        from spike.scene_engine.timing import CAPTION_PREFIX
        s = _scene([
            {"verb": "draw", "target": "box", "at": {"frac": 0.9}, "duration": 4.0},
        ])
        tl = compile_timeline(s, 6.0)
        cap = TimedAction(action=s.actions[0].model_copy(
            update={"target": f"{CAPTION_PREFIX}x", "verb": "reveal", "at": None}),
            start=5.5, duration=0.3)
        fitted = fit_to_audio(tl + [cap], 6.0, s.min_hold)
        assert [t for t in fitted if str(t.action.target).startswith(CAPTION_PREFIX)][0].start == 5.5

    def test_the_floor_protects_pace_and_the_clip_is_then_cut_not_padded(self):
        from spike.scene_engine.render import SceneRenderer
        # one anchor late in a 4 s narration, then a chain of long uncued
        # draws behind it: compression (floor 0.35) and the fit (floor 0.5)
        # both run out before the board fits
        s = _scene([
            {"verb": "draw", "target": "box", "at": {"frac": 0.95}, "duration": 7.0},
            {"verb": "draw", "target": "box", "duration": 7.0},
            {"verb": "draw", "target": "box", "duration": 7.0},
            {"verb": "draw", "target": "box", "duration": 7.0},
        ])
        r = SceneRenderer(s)
        r.compile(4.0)
        # after the floor the board still ends past the voice…
        assert animation_end(r.timeline) > 4.0 + TAIL_SLACK_SECS
        # …and the clip is the voice plus a breath, never the animation
        assert r.total_secs(4.0) <= 4.0 + TAIL_SLACK_SECS + 1 / 24 + 1e-6
        warnings = r.audit()["warnings"]
        assert any(w.startswith("TAIL_FIT") for w in warnings)
        assert any(w.startswith("TAIL_CUT") for w in warnings)

    def test_the_renderer_fits_after_dependency_enforcement(self):
        """Dependency enforcement only ever pushes an action LATER, and it
        used to run after the only fit — so the fitted board grew back past
        the voice. The renderer's clip is bounded whatever the order."""
        from spike.scene_engine.render import SceneRenderer
        s = _scene([
            {"verb": "draw", "target": "box", "at": {"frac": 0.8}, "duration": 5.0},
            {"verb": "write", "target": "lbl", "duration": 3.0},
        ])
        r = SceneRenderer(s)
        r.compile(6.0)
        assert r.total_secs(6.0) <= 6.0 + TAIL_SLACK_SECS + 1 / 24 + 1e-6


class TestTheManifestSaysHowLongTheClipReallyIs:
    def test_the_validator_lists_overruns_from_the_probe(self):
        from spike.scene_engine.validate import (tail_overruns,
                                                 validate_visual_language)
        segs = [
            {"segment_id": "s001", "renderer": "scene", "audio_path": "a.mp3",
             "audio_duration_seconds": 10.0, "clip_duration_seconds": 10.2},
            {"segment_id": "s002", "renderer": "scene", "audio_path": "b.mp3",
             "audio_duration_seconds": 8.0, "clip_duration_seconds": 11.4},
            # no probe: unknown, not counted
            {"segment_id": "s003", "renderer": "scene", "audio_path": "c.mp3",
             "audio_duration_seconds": 8.0},
        ]
        assert tail_overruns(segs) == ["s002: +3.4s"]
        r = validate_visual_language({"segments": segs}, {})
        assert r["tail_overruns"] == ["s002: +3.4s"]
        assert r["tail_overrun_secs"] == pytest.approx(3.6, abs=0.01)

    def test_the_gate_refuses_a_long_tail_and_notes_a_short_one(self, monkeypatch):
        from worker import process
        monkeypatch.setenv("VIDEO_ENGINE", "scene")

        def _m(over):
            return {"segments": [
                {"segment_id": "s001", "renderer": "scene", "audio_path": "a.mp3",
                 "audio_duration_seconds": 10.0, "clip_duration_seconds": 10.0 + over,
                 "scene_audit": []}]}

        long = process._acceptance_report({"visual_plan": {}}, _m(3.0))
        assert long is not None and long["ship"] is False
        assert "tail_overrun=3.0s" in long["summary"]
        short = process._acceptance_report({"visual_plan": {}}, _m(0.8))
        assert short is not None and short["ship"] is True
        assert "tail_overruns=1" in short["summary"]
        clean = process._acceptance_report({"visual_plan": {}}, _m(0.1))
        assert clean is not None and clean["ship"] is True and "tail" not in clean["summary"]

    def test_chapter_marks_advance_by_the_clip(self):
        from catalogue.timestamps import segment_duration
        assert segment_duration({"audio_duration_seconds": 8.0, "clip_duration_seconds": 11.4}) == 11.4
        assert segment_duration({"audio_duration_seconds": 8.0}) == 8.0
        assert segment_duration({"audio_duration_seconds": 8.0, "clip_duration_seconds": 0.0}) == 8.0

    def test_caption_cues_step_by_the_clip_but_spread_over_the_voice(self):
        from catalogue.publish import caption_cues
        script = {"script": {"segments": [
            {"segment_id": "s001", "text": "One. Two."},
            {"segment_id": "s002", "text": "Three."}]},
            "video": {"segments": [
                {"segment_id": "s001", "audio_duration_seconds": 4.0, "clip_duration_seconds": 6.0},
                {"segment_id": "s002", "audio_duration_seconds": 2.0}]}}
        cues = caption_cues(script)
        assert cues[-1]["start"] == 6.0 and cues[-1]["end"] == 8.0
        assert cues[1]["end"] == 4.0, "the first segment's sentences end with its voice"


class TestAStudentWhoIsSeatedSpeaks:
    PLAN = {"chapters": [
        {"concept": "c", "assets": {"cell": "a cell"},
         "elements": [{"id": "cell", "type": "illustration", "asset": "cell",
                       "at": [600, 380], "scale": 0.9}],
         "steps": [{"segment": 1, "decision": "NEW_VISUAL",
                    "actions": [{"verb": "draw", "target": "cell"}]}]}]}

    def _seated(self, two_voice_segments):
        from spike.scene_engine.continuity import compile_plan, parse_visual_plan
        from spike.scene_engine.whiteboard import STUDENT_ID
        plan = parse_visual_plan(self.PLAN)
        narr = {"s001": "A cell has a nucleus."}
        scenes, _, report = compile_plan(
            plan, narr, all_segments=list(narr),
            avatars={"teacher": "avatar_teacher", "student": "avatar_student"},
            style="conversational", two_voice_segments=two_voice_segments)
        return any(e.get("id") == STUDENT_ID for e in scenes["s001"]["elements"]), report

    def test_a_monologue_in_a_two_voice_style_seats_nobody(self):
        seated, report = self._seated(set())
        assert seated is False
        assert any(ln.startswith("STUDENT NOT SEATED") for ln in report)

    def test_a_lesson_with_a_student_line_seats_the_student(self):
        seated, report = self._seated({"s001"})
        assert seated is True
        assert not any(ln.startswith("STUDENT NOT SEATED") for ln in report)

    def test_a_caller_that_does_not_know_lets_the_label_decide(self):
        seated, _ = self._seated(None)
        assert seated is True

    def test_the_script_generator_tells_the_compiler(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "agent3_scripts"
               / "script_generator.py").read_text(encoding="utf-8")
        assert "two_voice_segments=two_voice_sids" in src
        assert "if s.dialogue}" in src

    def test_the_prompt_asks_the_two_voice_style_for_an_exchange(self):
        from agent3_scripts.prompts import NARRATION_STYLES
        from agent3_scripts.semantic_prompt import build_semantic_prompt
        for style in NARRATION_STYLES:
            p = build_semantic_prompt(style, chapter_title="T", difficulty_level="Grade 7",
                                      target_duration="6.0", episode_context="ctx")
            assert ("TWO-VOICE CONVERSATION" in p) is (style == "conversational"), style
        p = build_semantic_prompt("conversational", chapter_title="T", difficulty_level="Grade 7",
                                  target_duration="6.0", episode_context="ctx")
        assert 'at least one "student" line' in p
        assert "Never write a segment the student is not part of" in p
