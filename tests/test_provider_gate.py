"""shared.provider_gate — admission control learned from outcomes."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from shared import phase_clock
from shared import provider_gate as pg


class _Clock:
    """Monotonic + wall time the test advances; sleeps advance it too."""
    def __init__(self):
        self.t = 1000.0
        self.slept: list[float] = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(float(s))
        self.t += float(s)


def _gate(**over):
    s = {"in_flight": 2, "global_in_flight": 2, "start": 4.0, "floor": 2.0, "ceiling": 12.0,
         "step_after": 3, "cooldown": 30.0}
    s.update(over)
    c = _Clock()
    g = pg.ProviderGate("vertex_image", s, clock=c.now, wall=c.now, sleep=c.sleep)
    return g, c


@pytest.fixture(autouse=True)
def _clean():
    pg.reset()
    yield
    pg.reset()
    for gid in list(phase_clock._CLOCKS):
        phase_clock.stop(gid)


# ── AIMD ────────────────────────────────────────────────────────────────
def test_the_rate_climbs_by_one_after_step_after_clean_calls_and_halves_on_a_refusal():
    g, _ = _gate()
    assert g.rate == 4.0
    for _ in range(3):
        g.outcome(True)
    assert g.rate == 5.0 and g.successes == 0
    g.outcome(True)
    g.outcome(True)
    assert g.rate == 5.0, "two clean calls are not yet a step"
    g.outcome(False)
    assert g.rate == 2.5 and g.successes == 0 and g.refusals == 1
    g.outcome(False)
    assert g.rate == 2.0, "never below the floor"
    for _ in range(3 * 20):
        g.outcome(True)
    assert g.rate == 12.0, "never above the ceiling"


def test_an_unpaced_gate_stays_unpaced():
    g, _ = _gate(start=0.0, floor=0.0, ceiling=0.0)
    g.outcome(True); g.outcome(True); g.outcome(True)
    g.outcome(False)
    assert g.rate == 0.0


# ── cooldown ────────────────────────────────────────────────────────────
def test_a_refusal_silences_every_caller_for_the_cooldown_and_the_wait_is_accounted():
    g, c = _gate(start=0.0, floor=0.0, ceiling=0.0)
    clk = phase_clock.start("gen-cool")
    g.outcome(False)
    assert g.snapshot()["cooldown_left_s"] == 30.0
    tok = g.acquire("test")              # sleeps the cooldown away in ≤5 s chunks
    g.release(tok)
    assert sum(c.slept) == pytest.approx(30.0, abs=0.01)
    assert clk.phases["cooldown_wait"] == pytest.approx(30.0, abs=0.01)
    assert g.snapshot()["cooldown_left_s"] == 0.0


def test_the_server_s_retry_after_lengthens_the_silence():
    g, _ = _gate()
    g.outcome(False, retry_after=90.0)
    assert g.snapshot()["cooldown_left_s"] == 90.0


def test_the_cooldown_never_shortens_a_longer_one():
    g, c = _gate()
    g.outcome(False, retry_after=90.0)
    g.outcome(False)                     # the 30 s default must not pull 90 back
    assert g.snapshot()["cooldown_left_s"] == 90.0


# ── pacing ──────────────────────────────────────────────────────────────
def test_calls_beyond_the_rate_wait_for_the_window_and_the_wait_is_accounted():
    g, c = _gate(start=2.0, floor=2.0, ceiling=4.0, cooldown=0.0)
    clk = phase_clock.start("gen-pace")
    for _ in range(2):
        g.release(g.acquire())
    assert c.slept == []
    g.release(g.acquire())               # the third in the same minute waits
    assert 59.9 < sum(c.slept) <= 60.1, "a minute, slept in bounded chunks"
    assert max(c.slept) <= 5.0
    assert clk.phases["pacing_wait"] == pytest.approx(sum(c.slept))


def test_a_learned_higher_rate_lets_more_through():
    g, c = _gate(start=2.0, floor=2.0, ceiling=4.0, step_after=1, cooldown=0.0)
    g.outcome(True); g.outcome(True)     # 2 → 3 → 4 per minute
    for _ in range(4):
        g.release(g.acquire())
    assert c.slept == []


# ── in-flight cap ───────────────────────────────────────────────────────
def test_the_in_flight_cap_blocks_the_third_caller_until_one_releases():
    g, _ = _gate(start=0.0, floor=0.0, ceiling=0.0, in_flight=2)
    t1 = g.acquire(); t2 = g.acquire()
    entered = threading.Event()

    def third():
        t = g.acquire()
        entered.set()
        g.release(t)

    th = threading.Thread(target=third); th.start()
    assert not entered.wait(0.2), "a third call must wait"
    g.release(t1)
    assert entered.wait(2.0)
    th.join(2.0); g.release(t2)


# ── the shared store ────────────────────────────────────────────────────
class _Store:
    def __init__(self, grant=True, outcome_row=None, fail=False):
        self.grant, self.row, self.fail = grant, outcome_row, fail
        self.leases: list[str] = []
        self.released: list[str] = []
        self.outcomes: list[tuple] = []
        self.denials = 0

    def permit(self, provider, s):
        if self.fail:
            return None
        return {"granted": True, "wait": 0.0, "reason": "ok", "rate": None}

    def renew_lease(self, lease_id, ttl):
        return None if self.fail else True

    def acquire_lease(self, provider, max_in_flight, holder, ttl):
        if self.fail:
            return None
        if callable(self.grant):
            ok = self.grant()
        else:
            ok = self.grant
        if not ok:
            self.denials += 1
            return {"granted": False, "id": None}
        lid = f"lease-{len(self.leases) + 1}"
        self.leases.append(lid)
        return {"granted": True, "id": lid}

    def release_lease(self, lease_id):
        self.released.append(lease_id)

    def record_outcome(self, provider, ok, s, retry_after):
        self.outcomes.append((provider, ok, retry_after))
        return self.row


def test_a_lease_is_taken_and_given_back():
    g, _ = _gate(start=0.0, floor=0.0, ceiling=0.0)
    g.store = _Store()
    tok = g.acquire()
    assert tok.lease_id == "lease-1"
    g.release(tok)
    assert g.store.released == ["lease-1"]


def test_a_denied_lease_waits_and_tries_again():
    g, c = _gate(start=0.0, floor=0.0, ceiling=0.0)
    answers = iter([False, False, True])
    g.store = _Store(grant=lambda: next(answers))
    clk = phase_clock.start("gen-lease")
    tok = g.acquire()
    assert tok.lease_id == "lease-1" and g.store.denials == 2
    assert len(c.slept) == 2 and all(2.0 <= s <= 5.0 for s in c.slept)
    assert clk.phases["lease_wait"] == pytest.approx(sum(c.slept))
    g.release(tok)


def test_an_unreachable_store_degrades_to_the_local_regime():
    g, c = _gate(start=0.0, floor=0.0, ceiling=0.0)
    g.store = _Store(fail=True)
    tok = g.acquire()
    assert tok.lease_id is None and tok.degraded and c.slept == []
    assert g.snapshot()["degraded"]
    g.release(tok)


def test_the_shared_pacing_state_is_adopted_after_every_outcome():
    g, c = _gate()
    g.store = _Store(outcome_row={"rate_per_min": "2.5", "cooldown_until": "2099-01-01T00:00:00+00:00",
                                  "successes": 1})
    g.outcome(True)
    assert g.store.outcomes == [("vertex_image", True, None)]
    assert g.rate == 2.5 and g.successes == 1
    assert g.snapshot()["cooldown_left_s"] > 0, "a peer's refusal silences this replica too"


def test_adopt_clamps_a_peer_s_rate_to_this_gate_s_floor_and_ceiling():
    g, _ = _gate()
    g.adopt({"rate_per_min": 0.1})
    assert g.rate == 2.0
    g.adopt({"rate_per_min": 99})
    assert g.rate == 12.0
    g.adopt({"rate_per_min": "not a number"})
    assert g.rate == 12.0


# ── backoff ─────────────────────────────────────────────────────────────
def test_our_ladder_spreads_across_half_the_step_and_grows():
    lo = [pg.backoff_seconds(a, rng=lambda a_, b_: 0.0) for a in range(3)]
    hi = [pg.backoff_seconds(a, rng=lambda a_, b_: b_) for a in range(3)]
    assert lo == pytest.approx([6.0, 14.4, 34.56])
    assert hi == pytest.approx([9.0, 21.6, 51.84])
    assert pg.backoff_seconds(10, rng=lambda a_, b_: 0.0) == 60.0, "capped"


def test_a_server_named_wait_is_the_base_with_a_quarter_of_jitter():
    assert pg.backoff_seconds(0, retry_after=40.0, rng=lambda a_, b_: 0.0) == 40.0
    assert pg.backoff_seconds(0, retry_after=40.0, rng=lambda a_, b_: b_) == 42.0
    assert pg.backoff_seconds(3, retry_after=40.0, rng=lambda a_, b_: b_) == 48.0
    assert pg.backoff_seconds(0, retry_after=500.0, rng=lambda a_, b_: 0.0) == 60.0, "capped"
    assert pg.backoff_seconds(0, retry_after=0.0, rng=lambda a_, b_: 0.0) == 1.0, "never zero"


def test_three_refused_slots_do_not_retry_in_lockstep():
    waits = {pg.backoff_seconds(0) for _ in range(50)}
    assert len(waits) > 10 and all(6.0 <= w <= 9.0 for w in waits)


# ── settings ────────────────────────────────────────────────────────────
def test_defaults_and_env_overrides(monkeypatch):
    for k in list(__import__("os").environ):
        if k.startswith("PROVIDER_") or k in ("MODEL_CALL_CONCURRENCY", "IMAGE_CALLS_PER_MINUTE"):
            monkeypatch.delenv(k)
    s = pg._settings("vertex_image")
    assert (s["in_flight"], s["start"], s["floor"], s["ceiling"], s["step_after"], s["cooldown"]) == \
        (3, 4.0, 2.0, 12.0, 5, 30.0)
    t = pg._settings("vertex_text")
    assert (t["in_flight"], t["start"], t["cooldown"]) == (4, 0.0, 20.0)
    monkeypatch.setenv("PROVIDER_VERTEX_IMAGE_START_PER_MIN", "3")
    monkeypatch.setenv("PROVIDER_VERTEX_IMAGE_COOLDOWN_SECS", "45")
    monkeypatch.setenv("PROVIDER_VERTEX_TEXT_IN_FLIGHT", "2")
    assert pg._settings("vertex_image")["start"] == 3.0
    assert pg._settings("vertex_image")["cooldown"] == 45.0
    assert pg._settings("vertex_text")["in_flight"] == 2


def test_the_old_image_knobs_still_count(monkeypatch):
    for k in list(__import__("os").environ):
        if k.startswith("PROVIDER_"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("MODEL_CALL_CONCURRENCY", "5")
    monkeypatch.setenv("IMAGE_CALLS_PER_MINUTE", "8")
    s = pg._settings("vertex_image")
    assert s["in_flight"] == 5 and s["global_in_flight"] == 5 and s["ceiling"] == 8.0


def test_the_registry_hands_out_one_gate_per_provider_and_configure_shares_the_store():
    a = pg.gate("vertex_image")
    assert pg.gate("vertex_image") is a and pg.gate("vertex_text") is not a
    pg.configure(object())
    assert isinstance(a.store, pg.SupabaseStore) and isinstance(pg.gate("vertex_text").store, pg.SupabaseStore)


def test_configure_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("PROVIDER_GATE_GLOBAL", "0")
    pg.configure(object())
    assert pg.gate("vertex_image").store is None
