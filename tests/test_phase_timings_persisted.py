"""process_generation records params.timings on the generation row — on
success AND on failure — and the phase calls land in the expected phases."""
from __future__ import annotations

import pytest

from shared import phase_clock
from worker import client as db
from worker import process

GEN = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(autouse=True)
def _pin_db(monkeypatch):
    monkeypatch.setattr(process, "db", db)
    yield
    for gid in list(phase_clock._CLOCKS):
        phase_clock.stop(gid)
    phase_clock._CURRENT.set(None)


def _capture(monkeypatch):
    merged: list[tuple[str, dict]] = []
    monkeypatch.setattr(db, "merge_generation_params",
                        lambda sb, gid, patch: merged.append((gid, patch)))
    return merged


def _job(created="2026-10-13T14:46:05.907854+00:00"):
    return {"id": "job-1", "type": "presentation", "generation_id": GEN, "created_at": created}


def test_a_finished_generation_persists_its_phases_and_queue_wait(monkeypatch):
    merged = _capture(monkeypatch)

    def fake_generate(sb, job, generation_id):
        assert phase_clock.current() == generation_id, "the clock is open before the work"
        with phase_clock.phase("script"):
            pass
        phase_clock.add("compose", 42)
        phase_clock.count("rate_limit_retries", 3)

    monkeypatch.setattr(process, "_generate", fake_generate)
    process.process_generation(object(), _job(), GEN)

    assert len(merged) == 1 and merged[0][0] == GEN
    t = merged[0][1]["timings"]
    assert t["outcome"] == "done"
    assert t["phases"]["compose"] == 42 and "script" in t["phases"]
    assert t["counts"] == {"rate_limit_retries": 3}
    # created a year ago → the queue wait is the age of the row, in seconds
    assert t["phases"]["queue_wait"] > 3600
    assert phase_clock.get(GEN) is None, "the clock is closed after the job"


def test_a_failed_generation_still_persists_where_its_minutes_went(monkeypatch):
    merged = _capture(monkeypatch)

    def fake_generate(sb, job, generation_id):
        phase_clock.add("images_warm", 180)
        raise RuntimeError("images incomplete: 1/12 planned")

    monkeypatch.setattr(process, "_generate", fake_generate)
    with pytest.raises(RuntimeError, match="images incomplete"):
        process.process_generation(object(), _job(), GEN)

    t = merged[0][1]["timings"]
    assert t["outcome"] == "error" and t["phases"]["images_warm"] == 180
    assert phase_clock.get(GEN) is None


def test_an_unparseable_created_at_records_no_queue_wait(monkeypatch):
    merged = _capture(monkeypatch)
    monkeypatch.setattr(process, "_generate", lambda sb, job, gid: None)
    process.process_generation(object(), _job(created="not a stamp"), GEN)
    assert "queue_wait" not in merged[0][1]["timings"]["phases"]


def test_recording_failure_never_fails_the_lesson(monkeypatch):
    def boom(sb, gid, patch):
        raise ConnectionError("supabase down")

    monkeypatch.setattr(db, "merge_generation_params", boom)
    monkeypatch.setattr(process, "_generate", lambda sb, job, gid: None)
    process.process_generation(object(), _job(), GEN)   # no raise
    assert phase_clock.get(GEN) is None


def test_uploads_are_accounted_to_the_upload_phase(monkeypatch, tmp_path):
    pc = phase_clock.start(GEN)
    f = tmp_path / "lesson.mp4"
    f.write_bytes(b"x")
    monkeypatch.setattr(db, "_upload_artifact_now", lambda sb, p, d: d)
    assert db.upload_artifact(object(), f, "o/g/lesson.mp4") == "o/g/lesson.mp4"
    assert "upload" in pc.phases


def test_render_threads_bound_by_bind_generation_account_to_the_lesson():
    """The composer binds its pool threads through raster_assets.bind_generation;
    the phase clock must travel with the image budget."""
    from concurrent.futures import ThreadPoolExecutor

    from spike.scene_engine.raster_assets import bind_generation, reset_image_budget

    reset_image_budget(GEN)
    pc = phase_clock.start(GEN)

    def render_one():
        phase_clock.add("render", 3)
        return phase_clock.current()

    with ThreadPoolExecutor(max_workers=1) as ex:
        assert ex.submit(bind_generation(render_one)).result() == GEN
    assert pc.phases == {"render": 3}
