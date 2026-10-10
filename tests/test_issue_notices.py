"""support_agent/notices.py — one email per owner, at the end.

2026-10-02: a teacher whose slide deck failed three times, and who reported
it once by hand, got four emails about one fault inside three minutes. The
founder's direction: one email per user, a combined status update once
everything of theirs is resolved."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from support_agent import notices as N
from tests.catalogue_fakes import FakeSB, UNIQUE

UNIQUE.setdefault(N.TABLE, lambda r: r["issue_id"])

NOW = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)


def _sb(issues=(), notices=()):
    sb = FakeSB()
    sb.tables["platform_issues"] = [dict(i) for i in issues]
    sb.tables[N.TABLE] = [dict(n) for n in notices]
    sb.tables.setdefault("platform_audit_log", [])
    return sb


def _notice(issue, owner="u1", what="slide deck for \"Where's my bag?\"", note="Fixed.", age_min=30, sent=None):
    return {"issue_id": issue, "owner_id": owner, "what": what, "note": note,
            "created_at": (NOW - timedelta(minutes=age_min)).isoformat(), "sent_at": sent}


def _mails(monkeypatch, ok=True):
    sent = []
    monkeypatch.setattr(N, "notify_owner", lambda sb, owner, subject, text: sent.append((owner, subject, text)) or ok)
    return sent


class TestQueue:
    def test_a_notice_is_queued_once_per_issue(self):
        sb = _sb()
        assert N.queue_owner_notice(sb, "u1", "iss-1", "slide deck", "Fixed.") is True
        assert N.queue_owner_notice(sb, "u1", "iss-1", "slide deck", "Fixed again.") is False
        rows = sb.tables[N.TABLE]
        assert len(rows) == 1 and rows[0]["note"] == "Fixed." and rows[0]["sent_at"] is None if "sent_at" in rows[0] else True

    def test_nothing_is_queued_without_an_owner_or_an_issue(self):
        sb = _sb()
        assert N.queue_owner_notice(sb, "", "iss-1", "x", "y") is False
        assert N.queue_owner_notice(sb, "u1", "", "x", "y") is False
        assert sb.tables[N.TABLE] == []

    def test_what_carries_the_book_title(self):
        sb = _sb()
        sb.tables["books"] = [{"id": "b1", "title": "  اول انجليزي  فصل ثاني "}]
        gen = {"kind": "deck", "book_id": "b1"}
        assert N.what_for(sb, gen, {"id": "iss-1"}) == 'slide deck for "اول انجليزي فصل ثاني"'
        assert N.what_for(sb, {"kind": "worksheet"}, {"id": "iss-2"}) == "worksheet"


class TestTheRule:
    def test_not_due_while_an_issue_is_still_open(self):
        assert N.due([_notice("iss-1", age_min=60)], still_open=1, now=NOW) is False

    def test_not_due_until_the_queue_has_gone_quiet(self):
        assert N.due([_notice("iss-1", age_min=60), _notice("iss-2", age_min=2)], still_open=0, now=NOW) is False
        assert N.due([_notice("iss-1", age_min=60), _notice("iss-2", age_min=N.SETTLE_MINUTES)], still_open=0, now=NOW) is True

    def test_a_day_old_notice_goes_out_even_with_an_issue_still_open(self):
        old = _notice("iss-1", age_min=N.MAX_HOLD_HOURS * 60)
        assert N.due([old, _notice("iss-2", age_min=1)], still_open=3, now=NOW) is True


class TestTheDigest:
    def test_three_notices_about_one_deck_are_one_line_with_the_latest_sentence(self):
        subject, text = N.digest([_notice("iss-1", note="First attempt failed."),
                                  _notice("iss-2", note="Retry failed."),
                                  _notice("iss-3", note="The writer is fixed and the deck is in your library.")])
        assert subject == "About your slide deck for \"Where's my bag?\" on SketchCast"
        assert text.count("slide deck") == 1 and "The writer is fixed" in text
        assert "First attempt" not in text and "reply to this email" in text

    def test_notices_under_a_bare_category_are_never_folded_into_one(self):
        """2026-10-10: two reports made by hand both read "generation failed";
        the digest kept one sentence of two, and the report the teacher had
        just made was the one dropped."""
        subject, text = N.digest([_notice("iss-1", what="generation failed", note="The worksheet is in your library."),
                                  _notice("iss-2", what="generation failed", note="The test paper is in your library.")])
        assert subject == "An update on your SketchCast items"
        assert "The worksheet is in your library." in text and "The test paper is in your library." in text
        # a report's own title names the item, so its notices still fold
        subject, text = N.digest([_notice("iss-1", what="test paper (Class Eight · Chapter 1 · Part 3)", note="First."),
                                  _notice("iss-2", what="test paper (Class Eight · Chapter 1 · Part 3)", note="Second.")])
        assert "First." not in text and "Second." in text

    def test_a_report_made_by_hand_is_named_by_its_title(self):
        sb = _sb()
        issue = {"id": "iss-1", "title": "Test paper failed — Class Eight · Chapter 1 · Part 3", "category": "deck_docs"}
        assert N.what_for(sb, None, issue) == "test paper (Class Eight · Chapter 1 · Part 3)"
        assert N.what_for(sb, None, {"id": "iss-2", "title": "Worksheet failed", "category": "x"}) == "worksheet"
        assert N.what_for(sb, None, {"id": "iss-3", "title": "Something odd", "category": "generation_failed"}) == "generation failed"

    def test_several_items_are_numbered_and_the_open_ones_are_counted(self):
        subject, text = N.digest([_notice("iss-1", what="worksheet", note="Regenerated."),
                                  _notice("iss-2", what="test paper", note="Regenerated too.")], still_open=1)
        assert subject == "An update on your SketchCast items"
        assert "the 2 items" in text and "1. Your worksheet: Regenerated." in text and "2. Your test paper: Regenerated too." in text
        assert "1 other item of yours is still being looked at" in text


class TestFlush:
    def test_one_email_per_owner_once_everything_is_resolved(self, monkeypatch):
        sent = _mails(monkeypatch)
        issues = [{"id": f"iss-{i}", "reporter_id": "u1", "status": "resolved"} for i in range(1, 5)]
        sb = _sb(issues, [_notice("iss-1", note="a"), _notice("iss-2", note="b"), _notice("iss-3", note="c"),
                          _notice("iss-4", note="Deck regenerated.", age_min=20)])
        assert N.flush_due_notices(sb, NOW) == 1
        assert len(sent) == 1 and sent[0][0] == "u1" and "Deck regenerated." in sent[0][2]
        assert all(r["sent_at"] for r in sb.tables[N.TABLE])
        audits = [r for r in sb.tables["platform_audit_log"] if r["action"] == "issue_notice_sent"]
        assert sorted(r["target_id"] for r in audits) == ["iss-1", "iss-2", "iss-3", "iss-4"]
        assert audits[0]["detail"]["items"] == 1 and len(audits[0]["detail"]["with"]) == 3
        # a second tick sends nothing
        assert N.flush_due_notices(sb, NOW + timedelta(minutes=5)) == 0 and len(sent) == 1

    def test_held_while_the_owner_still_has_an_open_issue(self, monkeypatch):
        sent = _mails(monkeypatch)
        sb = _sb([{"id": "iss-1", "reporter_id": "u1", "status": "resolved"},
                  {"id": "iss-2", "reporter_id": "u1", "status": "triaged"}],
                 [_notice("iss-1", age_min=60)])
        assert N.flush_due_notices(sb, NOW) == 0 and sent == []
        assert sb.tables[N.TABLE][0]["sent_at"] is None

    def test_owners_are_independent(self, monkeypatch):
        sent = _mails(monkeypatch)
        sb = _sb([{"id": "iss-1", "reporter_id": "u1", "status": "resolved"},
                  {"id": "iss-2", "reporter_id": "u2", "status": "open"}],
                 [_notice("iss-1", owner="u1", age_min=60), _notice("iss-2", owner="u2", age_min=60)])
        assert N.flush_due_notices(sb, NOW) == 1 and [m[0] for m in sent] == ["u1"]

    def test_a_failed_send_leaves_the_notices_for_the_next_tick(self, monkeypatch):
        _mails(monkeypatch, ok=False)
        sb = _sb([{"id": "iss-1", "reporter_id": "u1", "status": "resolved"}], [_notice("iss-1", age_min=60)])
        assert N.flush_due_notices(sb, NOW) == 0
        assert sb.tables[N.TABLE][0]["sent_at"] is None
        assert not [r for r in sb.tables["platform_audit_log"] if r["action"] == "issue_notice_sent"]


class TestWiring:
    def test_the_housekeeping_tick_flushes_and_nothing_mails_an_owner_directly(self):
        from pathlib import Path
        import worker.run as run
        src = Path(run.__file__).read_text(encoding="utf-8")
        assert "flush_due_notices(sb)" in src
        from support_agent import agent, resolve
        for mod in (agent, resolve):
            text = Path(mod.__file__).read_text(encoding="utf-8")
            assert "notify_owner(" not in text, mod.__name__
