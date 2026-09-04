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
        assert state["attempts"] == 1, "the ATTEMPT is never refunded"

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
        assert ceiling == ra._IMAGE_BUDGET * 3
        for i in range(ceiling):
            assert ra._image_budget_ok() is True, i
            ra._refund_image_call()          # every call 429s
        assert ra.image_budget_state()["n"] == 0, "the budget looks untouched…"
        assert ra._image_budget_ok() is False, "…but the ceiling has stopped it"

    def test_it_resets_with_the_lesson(self):
        ra.reset_image_budget()
        for _ in range(ra.image_attempt_ceiling()):
            ra._image_budget_ok()
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
