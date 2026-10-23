"""Regressions from the adversarial review of W1 (findings 1, 2, 7, 8):
writes are never retried blindly, an admin's change is never overwritten,
a link that does not answer is not a dead link, and the adopt fallback
cannot take a sibling's pin."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet

from catalogue.pinterest import api as A
from catalogue.pinterest import common as C
from catalogue.pinterest import post as P
from catalogue.pinterest import tokens as TK
from tests.pinterest_fakes import FakeApi, FakeResponse, FakeSession, PinSB
from tests.test_pinterest_post import NOW, PIN, UTM, _pin, _post, _sb
from worker import client as db


@pytest.fixture(autouse=True)
def _gates(monkeypatch):
    monkeypatch.setattr(C, "builders_queued", lambda sb, exclude_job_id=None: False)
    monkeypatch.setattr("catalogue.auto_translate.send_email", lambda to, s, t: True)
    monkeypatch.setenv(TK.KEY_ENV, Fernet.generate_key().decode())
    monkeypatch.setenv(A.CLIENT_ID_ENV, "id")
    monkeypatch.setenv(A.CLIENT_SECRET_ENV, "secret")
    TK._unsaved["token"] = None
    yield
    TK._unsaved["token"] = None


# ── 1. a write is sent once ─────────────────────────────────────────────


@pytest.mark.parametrize("first", [FakeResponse(502, {"code": 1, "message": "bad gateway"}),
                                   TimeoutError("read timed out")])
def test_post_pins_is_sent_exactly_once_and_an_ambiguous_answer_is_ambiguous(first):
    session = FakeSession([first, FakeResponse(201, {"id": "would-be-a-duplicate"})])
    api = A.Api("tok", session=session, sleep=lambda s: None)
    with pytest.raises(A.AmbiguousWrite):
        api.create_pin({"title": "t"})
    assert len(session.requests) == 1, "a POST is never retried"


def test_create_board_and_section_are_sent_once_too():
    for call in (lambda a: a.create_board("x"), lambda a: a.create_section("b", "s")):
        session = FakeSession([FakeResponse(500, {}), FakeResponse(201, {"id": "dup"})])
        with pytest.raises(A.AmbiguousWrite):
            call(A.Api("tok", session=session, sleep=lambda s: None))
        assert len(session.requests) == 1


def test_an_ambiguous_create_keeps_the_token_and_the_next_run_adopts_instead_of_posting():
    sb = _sb(dry=False)
    out = _post(sb, FakeApi(raise_on={"create_pin": A.AmbiguousWrite(0, None, "timed out", "/pins")}))
    p = _pin(sb)
    assert out["step"] == "retry" and p["post_token"] and p["status"] == "approved"
    # the pin DID land; the next round's run finds it on the board
    p.update(status="scheduled", round_id="r2")
    landed = FakeApi(board_pins=[{"id": "landed", "link": f"https://sketchcast.app/topics/plant-cells?{UTM}"}])
    out = _post(sb, landed)
    assert out["how"] == "adopted:utm_content" and landed.created == [] and _pin(sb)["pinterest_pin_id"] == "landed"


# ── 2. an admin's change is never overwritten ───────────────────────────


class _RejectDuring(FakeApi):
    """An admin rejects the pin while the job is between its re-check and
    its intent write (here: during the adopt listing)."""

    def __init__(self, sb, **kw):
        super().__init__(**kw)
        self.sb = sb

    def list_board_pins(self, board_id, page_size=25):
        _pin(self.sb).update(status="rejected", reject_reason="wrong diagram", approved_by=None, approved_at=None)
        return super().list_board_pins(board_id, page_size)


def test_a_reject_before_the_intent_write_stops_the_post():
    sb = _sb(dry=False, post_token="old", post_started_at="2026-10-23T21:00:00+00:00")
    api = _RejectDuring(sb)
    out = _post(sb, api)
    assert out["step"] == "skipped" and api.created == []
    assert _pin(sb)["status"] == "rejected" and _pin(sb)["reject_reason"] == "wrong diagram"


class _RetireDuringCreate(FakeApi):
    def __init__(self, sb, exc=None, **kw):
        super().__init__(**kw)
        self.sb, self.exc = sb, exc

    def create_pin(self, payload):
        _pin(self.sb).update(status="retired")
        if self.exc:
            self.created.append(payload)
            raise self.exc
        return super().create_pin(payload)


def test_a_pin_retired_during_the_create_is_not_resurrected_but_its_id_is_kept():
    sb = _sb(dry=False)
    _post(sb, _RetireDuringCreate(sb))
    p = _pin(sb)
    assert p["status"] == "retired" and p["pinterest_pin_id"] == "pin-1", "the id is recorded; the status stands"


@pytest.mark.parametrize("exc", [A.PinterestError(400, 1, "bad"), A.TokenInvalid(401, 2, "no")])
def test_a_failure_after_a_retire_never_resurrects_the_pin(exc):
    sb = _sb(dry=False)
    _post(sb, _RetireDuringCreate(sb, exc=exc))
    assert _pin(sb)["status"] == "retired"


def test_a_dry_run_intent_is_conditional_too():
    sb = _sb(dry=True)
    _pin(sb)["status"] = "scheduled"
    real = P.recheck

    def recheck_then_reject(sb_, pin, link_check=None):
        board = real(sb_, pin, link_check=link_check)
        _pin(sb).update(status="rejected", reject_reason="no")
        return board

    P_recheck = P.recheck
    try:
        P.recheck = recheck_then_reject
        out = _post(sb, FakeApi())
    finally:
        P.recheck = P_recheck
    assert out["step"] == "skipped" and _pin(sb)["status"] == "rejected" and not _pin(sb).get("pinterest_pin_id")


# ── 7. a link that does not answer is not a dead link ───────────────────


def test_a_link_that_does_not_answer_sends_the_pin_back_without_an_attempt():
    sb = _sb(dry=False, post_token="tok-kept")
    api = FakeApi()
    out = _post(sb, api, link=lambda u: C.LINK_UNKNOWN)
    p = _pin(sb)
    assert out["step"] == "later" and api.calls == []
    assert p["status"] == "approved" and p["attempts"] == 0 and p["post_token"] == "tok-kept"


def test_a_dead_link_still_fails_the_pin():
    sb = _sb(dry=False)
    assert _post(sb, FakeApi(), link=lambda u: C.LINK_DEAD)["step"] == "refused" and _pin(sb)["status"] == "failed"


class _Http:
    def __init__(self, head, get=None, raise_head=None):
        self.head_code, self.get_code, self.raise_head, self.calls = head, get, raise_head, []

    def head(self, url, **kw):
        self.calls.append("HEAD")
        if self.raise_head:
            raise self.raise_head
        return FakeResponse(self.head_code)

    def get(self, url, **kw):
        self.calls.append("GET")
        return FakeResponse(self.get_code)


def test_link_status_falls_back_to_get_and_reads_timeouts_as_unknown():
    h = _Http(405, 200)
    assert C.link_status("https://x", http=h) == C.LINK_OK and h.calls == ["HEAD", "GET"]
    assert C.link_status("https://x", http=_Http(404)) == C.LINK_DEAD
    assert C.link_status("https://x", http=_Http(503)) == C.LINK_UNKNOWN
    assert C.link_status("https://x", http=_Http(0, raise_head=TimeoutError("t"))) == C.LINK_UNKNOWN


def test_an_unchecked_link_is_a_warning_at_draft_time_not_a_block():
    from catalogue.pinterest import precheck as PC

    copy = {"title": "Plant cells", "description": "A guide for teachers.", "alt_text": "A diagram."}
    blocking, warnings = PC.copy_checks(copy, template="explainer", language="en", source_text="", link="https://x",
                                        link_check=lambda u: C.LINK_UNKNOWN)
    assert blocking == [] and any("could not be checked" in w for w in warnings)


# ── 8. the adopt fallback cannot take a sibling's pin ───────────────────


def test_the_fallback_ignores_an_id_another_row_holds_and_a_pin_outside_three_minutes():
    sb = _sb(dry=False, post_token="old", post_started_at="2026-10-23T21:00:00+00:00")
    sb.pins().append({"id": "sibling", "source_kind": "figure", "source_ref": "x", "language": "en", "format": "static",
                      "template": "diagram", "status": "posted", "pinterest_pin_id": "sib-1"})
    api = FakeApi(board_pins=[
        {"id": "sib-1", "title": "Plant cell parts", "link": "https://x", "created_at": "2026-10-23T21:01:00"},
        {"id": "late", "title": "Plant cell parts", "link": "https://x", "created_at": "2026-10-23T21:05:00"}])
    out = _post(sb, api)
    assert out["how"] == "created" and len(api.created) == 1, "neither candidate is ours: post fresh"
    assert _pin(sb)["pinterest_pin_id"] == "pin-1"


def test_a_unique_violation_on_adopt_is_not_ours_and_not_a_counted_failure(monkeypatch):
    sb = _sb(dry=False, post_token="old", post_started_at="2026-10-23T21:00:00+00:00")
    api = FakeApi(board_pins=[{"id": "racy", "title": "Plant cell parts", "link": "https://x",
                               "created_at": "2026-10-23T21:01:00"}])
    monkeypatch.setattr(P, "ids_held_elsewhere", lambda sb, pid, ids: set())   # the race: written meanwhile
    sb.pins().append({"id": "other", "source_kind": "figure", "source_ref": "y", "language": "en",
                      "format": "static", "template": "diagram", "status": "posted", "pinterest_pin_id": "racy"})
    out = _post(sb, api)
    assert out["how"] == "created" and _pin(sb)["attempts"] == 0 and _pin(sb)["pinterest_pin_id"] == "pin-1"


