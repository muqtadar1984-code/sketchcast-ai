"""Regressions from the adversarial review of W1 (finding 6):
the rotated refresh token is never lost, two threads never both refresh,
and a token endpoint that is only down is a wait, never the breaker."""

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


# ── 6. the rotated refresh token is never lost ──────────────────────────


def _store(sb, access_days=1):
    TK.save(sb, TK.Token("pina_OLD", "pinr_OLD", NOW + timedelta(days=access_days), NOW + timedelta(days=50)), NOW)


class _Refresher:
    def __init__(self, body=None, exc=None, gate=None):
        self.body, self.exc, self.gate, self.calls = body, exc, gate, 0

    def __call__(self):
        return self

    def refresh(self, rt):
        self.calls += 1
        if self.gate:
            self.gate.wait(2)
        if self.exc:
            raise self.exc
        return self.body


ROTATED = {"access_token": "pina_NEW", "refresh_token": "pinr_NEW", "expires_in": 2592000,
           "refresh_token_expires_in": 5184000}


class _FlakySB(PinSB):
    """The pinterest_oauth upsert fails ``fail`` times."""

    def __init__(self, fail):
        super().__init__()
        self.fail = fail

    def table(self, name):
        q = super().table(name)
        if name == C.OAUTH:
            outer = self
            orig = q.execute

            def execute():
                if q.op == "upsert" and outer.fail > 0:
                    outer.fail -= 1
                    raise ConnectionError("database unreachable")
                return orig()

            q.execute = execute
        return q


def test_a_failed_save_is_retried():
    sb = _FlakySB(fail=0)
    _store(sb)
    sb.fail = 2
    assert TK.access_token(sb, now=NOW, api_factory=_Refresher(ROTATED), sleep=lambda s: None) == "pina_NEW"
    assert TK.load(sb).refresh_token == "pinr_NEW" and TK._unsaved["token"] is None


def test_a_save_that_keeps_failing_holds_the_rotated_token_in_memory_until_it_lands():
    sb = _FlakySB(fail=0)
    _store(sb)
    sb.fail = 99
    r = _Refresher(ROTATED)
    assert TK.access_token(sb, now=NOW, api_factory=r, sleep=lambda s: None) == "pina_NEW"
    assert TK._unsaved["token"].refresh_token == "pinr_NEW"
    # the database comes back: the next call stores the held token, no second refresh
    sb.fail = 0
    assert TK.access_token(sb, now=NOW, api_factory=r, sleep=lambda s: None) == "pina_NEW"
    assert r.calls == 1 and TK.load(sb).refresh_token == "pinr_NEW" and TK._unsaved["token"] is None


def test_two_threads_refresh_once():
    sb = PinSB()
    _store(sb)
    gate = threading.Event()
    r = _Refresher(ROTATED, gate=gate)
    got = []
    threads = [threading.Thread(target=lambda: got.append(TK.access_token(sb, now=NOW, api_factory=r,
                                                                          sleep=lambda s: None)))
               for _ in range(2)]
    for t in threads:
        t.start()
    gate.set()
    for t in threads:
        t.join(5)
    assert got == ["pina_NEW", "pina_NEW"] and r.calls == 1


@pytest.mark.parametrize("exc", [A.TokenUnavailable(503, None, "down", "/oauth/token"),
                                 A.PinterestError(0, None, "reset", "/oauth/token")])
def test_a_token_endpoint_outage_is_a_wait_not_the_breaker(exc):
    sb = PinSB()
    _store(sb)
    with pytest.raises(db.DeferredJob):
        TK.access_token(sb, now=NOW, api_factory=_Refresher(exc=exc), sleep=lambda s: None)
    assert TK.load(sb).refresh_token == "pinr_OLD"


@pytest.mark.parametrize("status,cls", [(400, A.TokenInvalid), (401, A.TokenInvalid), (503, A.TokenUnavailable),
                                        (403, A.TokenUnavailable)])
def test_only_400_and_401_from_the_token_endpoint_are_token_invalid(status, cls):
    session = FakeSession([FakeResponse(status, {"code": 1, "message": "x"})])
    with pytest.raises(cls):
        A.Api(session=session, sleep=lambda s: None).refresh("r")
    session = FakeSession([TimeoutError("t")])
    with pytest.raises(A.TokenUnavailable):
        A.Api(session=session, sleep=lambda s: None).refresh("r")
    assert len(session.requests) == 1


def test_a_post_job_waits_out_a_token_endpoint_outage(monkeypatch):
    sb = _sb(dry=False)
    TK.save(sb, TK.Token("pina_OLD", "pinr_OLD", NOW + timedelta(days=1), NOW + timedelta(days=50)), NOW)
    monkeypatch.setattr(TK, "Api", lambda: _Refresher(exc=A.TokenUnavailable(503, None, "down", "/oauth/token")))
    with pytest.raises(db.DeferredJob):
        P.post_pin(sb, "job-1", {"pin_row_id": PIN}, link_check=lambda u: True, now=NOW)
    assert sb.settings()["breaker"]["tripped"] is False and _pin(sb)["attempts"] == 0
