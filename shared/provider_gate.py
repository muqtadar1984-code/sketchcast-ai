"""Admission control for a shared model pool — learned from outcomes, and,
with the store configured, decided ONCE for the whole deployment.

Why. Both Gemini models the worker calls on Vertex (scripts and pictures) sit
on Google's shared pay-as-you-go pool: there is no per-project quota, and a
429 means the pool is busy right now. On 2026-10-13 the worker pushed 12-14
image requests a minute at a pool that was yielding 3-5, three in four were
refused, and the fixed retry ladder (7 s, 15 s, 36 s, no Retry-After from the
server) sent the refused threads back in lockstep. The afternoon's four
concurrent kits did the same to the text model. Sending more did not buy
more; it bought refusals. See docs/burst-control.md.

What. One ``ProviderGate`` per provider (``vertex_image``, ``vertex_text``)
that every call goes through. Three controls:

* **in-flight cap** — a process semaphore, and a *lease* in Supabase so
  several worker replicas share ONE cap. A lease is short (180 s) and the
  holder renews it from a background thread while its call runs, so a
  crashed holder frees its slot in minutes and a ten-minute script call
  never outlives its lease;
* **pace** — a per-minute send rate that climbs by one after ``step_after``
  consecutive successes and halves on a refusal, between a floor and a
  ceiling (AIMD). With the store, the SEND WINDOW itself lives in the
  database (``provider_permit``): one atomic "may I send now?" under the
  provider's lock, so N replicas together send the learned rate, not N
  times it. ``rate = 0`` means "no pacing" (text calls);
* **cooldown** — after a refusal nobody on this provider sends for
  ``cooldown`` seconds, or the server's Retry-After when longer, never above
  ``max_cooldown``. The store applies the identical rule, so a peer's
  refusal silences this replica too.

Fail-safe. When the store cannot be reached the gate does NOT fall back to
"unlimited": it runs DEGRADED — one call in flight per process, the floor
rate, the local cooldown — and says so once a minute. A lost coordinator
must never turn into a burst.

Nothing here raises into the pipeline. ``backoff_seconds`` is the retry
wait a caller sleeps between attempts: our own ladder with jitter across
half of it, or the server's Retry-After (capped) with a quarter of jitter;
the gate's cooldown sits on top of it.
"""
from __future__ import annotations

import logging
import os
import random
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from shared import phase_clock

logger = logging.getLogger(__name__)

# Defaults per provider. Image: three in flight (the old MODEL_CALL_CONCURRENCY),
# start at four a minute, never below two, never above twelve, up one every
# five clean calls, 30 s silence after a refusal. Text: four in flight, no
# per-minute pacing (a script call is seconds to minutes long; the in-flight
# cap IS the pace), 20 s silence after a refusal. Degraded (store lost): one
# in flight, the floor rate.
_DEFAULTS: dict[str, dict] = {
    "vertex_image": {"in_flight": 3, "global_in_flight": 3, "start": 4.0, "floor": 2.0,
                     "ceiling": 12.0, "step_after": 5, "cooldown": 30.0, "max_cooldown": 120.0,
                     "degraded_in_flight": 1},
    "vertex_text": {"in_flight": 4, "global_in_flight": 4, "start": 0.0, "floor": 0.0,
                    "ceiling": 0.0, "step_after": 5, "cooldown": 20.0, "max_cooldown": 120.0,
                    "degraded_in_flight": 1},
}
LEASE_TTL_SECONDS = 180          # short: a crashed holder frees its slot in minutes…
LEASE_RENEW_SECONDS = 45         # …and a live holder renews well inside it
LEASE_MAX_WAIT_SECONDS = 900     # a full cap for this long is an incident, not a queue
_LEASE_RETRY_SECONDS = (2.0, 5.0)
_PERMIT_SLEEP_CAP = 5.0          # one sleep call is never longer than this (interruptible)
_PERMIT_WAIT_CAP = 120.0         # the longest a single permit answer is trusted for
_RELEASE_LEASE_ABOVE = 10.0      # a wait longer than this gives the slot back meanwhile


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
        "max_cooldown": max(0.0, _env_float(f"PROVIDER_{key}_MAX_COOLDOWN_SECS", base["max_cooldown"])),
        "degraded_in_flight": max(1, int(_env_float(f"PROVIDER_{key}_DEGRADED_IN_FLIGHT",
                                                    base["degraded_in_flight"]))),
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
    degraded: bool = False


@dataclass
class _Lease:
    id: str
    expires_at: float          # monotonic


class SupabaseStore:
    """The shared half of a gate: the RPCs of migrations 0125 + 0126. Every
    method returns None on any failure so the gate drops to DEGRADED; the
    first failure is logged, later ones once a minute."""

    def __init__(self, sb, *, clock=time.monotonic) -> None:
        self._sb = sb
        self._clock = clock
        self._muted_until = 0.0

    def _fail(self, what: str, exc: BaseException) -> None:
        now = self._clock()
        if now >= self._muted_until:
            logger.warning("provider gate store: %s failed (%s); gate DEGRADED "
                           "(one in flight, floor rate) until it answers", what, exc)
            self._muted_until = now + 60.0

    def permit(self, provider: str, s: dict) -> Optional[dict]:
        try:
            res = self._sb.rpc("provider_permit", {
                "p_provider": provider, "p_start": float(s["start"]),
                "p_floor": float(s["floor"]), "p_ceiling": float(s["ceiling"])}).execute()
            data = getattr(res, "data", None)
            row = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else None)
            if not row:
                return None
            return {"granted": bool(row.get("granted")),
                    "wait": max(0.0, float(row.get("wait_seconds") or 0.0)),
                    "reason": str(row.get("reason") or ""),
                    "rate": row.get("rate_per_min")}
        except Exception as exc:  # noqa: BLE001
            self._fail("provider_permit", exc)
            return None

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

    def renew_lease(self, lease_id: str, ttl: int) -> Optional[bool]:
        try:
            res = self._sb.rpc("renew_provider_lease", {"p_id": lease_id, "p_ttl_seconds": int(ttl)}).execute()
            return bool(getattr(res, "data", None))
        except Exception as exc:  # noqa: BLE001
            self._fail("renew_provider_lease", exc)
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
                "p_retry_after": float(retry_after) if retry_after else None,
                "p_max_cooldown": float(s.get("max_cooldown", 120.0))}).execute()
            data = getattr(res, "data", None)
            return data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else None)
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
                 store: Optional[SupabaseStore] = None,
                 renewer: bool = True) -> None:
        self.name = name
        self.s = settings or _settings(name)
        # looked up at CALL time, so a test that patches time.sleep is honoured
        self._clock = clock or (lambda: time.monotonic())
        self._wall = wall or (lambda: time.time())
        self._sleep = sleep or (lambda s: time.sleep(s))
        self.store = store
        self._lock = threading.Lock()
        self._sem = threading.BoundedSemaphore(self.s["in_flight"])
        self._degraded_sem = threading.BoundedSemaphore(self.s.get("degraded_in_flight", 1))
        self.rate: float = float(self.s["start"])        # per minute; 0 = unpaced
        self.successes = 0
        self.refusals = 0
        self.cooldown_until: float = 0.0                 # wall seconds
        self._sent: deque[float] = deque()               # monotonic send times, last minute
        self._holder = f"{socket.gethostname()}:{os.getpid()}"
        self._held: dict[str, _Lease] = {}               # leases this process holds
        self._renewer_on = renewer
        self._renewer: Optional[threading.Thread] = None
        self.degraded_since: float | None = None

    # ── local pieces ────────────────────────────────────────────────────
    def _cooldown_left(self) -> float:
        return max(0.0, self.cooldown_until - self._wall())

    def _local_rate(self) -> float:
        """The rate the LOCAL window enforces: the learned one normally, the
        floor while degraded (a lost coordinator must not become a burst)."""
        if self.degraded_since is not None and self.s["floor"] > 0:
            return min(self.rate, self.s["floor"]) if self.rate > 0 else self.s["floor"]
        return self.rate

    def _pace_wait(self) -> float:
        """Seconds until a send fits the local window (0 = now; the send is
        recorded when it fits)."""
        with self._lock:
            rate = self._local_rate()
            if rate <= 0:
                return 0.0
            now = self._clock()
            while self._sent and now - self._sent[0] >= 60.0:
                self._sent.popleft()
            if len(self._sent) < max(1, int(rate)):
                self._sent.append(now)
                return 0.0
            return 60.0 - (now - self._sent[0]) + 0.01

    def _wait(self, seconds: float, phase: str) -> None:
        left = seconds
        while left > 0:
            chunk = min(left, _PERMIT_SLEEP_CAP)
            self._sleep(chunk)
            phase_clock.add(phase, chunk)
            left -= chunk

    def _enter_degraded(self) -> None:
        if self.degraded_since is None:
            self.degraded_since = self._clock()
            logger.warning("%s gate DEGRADED: the shared store is unreachable — %d in flight, "
                           "%.0f/min until it answers", self.name,
                           self.s.get("degraded_in_flight", 1), self._local_rate())

    def _leave_degraded(self) -> None:
        if self.degraded_since is not None:
            logger.info("%s gate back on the shared store after %.0fs degraded",
                        self.name, self._clock() - self.degraded_since)
            self.degraded_since = None

    # ── admission ───────────────────────────────────────────────────────
    def acquire(self, what: str = "") -> Token:
        """Block until this call may go out, then hold its slot(s).

        With the store: a LEASE (the deployment-wide in-flight cap), then a
        PERMIT (the deployment-wide cooldown and send window, decided
        atomically in the database). A long permit wait hands the lease back
        meanwhile. Without the store, or when it fails: the local window at
        the floor rate, the local cooldown, one in flight (DEGRADED).
        Accounted to the phase clock as ``lease_wait``, ``cooldown_wait``
        and ``pacing_wait``."""
        if self.store is None:
            self._local_admission()
            self._sem.acquire()
            return Token(self.name)
        lease = self._take_lease()
        if lease is None:                       # the store failed: degraded path
            self._local_admission()
            self._degraded_sem.acquire()
            return Token(self.name, degraded=True)
        self._leave_degraded()
        while True:
            p = self.store.permit(self.name, self.s)
            if p is None:                       # the store failed mid-way
                self._give_back(lease)
                self._local_admission()
                self._degraded_sem.acquire()
                return Token(self.name, degraded=True)
            if p["granted"]:
                self._adopt_rate(p.get("rate"))
                break
            # Sleep the WHOLE wait the store named (in 5 s chunks), then ask
            # again: a cooldown does not shrink, and a pace wait is exact.
            # Re-asking every few seconds churned the lease six times in one
            # thirty-second silence. A longer wait than this cap is re-read.
            wait = min(max(p["wait"], 0.5), _PERMIT_WAIT_CAP)
            phase = "cooldown_wait" if p["reason"] == "cooldown" else "pacing_wait"
            if p["wait"] > _RELEASE_LEASE_ABOVE and lease is not None:
                self._give_back(lease)
                lease = None
            self._wait(wait, phase)
            if lease is None:
                lease = self._take_lease()
                if lease is None:
                    self._local_admission()
                    self._degraded_sem.acquire()
                    return Token(self.name, degraded=True)
        self._sem.acquire()
        return Token(self.name, lease.id)

    def _local_admission(self) -> None:
        """Cooldown then pace, locally (no store, or the store is down)."""
        left = self._cooldown_left()
        if left > 0:
            self._wait(left, "cooldown_wait")
        for _ in range(10**6):
            wait = self._pace_wait()
            if wait <= 0:
                break
            self._wait(wait, "pacing_wait")

    def _take_lease(self) -> Optional[_Lease]:
        """A lease from the store, waiting while the cap is full. None means
        the store failed (degraded); a full cap beyond LEASE_MAX_WAIT_SECONDS
        is logged and treated the same way."""
        waited = 0.0
        while True:
            got = self.store.acquire_lease(self.name, self.s["global_in_flight"],
                                           self._holder, LEASE_TTL_SECONDS)
            if got is None:
                self._enter_degraded()
                return None
            if got["granted"] and got["id"]:
                lease = _Lease(got["id"], self._clock() + LEASE_TTL_SECONDS)
                with self._lock:
                    self._held[lease.id] = lease
                self._ensure_renewer()
                return lease
            if waited >= LEASE_MAX_WAIT_SECONDS:
                logger.error("%s: the shared in-flight cap stayed full for %.0fs; "
                             "proceeding degraded", self.name, waited)
                self._enter_degraded()
                return None
            wait = random.uniform(*_LEASE_RETRY_SECONDS)
            self._sleep(wait)
            phase_clock.add("lease_wait", wait)
            waited += wait

    def _give_back(self, lease: _Lease) -> None:
        with self._lock:
            self._held.pop(lease.id, None)
        self.store.release_lease(lease.id)

    def release(self, token: Token) -> None:
        try:
            if token.lease_id and self.store is not None:
                with self._lock:
                    lease = self._held.pop(token.lease_id, None)
                self.store.release_lease(token.lease_id)
        finally:
            (self._degraded_sem if token.degraded else self._sem).release()

    # ── renewal ─────────────────────────────────────────────────────────
    def _ensure_renewer(self) -> None:
        if not self._renewer_on:
            return
        with self._lock:
            if self._renewer is not None and self._renewer.is_alive():
                return
            self._renewer = threading.Thread(target=self._renew_loop, daemon=True,
                                             name=f"lease-renew-{self.name}")
            self._renewer.start()

    def renew_once(self) -> int:
        """Extend every lease this process holds; returns how many were renewed.
        A lease the store no longer knows (expired, swept) is forgotten here
        too — the call it guarded runs on; the cap simply counts it no more."""
        with self._lock:
            ids = list(self._held)
        n = 0
        for lid in ids:
            ok = self.store.renew_lease(lid, LEASE_TTL_SECONDS) if self.store is not None else None
            if ok:
                with self._lock:
                    if lid in self._held:
                        self._held[lid].expires_at = self._clock() + LEASE_TTL_SECONDS
                n += 1
            elif ok is False:
                with self._lock:
                    self._held.pop(lid, None)
                logger.warning("%s: lease %s was gone at renewal (expired?); the call runs on uncounted",
                               self.name, lid[:8])
        return n

    def _renew_loop(self) -> None:
        while True:
            time.sleep(LEASE_RENEW_SECONDS)
            try:
                if self._held:
                    self.renew_once()
            except Exception:  # noqa: BLE001 — a renewer must never die loudly
                logger.warning("%s: lease renewal failed", self.name, exc_info=True)

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

    def _adopt_rate(self, rate) -> None:
        if rate is None:
            return
        with self._lock:
            try:
                r = float(rate)
            except (TypeError, ValueError):
                return
            if self.s["ceiling"] > 0:
                self.rate = min(self.s["ceiling"], max(self.s["floor"], r))

    def adopt(self, row: dict) -> None:
        """Take the shared pacing state (a peer may have learned a lower rate)."""
        self._adopt_rate(row.get("rate_per_min"))
        with self._lock:
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
                    "in_flight": self.s["in_flight"], "global": self.store is not None,
                    "degraded": self.degraded_since is not None,
                    "leases_held": len(self._held)}


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
