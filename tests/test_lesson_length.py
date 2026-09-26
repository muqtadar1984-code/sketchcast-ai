"""The spoken-length floor: a catalogue lesson runs at least five minutes.

Aerobic Respiration, 2026-09-26. The analyser handed the script model a
9.0-minute target for a 1,170-word article; the model returned 620 words of
dialogue and the video ran 170 seconds. The draft named 11 of 14 topics
(0.786, "ok") at 260 characters a topic (not thin), so every gate passed it.
Nothing compared what was written to the minutes asked for. This does.
"""

from __future__ import annotations

import pytest

from shared import coverage, lesson_length

# Measured on the two Aerobic Respiration kits and the longest recent one:
# spoken characters and the audio they produced.
SHORT_KIT = {"chars": 3494, "audio_secs": 170.8}     # 2.8 min — shipped
EARLIER_KIT = {"chars": 4292, "audio_secs": 215.6}   # 3.6 min — shipped
LONG_KIT = {"chars": 6802, "audio_secs": 348.5}      # 5.8 min


def _script(chars: int, dialogue: bool = False) -> dict:
    """A script whose spoken text is exactly ``chars`` characters."""
    text = ("x" * 9 + " ") * (chars // 10) + "x" * (chars % 10)
    assert len(text) == chars
    if dialogue:
        seg = {"text": "", "dialogue": [{"who": "teacher", "line": text}]}
    else:
        seg = {"text": text}
    return {"segments": [seg, {"type": "title", "text": ""}]}


class TestTheRateIsTheFastestMeasured:
    @pytest.mark.parametrize("kit", [SHORT_KIT, EARLIER_KIT, LONG_KIT])
    def test_no_measured_voice_ran_faster_than_the_rate(self, kit):
        rate = kit["chars"] / (kit["audio_secs"] / 60)
        assert rate <= lesson_length.CHARS_PER_MINUTE, "a faster voice would beat the floor"

    def test_five_minutes_is_6500_characters(self):
        assert lesson_length.min_chars(5.0) == 6500
        assert lesson_length.min_words(5.0) == 1083


class TestTheIncident:
    def test_the_shipped_three_minute_draft_is_under_the_floor(self):
        m = lesson_length.measure(_script(SHORT_KIT["chars"]), 5.0)
        assert m["under"] is True and m["chars"] == 3494 and m["min_chars"] == 6500
        assert m["est_minutes"] == pytest.approx(2.69, abs=0.01)

    def test_the_earlier_three_and_a_half_minute_draft_is_under_it_too(self):
        assert lesson_length.measure(_script(EARLIER_KIT["chars"]), 5.0)["under"] is True

    def test_a_draft_that_ran_nearly_six_minutes_clears_it(self):
        assert lesson_length.measure(_script(LONG_KIT["chars"]), 5.0)["under"] is False


class TestWhatIsCounted:
    def test_dialogue_lines_count_when_the_text_field_is_blank(self):
        # The semantic prompt writes dialogue and leaves "text" empty.
        assert lesson_length.spoken_text(_script(100, dialogue=True)) == lesson_length.spoken_text(_script(100))

    def test_on_screen_labels_do_not_count(self):
        script = {"segments": [{"text": "spoken", "slide_heading": "A heading nobody reads aloud",
                                "scene": {"elements": [{"type": "text", "text": "label"}]}}]}
        assert lesson_length.spoken_text(script) == "spoken"

    def test_a_zero_floor_never_measures_under(self):
        assert lesson_length.measure(_script(10), 0.0)["under"] is False

    def test_the_env_var_moves_the_floor_and_junk_keeps_the_default(self, monkeypatch):
        monkeypatch.setenv("LESSON_MIN_MINUTES", "7")
        assert lesson_length.min_minutes() == 7.0
        monkeypatch.setenv("LESSON_MIN_MINUTES", "-3")
        assert lesson_length.min_minutes() == 0.0
        monkeypatch.setenv("LESSON_MIN_MINUTES", "long")
        assert lesson_length.min_minutes() == 5.0
        monkeypatch.delenv("LESSON_MIN_MINUTES")
        assert lesson_length.min_minutes() == 5.0


class TestTheGateWiring:
    def _report(self, chars: int, **over) -> dict:
        r = {"gated": True, "checked": True, "verdict": "ok", "covered": 0.9, "thin": False,
             "length": lesson_length.measure(_script(chars), 5.0)}
        r.update(over)
        return r

    def test_under_length_earns_the_retry_in_every_mode(self):
        r = self._report(3494)
        assert coverage.under_length(r)
        for mode in ("off", "warn", "strict"):
            assert coverage.wants_retry(r, mode) is True, mode
        assert coverage.should_retry(r, "strict") is False, "coverage itself asked for nothing"

    def test_a_pooled_part_is_still_held_to_its_own_length(self):
        r = self._report(3494, pooled=True, verdict="pooled")
        assert coverage.wants_retry(r, "warn") is True

    def test_a_report_without_a_measurement_is_never_under(self):
        assert coverage.under_length({"verdict": "ok"}) is False
        assert coverage.wants_retry({"verdict": "ok", "gated": True, "checked": True}, "warn") is False

    def test_length_outranks_depth_and_breadth_when_choosing_the_draft(self):
        short_full = self._report(3494, covered=1.0)
        long_partial = self._report(7000, covered=0.8)
        assert coverage.better_draft(short_full, long_partial) is True
        assert coverage.better_draft(long_partial, short_full) is False

    def test_between_two_short_drafts_the_longer_wins(self):
        assert coverage.better_draft(self._report(3494), self._report(4292)) is True
        assert coverage.better_draft(self._report(4292), self._report(3494)) is False

    def test_between_two_long_enough_drafts_the_old_rules_decide(self):
        thin, deep = self._report(7000, thin=True), self._report(7000, thin=False)
        assert coverage.better_draft(thin, deep) is True
        assert coverage.better_draft(deep, thin) is False
        assert coverage.better_draft(self._report(7000, covered=0.8), self._report(7000, covered=0.9)) is True


class TestThePrompt:
    def _ask(self, **kw):
        from agent3_scripts.script_generator import generate_episode_script

        seen: dict = {}

        class _Stub:
            model = "stub"

            def analyze(self, prompt, system=None, max_tokens=0, **kw):
                seen["prompt"] = prompt
                return {"data": {"segments": [{"type": "explore", "text": "Cells respire."}]}}

        analysis = {"chapter_title": "Aerobic respiration", "concepts": {"concepts": []},
                    "episodes": {"episodes": [{"episode_num": 1, "title": "Aerobic respiration",
                                               "sections_covered": ["Respiration"],
                                               "estimated_duration_minutes": 3.0}]}}
        generate_episode_script(analysis["episodes"]["episodes"][0], analysis, 1, _Stub(), **kw)
        return seen["prompt"]

    def test_the_floor_reaches_the_model_in_minutes_words_and_characters(self):
        prompt = self._ask(min_minutes=5.0)
        assert "MINIMUM LENGTH" in prompt and "at least 5 minutes" in prompt
        assert "1,083 words" in prompt and "6,500 characters" in prompt
        assert "LENGTH — a previous draft" not in prompt

    def test_the_target_duration_is_raised_above_the_floor(self):
        # A 3.0-minute analyser target would contradict a 5-minute floor on
        # the next line of the prompt.
        assert "TARGET DURATION: 6.0 minutes" in self._ask(min_minutes=5.0)
        assert "TARGET DURATION: 3.0 minutes" in self._ask()

    def test_the_retry_names_the_measured_shortfall(self):
        shortfall = lesson_length.measure(_script(3494), 5.0)
        prompt = self._ask(min_minutes=5.0, length_shortfall=shortfall)
        assert "LENGTH — a previous draft of this script ran about 2.69 minutes" in prompt
        assert "3,494 characters" in prompt and "floor of 5 minutes (6,500 characters)" in prompt

    def test_no_floor_leaves_the_prompt_untouched(self):
        prompt = self._ask()
        assert "MINIMUM LENGTH" not in prompt and "LENGTH —" not in prompt


class TestTheWorkerHelper:
    def test_with_length_attaches_the_measurement_only_when_a_floor_applies(self):
        from worker.process import _with_length

        report = {"part": 1, "of": 1}
        assert "length" not in _with_length(report, _script(100), 0.0)
        assert _with_length(report, _script(100), 5.0)["length"]["under"] is True

    def test_a_measurement_failure_never_fails_the_lesson(self, monkeypatch):
        from worker import process

        def boom(*a, **k):
            raise RuntimeError("measure broke")

        monkeypatch.setattr(process.lesson_length, "measure", boom)
        assert "length" not in process._with_length({}, _script(10), 5.0)
