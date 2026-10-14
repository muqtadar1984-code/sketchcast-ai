"""Gate hardening (migration 0126): the pace and the cooldown are decided
once for the deployment, catalogue admission is one transaction, leases are
renewed, the two cooldown rules agree, and a lost store degrades to a
trickle — never to a burst."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from shared import phase_clock
from shared import provider_gate as pg
from tests.gate_fakes import GateRpcSB
from tests.test_burst_control import KIT_A, KIT_B, _figure, _kit_job
from tests.test_observer_job_guard import BOOK, GEN, _builder
from worker import client as db
import worker.run as run

IMG = {"in_flight": 3, "global_in_flight": 3, "start": 2.0, "floor": 2.0, "ceiling": 12.0,
       "step_after": 5, "cooldown": 30.0, "max_cooldown": 120.0, "degraded_in_flight": 1}


class _Clock:
    """One clock the store and every replica share (the store's own
    timestamps, as wall seconds); sleeps advance it."""
    def __init__(self, sb):
        self.sb = sb
        self.slept: list[float] = []

    def now(self):
        return self.sb.now.timestamp()

    def sleep(self, s):
        self.slept.append(float(s))
        self.sb.tick(float(s))


def _replica(sb, clock, settings=IMG, name="vertex_image"):
    g = pg.ProviderGate(name, dict(settings), clock=clock.now, wall=clock.now, sleep=clock.sleep,
                        store=pg.SupabaseStore(sb, clock=clock.now), renewer=False)
    return g


@pytest.fixture(autouse=True)
def _clean():
    pg.reset()
    run._shutdown.clear()
    with run._inflight_lock:
        run._inflight_jobs.clear()
    yield
    pg.reset()
    with run._inflight_lock:
        run._inflight_jobs.clear()
    for gid in list(phase_clock._CLOCKS):
        phase_clock.stop(gid)


# ── 1. pacing and cooldown are deployment-wide ──────────────────────────
class TestThePaceIsDecidedOnce:
    def test_two_replicas_together_send_the_learned_rate_not_twice_it(self):
        sb = GateRpcSB()
        clock = _Clock(sb)
        a, b = _replica(sb, clock), _replica(sb, clock)
        clk = phase_clock.start("gen-pace")
        # rate 2/min shared: a sends, b sends, then whoever asks next waits ~a minute
        a.release(a.acquire()); b.release(b.acquire())
        assert clock.slept == []
        a.release(a.acquire())
        assert len(clock.slept) >= 1 and sum(clock.slept) >= 59.0, "the third send waited for the window"
        assert sb.pacing["vertex_image"]["rate_per_min"] == 2.0
        assert clk.phases["pacing_wait"] == pytest.approx(sum(clock.slept))

    def test_a_refusal_on_one_replica_silences_the_other(self):
        sb = GateRpcSB()
        clock = _Clock(sb)
        a, b = _replica(sb, clock), _replica(sb, clock)
        a.outcome(False)                       # a was refused: the store records 30 s of silence
        clk = phase_clock.start("gen-cool")
        b.release(b.acquire())                 # b, which saw nothing, still waits it out
        assert sum(clock.slept) == pytest.approx(30.0, abs=0.6)
        assert clk.phases["cooldown_wait"] == pytest.approx(sum(clock.slept))
        assert b.rate == 1.0 or b.rate == 2.0  # b adopted the halved rate (floor 2) from the permit

    def test_the_send_window_is_atomic_under_concurrent_replicas(self):
        """Eight threads on four replicas against a 3/min window: exactly three
        get through without a wait."""
        sb = GateRpcSB()
        sb.pacing["vertex_image"] = {"provider": "vertex_image", "rate_per_min": 3.0,
                                     "cooldown_until": None, "successes": 0, "refusals": 0}
        granted = []
        lock = threading.Lock()

        def one(i):
            # a replica whose sleep does NOT advance time: a denied permit is a recorded wait
            g = pg.ProviderGate("vertex_image", dict(IMG, in_flight=8, global_in_flight=8),
                                clock=lambda: 1000.0, wall=lambda: 1000.0, sleep=lambda s: None,
                                store=pg.SupabaseStore(sb), renewer=False)
            p = g.store.permit("vertex_image", g.s)
            with lock:
                granted.append(p["granted"])

        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(one, range(8)))
        assert granted.count(True) == 3 and granted.count(False) == 5

    def test_a_long_wait_hands_the_lease_back_meanwhile(self):
        sb = GateRpcSB()
        clock = _Clock(sb)
        a = _replica(sb, clock)
        a.outcome(False)                       # 30 s of silence
        tok = a.acquire()
        a.release(tok)
        names = [n for n, _ in sb.calls][1:]          # after the outcome call
        # lease, permit(denied: cooldown) → release, wait, lease again, permit(granted)
        assert names[:3] == ["acquire_provider_lease", "provider_permit", "release_provider_lease"]
        assert names.count("acquire_provider_lease") == 2
        assert len(sb.leases) == 0, "nothing left held"


# ── 4. one cooldown rule in both languages ──────────────────────────────
class TestTheCooldownCapIsTheSameEverywhere:
    def test_an_hour_long_retry_after_is_capped_at_the_same_120s_in_sql_and_python(self):
        sb = GateRpcSB()
        clock = _Clock(sb)
        a = _replica(sb, clock)
        a.outcome(False, retry_after=3600.0)
        assert a.snapshot()["cooldown_left_s"] == 120.0, "Python"
        until = sb.pacing["vertex_image"]["cooldown_until"]
        assert (until - sb.now).total_seconds() == pytest.approx(120.0), "SQL (mirrored)"
        assert sb.calls[-1][1]["p_max_cooldown"] == 120.0, "the cap travels in the call"

    def test_a_shorter_cap_is_honoured_by_both(self):
        sb = GateRpcSB()
        clock = _Clock(sb)
        a = _replica(sb, clock, dict(IMG, max_cooldown=45.0))
        a.outcome(False, retry_after=90.0)
        assert a.snapshot()["cooldown_left_s"] == 45.0
        assert (sb.pacing["vertex_image"]["cooldown_until"] - sb.now).total_seconds() == pytest.approx(45.0)


# ── 3. leases are renewed while a long call runs ────────────────────────
class TestLeasesAreRenewed:
    def test_renew_once_extends_every_held_lease(self):
        sb = GateRpcSB()
        clock = _Clock(sb)
        a = _replica(sb, clock)
        t1, t2 = a.acquire(), a.acquire()
        before = {lid: v["expires_at"] for lid, v in sb.leases.items()}
        clock.sleep(100.0)                     # a 600 s script call is well under way
        assert a.renew_once() == 2
        for lid, exp in before.items():
            assert sb.leases[lid]["expires_at"] > exp
        a.release(t1); a.release(t2)
        assert sb.leases == {}

    def test_a_lease_the_store_has_swept_is_forgotten_and_the_call_runs_on(self, caplog):
        sb = GateRpcSB()
        clock = _Clock(sb)
        a = _replica(sb, clock)
        tok = a.acquire()
        clock.sleep(pg.LEASE_TTL_SECONDS + 1)  # nobody renewed: the store sweeps it
        sb.leases.clear()
        assert a.renew_once() == 0
        assert a.snapshot()["leases_held"] == 0
        a.release(tok)                         # releasing a gone lease is harmless

    def test_the_ttl_is_short_and_renewal_fits_inside_it(self):
        assert pg.LEASE_TTL_SECONDS <= 300, "a crashed holder frees its slot in minutes"
        assert pg.LEASE_RENEW_SECONDS * 2 < pg.LEASE_TTL_SECONDS, "two missed renewals still fit"


# ── a lost store degrades, never bursts ─────────────────────────────────
class TestALostStoreDegradesNeverBursts:
    def test_without_the_store_one_call_in_flight_at_the_floor_rate(self):
        sb = GateRpcSB(fail=True)
        clock = _Clock(sb)
        a = _replica(sb, clock, dict(IMG, start=8.0, floor=2.0, in_flight=3))
        a.rate = 8.0
        tok = a.acquire()
        assert tok.degraded and tok.lease_id is None and a.snapshot()["degraded"]
        assert a._local_rate() == 2.0, "the floor, not the learned 8/min"
        entered = threading.Event()

        def second():
            t = a.acquire(); entered.set(); a.release(t)
        th = threading.Thread(target=second); th.start()
        assert not entered.wait(0.2), "a second call waits: one in flight while degraded"
        a.release(tok)
        assert entered.wait(2.0); th.join(2.0)

    def test_the_gate_recovers_when_the_store_answers_again(self, caplog):
        sb = GateRpcSB(fail=True)
        clock = _Clock(sb)
        a = _replica(sb, clock)
        a.release(a.acquire())
        assert a.snapshot()["degraded"]
        sb.fail = False
        tok = a.acquire()
        assert not tok.degraded and tok.lease_id is not None and not a.snapshot()["degraded"]
        a.release(tok)

    def test_a_store_lost_mid_admission_also_degrades(self):
        sb = GateRpcSB()
        clock = _Clock(sb)
        a = _replica(sb, clock)
        real = sb._rpc_provider_permit

        def permit_then_die(p):
            sb.fail = True
            return real(p)
        sb._rpc_provider_permit = lambda p: (_ for _ in ()).throw(ConnectionError("gone"))
        tok = a.acquire()
        assert tok.degraded
        a.release(tok)


# ── 2. catalogue admission is one transaction ───────────────────────────
class TestCatalogueAdmissionIsOneTransaction:
    def _queue(self, jobs):
        sb = GateRpcSB()
        sb.tables["generations"] = [{"id": GEN, "status": "queued", "kind": "worksheet",
                                     "owner_id": "u1", "book_id": BOOK, "params": {"catalogue": True}}]
        sb.tables["jobs"] = [dict(j) for j in jobs]
        return sb

    def test_eight_workers_claiming_together_start_exactly_one_kit(self):
        sb = self._queue([_kit_job(f"{k}-{t}", t, k, created=f"{i}")
                          for i, (k, t) in enumerate([(KIT_A, "worksheet"), (KIT_B, "worksheet"),
                                                      (KIT_A, "activity"), (KIT_B, "activity"),
                                                      (KIT_A, "deck"), (KIT_B, "deck")])])
        start = threading.Barrier(8)
        got = []
        lock = threading.Lock()

        def worker():
            start.wait()
            j = db.claim_catalogue_kit_job(sb, [], 1)
            with lock:
                got.append(j)

        ths = [threading.Thread(target=worker) for _ in range(8)]
        for t in ths: t.start()
        for t in ths: t.join()
        claimed = [j for j in got if j]
        kits = {j["params"]["kit_id"] for j in claimed}
        assert kits == {KIT_A}, f"only the oldest kit may start, got {kits}"
        assert len(claimed) == 3, "kit A's three jobs were claimed, one per worker"
        assert all(j["status"] == "queued" for j in sb.tables["jobs"] if j["params"]["kit_id"] == KIT_B)

    def test_the_observer_cap_holds_under_simultaneous_claims(self):
        sb = self._queue([_figure(f"fig-{i}") for i in range(8)])
        start = threading.Barrier(8)
        got = []
        lock = threading.Lock()

        def worker():
            start.wait()
            j = db.claim_catalogue_observer_job(sb, ["figure_render"], 2)
            with lock:
                got.append(j)

        ths = [threading.Thread(target=worker) for _ in range(8)]
        for t in ths: t.start()
        for t in ths: t.join()
        assert sum(1 for j in got if j) == 2
        assert sum(1 for j in sb.tables["jobs"] if j["status"] == "processing") == 2

    def test_a_deferred_kit_job_is_not_claimed_before_its_time(self):
        later = db._stamp(db.datetime.now(db.timezone.utc) + db.timedelta(hours=1))
        sb = self._queue([{**_kit_job("a-w", "worksheet", KIT_A), "params": {"catalogue": True, "kit_id": KIT_A, "deferred_until": later}}])
        sb.now = db.datetime.now(db.timezone.utc)
        assert db.claim_catalogue_kit_job(sb, [], 1) is None

    def test_the_claim_mirrors_the_generation_to_processing(self):
        sb = self._queue([_kit_job("a-w", "worksheet", KIT_A)])
        j = db.claim_catalogue_kit_job(sb, [], 1)
        assert j and j["status"] == "processing"
        assert sb.tables["generations"][0]["status"] == "processing"

    def test_the_lanes_use_the_transactional_claims(self, monkeypatch):
        import inspect
        src = inspect.getsource(run._claim_catalogue_generation)
        assert "claim_catalogue_kit_job" in src and "catalogue_kits_processing" not in src
        assert "claim_catalogue_observer_job" in inspect.getsource(run.run_once)

    def test_teachers_keep_priority_while_the_store_is_down(self, monkeypatch):
        """The database answering no catalogue claim must not touch a teacher's
        lane: her worksheet is claimed through the ordinary query."""
        monkeypatch.setattr("catalogue.kit.catalogue_window_open", lambda *a, **k: True)
        monkeypatch.setattr("catalogue.figures.builder_queued", lambda sb, *a, **k: False)
        monkeypatch.setattr(run, "process_generation", lambda sb, job, gid: db.finish_job(sb, job["id"], gid))
        sb = self._queue([_kit_job("a-w", "worksheet", KIT_A), _builder("job-w", "worksheet"), _figure("fig-1")])
        sb.fail = True
        assert run.run_once(sb) is True
        by_id = {j["id"]: j for j in sb.tables["jobs"]}
        assert by_id["job-w"]["status"] == "done"
        assert by_id["a-w"]["status"] == "queued" and by_id["fig-1"]["status"] == "queued", "fail closed"
        assert run.run_once(sb) is False
