"""The Pinterest probe's pure helpers, and the fonts check end to end (no network)."""
from __future__ import annotations

import json
import urllib.parse

import pytest

from scripts import pinterest_probe as probe


def test_utm_link_carries_only_our_tags():
    link = probe.utm_link("https://sketchcast.app/topics/cells", content="static-ab12cd34", language="fr")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(link).query))
    assert q == {"utm_source": "pinterest", "utm_medium": "organic", "utm_campaign": "pin_engine",
                 "utm_content": "static-ab12cd34", "utm_term": "fr"}


def test_utm_link_appends_to_an_existing_query():
    link = probe.utm_link("https://sketchcast.app/topics/cells?topic=cells", content="x")
    assert link.startswith("https://sketchcast.app/topics/cells?topic=cells&utm_source=pinterest")


def test_authorize_url_asks_for_comma_separated_scopes_and_state():
    url = probe.authorize_url("123", "http://localhost:8765/callback", "st")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert url.startswith(probe.AUTHORIZE_URL)
    assert q["response_type"] == "code" and q["state"] == "st"
    assert q["scope"].split(",") == list(probe.SCOPES)
    assert "pins:write" in q["scope"] and "boards:write" in q["scope"]


def test_compare_links_exact_round_trip():
    sent = probe.utm_link("https://sketchcast.app/topics/cells", content="static-1")
    r = probe.compare_links(sent, sent)
    assert r["exact"] and r["utm_content_found"] and not r["utm_dropped"] and not r["added"]


def test_compare_links_reports_what_pinterest_changed():
    sent = probe.utm_link("https://sketchcast.app/topics/cells", content="static-1")
    got = sent.replace("utm_source=pinterest", "utm_source=Pinterest") + "&utm_extra=1"
    r = probe.compare_links(sent, got)
    assert not r["exact"]
    assert r["utm_changed"] == ["utm_source"]
    assert r["added"] == ["utm_extra"]
    assert r["utm_content_found"]


def test_compare_links_when_no_link_comes_back():
    assert probe.compare_links("https://x", None) == {"exact": False, "returned": False}


def test_rate_headers_keeps_only_the_three():
    h = {"x-ratelimit-limit": "100", "x-ratelimit-remaining": "99", "x-ratelimit-reset": "60", "server": "x"}
    assert probe.rate_headers(h) == {"x-ratelimit-limit": "100", "x-ratelimit-remaining": "99",
                                     "x-ratelimit-reset": "60"}


def test_redact_removes_every_token_like_field_at_any_depth():
    body = {"access_token": "a", "nested": {"refresh_token": "b", "keep": 1},
            "list": [{"code": "c" * 30}], "client_secret": "d", "scope": "pins:write"}
    out = probe.redact(body)
    text = json.dumps(out)
    assert all(v not in text for v in ('"a"', '"b"', '"' + "c" * 30 + '"', '"d"'))
    assert out["nested"]["keep"] == 1 and out["scope"] == "pins:write"


def test_report_from_a_token_call_never_holds_the_tokens(monkeypatch):
    class Resp:
        status_code = 200
        content = b"{}"

        def json(self):
            return {"access_token": "ACCESS", "refresh_token": "REFRESH", "expires_in": 2592000,
                    "refresh_token_expires_in": 5184000, "scope": "pins:write"}

    api = probe.Api("id", "secret")
    monkeypatch.setattr(api._rq, "post", lambda *a, **k: Resp())
    first = api.exchange("code", "http://localhost/callback")
    assert first["got_refresh_token"] and not first["rotated"]
    assert "ACCESS" not in json.dumps(first) and "REFRESH" not in json.dumps(first)


def test_refresh_reports_rotation(monkeypatch):
    tokens = iter(["R1", "R2"])

    class Resp:
        status_code = 200
        content = b"{}"

        def json(self):
            return {"access_token": "A", "refresh_token": next(tokens)}

    api = probe.Api("id", "secret")
    monkeypatch.setattr(api._rq, "post", lambda *a, **k: Resp())
    api.exchange("code", "r")
    assert api.refresh()["rotated"] is True


def test_fonts_probe_draws_every_script(tmp_path):
    pytest.importorskip("PIL")
    report = probe.probe_fonts(tmp_path)
    assert set(report["cards"]) == set(probe.SCRIPTS)
    assert report["cards"]["latin"]["ok"]
    assert (tmp_path / "card_latin.png").exists()
    for name, entry in report["cards"].items():
        # Either drawn, or a named failure: never a silent broken card.
        assert entry["ok"] or entry["error"], name


def test_redact_keeps_pinterests_numeric_error_code():
    out = probe.redact({"code": 283, "message": "The authorization grant is invalid"})
    assert out["code"] == 283
    assert probe.redact({"code": "a" * 40})["code"] == "<redacted>"


def test_consent_ignores_favicon_and_keeps_the_first_callback(monkeypatch):
    import threading
    import urllib.request

    seen = {}

    def fake_open(url):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        seen["state"] = q["state"]

        def browser():
            base = "http://127.0.0.1:8799"
            try:
                urllib.request.urlopen(base + "/favicon.ico", timeout=5)
            except Exception:  # noqa: BLE001 — the 404 is expected
                pass
            urllib.request.urlopen(f"{base}/callback?code=FIRSTCODE123456&state={q['state']}", timeout=5).read()

        threading.Thread(target=browser, daemon=True).start()
        return True

    monkeypatch.setattr(probe.webbrowser, "open", fake_open)
    code, redirect = probe.consent("123", 8799)
    assert code == "FIRSTCODE123456"
    assert redirect == "http://localhost:8799/callback"
    assert [h["path"] for h in probe._Catch.hits] == ["/favicon.ico", "/callback"]


def test_sandbox_calls_use_the_sandbox_token_and_production_calls_do_not(monkeypatch):
    api = probe.Api("id", "secret")
    api._access = "PROD"
    monkeypatch.setenv(probe.SANDBOX_TOKEN_ENV, "SANDBOX")
    api.base = probe.API_BASES[0]
    assert api._bearer() == "PROD"
    api.base = probe.API_BASES[1]
    assert api._bearer() == "SANDBOX"
    monkeypatch.delenv(probe.SANDBOX_TOKEN_ENV)
    assert api._bearer() == "PROD"
