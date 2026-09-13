"""A lesson that cannot get its pictures does not ship without them.

Before the gate: the warm pass asked for every picture once, then rendered
whatever it had. A picture still missing at the deadline became a placeholder
in the frame, and the acceptance report tolerated up to n//4 placeholders per
lesson — so ONE blank board always passed. Measured on the founder's own
retriggers: `unresolved_assets=4/11(rate_limited=4)` shipped a lesson with
four boards where the pictures were merely not-yet.

The founder's rule is the reverse: a bad video is worse than no video. The gate
is that rule at the one seam where refusing is free — after the warm pass,
before a frame is rasterised or a TTS character is bought.

Every provider here is a fake. Nothing in this file may make a network call.
"""

from __future__ import annotations

import inspect

import pytest

from spike.scene_engine import asset_warm
from spike.scene_engine import raster_assets as ra
from spike.scene_engine.asset_warm import (DEFAULT_GATE_WAIT_SECS,
                                           DEFAULT_WARM_BUDGET_SECS,
                                           GATE_RETRY_SOON_SECS,
                                           ImagesIncomplete, gate_budget_secs,
                                           gate_wait_secs, image_gate_mode,
                                           missing_pictures, one_more_turn,
                                           warm_lesson_assets)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in ("IMAGE_GATE", "IMAGE_GATE_WAIT_SECS", "IMAGE_WARM_BUDGET_SECS",
                "IMAGE_WARM_RETRY_SECS", "IMAGE_DEFER_SECONDS"):
        monkeypatch.delenv(var, raising=False)
    ra.reset_image_budget()
    yield
    ra.reset_image_budget()


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += float(s)


# ── the mode ─────────────────────────────────────────────────────────────────

class TestTheGateShipsStrict:
    def test_strict_is_the_default(self):
        """Like the depth gate and unlike the coverage gate: the distribution
        is in hand. A missing picture is never ambiguous."""
        assert image_gate_mode() == "strict"

    @pytest.mark.parametrize("val,want", [("off", "off"), ("warn", "warn"),
                                          ("strict", "strict"),
                                          ("STRICT ", "strict"),
                                          ("nonsense", "strict")])
    def test_one_variable_stands_it_down_without_a_deploy(self, monkeypatch,
                                                          val, want):
        monkeypatch.setenv("IMAGE_GATE", val)
        assert image_gate_mode() == want

    def test_the_extra_wait_comes_from_the_environment_defensively(
            self, monkeypatch):
        assert gate_wait_secs() == DEFAULT_GATE_WAIT_SECS == 300.0
        monkeypatch.setenv("IMAGE_GATE_WAIT_SECS", "90")
        assert gate_wait_secs() == 90.0
        monkeypatch.setenv("IMAGE_GATE_WAIT_SECS", "soon")
        assert gate_wait_secs() == DEFAULT_GATE_WAIT_SECS
        monkeypatch.setenv("IMAGE_GATE_WAIT_SECS", "-5")
        assert gate_wait_secs() == DEFAULT_GATE_WAIT_SECS

    def test_only_strict_buys_the_longer_wait(self):
        """`warn` must measure what strict would have refused on the SAME
        lesson, so it cannot change how long the lesson looked for pictures —
        otherwise its numbers describe a different run."""
        assert gate_budget_secs("strict") == (DEFAULT_WARM_BUDGET_SECS
                                              + DEFAULT_GATE_WAIT_SECS)
        assert gate_budget_secs("warn") == DEFAULT_WARM_BUDGET_SECS
        assert gate_budget_secs("off") == DEFAULT_WARM_BUDGET_SECS


# ── what is missing, and why ─────────────────────────────────────────────────

class TestWhatIsMissingAndWhy:
    ENTRIES = [("q7f3a1", "p"), ("k2b9", "p"), ("z11", "p")]

    def test_nothing_missing_when_everything_landed(self):
        res = {"ready": ["q7f3a1", "k2b9", "z11"], "pending": []}
        assert missing_pictures(self.ENTRIES, res) == {}

    def test_a_deferred_key_is_a_rate_limit(self):
        ra.defer_asset("k2b9", 30)
        res = {"ready": ["q7f3a1", "z11"], "pending": ["k2b9"]}
        assert missing_pictures(self.ENTRIES, res) == {"k2b9": "rate_limited"}

    def test_an_abandoned_key_is_reported_as_the_resolver_reports_it(self):
        """The resolver calls an abandoned key `rate_limited` too, because
        abandonment is reserved for the end of a 429 ladder. The gate must
        say the same thing about the same key as the acceptance report."""
        ra.abandon_asset("z11")
        res = {"ready": ["q7f3a1", "k2b9"], "pending": []}
        assert missing_pictures(self.ENTRIES, res) == {"z11": "rate_limited"}

    def test_our_own_ceiling_is_named_as_ours(self, monkeypatch):
        monkeypatch.setattr(asset_warm, "image_budget_exhausted", lambda: True)
        res = {"ready": ["q7f3a1"], "pending": []}
        assert missing_pictures(self.ENTRIES, res) == {
            "k2b9": "budget_exhausted", "z11": "budget_exhausted"}

    def test_a_rate_limit_outranks_the_budget(self, monkeypatch):
        """A 429 that also drained the budget is still a 429 — the provider is
        the thing to look at. Same ordering as make_resolver."""
        monkeypatch.setattr(asset_warm, "image_budget_exhausted", lambda: True)
        ra.defer_asset("k2b9", 30)
        res = {"ready": ["q7f3a1", "z11"], "pending": ["k2b9"]}
        assert missing_pictures(self.ENTRIES, res) == {"k2b9": "rate_limited"}

    def test_everything_else_is_a_generation_failure(self):
        res = {"ready": ["q7f3a1"], "pending": []}
        assert missing_pictures(self.ENTRIES, res) == {
            "k2b9": "generation_failed", "z11": "generation_failed"}

    def test_readiness_is_by_picture_not_by_spelling(self):
        a, b = "neurone", "Neurone"
        assert ra.canonical_key(a) == ra.canonical_key(b), "fixture precondition"
        assert missing_pictures([(a, "p")], {"ready": [b], "pending": []}) == {}

    def test_the_check_is_subject_blind(self):
        """Renaming every key changes nothing but the names in the answer."""
        real = [("organelle_city", "p"), ("plant_cell", "p")]
        fake = [("q7f3a1", "p"), ("k2b9", "p")]
        got_real = missing_pictures(real, {"ready": ["plant_cell"]})
        got_fake = missing_pictures(fake, {"ready": ["k2b9"]})
        assert list(got_real.values()) == list(got_fake.values()) == [
            "generation_failed"]


# ── the extra turn ───────────────────────────────────────────────────────────

class TestOneMoreTurn:
    def test_success_and_rate_limits_pass_straight_through(self):
        f = one_more_turn(lambda k, p: (True, None))
        assert f("a", "p") == (True, None)
        f = one_more_turn(lambda k, p: (False, 42.0))
        assert f("a", "p") == (False, 42.0)

    def test_a_failure_that_is_not_a_rate_limit_earns_exactly_one_more(self):
        f = one_more_turn(lambda k, p: (False, None))
        assert f("a", "p") == (False, GATE_RETRY_SOON_SECS)
        assert f("a", "p") == (False, None)
        assert f("a", "p") == (False, None)

    def test_the_second_chance_is_per_picture_not_per_spelling(self):
        f = one_more_turn(lambda k, p: (False, None))
        assert f("neurone", "p") == (False, GATE_RETRY_SOON_SECS)
        assert f("Neurone", "p") == (False, None)

    def test_an_abandoned_key_gets_none(self):
        ra.abandon_asset("dead")
        f = one_more_turn(lambda k, p: (False, None))
        assert f("dead", "p") == (False, None)

    def test_a_spent_budget_gets_none(self, monkeypatch):
        monkeypatch.setattr(asset_warm, "image_budget_exhausted", lambda: True)
        f = one_more_turn(lambda k, p: (False, None))
        assert f("a", "p") == (False, None)

    def test_in_the_pass_the_second_turn_can_land_the_picture(self):
        """The plain pass drops a generation failure after one attempt; under
        the gate that one attempt would have failed the whole video."""
        calls = {"n": 0}

        def flaky(key, prompt):
            calls["n"] += 1
            return (calls["n"] >= 2), None
        clk = _Clock()
        plain = warm_lesson_assets([("a", "p")], fetch=flaky, budget_secs=60,
                                   clock=clk.now, sleep=clk.sleep, workers=1)
        assert plain["ready"] == [] and calls["n"] == 1
        calls["n"] = 0
        gated = warm_lesson_assets([("a", "p")], fetch=one_more_turn(flaky),
                                   budget_secs=60, clock=clk.now,
                                   sleep=clk.sleep, workers=1)
        assert gated["ready"] == ["a"] and calls["n"] == 2


# ── the refusal ──────────────────────────────────────────────────────────────

class TestTheRefusal:
    def test_a_key_still_pending_after_the_long_wait_is_a_rate_limit(self):
        """End to end through the real pass: a provider that never stops
        refusing leaves the key pending; the pass defers it; the gate reads
        that deferral back as the cause."""
        clk = _Clock()
        res = warm_lesson_assets([("a", "p"), ("b", "p")],
                                 fetch=lambda k, p: ((k == "b"), 10.0),
                                 budget_secs=gate_budget_secs("strict"),
                                 clock=clk.now, sleep=clk.sleep, workers=1)
        assert res["ready"] == ["b"] and res["pending"] == ["a"]
        assert clk.t - 1000.0 >= DEFAULT_WARM_BUDGET_SECS + DEFAULT_GATE_WAIT_SECS
        assert missing_pictures([("a", "p"), ("b", "p")], res) == {
            "a": "rate_limited"}

    def test_the_error_names_every_key_and_its_cause(self):
        exc = ImagesIncomplete({"sea_level_diagram": "rate_limited",
                                "spanner_bolt": "generation_failed",
                                "dashboard": "rate_limited"}, total=14)
        msg = str(exc)
        assert msg.startswith("images incomplete: 3/14 planned picture(s)")
        assert "rate_limited=2 (dashboard, sea_level_diagram)" in msg
        assert "generation_failed=1 (spanner_bolt)" in msg
        assert "IMAGE_GATE=warn" in msg, "the rollback is in the error itself"
        assert exc.missing["dashboard"] == "rate_limited" and exc.total == 14
        assert isinstance(exc, RuntimeError), \
            "process.py's job-error path catches RuntimeError"

    def test_a_long_list_is_truncated_not_dropped(self):
        exc = ImagesIncomplete({f"k{i}": "generation_failed" for i in range(9)},
                               total=9)
        assert "generation_failed=9 (" in str(exc) and "…" in str(exc)


class TestTheComposerHonoursIt:
    """The gate lives inside compose_episode_videos, whose warm block is
    wrapped in `except Exception: a warm-up never fails a lesson`. These pin
    the three things that make the refusal real rather than swallowed."""

    def _src(self):
        from agent6_animation import video_composer as vc
        return inspect.getsource(vc.compose_episode_videos)

    def test_the_refusal_is_raised_outside_the_swallowing_except(self):
        src = self._src()
        assert src.index("lesson image warm pass skipped") \
            < src.index("raise ImagesIncomplete(_gate_missing")

    def test_nothing_is_bought_for_a_lesson_about_to_be_refused(self):
        """The vision boxes the anchor repair buys come AFTER the gate check,
        so a strict refusal spends nothing past the pictures themselves."""
        src = self._src()
        assert src.index("raise _GateRefusal()") \
            < src.index("_repair_anchor_regions(slide_segments")

    def test_strict_gets_the_long_budget_and_the_extra_turn(self):
        src = self._src()
        assert "budget_secs=gate_budget_secs(_gate_mode)" in src
        assert 'one_more_turn(_warm_fetch) if _gate_mode == "strict"' in src

    def test_a_warm_pass_fault_cannot_certify_a_strict_lesson(self):
        """A lesson whose pictures were never checked is exactly the lesson
        the gate exists to refuse. warn keeps the old behaviour."""
        src = self._src()
        assert "_gate_fault = exc" in src
        assert "if _gate_fault is not None:" in src

    def test_warn_only_warns(self):
        src = self._src()
        assert 'if _gate_missing and _gate_mode == "strict":' in src
        assert "IMAGE_GATE=warn: strict would have refused" in src
