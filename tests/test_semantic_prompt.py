"""The semantic director prompt (SEMANTIC_PLAN=1).

Guards the properties that were each paid for by a real failure: minified
JSON, hard caps, a FILLED example the model can imitate, no coordinates, no
biology bias, and no instructions that contradict decisions the renderer owns.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent3_scripts.prompts import NARRATION_STYLES, build_episode_prompt
from agent3_scripts.semantic_prompt import build_semantic_prompt
from spike.scene_engine.semantic import adapt_semantic_plan


def _example(prompt: str) -> dict:
    """The worked example, parsed out of the prompt text."""
    start = prompt.index('{\n  "segments"')
    end = prompt.index("\n}", prompt.index('"visual_plan"')) + 2
    return json.loads(prompt[start:end])


def _p(style="conversational", **kw):
    kw.setdefault("chapter_title", "Rivers and Erosion")
    kw.setdefault("difficulty_level", "Grade 7")
    kw.setdefault("target_duration", "6.0")
    kw.setdefault("episode_context", "<sections>")
    return build_semantic_prompt(style, **kw)


class TestContract:
    def test_forbids_coordinates_and_timing(self):
        p = _p()
        for phrase in ("NO PIXELS", "never estimate where something is",
                       "timestamps", "durations"):
            assert phrase.lower() in p.lower(), phrase
        # and shows no coordinate literal anywhere
        assert not re.search(r'"(head|tail|at)":\s*\[\s*\d', p)

    def test_no_quoted_value_is_followed_by_a_parenthetical(self):
        """This model copies surface FORM, prose included. The prompt read
        'Speakers: "teacher" (primary explanatory voice)' and a real reply
        came back as {"who":("teacher"),...} — unparseable, lesson lost. Any
        quoted token trailed by a bracket teaches that shape, so none may
        appear anywhere in the prompt.
        """
        for style in NARRATION_STYLES:
            p = build_semantic_prompt(
                style, chapter_title="T", difficulty_level="Grade 7",
                target_duration="6.0", episode_context="ctx")
            bad = re.findall(r'"[a-z_]+"\s*\([^)]{0,60}\)', p)
            assert not bad, f"{style}: quoted value then parenthetical: {bad}"

    def test_demands_minified_json(self):
        # pretty-printed replies truncate mid-array — the most common way a
        # lesson dies
        assert "MINIFIED JSON" in _p()

    def test_states_hard_caps(self):
        p = _p()
        for cap in ("5 visual chapters", "12 elements", "10 steps",
                    "6 actions per step"):
            assert cap in p, cap

    def test_example_is_filled_and_parses(self):
        """The model imitates the EXAMPLE, not the prose — so it must be a
        complete, valid instance, not empty {} scaffolding."""
        p = _p()
        start = p.index("{\n  \"segments\"")
        end = p.index("\n}", p.index('"visual_plan"')) + 2
        example = json.loads(p[start:end])
        seg = example["segments"][0]
        assert seg["text"] == "" and seg["elevenlabs_text"] == ""
        assert len(seg["dialogue"]) >= 2
        ch = example["visual_plan"]["chapters"][0]
        assert ch["semantic_regions"] and ch["elements"] and ch["steps"]
        assert any(s["actions"] == [] for s in ch["steps"]), \
            "an empty-actions step must be modelled, not just described"

    def test_the_example_shows_a_chapter_boundary(self):
        """A real lesson declared three root visuals inside ONE chapter, so
        the compiler discarded two and hung every later label on the wrong
        picture. Prose alone does not fix that — this model copies the
        EXAMPLE — so the example must SHOW the second chapter."""
        example = _example(_p())
        chapters = example["visual_plan"]["chapters"]
        assert len(chapters) >= 2, "the example models only one chapter"
        for ch in chapters:
            roots = [e for e in ch["elements"]
                     if e.get("role") == "root_visual"]
            assert len(roots) == 1, f"{ch['id']} has {len(roots)} root visuals"
        # and the second one is entered by clearing the board
        assert any(s["decision"] == "CLEAR_AND_REDRAW"
                   for s in chapters[1]["steps"])

    def test_the_example_actually_adapts(self):
        """The worked example is the thing the model copies, so it had better
        survive the adapter in strict mode."""
        example = _example(_p())
        narr = {f"s{i + 1:03d}": " ".join(d["line"] for d in seg["dialogue"])
                for i, seg in enumerate(example["segments"])}
        plan, issues = adapt_semantic_plan(example["visual_plan"], narr,
                                           strict=True)
        assert issues == []
        ch = plan["chapters"][0]
        assert ch["transition"] == "clear_and_redraw"
        arrow = next(e for e in ch["elements"] if e.get("type") == "arrow")
        assert arrow["head"]["layer"] == "outer_bank"
        assert ch["steps"][0].get("moment", {}).get("role") == "student"


class TestNoRegressions:
    def test_no_biology_bias(self):
        """The legacy prompt showed a plant cell 20+ times in EVERY lesson,
        which biased maths and humanities toward labelled diagrams."""
        p = _p().lower()
        for w in ("nucleus", "chloroplast", "plant cell", "membrane"):
            assert w not in p, w
        # and it explicitly names non-diagram grammars
        for w in ("timeline", "graph", "equation", "map", "worked example"):
            assert w in p, w

    def test_renderer_owned_decisions_are_not_dictated(self):
        """Avatar persistence and how speech appears are OUR decisions —
        the founder asked for a persistent teacher and speech that appears
        rather than being drawn."""
        p = _p().lower()
        assert "not permanently visible" not in p
        assert "drawn by the same hand" not in p
        assert "the engine decides which avatar appears" in p

    def test_every_style_builds_and_names_itself(self):
        for s in NARRATION_STYLES:
            p = build_semantic_prompt(
                s, chapter_title="T", difficulty_level="Grade 9",
                target_duration="6.0", episode_context="ctx")
            assert f"NARRATION STYLE: {s}" in p
            assert "AVAILABLE NARRATION STYLES" in p
            assert "dialogue" in p.lower()

    def test_learner_profile_is_threaded(self):
        p = _p(subject="Geography", curriculum="CBSE", learner_age="12")
        assert "SUBJECT: Geography" in p
        assert "CURRICULUM: CBSE" in p
        assert "LEARNER AGE: 12" in p

    def test_absent_profile_degrades_readably(self):
        p = _p()
        assert "(not supplied)" in p and "infer from the source content" in p

    def test_is_not_longer_than_the_legacy_prompt(self):
        """Input tokens are cheap, but attention is not: the semantic prompt
        replaces the legacy one, it must not balloon it.

        The bound was 1.25 and was raised to 1.4 to buy ONE thing: a second
        chapter in the worked example. Prose saying "a new main visual is a
        new chapter" did not hold — a real lesson declared three root visuals
        in one chapter, two were discarded, and every later label landed on
        the wrong picture. This model copies the example, so the example has
        to show the boundary. ~1,100 extra input tokens per generation.

        This is a creep guard, not a physical limit. Raise it again only to
        buy something equally concrete, and say what.
        """
        legacy = build_episode_prompt(
            "conversational", chapter_title="Rivers and Erosion",
            difficulty_level="Grade 7", target_duration="6.0",
            episode_context="<sections>")
        assert len(_p()) < len(legacy) * 1.4


class TestFlagWiring:
    def test_flag_off_uses_the_legacy_prompt(self, monkeypatch):
        import agent3_scripts.script_generator as sg
        monkeypatch.delenv("SEMANTIC_PLAN", raising=False)
        src = __import__("inspect").getsource(sg.generate_episode_script)
        assert 'os.getenv("SEMANTIC_PLAN", "").strip() == "1"' in src
        assert "build_semantic_prompt" in src and "build_episode_prompt" in src

    def test_duration_is_estimated_from_speech_when_absent(self):
        from agent3_scripts.script_generator import _estimate_seconds
        # the semantic contract forbids the director estimating durations
        assert _estimate_seconds({}, " ".join(["word"] * 130)) == 50
        # an explicit value still wins (legacy path)
        assert _estimate_seconds({"estimated_duration_seconds": 42}, "x") == 42
        # nothing spoken at all keeps the old nominal default
        assert _estimate_seconds({}, "") == 30


class TestJsonRepair:
    """Malformed-but-complete replies were reaching callers as 'produced no
    segments ... almost certainly cut off at the output-token cap'. Measured:
    the reply was 1,901 output tokens against a 32,000 cap and ended cleanly —
    it was a model JSON error, not truncation."""

    def test_stray_closing_bracket_is_repaired(self):
        from shared.claude_client import _repair_json
        out = _repair_json('{"segments": [{"type": "hook"}], '
                           '"visual_plan": {"chapters": []}]}')
        assert out and set(out) == {"segments", "visual_plan"}

    def test_ssml_quotes_inside_a_string_are_repaired(self):
        from shared.claude_client import _repair_json
        out = _repair_json('{"dialogue": [{"line": "Hey! '
                           '<break time="0.3s"/> Ready?"}]}')
        assert out and out["dialogue"][0]["line"].startswith("Hey!")

    def test_trailing_commas_are_repaired(self):
        from shared.claude_client import _repair_json
        assert _repair_json('{"a": [1, 2,], "b": 3,}') == {"a": [1, 2], "b": 3}

    def test_a_fenced_reply_is_not_mistaken_for_garbage(self):
        from shared.claude_client import _repair_json
        assert _repair_json('```json\n{"a": [1,],}\n```') == {"a": [1]}

    def test_it_never_trims_content_to_force_a_parse(self):
        """A genuinely truncated reply must stay a LOUD failure. If the
        salvage were allowed to cut into content it would hand back a short
        but well-formed lesson, and nothing downstream would notice."""
        from shared.claude_client import _repair_json
        cut_mid_content = ('{"segments": [{"type": "hook", "text": "the whole '
                           'point of the lesson is')
        assert _repair_json(cut_mid_content) is None

    def test_a_reply_cut_after_a_whole_element_stays_a_failure(self):
        """The subtler truncation: the cut lands on an element boundary, so
        the text is re-closable. Completing it would silently deliver a
        one-segment lesson instead of failing."""
        from shared.claude_client import _repair_json
        assert _repair_json('{"segments": [{"type": "hook"},') is None

    def test_a_misnested_closer_is_repaired(self):
        """The shape a real semantic reply actually died on: it ended
        `"actions": []}]}}` with the chapters ARRAY never closed, so a `}`
        arrived while `[` was open. 2,735 output tokens of a 32,000 cap —
        complete, just built wrong."""
        from shared.claude_client import _repair_json
        out = _repair_json(
            '{"segments": [{"type": "hook"}], "visual_plan": {"chapters": '
            '[{"id": "chapter_1", "steps": [{"segment": 1, "actions": []}]}}')
        assert out is not None
        assert len(out["segments"]) == 1
        # the whole chapter survives — a repair that silently dropped it
        # would be worse than the failure it replaces
        assert out["visual_plan"]["chapters"][0]["steps"][0]["segment"] == 1

    def test_an_underclosed_but_complete_reply_is_closed(self):
        """Three consecutive live replies were malformed, at 1,901 / 2,735 /
        3,998 output tokens against a 32,000 cap. One was missing only the
        outer brace. Refusing to close it fails a whole lesson over a
        bracket, so a reply ending on a COMPLETE value gets closed."""
        from shared.claude_client import _rebalance_json
        out = _rebalance_json('{"segments": [{"type": "hook"}]}')
        assert out is None                       # already valid, nothing to do
        assert json.loads(_rebalance_json('{"segments": [{"type": "hook"}]')
                          )["segments"][0]["type"] == "hook"

    def test_rebalancing_refuses_a_severed_value(self):
        """What must never be closed is a reply that stopped mid-thought:
        completing it invents a short lesson nothing downstream would catch.
        The tell is how the text ENDS."""
        from shared.claude_client import _rebalance_json
        assert _rebalance_json('{"a": [1]}, "b": "half a sent') is None
        assert _rebalance_json('{"segments": [{"type": "hook"},') is None
        assert _rebalance_json('{"segments": [{"type":') is None

    def test_truncation_is_caught_by_the_finish_reason(self):
        """The compensating control for the relaxation above. A reply cut off
        at an element boundary parses fine, so the parser cannot catch it —
        the provider's finish reason can."""
        import inspect
        from agent3_scripts.script_generator import generate_episode_script
        src = inspect.getsource(generate_episode_script)
        assert 'result.get("truncated")' in src

    def test_truncation_is_never_inferred_from_usage(self):
        """usage SUMS both attempts when a client retries at double the
        budget, so a SUCCESSFUL retry reports ~2x the original cap. Deriving
        truncation from that fails a lesson that actually worked."""
        import inspect
        from agent3_scripts.script_generator import generate_episode_script
        src = inspect.getsource(generate_episode_script)
        assert "max_out * 0.98" not in src, \
            "truncation is being inferred from the token count again"

    def test_both_clients_report_the_finish_reason(self):
        """script_generator can only trust `truncated` if every client it can
        be handed actually sets it."""
        from shared.claude_client import ClaudeClient
        from shared.gemini_client import GeminiClient
        import inspect
        for cls in (ClaudeClient, GeminiClient):
            src = inspect.getsource(cls.analyze)
            assert '"truncated"' in src, f"{cls.__name__} does not report it"

    def test_a_key_emitted_twice_is_repaired(self):
        """Measured on a real reply that failed an entire lesson: the model
        wrote {"who":"who":"teacher"} — the key repeated with its own name
        standing in as the value. Brackets were fine, so the structural
        rebalancer could not help."""
        from shared.claude_client import _repair_json
        out = _repair_json(
            '{"dialogue": [{"who":"who":"teacher", "line": "Hi"}]}')
        assert out["dialogue"][0]["who"] == "teacher"
        assert out["dialogue"][0]["line"] == "Hi"

    def test_the_duplicate_key_rule_leaves_honest_json_alone(self):
        """It must not rewrite a value that merely equals its key."""
        from shared.claude_client import ClaudeClient
        out = ClaudeClient._extract_json('{"who": "who", "line": "Hi"}')
        assert out == {"who": "who", "line": "Hi"}

    def test_a_dropped_line_key_is_repaired(self):
        """{"who": "teacher": "text"} — the "line" key omitted and its text
        left hanging off the speaker. Fourth distinct malformation measured
        on this path, and like the other three it corrupts the dialogue
        object specifically."""
        from shared.claude_client import _repair_json
        out = _repair_json(
            '{"dialogue": [{"who": "teacher": "And then the nucleus."}]}')
        assert out["dialogue"][0] == {"who": "teacher",
                                      "line": "And then the nucleus."}

    def test_every_malformation_measured_so_far_is_covered(self):
        """One place to see the whole set, so the next one joins a list
        rather than being rediscovered from scratch."""
        from shared.claude_client import _repair_json
        cases = {
            "unclosed chapters array":
                '{"segments": [1], "visual_plan": {"chapters": [{"a": 1}}',
            "duplicated key":
                '{"segments": [1], "d": [{"who":"who":"teacher"}]}',
            "bracketed value":
                '{"segments": [1], "d": [{"who":("teacher")}]}',
            "dropped line key":
                '{"segments": [1], "d": [{"who": "teacher": "hi"}]}',
            "ssml quotes":
                '{"segments": [1], "d": "a <break time="0.3s"/> b"}',
            "trailing comma":
                '{"segments": [1], "d": [1,],}',
        }
        for name, text in cases.items():
            assert _repair_json(text) is not None, f"no longer repaired: {name}"

    def test_genuine_garbage_still_fails(self):
        from shared.claude_client import _repair_json
        assert _repair_json("this is not json at all") is None
        assert _repair_json("") is None

    def test_valid_json_never_goes_near_the_repairer(self):
        from shared.claude_client import ClaudeClient
        assert ClaudeClient._extract_json('{"ok": 1}') == {"ok": 1}


class TestValidatorCatchesAnEmptyLesson:
    """A 42-segment lesson whose visual plan was dropped rendered as 42 plain
    cards — 0 scenes, 0 chapters, 0 arrows — and validation returned PASSED,
    because `passed` only asked whether the LEGACY renderer had leaked in.
    Every quality number was zero, which read as "nothing wrong" rather than
    "nothing happened"."""

    @staticmethod
    def _manifest(renderer, n=6):
        # audio_path present so these exercise the SCENES check specifically;
        # the silence check is covered separately in TestSilentLessonGuards.
        return {"segments": [{"segment_id": f"s{i:03d}", "renderer": renderer,
                              "audio_path": f"/tmp/s{i}.mp3"}
                             for i in range(n)]}

    def test_a_lesson_with_no_scenes_fails(self):
        from spike.scene_engine.validate import (format_report,
                                                 validate_visual_language)
        r = validate_visual_language(self._manifest("whiteboard"), {})
        assert r["scene_segments"] == 0 and r["legacy_renderer_usage"] == 0
        assert r["no_scenes_produced"] is True
        assert r["passed"] is False, \
            "an all-fallback lesson must not pass"
        assert "NO scenes" in format_report(r)

    def test_a_normal_lesson_with_a_few_fallbacks_still_passes(self):
        from spike.scene_engine.validate import validate_visual_language
        m = self._manifest("scene", 5)
        m["segments"].append({"segment_id": "s099", "renderer": "whiteboard"})
        r = validate_visual_language(m, {})
        assert r["passed"] is True and r["no_scenes_produced"] is False

    def test_legacy_leakage_still_fails(self):
        from spike.scene_engine.validate import validate_visual_language
        m = self._manifest("scene", 5)
        m["segments"].append({"segment_id": "s099", "renderer": "native"})
        assert validate_visual_language(m, {})["passed"] is False


class TestShortScriptGuard:
    """One run returned a SINGLE segment for 13.8 minutes of source material,
    in 9 seconds, and everything downstream accepted it. Zero segments was
    already caught; one is the more dangerous shape, because it looks like
    output — a one-card video would ship and a credit would be spent."""

    def test_the_guard_is_wired(self):
        import inspect
        from agent3_scripts.script_generator import generate_episode_script
        src = inspect.getsource(generate_episode_script)
        assert "_min_segments" in src
        assert "too short to be a real" in src

    def test_a_short_lesson_may_legitimately_be_one_segment(self):
        """The floor must scale with the lesson, not punish genuinely tiny
        ones."""
        import inspect
        from agent3_scripts.script_generator import generate_episode_script
        src = inspect.getsource(generate_episode_script)
        assert "3 if target_duration >= 3.0 else 1" in src


class TestSilentLessonGuards:
    """A 4-minute video shipped with 25 of 26 segments carrying no narration
    at all — the director obeyed "set text to empty" and never wrote the
    dialogue meant to replace it. Nothing objected, and the report said
    PASSED."""

    def test_script_generation_refuses_a_mostly_silent_script(self):
        import inspect
        from agent3_scripts.script_generator import generate_episode_script
        src = inspect.getsource(generate_episode_script)
        assert "_silent" in src and "NO narration" in src

    def test_validation_fails_a_lesson_with_no_audio(self):
        from spike.scene_engine.validate import (format_report,
                                                 validate_visual_language)
        segs = [{"segment_id": f"s{i:03d}", "renderer": "scene"}
                for i in range(8)]
        r = validate_visual_language({"segments": segs}, {})
        assert r["mostly_silent"] is True
        assert r["passed"] is False
        assert "silent" in format_report(r)

    def test_a_lesson_with_audio_passes(self):
        from spike.scene_engine.validate import validate_visual_language
        segs = [{"segment_id": f"s{i:03d}", "renderer": "scene",
                 "audio_path": f"/tmp/s{i}.mp3"} for i in range(8)]
        r = validate_visual_language({"segments": segs}, {})
        assert r["mostly_silent"] is False and r["passed"] is True

    def test_one_quiet_segment_is_tolerated(self):
        from spike.scene_engine.validate import validate_visual_language
        segs = [{"segment_id": f"s{i:03d}", "renderer": "scene",
                 "audio_path": f"/tmp/s{i}.mp3"} for i in range(8)]
        segs[0].pop("audio_path")
        r = validate_visual_language({"segments": segs}, {})
        assert r["passed"] is True


class TestSingleLineDialogueIsNotSilence:
    """The cause of the silent lesson. Dialogue was only harvested when a
    segment carried TWO OR MORE lines, but the prompt tells the model to
    leave `text` empty AND permits a teacher-only segment — so every
    one-line segment ended up with no text and no dialogue. 28 of 29
    segments in a real render were silent."""

    @staticmethod
    def _seg(lines):
        return {"type": "explore", "text": "", "elevenlabs_text": "",
                "dialogue": [{"who": "teacher", "line": l} for l in lines],
                "slide_heading": "H", "slide_points": []}

    def _run(self, raw_segments):
        from agent3_scripts.script_generator import generate_episode_script

        class _Stub:
            def analyze(self, **kw):
                return {"data": {"segments": raw_segments}, "usage": {},
                        "truncated": False}

        return generate_episode_script(
            {"episode_num": 1, "title": "T", "sections": []},
            {"chapter_title": "T"}, 1, _Stub(),
            narration_style="conversational")

    def test_a_one_line_segment_still_speaks(self):
        out = self._run([self._seg(["Cells group into tissues."]),
                         self._seg(["Tissues group into organs."]),
                         self._seg(["Organs form systems."])])
        for s in out.segments:
            assert s.text.strip(), f"{s.segment_id} came out silent"
        assert out.segments[0].text == "Cells group into tissues."

    def test_two_lines_still_drive_two_voice_dialogue(self):
        out = self._run([self._seg(["A.", "B."]), self._seg(["C.", "D."]),
                         self._seg(["E.", "F."])])
        assert out.segments[0].dialogue is not None
        assert len(out.segments[0].dialogue) == 2
        assert out.segments[0].text == "A. B."

    def test_one_line_does_not_claim_a_two_voice_exchange(self):
        out = self._run([self._seg(["Only one."]), self._seg(["X.", "Y."]),
                         self._seg(["Z.", "W."])])
        assert out.segments[0].dialogue is None
        assert out.segments[0].text == "Only one."


class TestStudyNotesAreGone:
    """The deck and the video are being separated: they will be generated
    independently, so the video call no longer carries study-note fields.
    Founder decision, 2026-09-02."""

    def test_the_prompt_never_asks_for_study_notes(self):
        for style in NARRATION_STYLES:
            p = build_semantic_prompt(
                style, chapter_title="T", difficulty_level="Grade 7",
                target_duration="6.0", episode_context="ctx")
            assert "slide_points" not in p, style
            assert "slide_visual" not in p, style
            assert "STUDY NOTES" not in p, style

    def test_the_example_carries_no_study_note_fields(self):
        ex = _example(_p())
        for seg in ex["segments"]:
            assert "slide_points" not in seg
            assert "slide_visual" not in seg

    def test_slide_heading_survives_because_the_VIDEO_uses_it(self):
        """Not a study-note field: video_composer renders it as the heading on
        a whiteboard fallback card."""
        ex = _example(_p())
        assert all(s.get("slide_heading") for s in ex["segments"])

    def test_the_legacy_prompt_is_untouched(self):
        """The legacy path is what production runs; it still builds the deck."""
        legacy = build_episode_prompt(
            "conversational", chapter_title="T", difficulty_level="Grade 7",
            target_duration="6.0", episode_context="ctx")
        assert "slide_points" in legacy and "slide_visual" in legacy


class TestP0AcceptanceIsWired:
    """P0. The acceptance layer existed and production never called it: every
    "the report said PASSED" was a local driver talking to itself."""

    def test_the_worker_runs_the_acceptance_check(self):
        import inspect
        from worker import process
        src = inspect.getsource(process)
        assert "_acceptance_report" in src
        assert "validate_visual_language" in src, \
            "the validator must be reachable from the production worker"
        run = inspect.getsource(process._acceptance_report)
        assert "VIDEO_ENGINE" in run, "only applicable to the scene engine"

    def test_a_failed_acceptance_stops_the_lesson(self):
        import inspect
        from worker.process import process_generation
        src = inspect.getsource(process_generation)
        assert "failed acceptance" in src
        assert src.index("_acceptance_report") < src.index('"video_mp4"'), \
            "acceptance must run BEFORE the video is recorded as an artifact"

    def test_the_checker_never_destroys_a_good_lesson(self):
        """A validator bug must not fail a lesson that rendered fine."""
        import inspect
        from worker.process import _acceptance_report
        src = inspect.getsource(_acceptance_report)
        assert "except Exception" in src and "return None" in src


class TestP0NoLessonWithHoles:
    def test_a_missing_segment_stops_the_concat(self, tmp_path):
        from agent8_render.renderer import render_final_video
        good = tmp_path / "s001.mp4"
        good.write_bytes(b"x")
        manifest = {"book_id": "b", "chapter_num": 1, "episode_num": 1,
                    "segments": [
                        {"segment_id": "s001", "video_path": str(good),
                         "audio_duration_seconds": 1.0},
                        {"segment_id": "s002", "video_path": None},
                        {"segment_id": "s003",
                         "video_path": str(tmp_path / "nope.mp4")}]}
        with pytest.raises(RuntimeError) as e:
            render_final_video(video_manifest=manifest)
        msg = str(e.value)
        assert "2 of 3" in msg and "s002" in msg
        assert "holes" in msg


class TestP0StrictModeIsStrict:
    def test_adapter_error_is_re_raised_not_swallowed(self):
        import inspect
        from agent3_scripts.script_generator import generate_episode_script
        src = inspect.getsource(generate_episode_script)
        i_strict = src.index("except AdapterError")
        i_broad = src.index("except Exception:\n            logger.exception"
                            "(\"visual plan compilation failed")
        assert i_strict < i_broad, \
            "AdapterError must be caught BEFORE the broad handler"
        assert "raise" in src[i_strict:i_broad]
