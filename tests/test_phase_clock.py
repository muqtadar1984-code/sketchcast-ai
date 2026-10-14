"""shared.phase_clock — the per-generation phase accumulator."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from shared import phase_clock


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    for gid in list(phase_clock._CLOCKS):
        phase_clock.stop(gid)
    phase_clock._CURRENT.set(None)


class _Fake:
    """A clock the test advances by hand."""
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def test_phases_accumulate_wall_seconds_and_the_summary_rounds():
    fake = _Fake()
    pc = phase_clock.start("gen-1", clock=fake)
    with pc.phase("script"):
        fake.now += 12.34
    with pc.phase("script"):
        fake.now += 1.0
    pc.add("upload", 0.25)
    pc.add("upload", 0.0)       # a zero adds nothing (and creates no key)
    pc.count("rate_limit_retries")
    pc.count("rate_limit_retries", 2)
    fake.now += 5
    s = pc.summary(outcome="done")
    assert s["phases"] == {"script": 13.3, "upload": 0.2}
    assert s["counts"] == {"rate_limit_retries": 3}
    assert s["total_s"] == 18.3 and s["outcome"] == "done"   # 12.34 + 1 + 5
    assert s["started_at"].endswith("+00:00")


def test_the_line_prints_phases_in_pipeline_order_then_counts():
    fake = _Fake()
    pc = phase_clock.start("gen-2", clock=fake)
    pc.add("upload", 3)
    pc.add("script", 90)
    pc.add("queue_wait", 4)
    pc.add("zzz_custom", 1)
    pc.count("images_ready", 9)
    assert pc.line() == "total=0s queue_wait=4s script=90s upload=3s zzz_custom=1s | images_ready=9"


def test_module_calls_are_no_ops_without_a_clock():
    assert phase_clock.get() is None
    with phase_clock.phase("script"):
        pass
    phase_clock.add("script", 5)
    phase_clock.count("x")
    assert phase_clock.get("nobody") is None


def test_start_makes_the_clock_current_for_this_thread_and_stop_clears_it():
    pc = phase_clock.start("gen-3")
    assert phase_clock.current() == "gen-3" and phase_clock.get() is pc
    phase_clock.add("script", 2)
    assert pc.phases["script"] == 2
    assert phase_clock.stop("gen-3") is pc
    assert phase_clock.current() is None and phase_clock.get("gen-3") is None


def test_a_pool_thread_starts_unbound_and_bind_attaches_it():
    """ThreadPoolExecutor workers begin with an empty context: without bind()
    their seconds would vanish. raster_assets.bind_generation calls bind()."""
    phase_clock.start("gen-4")
    gid = phase_clock.current()

    def unbound():
        phase_clock.add("render", 7)          # no clock here → dropped
        return phase_clock.current()

    def bound():
        tok = phase_clock.bind(gid)
        try:
            phase_clock.add("render", 5)
        finally:
            phase_clock.unbind(tok)
        return phase_clock.current()

    with ThreadPoolExecutor(max_workers=1) as ex:
        assert ex.submit(unbound).result() is None
        assert ex.submit(bound).result() is None   # reset on the way out
    assert phase_clock.get("gen-4").phases == {"render": 5}


def test_concurrent_adds_are_not_lost():
    pc = phase_clock.start("gen-5")

    def work():
        for _ in range(2000):
            pc.add("tts", 0.001)
            pc.count("calls")

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert pc.counts["calls"] == 16000
    assert abs(pc.phases["tts"] - 16.0) < 1e-6


def test_two_generations_keep_separate_accounts():
    a = phase_clock.start("gen-a")
    b = phase_clock.start("gen-b")          # now current
    phase_clock.add("script", 1)            # → b
    phase_clock.add("script", 10, "gen-a")  # explicit → a
    assert a.phases == {"script": 10} and b.phases == {"script": 1}


def test_restarting_a_generation_replaces_its_account():
    first = phase_clock.start("gen-r")
    first.add("script", 99)
    second = phase_clock.start("gen-r")
    assert second is not first and phase_clock.get("gen-r").phases == {}
