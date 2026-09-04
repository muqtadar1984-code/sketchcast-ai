"""Prod-gate wiring tests: ScriptSegment fields, coverage harvest, dispatch.

All offline: the dispatch is exercised down to (and not past) the point where
ffmpeg would run; the flag-off path is pinned inert.
"""

from __future__ import annotations

import pytest

from agent3_scripts.models import ScriptSegment
from agent6_animation.video_composer import _render_scene_segment
from shared.coverage import script_text

_SCENE = {
    "id": "s", "narration": "ignored",
    "elements": [
        {"id": "d", "type": "shape", "shape": "path",
         "points": [(10, 10), (200, 200)]},
        {"id": "t", "type": "text", "text": "Photosynthesis", "at": (50, 50)},
    ],
    "actions": [{"verb": "draw", "target": "d"},
                {"verb": "write", "target": "t"}],
}


class TestSegmentField:
    def test_scene_fields_are_optional_and_additive(self):
        seg = ScriptSegment(segment_id="s001", type="explore", text="hi",
                            elevenlabs_text="hi", estimated_duration_seconds=10)
        assert seg.scene is None and seg.scene_assets is None
        seg2 = ScriptSegment(segment_id="s001", type="explore", text="hi",
                             elevenlabs_text="hi", estimated_duration_seconds=10,
                             scene=_SCENE, scene_assets={"cell": "a plant cell"})
        assert seg2.scene["id"] == "s"


class TestCoverageHarvest:
    def test_scene_text_elements_count_as_taught(self):
        script = {"segments": [{"text": "spoken words", "scene": _SCENE}]}
        harvested = script_text(script)
        assert "Photosynthesis" in harvested
        assert "spoken words" in harvested

    def test_malformed_scene_never_breaks_coverage(self):
        script = {"segments": [
            {"text": "a", "scene": "not-a-dict"},
            {"text": "b", "scene": {"elements": "nope"}},
            {"text": "c", "scene": {"elements": [None, {"type": "text"}]}},
        ]}
        assert "a b c" in script_text(script)


class TestAgent3Wiring:
    def test_prompt_carries_scene_spec_only_when_flag_on(self, monkeypatch):
        from agent3_scripts.prompts import NARRATION_STYLES, build_episode_prompt
        monkeypatch.delenv("VIDEO_ENGINE", raising=False)
        off = build_episode_prompt("socratic", "Cells", "Middle School", "5.0", "ctx")
        assert "VISUAL CONTINUITY" not in off
        monkeypatch.setenv("VIDEO_ENGINE", "scene")
        on = build_episode_prompt("socratic", "Cells", "Middle School", "5.0", "ctx")
        assert "VISUAL CONTINUITY" in on
        assert '"chapters"' in on and "VERBATIM" in on
        # the keys must appear in the OUTPUT FORMAT example itself — a
        # JSON-conforming model follows the example, not appended prose
        # (measured: spec-only wiring produced zero scenes on a real chapter)
        fmt = on[on.find("OUTPUT FORMAT"):]
        assert '"visual_plan": {' in fmt          # the plan lives IN the example
        assert '"decision": "NEW_VISUAL"' in fmt
        assert "VISUAL CONTINUITY" in on
        # all five narration styles share the identical block (schema contract)
        for st in NARRATION_STYLES:
            assert "VISUAL CONTINUITY" in build_episode_prompt(st, "C", "M", "5.0", "x")

    def _fake_client(self, seg_extra: dict):
        canned = {"segments": [{
            "type": "explore", "text": "the wall is drawn now",
            "elevenlabs_text": "the wall is drawn now",
            "slide_heading": "Walls", "slide_points": ["one point"],
            "estimated_duration_seconds": 30, **seg_extra,
        }]}

        class Fake:
            def analyze(self, prompt=None, system=None, max_tokens=0, **kw):
                return {"data": canned}
        return Fake()

    def test_scene_and_assets_pass_through_to_segments(self):
        from agent3_scripts.script_generator import generate_episode_script
        client = self._fake_client({
            "scene": {"id": "s", "elements": [{"id": "d", "type": "shape",
                                               "shape": "path",
                                               "points": [[0, 0], [9, 9]]}],
                      "actions": [{"verb": "draw", "target": "d"}]},
            "scene_assets": {"wall_diagram": "a wall"},
        })
        ep = generate_episode_script({"episode_num": 1}, {"chapter_title": "C"},
                                     1, client)
        seg = ep.segments[0]
        assert seg.scene is not None and seg.scene["id"] == "s"
        assert seg.scene_assets == {"wall_diagram": "a wall"}

    def test_malformed_scene_shapes_dropped_not_fatal(self):
        from agent3_scripts.script_generator import generate_episode_script
        client = self._fake_client({"scene": "not-a-dict",
                                    "scene_assets": ["not", "a", "map"]})
        ep = generate_episode_script({"episode_num": 1}, {"chapter_title": "C"},
                                     1, client)
        assert ep.segments[0].scene is None
        assert ep.segments[0].scene_assets is None


class TestDispatch:
    def test_invalid_scene_returns_false_never_raises(self):
        seg = {"segment_id": "s001", "scene": {"garbage": True}}
        assert _render_scene_segment(seg, "narr", None, 0.0, "x.mp4", "ltr") is False

    def test_missing_scene_returns_false(self):
        assert _render_scene_segment({"segment_id": "s"}, "n", None, 0.0,
                                     "x.mp4", "ltr") is False

    def test_valid_scene_reaches_encoder(self, tmp_path, monkeypatch):
        # stop at the encoder seam: prove the whole bind/compile path ran
        calls = {}

        def fake_encode(frames, total, audio, out, fps):
            calls["total"] = total
            for _ in frames:  # drain the generator like ffmpeg would
                pass
            return True

        import spike.scene_engine.encode as enc
        monkeypatch.setattr(enc, "encode_scene", fake_encode)
        seg = {"segment_id": "s001", "scene": dict(_SCENE)}
        ok = _render_scene_segment(seg, "some narration", None, 0.0,
                                   tmp_path / "o.mp4", "ltr")
        assert ok is True and calls["total"] > 0

    def test_rtl_direction_threads_into_scene(self, monkeypatch):
        seen = {}
        import spike.scene_engine.encode as enc

        def fake_encode(frames, total, audio, out, fps):
            next(iter(frames), None)
            return True
        monkeypatch.setattr(enc, "encode_scene", fake_encode)

        import spike.scene_engine.director as dr
        real = dr.parse_scene_response

        def spy(scene, narration):
            s = real(scene, narration)
            seen["scene"] = s
            return s
        monkeypatch.setattr(dr, "parse_scene_response", spy)
        _render_scene_segment({"segment_id": "s", "scene": dict(_SCENE)},
                              "n", None, 0.0, "x.mp4", "rtl")
        assert seen["scene"].direction == "rtl"


class TestAcceptanceReadsTheEpisodePlan:
    """The acceptance report read `script_data["visual_plan"]`, a key
    `part_scripts` never carried — the dump lives under episodes[0]. So every
    production lesson reported Visual Chapters 0 / Arrow Count 0 / Human
    Teaching Moments 0 whatever the compiler did, and the founder's Cells
    Part 2 (17 scene segments, one seeded moment, one seeded key point)
    printed zeros for all of them."""

    _MANIFEST = {"segments": [{"segment_id": "s001", "renderer": "scene",
                               "audio_path": "a.mp3", "scene_audit": []}]}
    _PLAN = {
        "plan": {"chapters": [{"concept": "cell", "elements": [
            {"id": "arr_nucleus", "type": "arrow"}]}]},
        "stats": {"visual_chapters": 3, "human_teaching_moments": 1,
                  "teacher_key_points": 1},
        "report": ["CHAPTER cell | SYNTHESIZED lbl_auto_nucleus"],
    }

    @staticmethod
    def _accept(script_data, monkeypatch):
        from worker import process
        monkeypatch.setenv("VIDEO_ENGINE", "scene")
        return process._acceptance_report(
            script_data, TestAcceptanceReadsTheEpisodePlan._MANIFEST)

    def test_plan_under_episodes_reaches_the_report(self, monkeypatch):
        out = self._accept({"episodes": [{"visual_plan": self._PLAN}]},
                           monkeypatch)
        assert out is not None
        assert out["report"]["visual_chapters"] == 3
        assert out["report"]["human_teaching_moments"] == 1
        assert out["report"]["teacher_key_points"] == 1
        # one declared arrow + one SYNTHESIZED report line
        assert out["report"]["arrow_count"] == 2
        assert out["plan_report"] == self._PLAN["report"]

    def test_the_direct_key_still_works(self, monkeypatch):
        out = self._accept({"visual_plan": self._PLAN}, monkeypatch)
        assert out is not None and out["report"]["visual_chapters"] == 3

    def test_no_plan_at_all_is_still_a_clean_zero(self, monkeypatch):
        out = self._accept({"episodes": [{}]}, monkeypatch)
        assert out is not None
        assert out["report"]["visual_chapters"] == 0
        assert out["plan_report"] == []

    def test_part_scripts_carries_the_plan_key(self):
        """The dict `process` builds must actually contain what the report
        now reads — a source check, because building it needs the whole
        pipeline."""
        import inspect
        from worker import process
        src = inspect.getsource(process.process_generation)
        assert '"visual_plan": script_dict.get("visual_plan")' in src
