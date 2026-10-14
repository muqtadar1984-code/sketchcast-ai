"""Admission control for a shared model pool — learned from outcomes.

Why. Both Gemini models the worker calls on Vertex (scripts and pictures) sit
on Google's shared pay-as-you-go pool: there is no per-project quota, and a
429 means the pool is busy right now. On 2026-10-13 the worker pushed 12-14
image requests a minute at a pool that was yielding 3-5, three in four were
refused, and the fixed retry ladder (7 s, 15 s, 36 s, no Retry-After from the
server) sent the refused threads back in lockstep. The afternoon's four
concurrent kits did the same to the text model. Sending more did not buy
more; it bought refusals. See docs/burst-control.md.

What. One ``ProviderGate`` per provider (``vertex_image``, ``vertex_text``)
that every call goes through:

* **in-flight cap** — a process semaphore, and optionally a *lease* in
  Supabase so several worker replicas share ONE cap (``configure``);
* **adaptive pacing** — a per-minute send rate that climbs by one after
  ``step_after`` consecutive successes and halves on a refusal, between a
  floor and a ceiling (AIMD: the classic way to find a shared link's usable
  rate without being told it). ``rate = 0`` means "no pacing" (text calls);
* **cooldown** — after a refusal nobody on this provider sends for
  ``cooldown`` seconds (or the server's Retry-After, when it names one), so
  threads do not re-collide; the wait is accounted to the phase clock.

The pacing state lives in ``provider_pacing`` when a store is configured, so
a replica that has never seen a 429 still starts at the rate its peers
learned. Without a store (tools, tests, a worker booted without a DB) the
gate is local and behaves identically for one process. Nothing here raises:
a store that cannot be reached logs once and the gate carries on locally.

``backoff_seconds`` is the retry wait a caller should use between attempts:
full jitter (uniform between 0 and the exponential cap) so that three slots
refused together come back spread out, and the server's Retry-After, when
present, as the floor.
"""
from __future__ import annotations

import logging
import os
import random
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from shared import phase_clock

logger = logging.getLogger(__name__)

# Defaults per provider. Image: three in flight (the old MODEL_CALL_CONCURRENCY),
# start at four a minute, never below two, never above twelve, up one every
# five clean calls, 30 s silence after a refusal. Text: four in flight, no
# per-minute pacing (a script call is seconds to minutes long; the in-flight
# cap IS the pace), 20 s silence after a refusal.
_DEFAULTS: dict[str, dict] = {
    "vertex_image": {"in_flight": 3, "global_in_flight": 3, "start": 4.0, "floor": 2.0,
                     "ceiling": 12.0, "step_after": 5, "cooldown": 30.0},
    "vertex_text": {"in_flight": 4, "global_in_flight": 4, "start": 0.0, "floor": 0.0,
                    "ceiling": 0.0, "step_after": 5, "cooldown": 20.0},
}
_LEASE_TTL_SECONDS = 600       # an image call is bounded by its 120 s HTTP timeout; text by 600 s
_LEASE_RETRY_SECONDS = (2.0, 5.0)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "")
    try:
        return float(raw) if str(raw).strip() else default
    except (TypeError, ValueError):
        logger.warning("%s is not a number; using %s", name, default)
        return default


def _settings(name: str) -> dict:
    base = dict(_DEFAULTS.get(name) or _DEFAULTS["vertex_image"])
    key = name.upper()
    out = {
        "in_flight": max(1, int(_env_float(f"PROVIDER_{key}_IN_FLIGHT", base["in_flight"]))),
        "global_in_flight": max(1, int(_env_float(f"PROVIDER_{key}_GLOBAL_IN_FLIGHT",
                                                  base["global_in_flight"]))),
        "start": max(0.0, _env_float(f"PROVIDER_{key}_START_PER_MIN", base["start"])),
        "floor": max(0.0, _env_float(f"PROVIDER_{key}_FLOOR_PER_MIN", base["floor"])),
        "ceiling": max(0.0, _env_float(f"PROVIDER_{key}_CEILING_PER_MIN", base["ceiling"])),
        "step_after": max(1, int(_env_float(f"PROVIDER_{key}_STEP_AFTER", base["step_after"]))),
        "cooldown": max(0.0, _env_float(f"PROVIDER_{key}_COOLDOWN_SECS", base["cooldown"])),
        # the longest silence a refusal can impose, the server's Retry-After
        # included: an hour-long header is a server quirk, not an instruction
        "max_cooldown": max(0.0, _env_float(f"PROVIDER_{key}_MAX_COOLDOWN_SECS", 120.0)),
    }
    # the image gate keeps honouring the pre-2026-10-14 knobs
    if name == "vertex_image":
        legacy = os.getenv("MODEL_CALL_CONCURRENCY", "").strip()
        if legacy and not os.getenv("PROVIDER_VERTEX_IMAGE_IN_FLIGHT", "").strip():
            out["in_flight"] = out["global_in_flight"] = max(1, int(_env_float("MODEL_CALL_CONCURRENCY", 3)))
        legacy_rate = os.getenv("IMAGE_CALLS_PER_MINUTE", "").strip()
        if legacy_rate and not os.getenv("PROVIDER_VERTEX_IMAGE_CEILING_PER_MIN", "").strip():
            out["ceiling"] = max(out["floor"], _env_float("IMAGE_CALLS_PER_MINUTE", out["ceiling"]))
    if out["ceiling"] and out["floor"] > out["ceiling"]:
        out["floor"] = out["ceiling"]
    if out["ceiling"] and out["start"] > out["ceiling"]:
        out["start"] = out["ceiling"]
    if out["floor"] and out["start"] < out["floor"]:
        out["start"] = out["floor"]
    return out


def backoff_seconds(attempt: int, *, base: float = 6.0, cap: float = 60.0,
                    growth: float = 2.4, retry_after: float | None = None,
                    rng=random.uniform) -> float:
    """The wait before retry ``attempt`` (0-based) of a refused call.

    Our own ladder: ``base * growth**attempt`` plus a jitter across HALF of
    it — three slots refused together then land several seconds apart
    (at a quarter they re-collided inside 1.5 s; measured 2026-09). When the
    SERVER names the wait (Retry-After) it is honoured as the base, capped,
    with jitter inside a quarter of it. The gate's cooldown sits on top of
    this: after a refusal nobody sends until it lifts.
    """
    if retry_after is not None:
        server = min(max(float(retry_after), 1.0), cap)
        return server + rng(0.0, min(2.0 + 2.0 * attempt, server * 0.25))
    step = min(cap, base * (growth ** max(0, attempt)))
    return step + rng(0.0, step * 0.5)


@dataclass
class Token:
    provider: str
    lease_id: Optional[str] = None


class SupabaseStore:
    """The shared half of a gate: leases and pacing rows through the RPCs of
    migration 0125. Every method returns None on any failure so the gate
    falls back to local behaviour; the first failure is logged, later ones
    once a minute."""

    def __init__(self, sb, *, clock=time.monotonic) -> None:
        self._sb = sb
        self._clock = clock
        self._muted_until = 0.0

    def _fail(self, what: str, exc: BaseException) -> None:
        now = self._clock()
        if now >= self._muted_until:
            logger.warning("provider gate store: %s failed (%s); continuing locally", what, exc)
            self._muted_until = now + 60.0

    def acquire_lease(self, provider: str, max_in_flight: int, holder: str, ttl: int) -> Optional[dict]:
        try:
            res = self._sb.rpc("acquire_provider_lease", {
                "p_provider": provider, "p_max": int(max_in_flight),
                "p_holder": holder, "p_ttl_seconds": int(ttl)}).execute()
            data = getattr(res, "data", None)
            return {"granted": bool(data), "id": data if isinstance(data, str) else None}
        except Exception as exc:  # noqa: BLE001
            self._fail("acquire_provider_lease", exc)
            return None

    def release_lease(self, lease_id: str) -> None:
        try:
            self._sb.rpc("release_provider_lease", {"p_id": lease_id}).execute()
        except Exception as exc:  # noqa: BLE001
            self._fail("release_provider_lease", exc)

    def record_outcome(self, provider: str, ok: bool, s: dict,
                       retry_after: float | None) -> Optional[dict]:
        try:
            res = self._sb.rpc("provider_outcome", {
                "p_provider": provider, "p_ok": bool(ok),
                "p_start": float(s["start"]), "p_floor": float(s["floor"]),
                "p_ceiling": float(s["ceiling"]), "p_step_after": int(s["step_after"]),
                "p_cooldown_seconds": float(s["cooldown"]),
                "p_retry_after": float(retry_after) if retry_after else None}).execute()
            data = getattr(res, "data", None)
            row = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else None)
            return row
        except Exception as exc:  # noqa: BLE001
            self._fail("provider_outcome", exc)
            return None


def _parse_stamp(value) -> float | None:
    """A timestamptz from PostgREST → wall seconds (None when absent)."""
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


class ProviderGate:
    def __init__(self, name: str, settings: dict | None = None, *,
                 clock: Optional[Callable[[], float]] = None,
                 wall: Optional[Callable[[], float]] = None,
                 sleep: Optional[Callable[[float], None]] = None,
                 store: Optional[SupabaseStore] = None) -> None:
        self.name = name
        self.s = settings or _settings(name)
        # looked up at CALL time, so a test that patches time.sleep is honoured
        self._clock = clock or (lambda: time.monotonic())
        self._wall = wall or (lambda: time.time())
        self._sleep = sleep or (lambda s: time.sleep(s))
        self.store = store
        self._lock = threading.Lock()
        self._sem = threading.BoundedSemaphore(self.s["in_flight"])
        self.rate: float = float(self.s["start"])        # per minute; 0 = unpaced
        self.successes = 0
        self.refusals = 0
        self.cooldown_until: float = 0.0                 # wall seconds
        self._sent: deque[float] = deque()               # monotonic send times, last minute
        self._holder = f"{socket.gethostname()}:{os.getpid()}"

    # ── admission ───────────────────────────────────────────────────────
    def _cooldown_left(self) -> float:
        return max(0.0, self.cooldown_until - self._wall())

    def _pace_wait(self) -> float:
        """Seconds until a send fits the current rate (0 = now); records the
        send when it fits."""
        with self._lock:
            if self.rate <= 0:
                return 0.0
            now = self._clock()
            while self._sent and now - self._sent[0] >= 60.0:
                self._sent.popleft()
            allowed = max(1, int(self.rate))
            if len(self._sent) < allowed:
                self._sent.append(now)
                return 0.0
            return 60.0 - (now - self._sent[0]) + 0.01

    def acquire(self, what: str = "") -> Token:
        """Block until this call may go out. Accounted to the phase clock:
        ``cooldown_wait`` (a refusal silenced the provider), ``pacing_wait``
        (the learned rate), ``lease_wait`` (the shared in-flight cap)."""
        # The silence is measured ONCE and slept in bounded chunks: a loop that
        # re-read the clock would spin forever under a test's recorded sleep,
        # and a refusal that lands while we wait extends the NEXT caller's
        # silence, not this one's (it re-checks at the pace step below).
        left = self._cooldown_left()
        while left > 0:
            chunk = min(left, 5.0)
            self._sleep(chunk)
            phase_clock.add("cooldown_wait", chunk)
            left -= chunk
        for _ in range(10**6):
            wait = self._pace_wait()
            if wait <= 0:
                break
            self._sleep(wait)
            phase_clock.add("pacing_wait", wait)
        self._sem.acquire()
        lease_id = None
        if self.store is not None:
            while True:
                got = self.store.acquire_lease(self.name, self.s["global_in_flight"],
                                               self._holder, _LEASE_TTL_SECONDS)
                if got is None:          # store unreachable: local cap only
                    break
                if got["granted"]:
                    lease_id = got["id"]
                    break
                wait = random.uniform(*_LEASE_RETRY_SECONDS)
                self._sleep(wait)
                phase_clock.add("lease_wait", wait)
        return Token(self.name, lease_id)

    def release(self, token: Token) -> None:
        try:
            if token.lease_id and self.store is not None:
                self.store.release_lease(token.lease_id)
        finally:
            self._sem.release()

    # ── learning ────────────────────────────────────────────────────────
    def outcome(self, ok: bool, *, retry_after: float | None = None) -> None:
        """A call finished: clean (``ok``) or refused on quota (``not ok``)."""
        with self._lock:
            if ok:
                self.successes += 1
                if self.rate > 0 and self.successes >= self.s["step_after"]:
                    self.rate = min(self.s["ceiling"], self.rate + 1.0)
                    self.successes = 0
            else:
                self.refusals += 1
                self.successes = 0
                if self.rate > 0:
                    self.rate = max(self.s["floor"], self.rate / 2.0)
                silence = max(self.s["cooldown"], float(retry_after or 0.0))
                silence = min(silence, float(self.s.get("max_cooldown", 120.0)))
                self.cooldown_until = max(self.cooldown_until, self._wall() + silence)
        if self.store is not None:
            row = self.store.record_outcome(self.name, ok, self.s, retry_after)
            if row:
                self.adopt(row)
        if not ok:
            logger.warning("%s refused on quota: rate now %.1f/min, silence %.0fs%s",
                           self.name, self.rate, self._cooldown_left(),
                           f" (server asked {retry_after:.0f}s)" if retry_after else "")

    def adopt(self, row: dict) -> None:
        """Take the shared pacing state (a peer may have learned a lower rate)."""
        with self._lock:
            try:
                rate = float(row.get("rate_per_min"))
                if self.s["ceiling"] > 0:
                    self.rate = min(self.s["ceiling"], max(self.s["floor"], rate))
            except (TypeError, ValueError):
                pass
            until = _parse_stamp(row.get("cooldown_until"))
            if until is not None:
                self.cooldown_until = max(self.cooldown_until, until)
            try:
                self.successes = int(row.get("successes") or 0)
            except (TypeError, ValueError):
                pass

    def snapshot(self) -> dict:
        with self._lock:
            return {"provider": self.name, "rate_per_min": self.rate,
                    "cooldown_left_s": round(self._cooldown_left(), 1),
                    "successes": self.successes, "refusals": self.refusals,
                    "in_flight": self.s["in_flight"], "global": self.store is not None}


# ── registry ─────────────────────────────────────────────────────────────
_GATES: dict[str, ProviderGate] = {}
_STORE: Optional[SupabaseStore] = None
_REG_LOCK = threading.Lock()


def configure(sb) -> None:
    """Share the gates across replicas through this Supabase client (the
    worker calls it at boot). ``PROVIDER_GATE_GLOBAL=0`` keeps them local."""
    global _STORE
    if str(os.getenv("PROVIDER_GATE_GLOBAL", "1")).strip().lower() in ("0", "false", "off", "no"):
        return
    store = SupabaseStore(sb) if sb is not None else None
    with _REG_LOCK:
        _STORE = store
        for g in _GATES.values():
            g.store = store


def gate(name: str) -> ProviderGate:
    with _REG_LOCK:
        g = _GATES.get(name)
        if g is None:
            g = _GATES[name] = ProviderGate(name, store=_STORE)
        return g


def reset() -> None:
    """Tests: forget every gate and the store."""
    global _STORE
    with _REG_LOCK:
        _GATES.clear()
        _STORE = None
