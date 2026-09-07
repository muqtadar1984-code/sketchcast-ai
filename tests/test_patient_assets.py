"""Patient mode: a catalogue kit waits a rate-limit deferral out; a teacher's
lesson still does not (catalogue Phase 3 follow-up, 2026-09-07).

WHY THIS EXISTS. A deferral is a negative cache — the first 429 on a key
records "not this key, not yet" and every later caller in the lesson is told
at once, so eight render threads do not each burn a two-minute ladder on one
picture. For a teacher's lesson that is the right trade: somebody is waiting,
and a board that falls back to the vector tier beats a lesson that stalls.

The first live catalogue kit showed what the same trade costs a batch. Its
acceptance report read `unresolved_assets=4/11(rate_limited=4)`: four pictures
were not missing, they were merely not-yet, and nothing inside a lesson ever
comes back for them. Nobody waits on a catalogue kit — it runs off-peak and is
rebuilt only by a human clicking Retry — so it should sleep and then draw.

Everything here drives a FAKE clock and a fake sleep. No test may sleep for
real, and none may touch a network.
"""

from __future__ import annotations

import pytest

from spike.scene_engine import raster_assets as ra

GEN = "kit-generation"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """A fresh per-generation bucket, a clock we drive, and a sleep that only
    advances that clock."""
    for var in ("VERTEX_PROJECT_ID", "GOOGLE_AI_API_KEY", "GEMINI_API_KEY",
                "CATALOGUE_ASSET_WAIT_MAX_S", "CATALOGUE_ASSET_WAIT_BUDGET_S"):
        monkeypatch.delenv(var, raising=False)
    now = {"t": 1000.0}
    slept: list[float] = []

    def _sleep(seconds: float) -> None:
        slept.append(seconds)
        now["t"] += seconds

    monkeypatch.setattr(ra, "_now", lambda: now["t"])
    monkeypatch.setattr(ra, "_sleep", _sleep)
    ra.reset_image_budget(GEN)
    yield {"now": now, "slept": slept}
    ra.set_patient_assets(False, GEN)


def _defer(key: str, seconds: float) -> float:
    return ra.defer_asset(key, seconds)


class TestUnarmed:
    """A teacher's lesson registers no patience and must behave exactly as it
    did before this existed."""

    def test_a_deferred_key_is_refused_at_once_when_patience_is_not_armed(self, _clean):
        _defer("cell", 60)
        assert ra._wait_out_deferral("cell", 60) is False
        assert _clean["slept"] == [], "an unarmed lesson must not sleep at all"

    def test_patience_state_reports_unarmed(self):
        assert ra.patience_state()["armed"] is False


class TestWaiting:
    def test_a_deferred_key_is_waited_out_and_then_drawn(self, _clean):
        ra.set_patient_assets(True, GEN)
        _defer("cell", 30)
        assert ra._wait_out_deferral("cell", 30) is True, "the caller should go on to generate"
        assert sum(_clean["slept"]) == pytest.approx(30.0)
        assert ra.asset_deferred("cell") is None, "the deferral has expired by the time we return"

    def test_the_wait_is_broken_into_ticks_so_the_never_starve_check_runs_often(self, _clean):
        ra.set_patient_assets(True, GEN)
        _defer("cell", 30)
        ra._wait_out_deferral("cell", 30)
        assert len(_clean["slept"]) > 1, "one long sleep would ignore a user arriving mid-wait"
        assert max(_clean["slept"]) <= ra._PATIENCE_TICK_S

    def test_the_time_waited_is_accounted_against_the_lesson(self, _clean):
        ra.set_patient_assets(True, GEN)
        _defer("cell", 20)
        ra._wait_out_deferral("cell", 20)
        st = ra.patience_state()
        assert st["spent"] == pytest.approx(20.0) and st["waits"] == 1


class TestBounds:
    def test_a_deferral_longer_than_the_cap_is_not_waited_out(self, _clean):
        ra.set_patient_assets(True, GEN, max_wait=60)
        _defer("cell", 200)
        assert ra._wait_out_deferral("cell", 200) is False
        assert _clean["slept"] == [], "past the cap we must not sleep at all"

    def test_the_lesson_budget_stops_patience_and_says_so(self, _clean, caplog):
        ra.set_patient_assets(True, GEN, max_wait=100, budget=50)
        _defer("a", 40)
        assert ra._wait_out_deferral("a", 40) is True
        _defer("b", 40)
        with caplog.at_level("WARNING", logger=ra.logger.name):
            # 10s of budget is left, so the second wait is clipped and the key
            # is still deferred when it ends: the caller is refused, and the
            # THIRD asset finds the budget spent.
            ra._wait_out_deferral("b", 40)
        _defer("c", 10)
        with caplog.at_level("WARNING", logger=ra.logger.name):
            assert ra._wait_out_deferral("c", 10) is False
        assert any("patience budget" in r.getMessage() for r in caplog.records)

    def test_a_key_still_deferred_after_the_wait_is_not_waited_on_twice(self, _clean):
        ra.set_patient_assets(True, GEN, max_wait=100, budget=10)
        _defer("cell", 60)
        # only 10s of budget, so the wait is clipped and the key is still deferred
        assert ra._wait_out_deferral("cell", 60) is False
        assert sum(_clean["slept"]) == pytest.approx(10.0)


class TestNeverStarveOutranksPatience:
    def test_a_user_job_arriving_mid_wait_ends_it_and_gives_the_pool_back(self, _clean, caplog):
        ra.set_patient_assets(True, GEN)
        _defer("cell", 60)
        answers = iter([True, True, False])  # the third check finds a user builder live
        ra.set_user_yield(lambda what: next(answers, False), GEN)
        with caplog.at_level("WARNING", logger=ra.logger.name):
            assert ra._wait_out_deferral("cell", 60) is False
        assert sum(_clean["slept"]) < 60, "the wait stopped early"
        assert any("a user's job arrived while waiting" in r.getMessage() for r in caplog.records)

    def test_the_time_already_waited_is_still_charged_when_a_user_interrupts(self, _clean):
        ra.set_patient_assets(True, GEN)
        _defer("cell", 60)
        answers = iter([True, True, False])
        ra.set_user_yield(lambda what: next(answers, False), GEN)
        ra._wait_out_deferral("cell", 60)
        assert ra.patience_state()["spent"] > 0

    def test_a_lesson_whose_yield_hook_already_gave_up_never_starts_waiting(self, _clean):
        ra.set_patient_assets(True, GEN)
        ra.set_user_yield(lambda what: False, GEN)
        ra._clear_to_generate("prime the sticky give-up")
        _defer("cell", 60)
        assert ra._wait_out_deferral("cell", 60) is False
        assert _clean["slept"] == []


class TestArming:
    def test_arming_is_per_generation_so_one_lesson_cannot_make_another_patient(self):
        """The kit is patient; a teacher's lesson sharing the process is not.
        The generation is SWITCHED here rather than reset, because
        reset_image_budget starts a fresh bucket by design — what is under
        test is which bucket a thread reads, not what a reset clears."""
        ra.set_patient_assets(True, GEN)
        ra._GENERATION_VAR.set("some-teachers-lesson")
        assert ra.patience_state()["armed"] is False, "a teacher's lesson is never made patient"
        ra._GENERATION_VAR.set(GEN)
        assert ra.patience_state()["armed"] is True, "and the kit kept its own patience"

    def test_disarming_clears_it(self):
        ra.set_patient_assets(True, GEN)
        ra.set_patient_assets(False, GEN)
        assert ra.patience_state()["armed"] is False

    def test_the_defaults_come_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("CATALOGUE_ASSET_WAIT_MAX_S", "42")
        monkeypatch.setenv("CATALOGUE_ASSET_WAIT_BUDGET_S", "77")
        ra.set_patient_assets(True, GEN)
        st = ra.patience_state()
        assert st["max_wait"] == 42 and st["budget"] == 77
