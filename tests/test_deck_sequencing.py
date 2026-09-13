"""The deck runs after its lesson's video, without blocking a worker slot.

WORKER_CONCURRENCY defaults to 1: a deck job that WAITED in-process for its
sibling presentation would wait for a job that cannot start until it leaves.
So it defers — back to the queue with a wake-up time the claim respects — and
spends no attempt doing so.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_observer_job_guard import FakeSB
from worker import client as db
from worker import process


def _stamp(dt):
    return db._stamp(dt)


class TestDeferPrimitive:
    def test_defer_puts_the_job_back_with_a_wake_up_time_and_no_attempt(self):
        sb = FakeSB()
        sb.tables["jobs"] = [{"id": "j1", "type": "deck", "status": "processing", "attempts": 0,
                              "params": {"part": 3}, "progress": 40}]
        assert db.defer_job(sb, sb.tables["jobs"][0], 60, "waiting for the video")
        row = sb.tables["jobs"][0]
        assert row["status"] == "queued" and row["attempts"] == 0 and row["progress"] == 0
        assert row["params"]["part"] == 3, "existing params survive"
        assert row["params"]["deferred_since"] and row["params"]["deferred_until"] > row["params"]["deferred_since"]
        assert row["stage"]["phase"] == "waiting"

    def test_deferred_since_is_set_once(self):
        sb = FakeSB()
        sb.tables["jobs"] = [{"id": "j1", "type": "deck", "status": "processing", "params": {}}]
        db.defer_job(sb, sb.tables["jobs"][0], 60, "first")
        first = sb.tables["jobs"][0]["params"]["deferred_since"]
        sb.tables["jobs"][0]["status"] = "processing"
        db.defer_job(sb, sb.tables["jobs"][0], 60, "second")
        assert sb.tables["jobs"][0]["params"]["deferred_since"] == first

    def test_a_row_that_moved_is_not_overwritten(self):
        sb = FakeSB()
        sb.tables["jobs"] = [{"id": "j1", "type": "deck", "status": "error", "params": {}}]
        assert not db.defer_job(sb, sb.tables["jobs"][0], 60, "x")
        assert sb.tables["jobs"][0]["status"] == "error"

    def test_deferred_seconds_reads_the_first_stamp(self):
        since = _stamp(datetime.now(timezone.utc) - timedelta(minutes=5))
        assert 290 < db.deferred_seconds({"params": {"deferred_since": since}}) < 320
        assert db.deferred_seconds({"params": {}}) == 0.0


class TestTheClaimRespectsIt:
    def _sb(self, until):
        sb = FakeSB()
        sb.tables["jobs"] = [{"id": "j1", "type": "deck", "status": "queued", "created_at": "2026-09-13T00:00:00",
                              "params": {"deferred_until": until}},
                             {"id": "j2", "type": "worksheet", "status": "queued", "created_at": "2026-09-13T00:00:01",
                              "params": {}}]
        return sb

    def test_a_job_deferred_into_the_future_is_invisible(self):
        sb = self._sb(_stamp(datetime.now(timezone.utc) + timedelta(minutes=1)))
        got = db.claim_next_job(sb, catalogue=False)
        assert got and got["id"] == "j2"

    def test_a_job_whose_time_has_come_is_claimed_in_order(self):
        sb = self._sb(_stamp(datetime.now(timezone.utc) - timedelta(seconds=1)))
        got = db.claim_next_job(sb, catalogue=False)
        assert got and got["id"] == "j1"

    def test_a_never_deferred_job_is_unaffected(self):
        sb = FakeSB()
        sb.tables["jobs"] = [{"id": "j1", "type": "deck", "status": "queued", "created_at": "x", "params": {}}]
        assert db.claim_next_job(sb, catalogue=False)["id"] == "j1"


class TestFindingTheSibling:
    def _gen(self, **over):
        g = {"id": "deck-1", "kind": "deck", "owner_id": "u1", "book_id": "b1", "chapter_ref": "0",
             "created_at": "2026-09-13T10:00:00+00:00", "params": {"part": 3}}
        g.update(over)
        return g

    def test_a_book_deck_finds_the_presentation_of_the_same_part(self):
        sb = FakeSB()
        sb.tables["generations"] = [
            {"id": "p2", "kind": "presentation", "owner_id": "u1", "book_id": "b1", "chapter_ref": "0",
             "created_at": "2026-09-13T10:00:00+00:00", "params": {"part": 2}, "status": "done"},
            {"id": "p3", "kind": "presentation", "owner_id": "u1", "book_id": "b1", "chapter_ref": "0",
             "created_at": "2026-09-13T10:00:00+00:00", "params": {"part": 3}, "status": "processing"},
        ]
        sib = process._sibling_presentation(sb, self._gen())
        assert sib and sib["id"] == "p3"

    def test_a_lesson_from_last_week_is_still_this_decks_sibling(self):
        """A deck regenerated later belongs to the same lesson unit and wants
        its video's pictures. The first live regeneration (6 h 40 min after
        the video) got none under a 6-hour window."""
        sb = FakeSB()
        sb.tables["generations"] = [
            {"id": "old", "kind": "presentation", "owner_id": "u1", "book_id": "b1", "chapter_ref": "0",
             "created_at": "2026-09-01T10:00:00+00:00", "params": {"part": 3}, "status": "done"}]
        sib = process._sibling_presentation(sb, self._gen())
        assert sib and sib["id"] == "old"

    def test_a_catalogue_deck_asks_its_kit(self):
        sb = FakeSB()
        sb.tables["topic_kits"] = [{"id": "kit-1", "presentation_generation_id": "pres-1"}]
        sb.tables["generations"] = [{"id": "pres-1", "kind": "presentation", "status": "queued", "params": {}}]
        sib = process._sibling_presentation(sb, self._gen(book_id=None, params={"catalogue": True, "kit_id": "kit-1"}))
        assert sib and sib["id"] == "pres-1"

    def test_no_book_and_no_kit_means_no_sibling(self):
        assert process._sibling_presentation(FakeSB(), self._gen(book_id=None, params={})) is None


class TestWaiting:
    def _sb(self, status):
        sb = FakeSB()
        sb.tables["generations"] = [
            {"id": "p3", "kind": "presentation", "owner_id": "u1", "book_id": "b1", "chapter_ref": "0",
             "created_at": "2026-09-13T10:00:00+00:00", "params": {"part": 3}, "status": status}]
        return sb

    GEN = {"id": "deck-1", "kind": "deck", "owner_id": "u1", "book_id": "b1", "chapter_ref": "0",
           "created_at": "2026-09-13T10:00:00+00:00", "params": {"part": 3}}

    def test_a_live_video_defers_the_deck(self):
        with pytest.raises(db.DeferredJob) as ei:
            process._wait_for_sibling_presentation(self._sb("processing"), {"id": "j1", "params": {}}, self.GEN)
        assert ei.value.seconds == process.DECK_WAIT_POLL_SECONDS
        assert "waiting for the lesson video" in ei.value.note

    def test_a_finished_video_lets_it_run(self):
        process._wait_for_sibling_presentation(self._sb("done"), {"id": "j1", "params": {}}, self.GEN)

    def test_a_failed_video_lets_it_run_too(self):
        """No pictures from the video, but a deck is still owed."""
        process._wait_for_sibling_presentation(self._sb("error"), {"id": "j1", "params": {}}, self.GEN)

    def test_it_stops_waiting_after_the_maximum(self):
        long_ago = _stamp(datetime.now(timezone.utc) - timedelta(seconds=process.DECK_WAIT_MAX_SECONDS + 5))
        process._wait_for_sibling_presentation(self._sb("processing"),
                                               {"id": "j1", "params": {"deferred_since": long_ago}}, self.GEN)


class TestTheHandlerAndTheOrder:
    def test_run_handles_a_deferral_before_the_transient_error(self):
        from worker import run
        src = inspect.getsource(run)
        assert src.index("except db.DeferredJob") < src.index("except db.TransientTierError")
        assert "db.defer_job(sb, job, exc.seconds, exc.note)" in src

    def test_the_deck_waits_before_any_model_call(self):
        """The wait sits right after the generation row is read — before the
        book prelude, the analysis, or anything that costs a call."""
        src = inspect.getsource(process.process_generation)
        wait = src.index("_wait_for_sibling_presentation(sb, job, gen)")
        assert wait < src.index("_book_prelude(") and wait < src.index("_build_from_analysis(")

    def test_the_embedded_deck_is_built_after_the_render_and_before_the_upload(self):
        src = inspect.getsource(process._build_from_analysis)
        render = src.index("final = render_final_video(")
        build = src.index("_embedded_deck = str(_dg.build_lesson_deck(")
        upload = src.index('deck_path = slides.get("deck_path") or _embedded_deck')
        assert render < build < upload

    def test_video_segments_come_from_the_finished_siblings_scripts(self):
        sb = FakeSB()
        sb.tables["generations"] = [
            {"id": "p3", "kind": "presentation", "owner_id": "u1", "book_id": "b1", "chapter_ref": "0",
             "created_at": "2026-09-13T10:00:00+00:00", "params": {"part": 3}, "status": "done"}]
        sb.tables["artifacts"] = [{"generation_id": "p3", "kind": "script_json", "storage_path": "u1/p3/script.json"}]
        import json

        class _St:
            def from_(self, _b): return self
            def download(self, p): return json.dumps({"script": {"segments": [{"segment_id": "s001", "scene": {}}]}}).encode()

        sb.storage = _St()
        segs = process._sibling_video_segments(sb, TestWaiting.GEN)
        assert [s["segment_id"] for s in segs] == ["s001"]
