"""Defer a rate-limited picture; do not hammer it from every render thread.

Lesson fa8c0d7d asked for 13 distinct pictures and made 154 resolver requests
for them across 8 render threads. When a key 429'd, each thread ran its own
four-attempt, ~two-minute ladder and then dropped the key for good — while
ciliated_cell generated successfully at 17:55:09, fourteen seconds AFTER
segment s017 had given up. Pacing harder cannot help: the incident 429s came
at under 10 requests per minute with at most 3 in flight.

So: one queue for the lesson, in first-use order, under the same concurrency
bound the transports already use; a refused key goes to the back with the
server's own retry time; a wall-clock budget ends the pass; what is still
pending is DEFERRED once, loudly, instead of rediscovered thirty times —
deferred and not abandoned, because fa8c0d7d's ciliated cell generated three
minutes into the burst, at or after this pass's own deadline.

Every provider here is a fake. Nothing in this file may make a network call.
"""

from __future__ import annotations

import pytest

from spike.scene_engine import raster_assets as ra
from spike.scene_engine.asset_warm import (collect_lesson_assets,
                                           order_segments_by_pending,
                                           segment_asset_keys,
                                           warm_budget_secs,
                                           warm_lesson_assets)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in ("VERTEX_PROJECT_ID", "GOOGLE_AI_API_KEY", "GEMINI_API_KEY",
                "GOOGLE_APPLICATION_CREDENTIALS",
                "GOOGLE_APPLICATION_CREDENTIALS_JSON",
                "AISTUDIO_IMAGE_FALLBACK", "IMAGE_WARM_BUDGET_SECS",
                "IMAGE_DEFER_SECONDS"):
        monkeypatch.delenv(var, raising=False)
    ra.reset_image_budget()
    yield
    ra.reset_image_budget()


class _Clock:
    """A clock the test drives; sleeping moves it."""

    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(round(float(s), 3))
        self.t += float(s)


# ── the negative cache ───────────────────────────────────────────────────────

class TestDeferralReplacesTheLadder:
    def test_a_deferred_key_is_not_attempted_again(self, monkeypatch, tmp_path):
        ra.reset_deferrals()
        ra.defer_asset("ciliated_cell", 30.0)
        assert 29.0 < ra.asset_deferred("ciliated_cell") <= 30.0
        calls = []
        monkeypatch.setattr(ra, "_vertex_call",
                            lambda *a, **k: calls.append("vertex") or None)
        monkeypatch.setattr(ra, "_aistudio_call",
                            lambda *a, **k: calls.append("aistudio") or None)
        assert ra._get_raster_asset("ciliated_cell", "a ciliated cell",
                                    tmp_path) is None
        assert calls == [], "a deferred key must cost no provider call at all"

    def test_the_first_caller_pays_and_the_rest_return_at_once(
            self, monkeypatch, tmp_path):
        """Eight render threads, one dead key, one ladder — not eight."""
        ra.reset_deferrals()
        import requests
        resp = requests.Response()
        resp.status_code = 429
        resp._content = b'{"error":{"status":"RESOURCE_EXHAUSTED"}}'
        err = requests.HTTPError(response=resp)
        calls = []

        def vertex(_prompt):
            calls.append("vertex")
            ra._note_rate_limited(err)
            return None
        monkeypatch.setattr(ra, "_vertex_call", vertex)
        monkeypatch.setattr(ra, "_aistudio_call", lambda *a, **k: None)

        for _ in range(8):
            assert ra._get_raster_asset("ciliated_cell", "a ciliated cell",
                                        tmp_path) is None
        assert calls == ["vertex"], f"{len(calls)} ladders, expected 1"
        assert ra.asset_deferred("ciliated_cell") is not None

    def test_the_deferral_expires_so_a_later_lesson_beat_may_retry(self, monkeypatch):
        ra.reset_deferrals()
        t = _Clock()
        monkeypatch.setattr(ra, "_now", t.now)
        ra.defer_asset("sk_bacteria", 20.0)
        assert ra.asset_deferred("sk_bacteria") == 20.0
        t.t += 21.0
        assert ra.asset_deferred("sk_bacteria") is None

    def test_the_server_s_retry_after_sets_the_wait(self):
        ra.reset_deferrals()
        assert ra.defer_asset("k1", 52.0) == 52.0
        ra.reset_deferrals()
        assert ra.defer_asset("k2", 3600.0) == 120.0, "capped: a lesson cannot wait an hour"
        ra.reset_deferrals()
        assert ra.defer_asset("k3", None) == 45.0, "the default when no header"

    def test_deferral_is_by_picture_not_by_spelling(self):
        ra.reset_deferrals()
        ra.defer_asset("Ciliated Cells Diagram", 30.0)
        assert ra.asset_deferred("ciliated_cell") is not None

    def test_an_abandoned_key_is_skipped_forever_this_lesson(
            self, monkeypatch, tmp_path):
        ra.reset_deferrals()
        ra.abandon_asset("sk_boat")
        calls = []
        monkeypatch.setattr(ra, "_vertex_call",
                            lambda *a, **k: calls.append(1) or None)
        monkeypatch.setattr(ra, "_aistudio_call", lambda *a, **k: None)
        assert ra._get_raster_asset("sk_boat", "a boat", tmp_path) is None
        assert calls == []

    def test_the_lesson_reset_clears_both(self):
        ra.defer_asset("k", 30.0)
        ra.abandon_asset("j")
        ra.reset_image_budget()
        assert ra.deferral_state() == {"deferred": {}, "abandoned": set()}

    def test_the_resolver_calls_a_rate_limit_a_rate_limit(self, monkeypatch, tmp_path):
        """Not 'generation_failed', and certainly not 'no prompt'."""
        ra.reset_deferrals()
        ra.defer_asset("ciliated_cell", 30.0)
        monkeypatch.setattr(ra, "_vertex_call", lambda *a, **k: None)
        monkeypatch.setattr(ra, "_aistudio_call", lambda *a, **k: None)
        resolve = ra.make_resolver({"ciliated_cell": "a ciliated cell"},
                                   cache_dir=tmp_path)
        assert resolve("ciliated_cell") is None
        assert resolve.last_reason["ciliated_cell"] == "rate_limited"


# ── the lesson queue ─────────────────────────────────────────────────────────

class TestTheWarmPass:
    def test_each_distinct_key_is_fetched_once_in_first_use_order(self):
        asked = []

        def fetch(key, _prompt):
            asked.append(key)
            return True, None
        entries = [("neurone", "p1"), ("red_blood_cell", "p2"),
                   ("ciliated_cell", "p3")]
        res = warm_lesson_assets(entries, fetch=fetch, workers=1)
        assert asked == ["neurone", "red_blood_cell", "ciliated_cell"]
        assert res["ready"] == asked and res["pending"] == []

    def test_the_default_concurrency_is_the_transports_own_gate(self, monkeypatch):
        """The warm pass must not widen the burst that caused the incident."""
        monkeypatch.setenv("MODEL_CALL_CONCURRENCY", "2")
        peak = {"n": 0, "cur": 0}
        import threading
        lock = threading.Lock()

        def fetch(_key, _prompt):
            with lock:
                peak["cur"] += 1
                peak["n"] = max(peak["n"], peak["cur"])
            import time as _t
            _t.sleep(0.01)
            with lock:
                peak["cur"] -= 1
            return True, None
        warm_lesson_assets([(f"k{i}", "p") for i in range(8)], fetch=fetch)
        assert peak["n"] <= ra.model_call_concurrency() == 2

    def test_a_rate_limited_key_goes_to_the_back_with_its_retry_time(self):
        t = _Clock()
        asked = []
        state = {"b": 0}

        def fetch(key, _prompt):
            asked.append(key)
            if key == "b":
                state["b"] += 1
                if state["b"] == 1:
                    return False, 12.0        # the server said 12 seconds
            return True, None
        res = warm_lesson_assets([("a", "p"), ("b", "p"), ("c", "p")],
                                 fetch=fetch, workers=1, clock=t.now,
                                 sleep=t.sleep, budget_secs=600)
        assert asked == ["a", "b", "c", "b"], "b is retried LAST, not immediately"
        # the full twelve seconds the server named, but taken in slices: ONE
        # three-minute sleep at the head of compose is a frozen progress ring
        assert sum(t.slept) == 12.0, "the time the server named is served out"
        assert max(t.slept) <= 5.0, "…in naps short enough to report progress"
        assert sorted(res["ready"]) == ["a", "b", "c"]
        assert res["pending"] == []

    def test_a_failure_that_is_not_a_rate_limit_is_not_requeued(self):
        asked = []

        def fetch(key, _prompt):
            asked.append(key)
            return False, None
        res = warm_lesson_assets([("a", "p")], fetch=fetch, workers=1)
        assert asked == ["a"], "one attempt; another would fail the same way"
        assert res["pending"] == [], "not pending — it was answered, badly"
        assert res["ready"] == []

    def test_a_bounded_budget_ends_the_pass(self):
        t = _Clock()
        asked = []

        def fetch(key, _prompt):
            asked.append(key)
            t.t += 10.0                      # each attempt costs ten seconds
            return False, 30.0               # …and is refused
        ra.reset_deferrals()
        res = warm_lesson_assets([("a", "p"), ("b", "p")], fetch=fetch,
                                 workers=1, clock=t.now, sleep=t.sleep,
                                 budget_secs=25.0)
        assert res["seconds"] >= 25.0
        assert res["pending"], "the budget ended the pass with work left"
        assert len(asked) < 20, "it did not spin"

    def _timed_out_pass(self, budget=50.0):
        t = _Clock()

        def fetch(_key, _prompt):
            t.t += 100.0
            return False, 30.0
        ra.reset_deferrals()
        res = warm_lesson_assets([("ciliated_cell", "p")], fetch=fetch,
                                 workers=1, clock=t.now, sleep=t.sleep,
                                 budget_secs=budget)
        assert res["pending"] == ["ciliated_cell"]
        return res

    def test_what_is_still_pending_is_deferred_not_rediscovered(self):
        """It must not be re-attempted by all eight render threads at once."""
        self._timed_out_pass()
        assert ra.asset_deferred("ciliated_cell") is not None

    def test_a_key_the_budget_ran_out_on_is_never_abandoned(self):
        """The pass's own deadline is three minutes. fa8c0d7d's ciliated cell
        generated at 17:55:09 — about three minutes into the burst, i.e. AT or
        AFTER that deadline. Abandoning at the deadline would guarantee, by
        design, the blank board the incident got by accident."""
        self._timed_out_pass()
        assert ra.asset_abandoned("ciliated_cell") is False, \
            "a rate limit expires; abandon is for a failure that will not"

    def test_exactly_one_later_segment_picks_the_key_back_up(self, monkeypatch):
        """The deferral is the retry: it expires, the next resolver call
        through it tries once, and the rest keep skipping."""
        self._timed_out_pass()
        left = ra.asset_deferred("ciliated_cell")
        assert 0 < left <= 120.0, "capped: a lesson cannot wait an hour"
        monkeypatch.setattr(ra, "_now", lambda: ra.time.monotonic() + left + 1)
        assert ra.asset_deferred("ciliated_cell") is None, \
            "a later segment may try again — which is what would have worked"

    def test_the_retry_delay_comes_from_the_environment_defensively(
            self, monkeypatch):
        from spike.scene_engine.asset_warm import warm_abandon_retry_secs
        assert warm_abandon_retry_secs() == 120.0
        monkeypatch.setenv("IMAGE_WARM_RETRY_SECS", "45")
        assert warm_abandon_retry_secs() == 45.0
        monkeypatch.setenv("IMAGE_WARM_RETRY_SECS", "banana")
        assert warm_abandon_retry_secs() == 120.0

    def test_the_pass_reports_progress_every_round_and_every_nap(self):
        """Up to three minutes of serial wall clock at the head of compose,
        with no progress, is a frozen ring during exactly the incident this
        was built for."""
        t = _Clock()
        seen = []
        state = {"b": 0}

        def fetch(key, _prompt):
            if key == "b":
                state["b"] += 1
                if state["b"] == 1:
                    return False, 12.0
            return True, None
        warm_lesson_assets([("a", "p"), ("b", "p")], fetch=fetch, workers=1,
                           clock=t.now, sleep=t.sleep, budget_secs=600,
                           on_progress=lambda d, n: seen.append((d, n)))
        assert len(t.slept) >= 2, "the wait really was taken in slices"
        assert len(seen) >= len(t.slept) + 1, \
            "every nap moves the ring, not just every round"
        assert seen[-1] == (2, 2)
        assert all(d <= n for d, n in seen), "a ring never exceeds its total"

    def test_a_progress_callback_that_raises_never_fails_the_lesson(self):
        def boom(_d, _n):
            raise RuntimeError("the UI is not the lesson")
        res = warm_lesson_assets([("a", "p")], fetch=lambda *_: (True, None),
                                 workers=1, on_progress=boom)
        assert res["ready"] == ["a"]

    def test_the_composer_labels_the_warm_pass_as_its_own_stage(self):
        import inspect
        from agent6_animation import video_composer as vc
        src = inspect.getsource(vc.compose_episode_videos)
        assert 'progress_callback(done, total, "images")' in src, \
            "a distinct stage label, so the ring is not mistaken for segments"
        assert "on_progress=_warm_progress" in src

    def test_a_fetch_that_raises_never_stops_the_pass(self):
        def fetch(key, _prompt):
            if key == "a":
                raise RuntimeError("provider exploded")
            return True, None
        res = warm_lesson_assets([("a", "p"), ("b", "p")], fetch=fetch, workers=1)
        assert res["ready"] == ["b"]

    def test_the_budget_comes_from_the_environment_defensively(self, monkeypatch):
        assert warm_budget_secs() == 180.0
        monkeypatch.setenv("IMAGE_WARM_BUDGET_SECS", "60")
        assert warm_budget_secs() == 60.0
        monkeypatch.setenv("IMAGE_WARM_BUDGET_SECS", "banana")
        assert warm_budget_secs() == 180.0


# ── what a lesson asks for ───────────────────────────────────────────────────

def _lesson():
    slide_segments = [{"segment_id": "s001"}, {"segment_id": "s002"},
                      {"segment_id": "s003"}]
    script_segments = {
        "s001": {"scene_assets": {"neurone": "a neurone"},
                 "scene": {"elements": [
                     {"id": "e1", "type": "illustration", "asset": "neurone"},
                     {"id": "t1", "type": "text", "text": "hello"}]}},
        "s002": {"scene": {"scene_assets": {"sk_boat": "a boat"},
                           "elements": [
                               {"id": "e2", "type": "illustration",
                                "asset": "sk_boat"},
                               {"id": "e3", "type": "illustration",
                                "asset": "avatar_teacher"}]}},
        # the same picture, spelled differently
        "s003": {"scene_assets": {"neurone_diagram": "a neurone"},
                 "scene": {"elements": [
                     {"id": "e4", "type": "illustration",
                      "asset": "neurone_diagram"},
                     {"id": "e5", "type": "illustration",
                      "asset": "no_prompt_for_this"}]}},
    }
    return slide_segments, script_segments


class TestWhatTheLessonAsksFor:
    def test_only_referenced_keys_with_prompts_are_collected(self):
        slide, script = _lesson()
        per_seg = segment_asset_keys(slide, script, {"avatar_teacher": "a teacher"})
        assert [k for k, _ in per_seg[0]] == ["neurone"]
        assert [k for k, _ in per_seg[1]] == ["sk_boat", "avatar_teacher"]
        assert [k for k, _ in per_seg[2]] == ["neurone_diagram"], \
            "an illustration with no prompt anywhere cannot be warmed"

    def test_the_avatar_roster_does_not_warm_thirteen_unused_faces(self):
        slide, script = _lesson()
        roster = {f"avatar_{i}": "x" for i in range(14)}
        roster["avatar_teacher"] = "a teacher"
        per_seg = segment_asset_keys(slide, script, roster)
        assert [k for k, _ in per_seg[1]] == ["sk_boat", "avatar_teacher"]

    def test_one_picture_spelled_twice_is_fetched_once(self):
        slide, script = _lesson()
        per_seg = segment_asset_keys(slide, script, {"avatar_teacher": "t"})
        entries = collect_lesson_assets(per_seg)
        assert [k for k, _ in entries] == ["neurone", "sk_boat", "avatar_teacher"]

    def test_segments_whose_pictures_are_ready_render_first(self):
        slide, script = _lesson()
        per_seg = segment_asset_keys(slide, script, {"avatar_teacher": "t"})
        order = order_segments_by_pending(per_seg, ["neurone"])
        assert order[0] == 1, "s002 needs nothing pending"
        assert set(order) == {0, 1, 2}, "no segment may be lost by reordering"

    def test_nothing_pending_leaves_the_natural_order(self):
        slide, script = _lesson()
        per_seg = segment_asset_keys(slide, script, {"avatar_teacher": "t"})
        assert order_segments_by_pending(per_seg, []) == [0, 1, 2]

    def test_the_composer_reorders_and_never_drops_a_segment(self):
        import inspect
        from agent6_animation import video_composer as vc
        src = inspect.getsource(vc.compose_episode_videos)
        assert "order_segments_by_pending" in src
        assert "warm_lesson_assets" in src
        assert 'sorted(_order) != list(range(total))' in src
