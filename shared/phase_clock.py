"""Where a generation's wall time goes — per phase, per generation.

Until 2026-10-14 the only timing the pipeline persisted was
``jobs.updated_at - jobs.created_at`` (queue wait + everything, one number) and
one ``compose:`` INFO line in a Railway log stream that ran up to 47 minutes
behind the worker. "Is the slow part the script, the pictures, the voice or the
render?" was unanswerable from our own data, so the Bedrock-failover and GPU
work had nothing to measure against.

This module is the accumulator. A ``PhaseClock`` is opened per generation
(``start``), phases add wall seconds to it (``phase`` / ``add``), counters
record what happened on the way (``count``: rate-limit sleeps, TTS calls), and
``summary()`` is what the worker persists on the generation row
(``generations.params.timings``) and prints as one INFO line.

Phase seconds are WALL seconds as seen by the thread that did the work. Inside
the video composer several segments render on threads at once, so ``tts`` and
``render`` are SUMS of per-segment time (busy seconds), while ``compose`` is
the wall time of the whole pass; the sum can exceed the wall. That is on
purpose: the sum says how much work the phase is, the wall says how long a
teacher waited for it.

Threads: the current clock travels in a ``ContextVar``. A thread started by a
``ThreadPoolExecutor`` begins with an EMPTY context, so the composer's render
threads are bound through ``raster_assets.bind_generation`` (which re-sets this
var alongside the image budget's). Anything else may pass ``generation_id``
explicitly. Without a clock every call is a no-op — tests and tools that never
opened one pay nothing and break nothing.
"""
from __future__ import annotations

import contextvars
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Optional

_CLOCKS: dict[str, "PhaseClock"] = {}
_LOCK = threading.Lock()
_CURRENT: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "phase_clock_generation", default=None)

# The order the summary line prints phases in — the pipeline's own order, so
# a reader scans left to right through a lesson's life. Phases not listed
# here (new ones, tool-specific ones) follow, alphabetically.
PHASE_ORDER = (
    "queue_wait", "prelude", "analysis", "script", "slides", "images_warm",
    "tts", "render", "compose", "concat", "acceptance", "document", "deck",
    "upload", "pacing_wait", "cooldown_wait", "lease_wait", "rate_limit_wait",
    "deferral_wait",
)


class PhaseClock:
    """Accumulated wall seconds per phase, plus counters, for one generation."""

    def __init__(self, generation_id: str, *, clock=time.monotonic) -> None:
        self.generation_id = str(generation_id)
        self._clock = clock
        self._t0 = clock()
        self._started_at = datetime.now(timezone.utc)
        self._lock = threading.Lock()
        self.phases: dict[str, float] = {}
        self.counts: dict[str, int] = {}
        # Facts about the run that are neither seconds nor tallies
        # (``reused_analysis``): persisted beside the phases, top level.
        self.flags: dict[str, object] = {}

    # ── recording ────────────────────────────────────────────────────────
    def add(self, name: str, seconds: float) -> None:
        if seconds <= 0:
            return
        with self._lock:
            self.phases[name] = self.phases.get(name, 0.0) + float(seconds)

    def count(self, name: str, n: int = 1) -> None:
        with self._lock:
            self.counts[name] = self.counts.get(name, 0) + int(n)

    def flag(self, name: str, value: object = True) -> None:
        with self._lock:
            self.flags[str(name)] = value

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        t = self._clock()
        try:
            yield
        finally:
            self.add(name, self._clock() - t)

    # ── reading ──────────────────────────────────────────────────────────
    def elapsed(self) -> float:
        return self._clock() - self._t0

    def summary(self, *, outcome: str | None = None) -> dict:
        """The persisted shape: whole seconds (one decimal) and counts, with
        the clock's own total so the unaccounted remainder is visible."""
        with self._lock:
            phases = {k: round(v, 1) for k, v in self.phases.items()}
            counts = dict(self.counts)
            flags = dict(self.flags)
        out = {
            "total_s": round(self.elapsed(), 1),
            "phases": phases,
            "counts": counts,
            "started_at": self._started_at.isoformat(timespec="seconds"),
        }
        if outcome:
            out["outcome"] = outcome
        for k, v in flags.items():
            out.setdefault(k, v)      # never over a reserved key
        return out

    def line(self) -> str:
        """One log line: ``total=2583s queue_wait=4s script=1500s … | rate_limited=12``."""
        summ = self.summary()
        phases = summ["phases"]
        ordered = [k for k in PHASE_ORDER if k in phases] + sorted(
            k for k in phases if k not in PHASE_ORDER)
        parts = [f"total={summ['total_s']:.0f}s"]
        parts += [f"{k}={phases[k]:.0f}s" for k in ordered]
        text = " ".join(parts)
        if summ["counts"]:
            text += " | " + " ".join(f"{k}={v}" for k, v in sorted(summ["counts"].items()))
        return text


# ── registry ─────────────────────────────────────────────────────────────
def start(generation_id: str, *, clock=time.monotonic) -> PhaseClock:
    """Open (or reopen) the clock for a generation and make it current for
    this thread. Reopening replaces: a requeued job starts its own account."""
    gid = str(generation_id)
    pc = PhaseClock(gid, clock=clock)
    with _LOCK:
        _CLOCKS[gid] = pc
    _CURRENT.set(gid)
    return pc


def get(generation_id: str | None = None) -> Optional[PhaseClock]:
    gid = str(generation_id) if generation_id is not None else _CURRENT.get()
    if gid is None:
        return None
    with _LOCK:
        return _CLOCKS.get(gid)


def current() -> Optional[str]:
    """The generation id this thread's calls account to (for re-binding a
    pool thread; see raster_assets.bind_generation)."""
    return _CURRENT.get()


def bind(generation_id: str | None) -> contextvars.Token:
    """Make ``generation_id`` current for this thread; reset with ``unbind``."""
    return _CURRENT.set(str(generation_id) if generation_id is not None else None)


def unbind(token: contextvars.Token) -> None:
    _CURRENT.reset(token)


def stop(generation_id: str) -> Optional[PhaseClock]:
    """Close a generation's clock (the summary should have been taken)."""
    gid = str(generation_id)
    with _LOCK:
        pc = _CLOCKS.pop(gid, None)
    if _CURRENT.get() == gid:
        _CURRENT.set(None)
    return pc


# ── module-level conveniences (no clock → no-op) ─────────────────────────
def flag(name: str, value: object = True, generation_id: str | None = None) -> None:
    pc = get(generation_id)
    if pc is not None:
        pc.flag(name, value)


@contextmanager
def phase(name: str, generation_id: str | None = None) -> Iterator[None]:
    pc = get(generation_id)
    if pc is None:
        yield
        return
    with pc.phase(name):
        yield


def add(name: str, seconds: float, generation_id: str | None = None) -> None:
    pc = get(generation_id)
    if pc is not None:
        pc.add(name, seconds)


def count(name: str, n: int = 1, generation_id: str | None = None) -> None:
    pc = get(generation_id)
    if pc is not None:
        pc.count(name, n)
