"""The Pinterest v5 client (catalogue/pinterest/api.py) and the encrypted token
store (catalogue/pinterest/tokens.py). A fake session stands in for
``requests``; nothing here reaches the network."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet

from catalogue.pinterest import api as A
from catalogue.pinterest import common as C
from catalogue.pinterest import tokens as TK
from tests.pinterest_fakes import FakeResponse, FakeSession, PinSB
from worker import client as db

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
SECRET_ACCESS = "pina_SECRET_ACCESS_TOKEN_123"
SECRET_REFRESH = "pinr_SECRET_REFRESH_TOKEN_456"


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv(TK.KEY_ENV, Fernet.generate_key().decode())
    monkeypatch.setenv(A.CLIENT_ID_ENV, "1628460")
    monkeypatch.setenv(A.CLIENT_SECRET_ENV, "client-secret")
    monkeypatch.delenv(A.BASE_ENV, raising=False)


def _api(*responses):
    session = FakeSession(list(responses))
    return A.Api("tok", session=session, sleep=lambda s: None), session


# ── api ─────────────────────────────────────────────────────────────────


def test_a_429_defers_for_the_reset_and_at_least_thirty_seconds():
    api, _ = _api(FakeResponse(429, {"code": 8, "message": "slow down"}, {"x-ratelimit-reset": "90"}))
    with pytest.raises(db.DeferredJob) as e:
        api.user_account()
    assert e.value.seconds == 90
    api, _ = _api(FakeResponse(429, {"code": 8, "message": "slow down"}, {"x-ratelimit-reset": "2"}))
    with pytest.raises(db.DeferredJob) as e:
        api.user_account()
    assert e.value.seconds == 30


def test_401_is_token_invalid_and_403_code_29_is_trial_access_only():
    api, _ = _api(FakeResponse(401, {"code": 2, "message": "Authentication failed"}))
    with pytest.raises(A.TokenInvalid) as e:
        api.user_account()
    assert e.value.code == 2 and "Authentication failed" in e.value.message
    api, _ = _api(FakeResponse(403, {"code": 29, "message": "Apps with Trial access may not create Pins"}))
    with pytest.raises(A.TrialAccessOnly) as e:
        api.create_pin({})
    assert "Trial access" in str(e.value) and "Standard access" in str(e.value)
    api, _ = _api(FakeResponse(403, {"code": 3, "message": "blocked"}))
    with pytest.raises(A.Forbidden):
        api.create_pin({})


def test_5xx_retries_twice_then_keeps_pinterests_code_and_message():
    api, s = _api(FakeResponse(503, {}), FakeResponse(502, {}), FakeResponse(200, {"username": "sketchcast"}))
    assert api.user_account()["username"] == "sketchcast" and len(s.requests) == 3
    api, s = _api(*(FakeResponse(500, {"code": 1, "message": "boom"}) for _ in range(3)))
    with pytest.raises(A.PinterestError) as e:
        api.user_account()
    assert e.value.status == 500 and e.value.code == 1 and e.value.message == "boom" and len(s.requests) == 3


def test_non_2xx_keeps_code_and_message_and_rate_headers_are_read():
    api, _ = _api(FakeResponse(400, {"code": 1, "message": "Invalid board"}, {"X-RateLimit-Remaining": "99"}))
    with pytest.raises(A.PinterestError) as e:
        api.create_board("x")
    assert e.value.status == 400 and e.value.message == "Invalid board"
    assert api.last_ratelimit == {"x-ratelimit-remaining": "99"}


def test_the_base_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv(A.BASE_ENV, "https://api-sandbox.pinterest.com/v5/")
    api, s = _api(FakeResponse(200, {"items": []}))
    api.list_board_pins("b1")
    assert s.requests[0]["url"] == "https://api-sandbox.pinterest.com/v5/boards/b1/pins"
    assert A.is_sandbox()


def test_the_token_calls_use_basic_auth_on_the_oauth_host_and_never_repr_the_token():
    api, s = _api(FakeResponse(200, {"access_token": SECRET_ACCESS, "refresh_token": SECRET_REFRESH,
                                     "expires_in": 2592000}))
    body = api.refresh("old-refresh")
    assert body["refresh_token"] == SECRET_REFRESH
    req = s.requests[0]
    assert req["url"] == "https://api.pinterest.com/v5/oauth/token" and req["headers"]["Authorization"].startswith("Basic ")
    assert req["data"]["continuous_refresh"] == "true"
    assert "tok" not in repr(api)


# ── tokens ──────────────────────────────────────────────────────────────


def _store(sb, *, access_days=20, refresh_days=50):
    TK.save(sb, TK.Token(access_token=SECRET_ACCESS, refresh_token=SECRET_REFRESH,
                         access_expires_at=NOW + timedelta(days=access_days),
                         refresh_expires_at=NOW + timedelta(days=refresh_days), scopes=["pins:write"],
                         account_username="sketchcast"), NOW)


def test_fernet_round_trip_stores_ciphertext_only():
    sb = PinSB()
    _store(sb)
    row = sb.tables[C.OAUTH][0]
    assert row["id"] == 1 and SECRET_ACCESS not in str(row) and SECRET_REFRESH not in str(row)
    got = TK.load(sb)
    assert got.access_token == SECRET_ACCESS and got.refresh_token == SECRET_REFRESH
    assert SECRET_ACCESS not in repr(got)


def test_a_wrong_key_reads_as_token_invalid(monkeypatch):
    sb = PinSB()
    _store(sb)
    monkeypatch.setenv(TK.KEY_ENV, Fernet.generate_key().decode())
    with pytest.raises(A.TokenInvalid):
        TK.load(sb)


def test_a_fresh_token_is_used_as_is():
    sb = PinSB()
    _store(sb, access_days=20)
    called = []
    assert TK.access_token(sb, now=NOW, api_factory=lambda: called.append(1)) == SECRET_ACCESS
    assert called == []


class _Refresher:
    def __init__(self, body=None, exc=None):
        self.body, self.exc, self.seen = body, exc, []

    def __call__(self):
        return self

    def refresh(self, rt):
        self.seen.append(rt)
        if self.exc:
            raise self.exc
        return self.body


def test_refresh_under_five_days_stores_the_rotated_refresh_token(caplog):
    sb = PinSB()
    _store(sb, access_days=4)
    r = _Refresher({"access_token": "pina_NEW", "refresh_token": "pinr_ROTATED", "expires_in": 2592000,
                    "refresh_token_expires_in": 5184000, "scope": "pins:read,pins:write"})
    with caplog.at_level(logging.DEBUG):
        assert TK.access_token(sb, now=NOW, api_factory=r) == "pina_NEW"
    assert r.seen == [SECRET_REFRESH]
    got = TK.load(sb)
    assert got.refresh_token == "pinr_ROTATED", "Pinterest rotates the refresh token: the new one is stored"
    assert got.refresh_expires_at == NOW + timedelta(days=60)
    assert "pina_NEW" not in caplog.text and "pinr_ROTATED" not in caplog.text and SECRET_REFRESH not in caplog.text


def test_a_refused_refresh_is_token_invalid_and_records_no_secret():
    sb = PinSB()
    _store(sb, access_days=1)
    r = _Refresher(exc=A.TokenInvalid(400, 2, "invalid refresh", "/oauth/token"))
    with pytest.raises(A.TokenInvalid) as e:
        TK.access_token(sb, now=NOW, api_factory=r)
    row = sb.tables[C.OAUTH][0]
    assert "refresh failed" in row["last_error"] and SECRET_REFRESH not in row["last_error"]
    assert SECRET_REFRESH not in str(e.value)


def test_no_token_stored_is_token_invalid():
    with pytest.raises(A.TokenInvalid):
        TK.access_token(PinSB(), now=NOW)


def test_the_sandbox_host_uses_the_sandbox_token(monkeypatch):
    sb = PinSB()
    TK.save(sb, TK.Token(SECRET_ACCESS, SECRET_REFRESH, NOW + timedelta(days=20), NOW + timedelta(days=50),
                         sandbox_access_token="pina_SANDBOX"), NOW)
    assert TK.access_token(sb, now=NOW, base="https://api-sandbox.pinterest.com/v5") == "pina_SANDBOX"
    assert TK.access_token(sb, now=NOW, base="https://api.pinterest.com/v5") == SECRET_ACCESS


def test_expiry_emails_once_a_day_under_fourteen_days(monkeypatch):
    sent = []
    monkeypatch.setattr("catalogue.auto_translate.send_email", lambda to, s, t: sent.append((to, s)) or True)
    sb = PinSB()
    sb.set_settings(enabled=True, summary_email="f@example.com")
    _store(sb, refresh_days=10)
    assert TK.check_expiry(sb, C.read_settings(sb), NOW) == "email"
    assert TK.check_expiry(sb, C.read_settings(sb), NOW + timedelta(hours=3)) is None, "one email per day"
    assert TK.check_expiry(sb, C.read_settings(sb), NOW + timedelta(days=1)) == "email"
    assert len(sent) == 2 and sent[0][0] == "f@example.com"
    assert sb.settings().get("paused") is not True


def test_expiry_under_three_days_pauses_with_the_reason(monkeypatch):
    sent = []
    monkeypatch.setattr("catalogue.auto_translate.send_email", lambda to, s, t: sent.append(s) or True)
    sb = PinSB()
    sb.set_settings(enabled=True, summary_email="f@example.com")
    _store(sb, refresh_days=2)
    assert TK.check_expiry(sb, C.read_settings(sb), NOW) == "pause"
    assert sb.settings()["paused"] is True and sb.settings()["pause_reason"] == "token_expiring"
    assert sent and "PAUSED" in sent[0]


def test_plenty_of_time_says_nothing():
    sb = PinSB()
    _store(sb, refresh_days=40)
    assert TK.check_expiry(sb, C.read_settings(sb), NOW) is None
