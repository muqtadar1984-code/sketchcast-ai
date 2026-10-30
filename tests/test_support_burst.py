"""Burst dedupe for auto-filed support issues (surge plan §4.5 items 1–2).

2026-10-22: one "Generate all" filed 17 issues, ran 17 Sonnet diagnoses and
could send 17 staff emails for three root causes. The rules a reviewer must
see hold:

* 13 identical part-map failures → 1 issue, 1 diagnosis, 0 extra emails, and
  every attached failure keeps its own raw error (staff can split later).
* the signature ignores ids, numbers and part indices but not the chapter.
* a resolved or > 2 h old issue is never attached to.
* ≥ 5 failures from one owner in 15 min that would open a NEW issue file ONE
  burst issue to staff instead (signature table + console queue link), with
  no diagnosis; later new causes attach to the burst.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from support_agent import burst
from tests.catalogue_fakes import FakeSB
from worker import run as R

T0 = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)


class _Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def tick(self, **kw):
        self.now += timedelta(**kw)


class ClockedSB(FakeSB):
    """platform_issues rows get created_at = the clock, as Postgres's default
    now() would; lease RPCs are granted (or not) and recorded."""

    def __init__(self, clock, grant_leases=True):
        super().__init__()
        self.clock = clock
        self.grant = grant_leases
        self.leases = []
        self.tables["platform_issues"] = []

    def table(self, name):
        q = super().table(name)
        if name == "platform_issues":
            orig = q.execute

            def execute():
                if q.op == "insert":
                    rows = q.payload if isinstance(q.payload, list) else [q.payload]
                    for r in rows:
                        r.setdefault("created_at", burst._stamp(self.clock()))
                return orig()

            q.execute = execute
        return q

    def rpc(self, name, params=None):
        if name in ("acquire_provider_lease", "release_provider_lease"):
            self.leases.append((name, dict(params or {})))
            data = "lease-x" if name == "acquire_provider_lease" and self.grant else None

            class _X:
                def execute(self_):
                    return type("R", (), {"data": data})()

            return _X()
        return super().rpc(name, params)


@pytest.fixture
def env(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(burst, "_now", clock)
    mails = []
    monkeypatch.setattr("support_agent.actions.send_staff_email",
                        lambda subject, text, issue_id=None: mails.append((subject, text, issue_id)))
    sb = ClockedSB(clock)
    sb.tables["books"] = [{"id": "book-1", "title": "Physics 7"}]
    return sb, clock, mails


def _fail(sb, n, error, *, owner="user-1", book="book-1"):
    gid = f"gen-{owner}-{len(sb.tables['generations']) + 1}"
    sb.tables["generations"].append({"id": gid, "owner_id": owner, "book_id": book, "kind": "presentation",
                                     "chapter_ref": "7", "params": {"part": n}})
    R._auto_file_support_issue(sb, {"id": f"job-{gid}", "type": "presentation", "generation_id": gid}, error)
    return gid


def _part_map(title, parts, part):
    return f"Chapter '{title}' has {parts} part(s) — part {parts + part} does not exist"


def _diagnoses(sb):
    return [j for j in sb.tables["jobs"] if j["type"] == "support_diagnose"]


# ── signature ────────────────────────────────────────────────────────────────
def test_the_signature_ignores_numbers_and_ids_but_not_the_chapter():
    a = burst.signature("u", "b", _part_map("Light", 3, 2))
    b = burst.signature("u", "b", _part_map("Light", 2, 4))
    c = burst.signature("u", "b", _part_map("Sound", 3, 2))
    assert a == b and a != c
    assert (burst.normalize_error("job 0f6b0c1e-3d0a-4c8e-9f3b-2b9c4f0a1d22 failed in /tmp/tmpab12x9/x.mp4")
            == burst.normalize_error("job 11111111-2222-3333-4444-555555555555 failed in /tmp/tmpzz/x.mp4"))
    # the owner and the book are part of it
    assert burst.signature("u", "b", "boom") != burst.signature("u2", "b", "boom")
    assert burst.signature("u", "b", "boom") != burst.signature("u", "b2", "boom")


# ── attach ───────────────────────────────────────────────────────────────────
def test_thirteen_identical_part_map_failures_are_one_issue_one_diagnosis_no_email(env):
    sb, clock, mails = env
    gens = []
    for i in range(13):
        gens.append(_fail(sb, i + 1, _part_map("Light", 3, i + 1)))
        clock.tick(seconds=20)
    issues = sb.tables["platform_issues"]
    assert len(issues) == 1
    assert len(_diagnoses(sb)) == 1
    assert mails == []
    ctx = issues[0]["context"]
    assert ctx["related_count"] == 12
    assert ctx["related_generation_ids"] == gens[1:]
    # the raw error of every attached failure survives, for a later split
    assert [r["error"] for r in ctx["related"]] == [_part_map("Light", 3, i + 1) for i in range(1, 13)]
    assert all(r["signature"] == ctx["signature"] for r in ctx["related"])


def test_another_chapter_is_another_issue(env):
    sb, clock, mails = env
    _fail(sb, 1, _part_map("Light", 3, 1))
    _fail(sb, 1, _part_map("Sound", 3, 1))
    assert len(sb.tables["platform_issues"]) == 2 and len(_diagnoses(sb)) == 2


def test_a_resolved_issue_is_never_attached_to(env):
    sb, clock, mails = env
    _fail(sb, 1, "boom 1")
    sb.tables["platform_issues"][0]["status"] = "resolved"
    _fail(sb, 2, "boom 2")
    assert len(sb.tables["platform_issues"]) == 2 and len(_diagnoses(sb)) == 2


def test_an_issue_older_than_two_hours_is_never_attached_to(env):
    sb, clock, mails = env
    _fail(sb, 1, "boom 1")
    clock.tick(hours=2, minutes=1)
    _fail(sb, 2, "boom 2")
    assert len(sb.tables["platform_issues"]) == 2 and len(_diagnoses(sb)) == 2


def test_another_owners_identical_failure_is_their_own_issue(env):
    sb, clock, mails = env
    _fail(sb, 1, "boom")
    _fail(sb, 1, "boom", owner="user-2")
    assert {i["reporter_id"] for i in sb.tables["platform_issues"]} == {"user-1", "user-2"}


# ── burst ────────────────────────────────────────────────────────────────────
def test_five_new_causes_in_fifteen_minutes_become_one_burst_issue_to_staff(env):
    sb, clock, mails = env
    for i, title in enumerate(["Light", "Sound", "Heat", "Force"]):
        _fail(sb, 1, _part_map(title, 3, 1))
        clock.tick(minutes=2)
    assert len(_diagnoses(sb)) == 4 and mails == []
    _fail(sb, 1, _part_map("Magnets", 3, 1))

    issues = sb.tables["platform_issues"]
    burst_issue = [i for i in issues if i["context"].get("burst")]
    assert len(burst_issue) == 1 and len(issues) == 5
    assert len(_diagnoses(sb)) == 4, "no diagnosis runs for the burst"
    b = burst_issue[0]
    assert b["status"] == "triaged" and b["agent_action"] == "escalated"
    assert b["context"]["failures_in_window"] == 5
    assert len(b["context"]["signatures"]) == 5
    assert len(mails) == 1
    subject, text, issue_id = mails[0]
    assert "[Burst]" in subject and issue_id == b["id"]
    assert "https://console.sketchcast.app/console/queue?owner=user-1" in text
    assert "Magnets" in text and "Light" in text

    # a sixth new cause attaches to the burst: no issue, no diagnosis, no mail
    clock.tick(minutes=1)
    gid = _fail(sb, 1, _part_map("Waves", 3, 1))
    assert len(sb.tables["platform_issues"]) == 5 and len(_diagnoses(sb)) == 4 and len(mails) == 1
    assert b["context"]["related_generation_ids"] == [gid] and b["context"]["related_count"] == 1


def test_sara_shape_repeats_attach_and_the_next_cause_is_the_burst(env):
    """Seven failures of one cause, then a second cause: the seven are one
    issue with one diagnosis, the second cause files the one burst email."""
    sb, clock, mails = env
    for i in range(7):
        _fail(sb, i + 1, _part_map("Light", 3, i + 1))
        clock.tick(seconds=30)
    _fail(sb, 1, "TTS request refused: quota exhausted for 120 s")
    issues = sb.tables["platform_issues"]
    assert len(issues) == 2 and len(_diagnoses(sb)) == 1 and len(mails) == 1
    table = next(i for i in issues if i["context"].get("burst"))["context"]["signatures"]
    assert sorted(r["count"] for r in table.values()) == [1, 7]


def test_failures_spread_over_more_than_fifteen_minutes_do_not_burst(env):
    sb, clock, mails = env
    for title in ["Light", "Sound", "Heat", "Force", "Magnets"]:
        _fail(sb, 1, _part_map(title, 3, 1))
        clock.tick(minutes=5)
    assert not any(i["context"].get("burst") for i in sb.tables["platform_issues"])
    assert len(_diagnoses(sb)) == 5 and mails == []


# ── the owner lease ──────────────────────────────────────────────────────────
def test_the_find_or_insert_runs_under_the_owners_lease_and_releases_it(env):
    sb, clock, mails = env
    _fail(sb, 1, "boom")
    names = [n for n, _ in sb.leases]
    assert names == ["acquire_provider_lease", "release_provider_lease"]
    assert sb.leases[0][1]["p_provider"] == "support_file:user-1" and sb.leases[0][1]["p_max"] == 1
    assert sb.leases[1][1]["p_id"] == "lease-x"


def test_a_lease_store_that_cannot_answer_still_files_the_issue(env, monkeypatch):
    sb, clock, mails = env

    def broken(name, params=None):
        raise RuntimeError("PGRST202")

    monkeypatch.setattr(sb, "rpc", broken)
    _fail(sb, 1, "boom")
    assert len(sb.tables["platform_issues"]) == 1 and len(_diagnoses(sb)) == 1


def test_a_held_lease_is_waited_for_then_given_up(monkeypatch):
    """Another thread holds the owner's lease past the wait: the caller
    proceeds with None (not ""), and never spins forever."""
    sb = ClockedSB(_Clock(), grant_leases=False)
    slept = []
    t = iter([0.0, 0.0, 1.0, 2.0, 9.0])
    monkeypatch.setattr(burst.time, "monotonic", lambda: next(t))
    out = burst.acquire_lease(sb, "support_file:u", 30, wait=8.0, sleep=slept.append)
    assert out is None and slept
