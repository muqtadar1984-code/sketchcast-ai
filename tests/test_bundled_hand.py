"""The drawing hand ships with the code; paid model calls are paced per minute.

Founder, 2026-09-04: "can we store the hand sprite as part of the repository
so we don't have to call it from AI every time? we need to ensure we don't hit
rate limits while generating videos."
"""
from __future__ import annotations

import json
import time

import numpy as np
from PIL import Image

from shared.ratelimit import RateLimiter
from spike.scene_engine import raster_assets as ra


BUNDLED = ra.BUNDLED_DIR / "hand_pen"


class TestBundledHand:
    def test_the_sprite_is_committed_with_a_tip(self):
        assert (BUNDLED / "asset.png").exists(), "hand sprite missing from the repo"
        meta = json.loads((BUNDLED / "meta.json").read_text(encoding="utf-8"))
        img = Image.open(BUNDLED / "asset.png").convert("RGBA")
        tx, ty = meta["tip"]
        assert 0 <= tx < img.width and 0 <= ty < img.height
        a = np.asarray(img.getchannel("A"))
        assert a[ty, tx] > 128, "tip must sit on an opaque pixel"
        # the nib points to the lower left: tip in the left third, lower half
        assert tx < img.width / 3 and ty > img.height / 2
        # a transparent cut-out, not a white rectangle
        assert a.min() == 0 and (a == 0).mean() > 0.2

    def test_load_hand_returns_the_bundled_sprite_without_any_model_call(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(ra, "_vertex_call", lambda *a, **k: calls.append("vertex") or None)
        monkeypatch.setattr(ra, "_aistudio_call", lambda *a, **k: calls.append("aistudio") or None)
        res = ra.load_hand("hand_pen", cache_dir=tmp_path, allow_generate=True)
        assert res is not None
        img, tip = res
        assert img.mode == "RGBA" and tip[0] < img.width / 3
        assert calls == [], "the bundled hand must never trigger generation"
        assert not (tmp_path / "hand_pen").exists(), "nothing written to the cache"

    def test_an_unshipped_key_still_uses_the_cache_tier(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ra, "_vertex_call", lambda *a, **k: None)
        monkeypatch.setattr(ra, "_aistudio_call", lambda *a, **k: None)
        assert ra.load_hand("hand_left", cache_dir=tmp_path, allow_generate=False) is None


def _fake_clock():
    now = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        now[0] += s
    return now, slept, sleep


class TestModelCallPacing:
    def test_calls_beyond_the_per_minute_budget_wait_for_the_window(self):
        now, slept, sleep = _fake_clock()
        lim = RateLimiter(3, clock=lambda: now[0], sleep=sleep)
        assert [lim.acquire() for _ in range(3)] == [0.0, 0.0, 0.0]
        waited = lim.acquire()          # 4th inside the same minute
        assert waited >= 60.0 and slept and now[0] >= 60.0

    def test_with_backoff_paces_through_the_kind_s_own_window(self, monkeypatch):
        acquired = []

        class Spy(RateLimiter):
            def __init__(self, name):
                super().__init__(100)
                self.name = name

            def acquire(self):
                acquired.append(self.name)
                return 0.0
        monkeypatch.setattr(ra, "_LIMITERS", {"image": Spy("image"), "vision": Spy("vision")})
        assert ra._with_backoff(lambda: "ok", "test") == "ok"
        assert ra._with_backoff(lambda: "ok", "test", kind="vision") == "ok"
        assert acquired == ["image", "vision"], "vision must not queue behind image generation"

    def test_the_vision_call_site_declares_its_kind(self):
        import inspect
        src = inspect.getsource(ra)
        assert '_with_backoff(_go, "vision", kind="vision")' in src

    def test_a_paced_caller_holds_no_gate_slot(self):
        """The pacing sleep happens before `with gate:` — the invariant the
        429 sleep already keeps."""
        import inspect
        lines = inspect.getsource(ra._with_backoff).splitlines()
        pace = next(i for i, l in enumerate(lines) if ".acquire()" in l)
        gate = next(i for i, l in enumerate(lines) if "with gate:" in l)
        assert pace < gate

    def test_limiters_are_built_from_the_environment_defensively(self, monkeypatch):
        monkeypatch.setattr(ra, "_LIMITERS", {})
        monkeypatch.setenv("IMAGE_CALLS_PER_MINUTE", "banana")
        monkeypatch.setenv("VISION_CALLS_PER_MINUTE", "90")
        assert ra._limiter("image").per_minute == 15      # default, not a crash
        assert ra._limiter("vision").per_minute == 90
        assert ra._limiter("image") is ra._limiter("image")  # built once

    def test_a_429_with_retry_after_waits_the_server_s_number(self, monkeypatch):
        import requests
        slept = []
        monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
        resp = requests.Response()
        resp.status_code = 429
        resp.headers["Retry-After"] = "7"
        err = requests.HTTPError(response=resp)
        n = {"i": 0}

        def fn():
            n["i"] += 1
            if n["i"] == 1:
                raise err
            return "ok"
        assert ra._with_backoff(fn, "test") == "ok"
        assert slept and 7.0 <= slept[0] <= 9.0, slept

    def test_an_hour_long_retry_after_is_capped_at_a_minute(self, monkeypatch):
        import requests
        slept = []
        monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
        resp = requests.Response()
        resp.status_code = 429
        resp.headers["retry-after"] = "3600"
        err = requests.HTTPError(response=resp)
        n = {"i": 0}

        def fn():
            n["i"] += 1
            if n["i"] == 1:
                raise err
            return "ok"
        assert ra._with_backoff(fn, "test") == "ok"
        assert slept and 60.0 <= slept[0] <= 62.0, slept

    def test_the_tts_provider_shares_the_class(self):
        from shared.tts.providers import google
        assert google._RateLimiter is RateLimiter
