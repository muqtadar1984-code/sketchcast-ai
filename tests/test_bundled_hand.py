"""The drawing hand ships with the code; image calls are paced per minute.

Founder, 2026-09-04: "can we store the hand sprite as part of the repository
so we don't have to call it from AI every time? we need to ensure we don't hit
rate limits while generating videos."
"""
from __future__ import annotations

import json
import time

import numpy as np
from PIL import Image

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


class TestImagePacing:
    def test_calls_beyond_the_per_minute_budget_wait_for_the_window(self):
        from shared.ratelimit import RateLimiter
        now = [0.0]
        slept = []

        def sleep(s):
            slept.append(s)
            now[0] += s
        lim = RateLimiter(3, clock=lambda: now[0], sleep=sleep)
        assert [lim.acquire() for _ in range(3)] == [0.0, 0.0, 0.0]
        waited = lim.acquire()          # 4th inside the same minute
        assert waited >= 60.0 and slept and now[0] >= 60.0

    def test_with_backoff_paces_through_the_shared_limiter(self, monkeypatch):
        from shared.ratelimit import RateLimiter
        acquired = []

        class Spy(RateLimiter):
            def acquire(self):
                acquired.append(1)
                return 0.0
        monkeypatch.setattr(ra, "_IMAGE_LIMITER", Spy(100))
        assert ra._with_backoff(lambda: "ok", "test") == "ok"
        assert acquired == [1]

    def test_a_429_with_retry_after_waits_the_server_s_number(self, monkeypatch):
        import requests
        from shared.ratelimit import RateLimiter
        monkeypatch.setattr(ra, "_IMAGE_LIMITER", RateLimiter(100))
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

    def test_the_tts_provider_shares_the_class(self):
        from shared.ratelimit import RateLimiter
        from shared.tts.providers import google
        assert google._RateLimiter is RateLimiter
