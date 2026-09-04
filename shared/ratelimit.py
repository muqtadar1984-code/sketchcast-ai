"""A sliding one-minute window shared by every thread in the process.

Lifted out of the Google TTS provider so the image/vision path paces the same
way: a caller that would exceed the rate sleeps until the oldest request in the
window is a minute old. Injectable clock/sleep for tests.
"""
from __future__ import annotations

import collections
import threading
import time


class RateLimiter:
    def __init__(self, per_minute: int, clock=time.monotonic, sleep=time.sleep):
        self.per_minute = max(1, int(per_minute))
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._times: collections.deque[float] = collections.deque()

    def acquire(self) -> float:
        """Block until a request may go out; returns the seconds waited."""
        waited = 0.0
        while True:
            with self._lock:
                now = self._clock()
                while self._times and now - self._times[0] >= 60.0:
                    self._times.popleft()
                if len(self._times) < self.per_minute:
                    self._times.append(now)
                    return waited
                wait = 60.0 - (now - self._times[0]) + 0.01
            self._sleep(wait)
            waited += wait
