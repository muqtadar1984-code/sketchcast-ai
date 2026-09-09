"""A script can name every topic and still say nothing about any of them.

Both numbers below were measured by downloading the two live Cells scripts and
running `coverage.script_text` over them — the same function the gate uses:

    script_text   chars/topic   video     coverage   verdict
       6,363         219.4      5.9 min    1.000       ok
       3,132         108.0      2.4 min    0.897       ok     <- shipped

Same article, same 29 topics, same 11 segments, six hours apart. Coverage
passed the second one because 26 of 29 topics were named. Naming is not
teaching, and nothing looked at how much was said.
"""

from __future__ import annotations

import pytest

from shared import coverage

TOPICS = 29
THIN_CHARS = 3132       # the 2.4-minute script, measured
HEALTHY_CHARS = 6363    # the 5.9-minute script, measured


def _report(chars: int, topics: int = TOPICS, **over) -> dict:
    r = {"gated": True, "checked": True, "pooled": False,
         "topics": topics, "chars": chars,
         "chars_per_topic": round(chars / topics, 1),
         "thin": (chars / topics) < coverage._DEPTH_MIN_CHARS_PER_TOPIC}
    r.update(over)
    return r


class TestTheFloorSeparatesTheTwoLiveRuns:
    def test_the_shipped_2_4_minute_script_is_refused(self):
        assert coverage.is_thin(_report(THIN_CHARS), mode="strict")

    def test_the_5_9_minute_script_passes(self):
        assert not coverage.is_thin(_report(HEALTHY_CHARS), mode="strict")

    def test_the_floor_sits_between_them_with_margin_on_both_sides(self):
        """If someone retunes this, the measured tiers must still separate."""
        thin = THIN_CHARS / TOPICS
        healthy = HEALTHY_CHARS / TOPICS
        floor = coverage._DEPTH_MIN_CHARS_PER_TOPIC
        assert thin < floor < healthy
        assert floor / thin > 1.2, "too close to the broken tier"
        assert healthy / floor > 1.2, "too close to the healthy tier"


class TestItSharesEveryGuardWithTheCoverageGate:
    """A depth failure must be no more eager to fire than a breadth failure."""

    def test_a_pooled_part_scoped_report_never_fails(self):
        """Its denominator is the whole chapter's concept list, so a part-3
        script measuring thin is the gate failing, not the script."""
        assert not coverage.is_thin(_report(THIN_CHARS, pooled=True),
                                    mode="strict")

    def test_an_ungated_report_never_fails(self):
        assert not coverage.is_thin(_report(THIN_CHARS, gated=False),
                                    mode="strict")

    def test_an_unchecked_report_never_fails(self):
        assert not coverage.is_thin(_report(THIN_CHARS, checked=False),
                                    mode="strict")

    @pytest.mark.parametrize("mode", ["off", "warn"])
    def test_off_and_warn_measure_without_refusing(self, mode):
        assert not coverage.is_thin(_report(THIN_CHARS), mode=mode)


class TestMeasurement:
    def test_measure_records_depth_beside_breadth(self):
        """The ratio must be recorded on every run, failing or not — the
        threshold above was only settable because duration was already being
        recorded on runs nobody was gating."""
        analysis = {"concepts": {"concepts": [
            {"concept_id": "c1", "name": "Mitochondrion"},
            {"concept_id": "c2", "name": "Nucleus"},
            {"concept_id": "c3", "name": "Cell Wall"},
            {"concept_id": "c4", "name": "Ribosome"}]}}
        text = "The nucleus is the control centre. " * 20
        rep = coverage.measure(analysis, None, text)
        if rep.get("checked") and rep.get("topics"):
            assert rep["chars"] == len(text)
            assert rep["chars_per_topic"] == round(len(text) / rep["topics"], 1)
            assert isinstance(rep["thin"], bool)

    def test_depth_is_a_different_question_from_breadth(self):
        """The whole point: a script that names everything briefly scores well
        on coverage and badly here. If these two ever agree on every input,
        one of them is redundant."""
        assert _report(THIN_CHARS)["thin"] is True
        assert _report(HEALTHY_CHARS)["thin"] is False


class TestDefaultMode:
    def test_it_ships_strict_unlike_the_coverage_gate(self, monkeypatch):
        """Coverage shipped as `warn` because no distribution existed. Depth
        ships `strict` because the distribution is in hand and the two tiers do
        not overlap. A bad video is worse than no video, and this refuses one
        after the script call — before a character of TTS or a single frame."""
        monkeypatch.delenv("DEPTH_GATE", raising=False)
        assert coverage.depth_gate_mode() == "strict"

    @pytest.mark.parametrize("val,want", [("off", "off"), ("warn", "warn"),
                                          ("strict", "strict"),
                                          ("nonsense", "strict")])
    def test_the_env_var_can_stand_it_down_without_a_deploy(
            self, monkeypatch, val, want):
        monkeypatch.setenv("DEPTH_GATE", val)
        assert coverage.depth_gate_mode() == want
