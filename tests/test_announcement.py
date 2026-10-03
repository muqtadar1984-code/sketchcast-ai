"""shared/announcement.py — a product announcement to the members, in
SketchCast's own clothes, with a sample before the audience."""

from __future__ import annotations

import hashlib
import hmac
from types import SimpleNamespace

import pytest

from shared import announcement as A
from tests.catalogue_fakes import FakeSB, UNIQUE

UNIQUE.setdefault("platform_settings", lambda r: r["key"])
CAMPAIGN = "colour_and_youtube_2026_10"


class _Auth:
    def __init__(self, users):
        self._users = users
        self.admin = self

    def list_users(self, page=1, per_page=200):
        start = (page - 1) * per_page
        return [SimpleNamespace(id=i, email=e) for i, e in self._users[start:start + per_page]]


def _sb(users=(), profiles=()):
    sb = FakeSB()
    sb.auth = _Auth(list(users))
    sb.tables["profiles"] = [dict(p) for p in profiles]
    sb.tables.setdefault("platform_settings", [])
    return sb


def _job(sb, params):
    sb.tables["jobs"].append({"id": "job-1", "type": A.JOB_TYPE, "status": "processing", "attempts": 0,
                              "params": params})
    return sb.tables["jobs"][0]


class TestTheWords:
    def test_the_html_wears_the_portal_palette_and_links_the_facts(self):
        html = A.render_html(CAMPAIGN, "Rana", "https://app.sketchcast.app/api/lifecycle/unsubscribe?t=u.m")
        for token in (A.CANVAS, A.PAPER, A.INK, A.TEAL, A.TEAL_INK, A.MARKER, A.LINE, "Space Grotesk"):
            assert token in html
        assert "Hi Rana," in html and "The board just got " in html
        for v in A.CAMPAIGNS[CAMPAIGN]["videos"]:
            assert v["url"] in html and v["thumb"] in html and v["title"] in html
        for _name, pid in A.CAMPAIGNS[CAMPAIGN]["playlists"]:
            assert f"playlist?list={pid}" in html
        assert A.CHANNEL_URL in html and "sub_confirmation=1" in html
        assert 'href="https://app.sketchcast.app/api/lifecycle/unsubscribe?t=u.m"' in html
        assert "<script" not in html

    def test_without_a_name_or_a_link_the_email_says_so(self):
        html = A.render_html(CAMPAIGN, None, None)
        assert "Hi there," in html and "Unsubscribe link goes here" in html
        text = A.render_text(CAMPAIGN, None, None)
        assert "Hi there," in text and "reply with the word unsubscribe" in text and "<" not in text

    def test_the_text_twin_carries_every_link(self):
        text = A.render_text(CAMPAIGN, "Rana", "https://u/x")
        for v in A.CAMPAIGNS[CAMPAIGN]["videos"]:
            assert v["url"] in text
        assert A.CHANNEL_URL in text and "Unsubscribe: https://u/x" in text and "Muqtadar" in text


class TestThePeople:
    def test_the_unsubscribe_link_is_the_portals_own_token(self):
        mac = hmac.new(b"s3cret", b"user-1", hashlib.sha256).hexdigest()[:32]
        assert A.unsubscribe_url("user-1", "s3cret") == f"{A.APP_URL}/api/lifecycle/unsubscribe?t=user-1.{mac}"
        assert A.unsubscribe_url("user-1", "") is None

    def test_the_audience_is_members_only(self):
        sb = _sb(users=[("t1", "ann@school.org"), ("t2", "ben@school.org"), ("s1", "kid@students.sketchcast.app"),
                        ("s2", "teen@gmail.com"), ("o1", "out@school.org"), ("x1", "gone@school.org"),
                        ("d1", "demo@school.org"), ("p1", "parent@home.net"), ("n1", "noprofile@x.org")],
                 profiles=[{"id": "t1", "role": "teacher", "full_name": "Ann  Lee"},
                           {"id": "t2", "role": "teacher", "full_name": None},
                           {"id": "s1", "role": "student", "full_name": "Kid"},
                           {"id": "s2", "role": "student", "full_name": "Teen"},
                           {"id": "o1", "role": "teacher", "email_optout_at": "2026-01-01T00:00:00Z"},
                           {"id": "x1", "role": "teacher", "suspended_at": "2026-01-01T00:00:00Z"},
                           {"id": "d1", "role": "teacher", "is_demo": True},
                           {"id": "p1", "role": "parent", "full_name": "Priya K"},
                           {"id": "e1", "role": "teacher", "full_name": "No Email"}])
        out = A.audience(sb)
        assert [(r["id"], r["email"], r["first_name"]) for r in out] == [
            ("t1", "ann@school.org", "Ann"), ("t2", "ben@school.org", None), ("p1", "parent@home.net", "Priya")]

    def test_auth_pages_are_walked(self):
        users = [(f"u{i}", f"u{i}@x.org") for i in range(450)]
        sb = _sb(users=users, profiles=[{"id": f"u{i}", "role": "teacher"} for i in range(450)])
        assert len(A.audience(sb)) == 450


class TestTheSend:
    def test_a_sample_goes_to_the_named_addresses_only_and_is_not_recorded(self, monkeypatch):
        monkeypatch.delenv("LIFECYCLE_TOKEN_SECRET", raising=False)
        sb = _sb(users=[("t1", "ann@school.org")], profiles=[{"id": "t1", "role": "teacher"}])
        batches = []
        out = A.run_announcement_job(sb, _job(sb, {"campaign": CAMPAIGN, "to": ["muqtadar1984@gmail.com"]}),
                                     transport=lambda msgs: batches.append(msgs) or len(msgs))
        assert out["mode"] == "sample" and out["sent"] == 1 and out["unsubscribe_links"] is False
        assert len(batches) == 1 and batches[0][0]["to"] == ["muqtadar1984@gmail.com"]
        msg = batches[0][0]
        assert msg["from"] == A.FROM and msg["subject"] == A.CAMPAIGNS[CAMPAIGN]["subject"]
        assert "<!doctype html>" in msg["html"] and "Hi there," in msg["text"] and "headers" not in msg
        assert sb.tables["platform_settings"] == [] and sb.tables["jobs"][0]["status"] == "done"

    def test_the_audience_send_is_refused_without_the_unsubscribe_secret(self, monkeypatch):
        monkeypatch.delenv("LIFECYCLE_TOKEN_SECRET", raising=False)
        sb = _sb(users=[("t1", "ann@school.org")], profiles=[{"id": "t1", "role": "teacher"}])
        sent = []
        assert A.run_announcement_job(sb, _job(sb, {"campaign": CAMPAIGN, "audience": "members"}),
                                      transport=lambda m: sent.append(m) or len(m)) is None
        assert sent == [] and "LIFECYCLE_TOKEN_SECRET" in sb.tables["jobs"][0]["error"]

    def test_the_audience_send_batches_links_records_and_refuses_a_repeat(self, monkeypatch):
        monkeypatch.setenv("LIFECYCLE_TOKEN_SECRET", "s3cret")
        users = [(f"u{i}", f"u{i}@x.org") for i in range(230)]
        sb = _sb(users=users, profiles=[{"id": f"u{i}", "role": "teacher", "full_name": f"T{i} X"} for i in range(230)])
        batches = []
        out = A.run_announcement_job(sb, _job(sb, {"campaign": CAMPAIGN, "audience": "members"}),
                                     transport=lambda msgs: batches.append(msgs) or len(msgs))
        assert out["sent"] == 230 and out["batches"] == 3 and [len(b) for b in batches] == [100, 100, 30]
        first = batches[0][0]
        assert first["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
        assert "/api/lifecycle/unsubscribe?t=" in first["headers"]["List-Unsubscribe"] and "Hi T0," in first["text"]
        rec = sb.tables["platform_settings"][0]
        assert rec["key"] == f"announcement_{CAMPAIGN}" and rec["value"]["sent"] == 230
        # a second send of the same campaign is refused, force sends again
        sb.tables["jobs"][0]["status"] = "processing"
        assert A.run_announcement_job(sb, sb.tables["jobs"][0], transport=lambda m: len(m)) is None
        assert "already sent" in sb.tables["jobs"][0]["error"]
        sb.tables["jobs"][0]["params"]["force"] = True
        sb.tables["jobs"][0]["status"] = "processing"
        assert A.run_announcement_job(sb, sb.tables["jobs"][0], transport=lambda m: len(m))["sent"] == 230

    def test_a_dry_run_counts_and_sends_nothing(self):
        sb = _sb(users=[("t1", "ann@school.org"), ("s1", "kid@students.sketchcast.app")],
                 profiles=[{"id": "t1", "role": "teacher"}, {"id": "s1", "role": "student"}])
        out = A.send(sb, {"campaign": CAMPAIGN, "audience": "members", "dry_run": True},
                     transport=lambda m: pytest.fail("sent"))
        assert out["recipients"] == 1 and out["dry_run"] is True

    def test_an_unknown_campaign_or_no_recipients_is_refused(self):
        sb = _sb()
        with pytest.raises(RuntimeError, match="unknown campaign"):
            A.send(sb, {"campaign": "nope", "to": ["a@b.c"]})
        with pytest.raises(RuntimeError, match="say who"):
            A.send(sb, {"campaign": CAMPAIGN})


class TestWiring:
    def test_the_worker_runs_it_in_the_last_lane(self):
        from pathlib import Path
        import worker.run as run
        from worker import client as db
        assert A.JOB_TYPE in run.CATALOGUE_JOB_TYPES and A.JOB_TYPE in db.OBSERVER_JOB_TYPES
        src = Path(run.__file__).read_text(encoding="utf-8")
        assert 'job_type == "announcement_email"' in src and "run_announcement_job(sb, job)" in src
