"""A Supabase fake that answers the provider-gate RPCs of migrations 0125 and
0126 with the SQL bodies' semantics, in Python, under one lock — so the
Python wiring can be tested for atomicity (threads) and for failure
(``fail`` raises on every RPC, like a store that is down).

The SQL itself is exercised against production inside a rolled-back
transaction before each migration is applied (see the PR); this fake
mirrors it line for line so a divergence shows up as a failing test.
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone

from tests.test_observer_job_guard import FakeSB


class _Res:
    def __init__(self, data):
        self.data = data


class _ClaimCall:
    """``sb.rpc(name, params).execute()`` for the two catalogue claims on a
    plain FakeSB (no gate state): the SQL bodies' semantics in Python."""
    def __init__(self, sb, name, params):
        self.sb, self.name, self.params = sb, name, params

    def execute(self):
        if self.name == "claim_catalogue_kit_job":
            return _Res(claim_kit_job(self.sb, self.params))
        if self.name == "claim_catalogue_observer_job":
            return _Res(claim_observer_job(self.sb, self.params))
        raise RuntimeError(f"PGRST202: function public.{self.name} does not exist")


def claim_rpc(sb, name, params):
    return _ClaimCall(sb, name, params)


_CLAIM_LOCK = threading.RLock()


def _now(sb):
    return getattr(sb, "now", None) or datetime.now(timezone.utc)


def _deferred_ok(sb, j):
    until = (j.get("params") or {}).get("deferred_until")
    if not until:
        return True
    return datetime.strptime(until, "%Y-%m-%dT%H:%M:%S.%f+00:00").replace(tzinfo=timezone.utc) < _now(sb)


def _claim(sb, job):
    job["status"] = "processing"
    job["progress"] = 1
    sb.log.append(("update", "jobs", job["id"], "processing"))
    return [dict(job)]


def claim_kit_job(sb, p):
    with _CLAIM_LOCK:
        jobs = sb.tables["jobs"]
        kits = {(j.get("params") or {}).get("kit_id") for j in jobs
                if j["status"] == "processing" and str((j.get("params") or {}).get("catalogue")).lower() == "true"
                and (j.get("params") or {}).get("kit_id")}
        only = len(kits) >= max(1, p["p_max_kits"])
        excl = set(p.get("p_exclude_types") or [])
        cands = [j for j in jobs if j["status"] == "queued"
                 and str((j.get("params") or {}).get("catalogue")).lower() == "true"
                 and j["type"] not in excl and _deferred_ok(sb, j)
                 and (not only or (j.get("params") or {}).get("kit_id") in kits)]
        if not cands:
            return []
        return _claim(sb, min(cands, key=lambda j: str(j["created_at"])))


def claim_observer_job(sb, p):
    with _CLAIM_LOCK:
        jobs = sb.tables["jobs"]
        types = set(p["p_types"])
        if sum(1 for j in jobs if j["status"] == "processing" and j["type"] in types) >= max(1, p["p_max_jobs"]):
            return []
        cands = [j for j in jobs if j["status"] == "queued" and j["type"] in types and _deferred_ok(sb, j)]
        if not cands:
            return []
        return _claim(sb, min(cands, key=lambda j: str(j["created_at"])))


class _Call:
    def __init__(self, sb, name, params):
        self.sb, self.name, self.params = sb, name, params

    def execute(self):
        return _Res(self.sb._rpc(self.name, self.params))


class GateRpcSB(FakeSB):
    """FakeSB (jobs/generations tables) + the gate's RPCs + a settable clock."""

    def __init__(self, fail: bool = False):
        super().__init__()
        self.fail = fail
        self.now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
        self.leases: dict[str, dict] = {}
        self.pacing: dict[str, dict] = {}
        self.sends: dict[str, list[datetime]] = {}
        self.calls: list[tuple[str, dict]] = []
        self._lock = threading.RLock()

    def rpc(self, name, params=None):
        return _Call(self, name, dict(params or {}))

    def tick(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)

    # ── the SQL, in Python ──────────────────────────────────────────────
    def _rpc(self, name, p):
        if self.fail:
            raise ConnectionError("store down")
        with self._lock:
            self.calls.append((name, p))
            return getattr(self, "_rpc_" + name)(p)

    def _rpc_acquire_provider_lease(self, p):
        prov = p["p_provider"]
        for lid in [k for k, v in self.leases.items() if v["provider"] == prov and v["expires_at"] < self.now]:
            del self.leases[lid]
        if sum(1 for v in self.leases.values() if v["provider"] == prov) >= max(1, p["p_max"]):
            return None
        lid = str(uuid.uuid4())
        self.leases[lid] = {"provider": prov, "holder": p["p_holder"],
                            "expires_at": self.now + timedelta(seconds=max(1, p["p_ttl_seconds"]))}
        return lid

    def _rpc_renew_provider_lease(self, p):
        lease = self.leases.get(p["p_id"])
        if lease is None or lease["expires_at"] <= self.now:
            return False
        lease["expires_at"] = self.now + timedelta(seconds=max(1, p["p_ttl_seconds"]))
        return True

    def _rpc_release_provider_lease(self, p):
        self.leases.pop(p["p_id"], None)
        return None

    def _row(self, prov, start):
        return self.pacing.setdefault(prov, {"provider": prov, "rate_per_min": float(start),
                                             "cooldown_until": None, "successes": 0, "refusals": 0})

    def _rpc_provider_permit(self, p):
        prov = p["p_provider"]
        r = self._row(prov, p["p_start"])
        if r["cooldown_until"] is not None and r["cooldown_until"] > self.now:
            wait = (r["cooldown_until"] - self.now).total_seconds()
            return [{"granted": False, "wait_seconds": round(wait, 2), "reason": "cooldown",
                     "rate_per_min": r["rate_per_min"]}]
        sends = self.sends.setdefault(prov, [])
        sends[:] = [t for t in sends if t >= self.now - timedelta(seconds=60)]
        if r["rate_per_min"] > 0:
            allowed = max(1, int(r["rate_per_min"]))
            if len(sends) >= allowed:
                wait = 60 - (self.now - min(sends)).total_seconds() + 0.01
                return [{"granted": False, "wait_seconds": round(wait, 2), "reason": "pace",
                         "rate_per_min": r["rate_per_min"]}]
        sends.append(self.now)
        return [{"granted": True, "wait_seconds": 0, "reason": "ok", "rate_per_min": r["rate_per_min"]}]

    def _rpc_provider_outcome(self, p):
        prov = p["p_provider"]
        r = self._row(prov, p["p_start"])
        if p["p_ok"]:
            r["successes"] += 1
            if r["rate_per_min"] > 0 and r["successes"] >= max(1, p["p_step_after"]):
                r["rate_per_min"] = min(p["p_ceiling"], r["rate_per_min"] + 1)
                r["successes"] = 0
        else:
            r["refusals"] += 1
            r["successes"] = 0
            if r["rate_per_min"] > 0:
                r["rate_per_min"] = max(p["p_floor"], r["rate_per_min"] / 2)
            silence = min(p.get("p_max_cooldown") if p.get("p_max_cooldown") is not None else 120,
                          max(p["p_cooldown_seconds"], p.get("p_retry_after") or 0))
            base = r["cooldown_until"] if r["cooldown_until"] and r["cooldown_until"] > self.now else self.now
            r["cooldown_until"] = max(base, self.now + timedelta(seconds=silence))
        return [{"rate_per_min": r["rate_per_min"],
                 "cooldown_until": r["cooldown_until"].isoformat() if r["cooldown_until"] else None,
                 "successes": r["successes"], "refusals": r["refusals"]}]

    def _rpc_claim_catalogue_kit_job(self, p):
        return claim_kit_job(self, p)

    def _rpc_claim_catalogue_observer_job(self, p):
        return claim_observer_job(self, p)
