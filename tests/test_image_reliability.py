"""Image generation under a rate limit: what we can SEE, and what we STOP.

The 2026-09-04 incident (lesson fa8c0d7d, Cells Part 3) shipped two blank
boards and two WRONG pictures. The measurements behind these tests:

* Vertex returned 429 continuously for three minutes at under 10 requests per
  minute with at most 3 in flight — and every one of those 429s was logged as
  its status line alone, so nobody could tell a raisable quota from endpoint
  capacity. The body is the only thing that distinguishes them.
* The AI Studio image fallback has produced zero images EVER: that key's
  project is on the free tier and gemini-2.5-flash-image has a free-tier limit
  of 0. Each fallback cost 4 attempts, ~58 s of a render thread, and one unit
  of the per-lesson image budget.
* The budget was charged on ENTRY, so a burst of failures spent the allowance
  the successes needed.

No test here may make a live call: every provider is a fake, and the
credential variables the repo .env carries are removed first.
"""

from __future__ import annotations

import base64
import inspect
import logging
import threading
import time

import pytest
import requests

from spike.scene_engine import raster_assets as ra


_QUOTA_BODY = (
    '{"error":{"code":429,"message":"Quota exceeded for quota metric '
    "'Generate requests' and limit 'GenerateRequestsPerDayPerProjectPerModel-"
    "FreeTier' of service 'generativelanguage.googleapis.com'\","
    '"status":"RESOURCE_EXHAUSTED","details":[{"quotaId":'
    '"GenerateRequestsPerDayPerProjectPerModel-FreeTier","quotaValue":"0"}]}}'
)

_PNG = b"\x89PNG\r\n\x1a\n" + b"pretend-image-bytes"


@pytest.fixture(autouse=True)
def _no_live_calls(monkeypatch):
    """The repo .env carries real credentials; nothing here may use them."""
    for var in ("VERTEX_PROJECT_ID", "GOOGLE_AI_API_KEY", "GEMINI_API_KEY",
                "GOOGLE_APPLICATION_CREDENTIALS",
                "GOOGLE_APPLICATION_CREDENTIALS_JSON",
                "AISTUDIO_IMAGE_FALLBACK"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    # The per-minute limiters are process-wide and capture time.sleep at
    # construction, so a shared one would make this file's fake 429 ladders
    # sleep for real. Fresh, generous ones per test.
    from shared.ratelimit import RateLimiter
    monkeypatch.setattr(ra, "_LIMITERS", {"image": RateLimiter(100_000),
                                          "vision": RateLimiter(100_000)})
    # spend accounting appends to the repo's token log; not this file's job
    monkeypatch.setattr(ra, "_note_spend", lambda *a, **k: None)
    ra.reset_image_budget()
    yield
    ra.reset_image_budget()


def _response(status: int, body: str = "", headers: dict | None = None):
    resp = requests.Response()
    resp.status_code = status
    resp._content = body.encode("utf-8")
    resp.encoding = "utf-8"
    for k, v in (headers or {}).items():
        resp.headers[k] = v
    return resp


def _http_error(status: int = 429, body: str = _QUOTA_BODY,
                headers: dict | None = None) -> requests.HTTPError:
    return requests.HTTPError(f"{status} Client Error", response=_response(
        status, body, headers))


class TestTheRateLimitBodyIsLogged:
    """"429" is the one fact that was never in doubt. A Vertex QUOTA 429 names
    quotaMetric/quotaId (there is a number to raise); a CAPACITY 429 says only
    "Resource exhausted, please try again later" (there is not, and the answer
    is deferral). Nothing in the logs could tell them apart."""

    def test_every_attempt_and_the_final_raise_log_the_body(self, caplog):
        caplog.set_level(logging.WARNING, logger=ra.logger.name)
        with pytest.raises(requests.HTTPError):
            ra._with_backoff(_raise_429, "Vertex image", tries=3)
        bodies = [r.getMessage() for r in caplog.records
                  if "body=" in r.getMessage()]
        assert len(bodies) == 3, "one per attempt, the final raise included"
        assert all("GenerateRequestsPerDayPerProjectPerModel-FreeTier" in b
                   for b in bodies)
        assert all("attempt " in b for b in bodies)

    def test_the_server_s_retry_after_is_logged_next_to_the_body(self, caplog):
        caplog.set_level(logging.WARNING, logger=ra.logger.name)
        err = _http_error(headers={"Retry-After": "52"})

        def fn():
            raise err
        with pytest.raises(requests.HTTPError):
            ra._with_backoff(fn, "Vertex image", tries=1)
        line = next(r.getMessage() for r in caplog.records if "body=" in r.getMessage())
        assert "retry_after=52.0" in line

    def test_only_the_response_body_is_logged_never_headers(self):
        """A request header carries the bearer token; a request body carries
        the prompt. Neither may ever reach a log line."""
        src = inspect.getsource(ra._error_body)
        assert ".text" in src
        assert ".headers" not in src and ".request" not in src
        assert ra._ERROR_BODY_CHARS == 500
        err = _http_error(body="x" * 4000)
        assert len(ra._error_body(err)) == 500

    def test_a_body_less_error_does_not_break_the_log(self):
        assert ra._error_body(RuntimeError("no response at all")) == ""
        assert ra._status_of(RuntimeError("no response at all")) == "?"

    def test_the_transports_log_the_body_too(self):
        """requests' HTTPError str() is the status line only."""
        for fn in (ra._vertex_call, ra._aistudio_call):
            assert "_error_body(e)" in inspect.getsource(fn), fn.__name__


def _raise_429():
    raise _http_error()


class TestTheAIStudioImageFallbackIsOffByDefault:
    """Its project has a free-tier limit of 0 for the image model, so the
    fallback is a guaranteed 4-attempt, ~58-second, one-budget-unit zero."""

    def test_it_makes_no_call_at_all_when_unset(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_AI_API_KEY", "not-a-real-key")
        posts = []
        monkeypatch.setattr(ra.requests, "post",
                            lambda *a, **k: posts.append(a) or _response(200))
        assert ra.aistudio_image_fallback_enabled() is False
        assert ra._aistudio_call("a diagram of a cell") is None
        assert posts == [], "the dead fallback must not be dialled at all"

    def test_and_spends_none_of_the_lesson_budget(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_AI_API_KEY", "not-a-real-key")
        ra.reset_image_budget()
        for _ in range(5):
            ra._aistudio_call("a diagram of a cell")
        state = ra.image_budget_state()
        assert state["n"] == 0 and state["attempts"] == 0

    @pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
    def test_the_flag_turns_it_back_on(self, monkeypatch, value):
        monkeypatch.setenv("AISTUDIO_IMAGE_FALLBACK", value)
        monkeypatch.setenv("GOOGLE_AI_API_KEY", "not-a-real-key")
        posts = []

        def fake_post(url, **kwargs):
            posts.append(url)
            return _response(200, '{"candidates":[{"content":{"parts":[{'
                                  '"inlineData":{"mimeType":"image/png","data":"'
                             + base64.b64encode(_PNG).decode() + '"}}]}}]}')
        monkeypatch.setattr(ra.requests, "post", fake_post)
        assert ra.aistudio_image_fallback_enabled() is True
        assert ra._aistudio_call("a diagram of a cell") == _PNG
        assert len(posts) == 1 and "generativelanguage" in posts[0]

    def test_the_vision_path_is_not_touched_by_the_switch(self):
        """flash VISION still has real free-tier quota; only IMAGE is dead."""
        src = inspect.getsource(ra._vision_json)
        assert "AISTUDIO_IMAGE_FALLBACK" not in src
        assert "aistudio_image_fallback_enabled" not in src


class TestFailuresDoNotSpendTheLessonAllowance:
    def test_a_rate_limited_call_gives_its_unit_back(self, monkeypatch):
        monkeypatch.setenv("AISTUDIO_IMAGE_FALLBACK", "1")
        monkeypatch.setenv("GOOGLE_AI_API_KEY", "not-a-real-key")
        monkeypatch.setattr(ra.requests, "post",
                            lambda *a, **k: _response(429, _QUOTA_BODY))
        ra.reset_image_budget()
        assert ra._aistudio_call("a diagram of a cell") is None
        state = ra.image_budget_state()
        assert state["n"] == 0, "a 429 produced no image; it spent no credit"
        assert state["refunded"] == 1
        # four HTTP requests really went out (the four-try ladder), and the
        # ceiling counts REQUESTS, not transport entries
        assert state["attempts"] == 4, "the ATTEMPT is never refunded"

    def test_a_successful_call_keeps_its_unit(self, monkeypatch):
        monkeypatch.setenv("AISTUDIO_IMAGE_FALLBACK", "1")
        monkeypatch.setenv("GOOGLE_AI_API_KEY", "not-a-real-key")
        monkeypatch.setattr(
            ra.requests, "post",
            lambda *a, **k: _response(200, '{"candidates":[{"content":{"parts"'
                                           ':[{"inlineData":{"mimeType":'
                                           '"image/png","data":"'
                                      + base64.b64encode(_PNG).decode() + '"}}]}}]}'))
        ra.reset_image_budget()
        assert ra._aistudio_call("a diagram of a cell") == _PNG
        assert ra.image_budget_state()["n"] == 1

    def test_both_transports_refund(self):
        for fn in (ra._vertex_call, ra._aistudio_call):
            src = inspect.getsource(fn)
            assert "_refund_image_call()" in src, fn.__name__
            assert "_is_rate_limited(e)" in src, fn.__name__

    def test_the_charge_on_entry_survived_the_refund(self):
        """The refund must not be implemented by moving the charge: two
        existing tests pin _image_budget_ok() at the top of each transport."""
        for fn in (ra._vertex_call, ra._aistudio_call):
            assert "_image_budget_ok" in inspect.getsource(fn), fn.__name__


class TestTheAttemptCeiling:
    """Refunding removes the cap that failures used to provide, so a dead
    provider could be retried for the whole lesson. Attempts are counted
    separately and never refunded."""

    def test_a_dead_provider_cannot_loop_forever(self):
        ra.reset_image_budget()
        ceiling = ra.image_attempt_ceiling()
        assert ceiling == ra._IMAGE_BUDGET * 2
        for i in range(ceiling):
            assert ra._image_budget_ok() is True, i
            ra._note_image_attempt()         # one request went out…
            ra._refund_image_call()          # …and 429'd
        assert ra.image_budget_state()["n"] == 0, "the budget looks untouched…"
        assert ra._image_budget_ok() is False, "…but the ceiling has stopped it"

    def test_it_resets_with_the_lesson(self):
        ra.reset_image_budget()
        for _ in range(ra.image_attempt_ceiling()):
            ra._image_budget_ok()
            ra._note_image_attempt()
            ra._refund_image_call()
        assert ra._image_budget_ok() is False
        ra.reset_image_budget()
        assert ra._image_budget_ok() is True

    def test_the_ordinary_budget_still_refuses_past_the_cap(self):
        """The unchanged behaviour: successes, not failures, fill the budget."""
        ra.reset_image_budget()
        allowed = sum(1 for _ in range(ra._IMAGE_BUDGET + 5)
                      if ra._image_budget_ok())
        assert allowed == ra._IMAGE_BUDGET
        assert ra.image_budget_state()["blocked"] == 5


class TestOnePerKeyLockForTheWholeDecision:
    """Two render threads both read "not cached" for ciliated_cell and both
    logged generated+published for one image (17:55:09 in fa8c0d7d), because
    the lock lived inside the generator while the decision lived outside it."""

    def test_the_lock_is_keyed_by_cache_identity_not_spelling(self):
        assert ra.asset_lock("plant_cell") is ra.asset_lock("Plant Cells Diagram")
        assert ra.asset_lock("plant_cell") is not ra.asset_lock("animal_cell")

    def test_it_is_reentrant_so_the_wrapper_may_hold_it(self):
        lock = ra.asset_lock("ciliated_cell")
        with lock:
            assert lock.acquire(blocking=False) is True
            lock.release()

    def test_it_really_serialises_two_threads(self):
        overlap = []
        inside = []

        def worker():
            with ra.asset_lock("red_blood_cell"):
                inside.append(1)
                overlap.append(len(inside))
                inside.pop()
        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert max(overlap) == 1

    def test_the_library_wrapper_holds_it_across_its_whole_decision(self):
        import shared.visual_library_integration as vli
        src = inspect.getsource(vli._patch)
        assert "with ra.asset_lock(key):" in src
        # the decision — existed_before, hydrate, generate, publish, log — must
        # be inside it, not merely the generator call
        assert "existed_before" in src


class TestTheCeilingCountsRequestsNotEntries:
    """One transport ENTRY runs a four-try ladder inside _with_backoff. A
    ceiling counted per entry therefore permitted 3 x 24 x 4 = 288 requests
    into the burst it exists to stop, while the log line said "72 attempts"."""

    def test_every_http_try_is_counted(self, monkeypatch):
        monkeypatch.setenv("AISTUDIO_IMAGE_FALLBACK", "1")
        monkeypatch.setenv("GOOGLE_AI_API_KEY", "not-a-real-key")
        posts = []
        monkeypatch.setattr(
            ra.requests, "post",
            lambda *a, **k: posts.append(1) or _response(429, _QUOTA_BODY))
        ra.reset_image_budget()
        assert ra._aistudio_call("a diagram of a cell") is None
        assert len(posts) == 4, "the ladder really fired four requests"
        assert ra.image_budget_state()["attempts"] == len(posts)

    def test_the_ceiling_is_a_request_count_an_operator_can_read(self):
        assert ra.image_attempt_ceiling() == ra._IMAGE_BUDGET * 2

    def test_vision_requests_do_not_spend_the_image_ceiling(self, monkeypatch):
        """flash vision was never throttled in any measured lesson; it must
        not consume the image provider's hard stop."""
        ra.reset_image_budget()
        calls = {"n": 0}

        def boom():
            calls["n"] += 1
            raise RuntimeError("no vision here")
        try:
            ra._with_backoff(boom, "vision", tries=2, kind="vision")
        except RuntimeError:
            pass
        assert calls["n"] == 1
        assert ra.image_budget_state()["attempts"] == 0


class TestJitterBreaksTheLockstep:
    """MODEL_CALL_CONCURRENCY=3 gate slots that 429 together used to retry on
    an identical cadence and re-collide every round."""

    def _waits(self, monkeypatch, headers=None, samples=200):
        waits = []

        def sleeper(s):
            waits.append(float(s))
        monkeypatch.setattr(time, "sleep", sleeper)

        def flaky():
            r = requests.Response()
            r.status_code = 429
            for k, v in (headers or {}).items():
                r.headers[k] = v
            raise requests.HTTPError(response=r)
        for _ in range(samples):
            try:
                ra._with_backoff(flaky, "test", tries=2)
            except requests.HTTPError:
                pass
        return waits

    def test_our_own_ladder_spreads_across_half_the_wait(self, monkeypatch):
        waits = self._waits(monkeypatch)
        assert min(waits) >= 6.0, "never shorter than the ladder's own step"
        assert max(waits) <= 9.0, "…and never more than base + half of it"
        assert max(waits) > 7.5, ("a 25% cap could not exceed 7.5s: three "
                                  "slots would re-collide inside 1.5s")

    def test_a_server_named_retry_after_is_still_honoured(self, monkeypatch):
        waits = self._waits(monkeypatch, headers={"Retry-After": "7"},
                            samples=50)
        assert min(waits) >= 7.0 and max(waits) <= 9.0


class TestLessonsDoNotShareState:
    """WORKER_CONCURRENCY>1 runs several lessons in ONE process (worker/run.py).
    Measured before this fix, with no network at all: lesson A deferring
    ciliated_cell and abandoning red_blood_cell made lesson B — a different
    book — skip both pictures with zero attempts; and lesson B's reset wiped
    lesson A's protection mid-flight, so A's render threads resumed hammering
    the 429'd key and the never-refunded ceiling stopped binding."""

    def _in_lesson(self, gen_id, fn):
        """Run fn on its own thread, as a second job thread would."""
        out = {}

        def body():
            ra.reset_image_budget(gen_id)
            out["v"] = fn()
        t = threading.Thread(target=body)
        t.start()
        t.join()
        return out.get("v")

    def test_one_lessons_deferral_does_not_blank_anothers_board(self):
        ra.reset_image_budget("lesson-a")
        ra.defer_asset("ciliated_cell", 45)
        ra.abandon_asset("red_blood_cell")

        def lesson_b():
            return (ra.asset_deferred("ciliated cells diagram"),
                    ra.asset_abandoned("red_blood_cell"))
        deferred, abandoned = self._in_lesson("lesson-b", lesson_b)
        assert deferred is None, "lesson B never saw a 429; it must try"
        assert abandoned is False

    def test_another_lessons_reset_does_not_wipe_this_ones_protection(self):
        ra.reset_image_budget("lesson-a")
        ra.defer_asset("ciliated_cell", 45)
        self._in_lesson("lesson-b", lambda: None)   # B starts, and resets
        assert ra.asset_deferred("ciliated_cell") is not None, \
            "lesson A's render threads would resume hammering the 429'd key"

    def test_the_attempt_ceiling_cannot_be_reset_away_by_a_neighbour(self):
        ra.reset_image_budget("lesson-a")
        for _ in range(ra.image_attempt_ceiling()):
            ra._image_budget_ok()
            ra._note_image_attempt()
            ra._refund_image_call()
        assert ra._image_budget_ok() is False
        self._in_lesson("lesson-b", lambda: ra._image_budget_ok())
        assert ra._image_budget_ok() is False, \
            "a hard stop a concurrent lesson can lift is not a hard stop"

    def test_the_budget_is_charged_to_the_lesson_that_spent_it(self):
        ra.reset_image_budget("lesson-a")
        for _ in range(3):
            ra._image_budget_ok()
        b_state = self._in_lesson("lesson-b", ra.image_budget_state)
        assert b_state["n"] == 0
        assert ra.image_budget_state()["n"] == 3

    def test_a_render_thread_is_bound_to_its_own_lesson(self):
        """The pools do not inherit a context; bind_generation carries it."""
        ra.reset_image_budget("lesson-a")
        ra.defer_asset("ciliated_cell", 45)
        seen = {}

        def on_a_render_thread():
            seen["deferred"] = ra.asset_deferred("ciliated_cell")
        t = threading.Thread(target=ra.bind_generation(on_a_render_thread))
        t.start()
        t.join()
        assert seen["deferred"] is not None

        seen.clear()
        t = threading.Thread(target=on_a_render_thread)   # unbound
        t.start()
        t.join()
        assert seen["deferred"] is None, "the leak this proves is fixed"

    def test_the_composer_binds_its_render_threads(self):
        import agent6_animation.video_composer as vc
        src = inspect.getsource(vc.compose_episode_videos)
        assert "bind_generation" in src

    def test_the_worker_names_the_generation(self):
        from worker.process import process_generation
        assert "reset_image_budget(generation_id)" in \
            inspect.getsource(process_generation)

    def test_a_finished_lessons_state_is_eventually_dropped(self, monkeypatch):
        ra.reset_image_budget("old-lesson")
        ra.defer_asset("k", 45)
        monkeypatch.setattr(ra, "_STATE_TTL_SECS", -1.0)
        ra.reset_image_budget("new-lesson")
        assert "old-lesson" not in ra._STATE
