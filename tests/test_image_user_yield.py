"""The never-starve hook in the scene engine (catalogue Phase 3, review fix
2026-09-06): a catalogue kit registers a per-generation yield hook and every
image generation of that lesson asks it before the transport; a user's own
lesson registers nothing and is never delayed.

Every provider here is a fake. Nothing in this file may make a network call.
"""

from __future__ import annotations

import threading

import pytest

from spike.scene_engine import raster_assets as ra

KIT = "kit-lesson"


@pytest.fixture(autouse=True)
def _no_live_calls(monkeypatch, tmp_path):
    for var in ("VERTEX_PROJECT_ID", "GOOGLE_AI_API_KEY", "GEMINI_API_KEY",
                "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_APPLICATION_CREDENTIALS_JSON",
                "AISTUDIO_IMAGE_FALLBACK", "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"):
        monkeypatch.delenv(var, raising=False)
    import shared.visual_library as vl
    monkeypatch.setattr(vl, "LIBRARY_DIR", tmp_path / "empty_library")
    monkeypatch.setattr(ra, "CACHE_DIR", tmp_path / "empty_cache")
    ra.reset_image_budget(KIT)


def _transports(monkeypatch):
    """Both transports as recorders that produce nothing (the board degrades)."""
    calls = []
    monkeypatch.setattr(ra, "_vertex_call", lambda prompt: (calls.append("vertex"), None)[1])
    monkeypatch.setattr(ra, "_aistudio_call", lambda prompt: (calls.append("aistudio"), None)[1])
    return calls


def test_a_lesson_without_a_hook_generates_exactly_as_before(tmp_path, monkeypatch):
    calls = _transports(monkeypatch)
    assert ra.get_raster_asset("plant_cell", "a plant cell", cache_dir=tmp_path) is None
    assert calls == ["vertex", "aistudio"]
    assert ra.user_yield_state() == {"armed": False, "gave_up": False, "skipped": 0}


def test_the_hook_is_asked_before_the_transport_and_a_clear_answer_generates(tmp_path, monkeypatch):
    calls, asked = _transports(monkeypatch), []
    ra.set_user_yield(lambda what: (asked.append(what), True)[1])
    ra.get_raster_asset("plant_cell", "a plant cell", cache_dir=tmp_path)
    assert asked == ["image for 'plant_cell'"] and calls == ["vertex", "aistudio"]
    assert ra.user_yield_state() == {"armed": True, "gave_up": False, "skipped": 0}


def test_a_hook_that_gave_up_skips_the_image_charges_nothing_and_stays_given_up(tmp_path, monkeypatch):
    """A teacher's job outlasted the wait: this picture is skipped (no
    budget spent, no deferral — it was not a rate limit) and, sticky, so are
    the lesson's remaining pictures: a contended lesson does not wait the
    whole cap again for each of its thirty boards."""
    calls, asked = _transports(monkeypatch), []
    ra.set_user_yield(lambda what: (asked.append(what), False)[1])
    assert ra.get_raster_asset("plant_cell", "a plant cell", cache_dir=tmp_path) is None
    assert calls == [] and ra.image_budget_state()["n"] == 0 and ra.image_budget_state()["attempts"] == 0
    assert ra.user_yield_state() == {"armed": True, "gave_up": True, "skipped": 1}
    assert ra.asset_deferred("plant_cell") is None and not ra.asset_abandoned("plant_cell")
    assert ra.get_raster_asset("animal_cell", "an animal cell", cache_dir=tmp_path) is None
    assert len(asked) == 1 and calls == [] and ra.user_yield_state()["skipped"] == 2, "not asked again"


def test_a_users_own_lesson_in_the_same_process_is_never_delayed(tmp_path, monkeypatch):
    """WORKER_CONCURRENCY>1: the kit's hook lives in the kit's bucket; a
    teacher's lesson on a sibling thread has its own, empty one."""
    calls = _transports(monkeypatch)
    ra.set_user_yield(lambda what: False)            # the kit's lesson, this thread
    out = {}

    def teachers_lesson():
        ra.reset_image_budget("teacher-lesson")
        out["asset"] = ra.get_raster_asset("plant_cell", "a plant cell", cache_dir=tmp_path)
        out["state"] = ra.user_yield_state()

    t = threading.Thread(target=teachers_lesson)
    t.start()
    t.join()
    assert calls == ["vertex", "aistudio"], "the teacher's render asked the provider"
    assert out["state"]["armed"] is False
    assert ra.user_yield_state()["armed"] is True, "the kit's hook still stands in its own bucket"


def test_removing_the_hook_or_resetting_the_budget_disarms_it(tmp_path, monkeypatch):
    calls = _transports(monkeypatch)
    ra.set_user_yield(lambda what: False)
    ra.set_user_yield(None)
    assert ra.user_yield_state()["armed"] is False
    ra.get_raster_asset("plant_cell", "a plant cell", cache_dir=tmp_path)
    assert calls == ["vertex", "aistudio"]
    ra.set_user_yield(lambda what: False, KIT)
    assert ra.user_yield_state()["armed"] is True, "registered by generation id, found by the bound thread"
    ra.reset_image_budget(KIT)
    assert ra.user_yield_state()["armed"] is False, "a fresh lesson starts unarmed"
    ra.set_user_yield(lambda what: False, "a-lesson-not-started-yet")
    assert ra.user_yield_state()["armed"] is False, "another generation's registration is not this one's"


def test_a_broken_hook_generates_rather_than_blanking_the_board(tmp_path, monkeypatch):
    calls = _transports(monkeypatch)

    def broken(what):
        raise RuntimeError("probe died")

    ra.set_user_yield(broken)
    ra.get_raster_asset("plant_cell", "a plant cell", cache_dir=tmp_path)
    assert calls == ["vertex", "aistudio"] and ra.user_yield_state()["gave_up"] is False
