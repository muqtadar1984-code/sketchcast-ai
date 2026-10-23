"""The ``pin_post`` job (catalogue/pinterest/post.py): dry run never calls
Pinterest, every re-check gate, adopt-on-rerun, 429 waits, the breaker on a
refused token, three attempts, the AI disclosure, and no token anywhere."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet

from catalogue.pinterest import api as A
from catalogue.pinterest import common as C
from catalogue.pinterest import post as P
from catalogue.pinterest import tokens as TK
from tests.pinterest_fakes import ADMIN, TOPIC, FakeApi, png_bytes, world
from worker import client as db

NOW = datetime(2026, 10, 23, 21, 5, tzinfo=timezone.utc)
SECRET = "pina_SECRET_ACCESS_TOKEN_xyz"
PIN = "pppppppp-1111-2222-3333-444444444444"
UTM = "utm_content=static-pppppppp"


@pytest.fixture(autouse=True)
def _gates(monkeypatch):
    monkeypatch.setattr(C, "builders_queued", lambda sb, exclude_job_id=None: False)
    monkeypatch.setattr("catalogue.auto_translate.send_email", lambda to, s, t: True)


def _sb(*, dry=True, **pin):
    sb = world()
    sb.set_settings(dry_run=dry)
    row = {"id": PIN, "source_kind": "figure", "source_ref": "fig-1", "topic_id": TOPIC, "kit_id": "kit-1",
           "language": "en", "format": "static", "template": "diagram", "board_id": "b-biology",
           "grade_band": None, "title": "Plant cell parts", "description": "The parts of a plant cell.",
           "alt_text": "A labelled plant cell.", "ai_modified": True,
           "link": f"https://sketchcast.app/topics/plant-cells?utm_source=pinterest&{UTM}&utm_term=en",
           "images": [{"path": "en/plant-cells/diagram-abc.png", "dominant_color": "#ffffff"}],
           "status": "scheduled", "approved_by": ADMIN, "approved_at": "2026-10-22T10:00:00+00:00",
           "round_id": "r1", "attempts": 0, "post_token": None, "pinterest_pin_id": None}
    row.update(pin)
    sb.pins().append(row)
    sb.files[("pinterest-pins", "en/plant-cells/diagram-abc.png")] = png_bytes((1000, 1500))
    return sb


def _post(sb, api=None, link=lambda u: True):
    return P.post_pin(sb, "job-1", {"pin_row_id": PIN, "round_id": "r1"}, api=api, link_check=link, now=NOW)


def _pin(sb):
    return next(p for p in sb.pins() if p["id"] == PIN)


def test_a_dry_run_never_calls_create_and_marks_the_pin_posted(caplog):
    sb = _sb(dry=True)
    api = FakeApi()
    with caplog.at_level(logging.INFO):
        out = _post(sb, api)
    assert api.calls == [] and api.created == [], "a dry run makes no Pinterest call at all"
    p = _pin(sb)
    assert p["status"] == "posted" and p["pinterest_pin_id"] == f"dryrun:{PIN}" and p["posted_at"]
    assert out["dry_run"] is True and "DRY RUN" in caplog.text
    assert "<pinterest-pins/en/plant-cells/diagram-abc.png, not read in a dry run>" in caplog.text
    audit = sb.tables["platform_audit_log"][0]
    assert audit["action"] == "pinterest_pin_post" and audit["actor_id"] == ADMIN and audit["detail"]["dry_run"]


def test_dry_run_is_the_default_when_the_setting_is_absent_or_odd():
    sb = _sb()
    sb.tables["platform_settings"][0]["value"].pop("dry_run")
    api = FakeApi()
    _post(sb, api)
    assert api.created == [] and _pin(sb)["pinterest_pin_id"].startswith("dryrun:")
    sb = _sb()
    sb.set_settings(dry_run="false")
    _post(sb, FakeApi())
    assert _pin(sb)["pinterest_pin_id"].startswith("dryrun:")


def test_a_live_post_creates_once_with_the_full_payload():
    sb = _sb(dry=False)
    api = FakeApi()
    out = _post(sb, api)
    assert out["how"] == "created" and len(api.created) == 1
    payload = api.created[0]
    assert payload["board_id"] == "pb-biology" and payload["title"] == "Plant cell parts"
    assert payload["alt_text"] and payload["link"].endswith("utm_term=en") and payload["dominant_color"] == "#ffffff"
    assert payload["media_source"]["source_type"] == "image_base64" and len(payload["media_source"]["data"]) > 100
    assert payload["ai_disclosures"] == {"values": ["AI_MODIFIED"]}
    p = _pin(sb)
    assert p["status"] == "posted" and p["pinterest_pin_id"] == "pin-1" and p["post_token"] and p["post_started_at"]


def test_no_ai_disclosure_on_a_text_only_pin():
    sb = _sb(dry=False, ai_modified=False)
    api = FakeApi()
    _post(sb, api)
    assert "ai_disclosures" not in api.created[0]


@pytest.mark.parametrize("change,needle", [
    (lambda sb: _pin(sb).update(status="approved"), "not scheduled"),
    (lambda sb: sb.current_kits.clear(), "no longer public"),
    (lambda sb: sb.tables["topic_kits"][0].update(status="superseded"), "no longer public"),
    (lambda sb: sb.tables["platform_admins"][0].update(revoked_at="2026-10-22T12:00:00+00:00"), "platform admin"),
    (lambda sb: sb.tables["pinterest_boards"][0].update(status="approved"), "board is not created"),
])
def test_every_recheck_failure_posts_nothing(change, needle):
    sb = _sb(dry=False)
    change(sb)
    api = FakeApi()
    out = _post(sb, api)
    assert out["step"] == "refused" and needle in out["why"] and api.calls == []
    if _pin(sb)["status"] != "approved":
        assert _pin(sb)["status"] == "failed" and needle in _pin(sb)["last_error"]


def test_a_dead_link_posts_nothing():
    sb = _sb(dry=False)
    api = FakeApi()
    out = _post(sb, api, link=lambda u: False)
    assert out["step"] == "refused" and api.calls == [] and _pin(sb)["status"] == "failed"


def test_an_interrupted_run_adopts_by_utm_content():
    sb = _sb(dry=False, status="posting", post_token="old", post_started_at="2026-10-23T21:00:00+00:00")
    api = FakeApi(board_pins=[{"id": "other", "link": "https://x"},
                              {"id": "landed", "link": f"https://sketchcast.app/topics/plant-cells?{UTM}"}])
    out = _post(sb, api)
    assert out["how"] == "adopted:utm_content" and api.created == []
    assert _pin(sb)["pinterest_pin_id"] == "landed" and _pin(sb)["status"] == "posted"


def test_the_adopt_fallback_matches_title_and_time():
    sb = _sb(dry=False, status="posting", post_token="old", post_started_at="2026-10-23T21:00:00+00:00")
    api = FakeApi(board_pins=[
        {"id": "too-old", "title": "Plant cell parts", "link": "https://x", "created_at": "2026-10-23T20:30:00"},
        {"id": "ours", "title": "Plant cell parts", "link": "https://x", "created_at": "2026-10-23T21:04:00"}])
    out = _post(sb, api)
    assert out["how"] == "adopted:title+time" and _pin(sb)["pinterest_pin_id"] == "ours" and api.created == []


def test_nothing_to_adopt_means_a_fresh_create():
    sb = _sb(dry=False, post_token="old", post_started_at="2026-10-23T21:00:00+00:00")
    api = FakeApi(board_pins=[{"id": "x", "title": "Something else", "link": "https://x"}])
    _post(sb, api)
    assert len(api.created) == 1 and _pin(sb)["post_token"] != "old"


def test_a_429_defers_and_spends_no_attempt():
    sb = _sb(dry=False)
    api = FakeApi(raise_on={"create_pin": db.DeferredJob(90, "Pinterest rate limit")})
    with pytest.raises(db.DeferredJob):
        _post(sb, api)
    p = _pin(sb)
    assert p["attempts"] == 0 and p["post_token"], "the token stays so the next run adopts"


def test_a_live_builder_defers_before_the_create_call(monkeypatch):
    sb = _sb(dry=False)
    monkeypatch.setattr(C, "builders_queued", lambda sb, exclude_job_id=None: True)
    api = FakeApi()
    with pytest.raises(db.DeferredJob) as e:
        _post(sb, api)
    assert e.value.note == C.WAITING_BUILDERS and api.created == []


@pytest.mark.parametrize("exc", [
    A.TokenInvalid(401, 2, "Authentication failed", "/pins"),
    A.TrialAccessOnly(403, 29, "Apps with Trial access may not create Pins in production", "/pins"),
    A.Forbidden(403, 3, "blocked", "/pins"),
])
def test_a_refused_token_or_403_trips_the_breaker_without_counting_an_attempt(exc):
    sb = _sb(dry=False)
    out = _post(sb, FakeApi(raise_on={"create_pin": exc}))
    p = _pin(sb)
    assert out["step"] == "breaker" and p["attempts"] == 0 and p["status"] == "approved" and p["round_id"] is None
    s = sb.settings()
    assert s["breaker"]["tripped"] is True and s["paused"] is True


def test_three_failures_end_in_failed_with_the_approval_kept():
    sb = _sb(dry=False)
    for i in range(3):
        _pin(sb).update(status="scheduled", round_id="r1")
        out = _post(sb, FakeApi(raise_on={"create_pin": A.PinterestError(500, 1, "boom", "/pins")}))
        p = _pin(sb)
        assert p["attempts"] == i + 1
        assert out["step"] == ("retry" if i < 2 else "failed")
        assert p["status"] == ("approved" if i < 2 else "failed")
    p = _pin(sb)
    assert p["approved_at"] and p["approved_by"] == ADMIN and p["post_token"], "approval and token kept"
    assert sb.settings()["breaker"]["failures"] == 3


def test_a_success_resets_the_failure_count():
    sb = _sb(dry=False)
    sb.set_settings(breaker={"tripped": False, "failures": 2})
    _post(sb, FakeApi())
    assert sb.settings()["breaker"]["failures"] == 0


def test_no_token_reaches_a_log_a_job_field_or_an_audit_row(monkeypatch, caplog):
    monkeypatch.setenv(TK.KEY_ENV, Fernet.generate_key().decode())
    sb = _sb(dry=False)
    TK.save(sb, TK.Token(SECRET, "pinr_SECRET_R", NOW + timedelta(days=20), NOW + timedelta(days=50)), NOW)
    sent = []

    class _Session:
        def request(self, method, url, **kw):
            sent.append(kw["headers"]["Authorization"])
            from tests.pinterest_fakes import FakeResponse

            return FakeResponse(500, {"code": 1, "message": "server error"})

    monkeypatch.setattr(P, "Api", lambda token: A.Api(token, session=_Session(), sleep=lambda s: None))
    job = {"id": "job-1", "type": "pin_post", "status": "processing", "params": {"pin_row_id": PIN, "round_id": "r1"}}
    sb.tables["jobs"].append(job)
    with caplog.at_level(logging.DEBUG):
        P.run_post_job(sb, job, link_check=lambda u: True)
    assert sent and all(SECRET in h for h in sent), "the token was used on the wire"
    stored = json.dumps({"jobs": sb.tables["jobs"], "pins": sb.pins(), "audit": sb.tables["platform_audit_log"],
                         "settings": sb.tables["platform_settings"]}, default=str)
    assert SECRET not in stored and "pinr_SECRET_R" not in stored
    assert SECRET not in caplog.text and "pinr_SECRET_R" not in caplog.text
    assert job["status"] == "error" and "server error" in job["error"]


def test_the_job_row_is_finished_with_the_outcome():
    sb = _sb(dry=True)
    job = {"id": "job-1", "type": "pin_post", "status": "processing", "params": {"pin_row_id": PIN, "round_id": "r1"}}
    sb.tables["jobs"].append(job)
    P.run_post_job(sb, job, api=FakeApi(), link_check=lambda u: True)
    assert job["status"] == "done" and job["stage"]["pinterest_pin_id"] == f"dryrun:{PIN}"
