"""Guard tests for the support agent — the rules a reviewer must see hold:

* SCOPE: the agent refuses content the reporter doesn't own (books are checked
  INDEPENDENTLY of the generation — a foreign book_id on an owned generation
  is refused), students can never invoke it.
* ASSIGNED-CONTENT: regeneration never touches shares; an assigned artifact
  yields `regenerated_pending`; retry never re-runs a non-failed or assigned
  generation (re-running overwrites artifacts in place).
* LOOP CAP: counted from platform_issues (reporter-immutable), so deleting
  regenerated rows cannot reset it.
* ROLLBACK: a reindex that commits nothing restores books.chapters exactly.
* CONFIDENCE: low-confidence diagnoses never auto-regenerate.
* DIAGNOSIS PARSING: model output is validated against known vocabularies.
* USAGE: set_job_usage merges additively (inline re-index spend survives).

All DB access is stubbed; no network, no Claude calls.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from support_agent.bundle import ScopeViolation, assert_scope
from support_agent import actions
from support_agent.diagnose import CATEGORIES, ACTIONS


# ── stub supabase ─────────────────────────────────────────────────────────────
class _Q:
    def __init__(self, sb, table):
        self._sb = sb
        self._table = table

    # chain no-ops
    def select(self, *a, **k):
        return self

    def eq(self, *a):
        return self

    def neq(self, *a):
        return self

    def in_(self, *a):
        return self

    def gte(self, *a):
        return self

    def limit(self, *a):
        return self

    def order(self, *a, **k):
        return self

    def maybe_single(self):
        return self

    def single(self):
        return self

    def insert(self, row):
        self._sb.inserts.append((self._table, row))
        self._insert_row = row
        return self

    def update(self, row):
        self._sb.updates.append((self._table, row))
        return self

    def execute(self):
        if hasattr(self, "_insert_row"):
            row = dict(self._insert_row)
            row.setdefault("id", f"new-{self._table}")
            return SimpleNamespace(data=[row])
        return SimpleNamespace(data=self._sb.tables.get(self._table))


class FakeSB:
    def __init__(self, tables):
        self.tables = tables
        self.inserts = []
        self.updates = []

    def table(self, name):
        return _Q(self, name)


def _profile_sb(profiles_by_call):
    """FakeSB whose profiles queries return, in order, the given dicts."""

    class SB(FakeSB):
        def __init__(self):
            super().__init__({})
            self._i = 0

        def table(self, name):
            q = _Q(self, name)
            if name == "profiles":
                data = profiles_by_call[min(self._i, len(profiles_by_call) - 1)]
                self._i += 1
                q.execute = lambda d=data: SimpleNamespace(data=d)  # type: ignore[method-assign]
            return q

    return SB()


# ── scope ─────────────────────────────────────────────────────────────────────
def test_scope_owner_passes():
    sb = _profile_sb([{"role": "teacher", "school_id": None}])
    assert_scope(sb, "u1", {"owner_id": "u1"}, None)  # no raise


def test_scope_student_reporter_refused():
    sb = _profile_sb([{"role": "student", "school_id": "s1"}])
    with pytest.raises(ScopeViolation):
        assert_scope(sb, "kid", {"owner_id": "kid"}, None)


def test_scope_stranger_refused():
    sb = _profile_sb([{"role": "teacher", "school_id": "s1"}, {"school_id": "s1"}])
    with pytest.raises(ScopeViolation):
        assert_scope(sb, "stranger", {"owner_id": "owner"}, None)


def test_scope_cross_school_admin_refused():
    sb = _profile_sb([{"role": "school_admin", "school_id": "school-A"}, {"school_id": "school-B"}])
    with pytest.raises(ScopeViolation):
        assert_scope(sb, "adminA", {"owner_id": "teacherB"}, None)


def test_scope_same_school_admin_passes():
    sb = _profile_sb([{"role": "school_admin", "school_id": "school-A"}, {"school_id": "school-A"}])
    assert_scope(sb, "adminA", {"owner_id": "teacherA"}, None)  # no raise


def test_scope_foreign_book_on_owned_generation_refused():
    # THE cross-tenant primitive: reporter owns the generation, but its book_id
    # points at another tenant's book. The book must be checked independently.
    sb = _profile_sb([
        {"role": "teacher", "school_id": "school-A"},  # reporter
        {"school_id": "school-B"},                      # book owner's school
    ])
    with pytest.raises(ScopeViolation):
        assert_scope(
            sb,
            "u1",
            {"owner_id": "u1", "book_id": "bX"},
            {"owner_id": "victim", "school_id": "school-B"},
        )


def test_scope_school_library_book_passes():
    # A same-school library book is legitimate.
    sb = _profile_sb([
        {"role": "teacher", "school_id": "school-A"},  # reporter
        {"school_id": "school-A"},                      # gen owner's school (consistency)
    ])
    assert_scope(
        sb,
        "u1",
        {"owner_id": "u1", "book_id": "b1"},
        {"owner_id": "colleague", "school_id": "school-A"},
    )


def test_scope_no_content_refused():
    with pytest.raises(ScopeViolation):
        assert_scope(_profile_sb([{"role": "teacher"}]), "u1", None, None)


# ── regeneration guards ───────────────────────────────────────────────────────
_GEN = {"id": "g1", "owner_id": "u1", "book_id": "b1", "chapter_ref": "3",
        "kind": "presentation", "school_id": None, "params": {}, "status": "error"}
_BOOK = {"id": "b1", "owner_id": "u1", "storage_path": "x", "chapters": [{"num": 0}], "title": "T", "status": "ready"}
_ISSUE = {"id": "i1", "reporter_id": "u1"}
_HIGH = {"confidence": 0.9}


def test_regen_loop_cap_blocks(monkeypatch):
    # Two prior regenerations recorded on platform_issues (reporter-immutable)
    # → blocked, and index_book must NOT run. Deleting generations can't reset
    # this counter because it isn't derived from generations at all.
    sb = FakeSB({"platform_issues": [{"id": "p1"}, {"id": "p2"}]})
    called = []
    monkeypatch.setattr("worker.process.index_book", lambda *a, **k: called.append(1))
    out = actions.reindex_and_regenerate(sb, _ISSUE, _GEN, _BOOK, _HIGH, client=None, job_id="j1")
    assert out["action"] == "regen_blocked_cap"
    assert not called
    assert not sb.inserts


def test_regen_low_confidence_blocks(monkeypatch):
    sb = FakeSB({"platform_issues": []})
    monkeypatch.setattr("worker.process.index_book", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not index")))
    out = actions.reindex_and_regenerate(sb, _ISSUE, _GEN, _BOOK, {"confidence": 0.5}, client=None, job_id="j1")
    assert out["action"] == "regen_blocked_confidence"
    assert not sb.inserts


def _patch_regen_path(monkeypatch, verify_ok=True):
    monkeypatch.setattr("worker.process.index_book", lambda *a, **k: None)
    monkeypatch.setattr(
        "support_agent.bundle._chapter_source_text",
        lambda sb, book, ref, tmp: ("chapter text", {"stored_chapter": {"title": "Real Title"}}),
    )
    monkeypatch.setattr(
        "agent1_ingestion.chapter_check.verify_chapter_content",
        lambda title, text, client: (verify_ok, "" if verify_ok else "another topic"),
    )


class _RegenSB(FakeSB):
    def __init__(self, tables, assigned):
        super().__init__(tables)
        self._assigned = assigned

    def table(self, name):
        q = _Q(self, name)
        if name == "generation_shares":
            q.execute = lambda: SimpleNamespace(data=[{"id": "share1"}] if self._assigned else [])  # type: ignore[method-assign]
        elif name == "books" and not any(t == "books" for t, _ in self.updates):
            # select("*") path returns the book; updates still recorded via _Q
            orig_execute = q.execute

            def exec_books():
                if hasattr(q, "_insert_row"):
                    return orig_execute()
                return SimpleNamespace(data=_BOOK)

            q.execute = exec_books  # type: ignore[method-assign]
        return q


def test_regen_assigned_content_goes_pending(monkeypatch):
    sb = _RegenSB({"platform_issues": []}, assigned=True)
    _patch_regen_path(monkeypatch, verify_ok=True)
    out = actions.reindex_and_regenerate(sb, _ISSUE, _GEN, _BOOK, _HIGH, client=None, job_id="j1")
    assert out["action"] == "regenerated_pending"
    assert out["assigned"] is True
    tables_written = {t for t, _ in sb.inserts}
    assert "generation_shares" not in tables_written, "regeneration must never touch shares"
    gen_inserts = [row for t, row in sb.inserts if t == "generations"]
    assert len(gen_inserts) == 1 and gen_inserts[0]["chapter_ref"] == "3"


def test_regen_unassigned_auto_resolves(monkeypatch):
    sb = _RegenSB({"platform_issues": []}, assigned=False)
    _patch_regen_path(monkeypatch, verify_ok=True)
    out = actions.reindex_and_regenerate(sb, _ISSUE, _GEN, _BOOK, _HIGH, client=None, job_id="j1")
    assert out["action"] == "regenerated"
    # the old row is cross-linked, never deleted
    gen_updates = [row for t, row in sb.updates if t == "generations"]
    assert any("superseded_by" in (row.get("params") or {}) for row in gen_updates)


def test_regen_blocked_verify_rolls_back_book(monkeypatch):
    # Verify fails after reindex → the book's stored split must be restored
    # exactly (student headings render from books.chapters).
    sb = _RegenSB({"platform_issues": []}, assigned=False)
    _patch_regen_path(monkeypatch, verify_ok=False)
    out = actions.reindex_and_regenerate(sb, _ISSUE, _GEN, _BOOK, _HIGH, client=None, job_id="j1")
    assert out["action"] == "regen_blocked_verify"
    assert not [t for t, _ in sb.inserts if t == "generations"], "must not regenerate an unfixed split"
    book_updates = [row for t, row in sb.updates if t == "books"]
    assert any(row.get("chapters") == _BOOK["chapters"] and row.get("status") == "ready" for row in book_updates), \
        "books.chapters must be rolled back on the blocked path"


# ── transient retry guards ────────────────────────────────────────────────────
def test_retry_refuses_non_failed_generation():
    sb = FakeSB({"jobs": [{"id": 1}], "generation_shares": []})
    done_gen = dict(_GEN, status="done")
    assert actions.retry_transient(sb, done_gen) == "not_failed"
    assert not sb.inserts, "a completed generation must never be re-run in place"


def test_retry_refuses_assigned_generation():
    sb = FakeSB({"jobs": [{"id": 1}], "generation_shares": [{"id": "share1"}]})
    assert actions.retry_transient(sb, _GEN) == "assigned_blocked"
    assert not sb.inserts


def test_retry_transient_capped():
    sb = FakeSB({"jobs": [{"id": 1}, {"id": 2}, {"id": 3}], "generation_shares": []})
    assert actions.retry_transient(sb, _GEN) == "retry_cap_reached"
    assert not sb.inserts


def test_retry_transient_requeues_with_kind_type():
    sb = FakeSB({"jobs": [{"id": 1}], "generation_shares": []})
    assert actions.retry_transient(sb, _GEN) == "requeued"
    job_rows = [row for t, row in sb.inserts if t == "jobs"]
    assert job_rows and job_rows[0]["type"] == "presentation"


class _TypedJobsSB(FakeSB):
    """A FakeSB whose jobs rows carry a `type` and whose `.eq("type", …)` is
    HONOURED, so a test can tell whether the cap counted the diagnosis rows.
    The plain FakeSB ignores every filter, which is exactly how the original
    over-count stayed green."""

    def table(self, name):
        q = _Q(self, name)
        if name != "jobs":
            return q
        q._type = None

        def eq(col, val, _q=q):
            if col == "type":
                _q._type = val
            return _q

        def execute(_q=q):
            if hasattr(_q, "_insert_row"):
                row = dict(_q._insert_row)
                row.setdefault("id", "new-jobs")
                return SimpleNamespace(data=[row])
            rows = list(self.tables.get("jobs") or [])
            if _q._type is not None:
                rows = [r for r in rows if r.get("type") == _q._type]
            return SimpleNamespace(data=rows)

        q.eq = eq  # type: ignore[method-assign]
        q.execute = execute  # type: ignore[method-assign]
        return q


def _builder(i):
    return {"id": f"b{i}", "type": "presentation", "generation_id": _GEN["id"]}


def _diagnosis(i):
    return {"id": f"d{i}", "type": "support_diagnose", "generation_id": _GEN["id"]}


def test_retry_cap_ignores_the_agents_own_diagnosis_rows():
    """The prod shape after ONE failure with the support agent on: the builder
    job plus its diagnosis job (and, on a manual report, a second one). Those
    rows share the generation_id but they are not attempts; counting them
    closed the cap after a single real retry."""
    sb = _TypedJobsSB({"jobs": [_builder(1), _diagnosis(1), _diagnosis(2)], "generation_shares": []})
    assert actions.retry_transient(sb, _GEN) == "requeued"
    assert [row for t, row in sb.inserts if t == "jobs"], "a retry must have been queued"


def test_retry_cap_allows_two_real_retries_then_stops():
    """MAX_TRANSIENT_RETRIES = 2 means two retries AFTER the original build:
    1 builder row → retry; 2 builder rows → retry; 3 → cap. Diagnosis rows
    interleaved everywhere must not move that line."""
    two = [_builder(1), _diagnosis(1), _builder(2), _diagnosis(2)]
    sb = _TypedJobsSB({"jobs": two, "generation_shares": []})
    assert actions.retry_transient(sb, _GEN) == "requeued"

    three = two + [_builder(3), _diagnosis(3)]
    sb = _TypedJobsSB({"jobs": three, "generation_shares": []})
    assert actions.retry_transient(sb, _GEN) == "retry_cap_reached"
    assert not sb.inserts


def test_retry_counts_only_this_generations_own_kind():
    """The filter is type = the generation's kind, so a worksheet's builder
    rows never count against a presentation's cap (and vice versa) even if a
    row somehow carried the other type."""
    sb = _TypedJobsSB({"jobs": [_builder(1), dict(_builder(2), type="worksheet"),
                                dict(_builder(3), type="worksheet")], "generation_shares": []})
    assert actions.retry_transient(sb, _GEN) == "requeued"
    queued = [row for t, row in sb.inserts if t == "jobs"]
    assert queued and queued[0]["type"] == "presentation"


# ── usage merge ───────────────────────────────────────────────────────────────
def test_set_job_usage_merges_additively():
    from worker import client as wc

    class SB(FakeSB):
        def table(self, name):
            q = _Q(self, name)
            if name == "jobs" and not hasattr(q, "_insert_row"):
                orig = q.execute

                def exec_jobs():
                    if hasattr(q, "_insert_row"):
                        return orig()
                    return SimpleNamespace(data={"usage": {"calls": 5, "input_tokens": 100, "output_tokens": 200, "cost_usd": 1.0}})

                q.execute = exec_jobs  # type: ignore[method-assign]
            return q

    sb = SB({})
    wc.set_job_usage(sb, "j1", {"calls": 2, "input_tokens": 10, "output_tokens": 20, "cost_usd": 0.5})
    merged = [row for t, row in sb.updates if t == "jobs"][0]["usage"]
    assert merged == {"calls": 7, "input_tokens": 110, "output_tokens": 220, "cost_usd": 1.5}


# ── diagnosis parsing ─────────────────────────────────────────────────────────
def test_diagnosis_vocab_validation(monkeypatch):
    from support_agent import diagnose as dg

    class MockClient:
        def analyze(self, prompt, max_tokens=0, **k):
            return {"data": {"category": "not-a-category", "confidence": 7,
                             "recommended_action": "rm -rf", "user_message": "hi", "staff_note": "n"}}

    monkeypatch.setattr(dg, "_gate_signals", lambda bundle, client: {})
    out = dg.diagnose(MockClient(), {"chapters": []})
    assert out["category"] == "unknown"
    assert out["recommended_action"] == "escalate"
    assert out["confidence"] == 1.0  # clamped
    assert out["category"] in CATEGORIES and out["recommended_action"] in ACTIONS


def test_gate_ground_truth_forces_reindex(monkeypatch):
    # The model timidly escalates, but the validation gate concretely found the
    # source slice wrong (and the artifact fine) → code forces reindex_regenerate.
    from support_agent import diagnose as dg

    class MockClient:
        def analyze(self, prompt, max_tokens=0, **k):
            return {"data": {"category": "wrong_chapter_slicing", "confidence": 0.6,
                             "recommended_action": "escalate", "user_message": "u", "staff_note": "s"}}

    monkeypatch.setattr(dg, "_gate_signals", lambda bundle, client: {
        "source_matches_title": False, "artifact_matches_title": True})
    out = dg.diagnose(MockClient(), {"chapters": []})
    assert out["recommended_action"] == "reindex_regenerate"
    assert out["confidence"] >= 0.85


def test_gate_artifact_mismatch_stays_escalated(monkeypatch):
    # Source is fine but the ARTIFACT drifted → NOT a reindex case; respect the
    # model's escalate (reindexing wouldn't fix generation drift).
    from support_agent import diagnose as dg

    class MockClient:
        def analyze(self, prompt, max_tokens=0, **k):
            return {"data": {"category": "generation_drift", "confidence": 0.7,
                             "recommended_action": "escalate", "user_message": "u", "staff_note": "s"}}

    monkeypatch.setattr(dg, "_gate_signals", lambda bundle, client: {
        "source_matches_title": True, "artifact_matches_title": False})
    out = dg.diagnose(MockClient(), {"chapters": []})
    assert out["recommended_action"] == "escalate"


# ── escalation notifies staff ─────────────────────────────────────────────────
def test_escalate_sends_staff_email(monkeypatch):
    # "Escalated to the SketchCast team" must actually reach the team: the
    # escalate path fires a staff email (a triaged status nobody reads is not
    # an escalation).
    pytest.importorskip("supabase")  # agent → worker.client needs it
    from support_agent import agent as agent_mod

    calls = []
    monkeypatch.setattr(agent_mod, "notify_staff", lambda issue, reason: calls.append((issue["id"], reason)))
    sb = FakeSB({})
    issue = {"id": "iss-1", "category": "video", "severity": "normal", "title": "t"}
    agent_mod._escalate(sb, issue, {"user_message": "u"}, "no safe automatic action")

    assert calls == [("iss-1", "no safe automatic action")]
    # and the issue row was still updated to triaged/escalated
    assert any(t == "platform_issues" and r.get("agent_action") == "escalated" for t, r in sb.updates)


def test_notify_staff_skips_cleanly_without_key(monkeypatch):
    # Without RESEND_API_KEY the email is skipped (logged), never raised — an
    # unconfigured mailer must not crash the agent run.
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    actions.notify_staff({"id": "iss-2", "category": "other"}, "reason")  # no raise


def test_notify_staff_posts_console_link(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "test-key")
    monkeypatch.setenv("SUPPORT_STAFF_EMAIL", "team@example.com")
    import requests

    sent = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        sent["url"] = url
        sent["json"] = json
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(requests, "post", fake_post)
    actions.notify_staff(
        {"id": "iss-3", "category": "video", "severity": "high", "title": "Wrong chapter", "description": "d"},
        "agent crashed: boom",
    )
    assert sent["url"] == "https://api.resend.com/emails"
    assert sent["json"]["to"] == ["team@example.com"]
    assert "https://app.sketchcast.app/console/issues/iss-3" in sent["json"]["text"]
    assert "agent crashed: boom" in sent["json"]["text"]


# ── the deck lives on a sibling generation now ───────────────────────────────
def _lesson(part="1"):
    return {"id": "pres-1", "kind": "presentation", "owner_id": "u1", "book_id": "b1",
            "chapter_ref": "3", "params": {"part": part}}


def test_sibling_deck_is_found_for_the_same_unit_newest_first():
    from support_agent.bundle import _sibling_deck_artifact
    sb = FakeSB({
        "generations": [
            {"id": "deck-old", "book_id": "b1", "chapter_ref": "3", "params": {"part": "1"},
             "created_at": "2026-09-04T09:00:00Z"},
            {"id": "deck-new", "book_id": "b1", "chapter_ref": "3", "params": {"part": 1},
             "created_at": "2026-09-04T10:00:00Z"},
        ],
        "artifacts": [{"kind": "deck_pptx", "storage_path": "u1/deck-new/deck.pptx"}],
    })
    art = _sibling_deck_artifact(sb, _lesson("1"))
    assert art == {"kind": "deck_pptx", "storage_path": "u1/deck-new/deck.pptx"}


def test_sibling_deck_of_another_part_or_chapter_is_not_borrowed():
    from support_agent.bundle import _sibling_deck_artifact
    sb = FakeSB({
        "generations": [
            {"id": "deck-p2", "book_id": "b1", "chapter_ref": "3", "params": {"part": 2}},
            {"id": "deck-c4", "book_id": "b1", "chapter_ref": "4", "params": {"part": 1}},
        ],
        "artifacts": [{"kind": "deck_pptx", "storage_path": "u1/deck-p2/deck.pptx"}],
    })
    assert _sibling_deck_artifact(sb, _lesson("1")) is None


def test_sibling_deck_lookup_failure_is_best_effort():
    from support_agent.bundle import _sibling_deck_artifact

    class Broken:
        def table(self, name):
            raise RuntimeError("db down")
    assert _sibling_deck_artifact(Broken(), _lesson()) is None


# ── an internal error is a bug, never the teacher's PDF ──────────────────────

def test_a_pipeline_exception_escalates_without_a_model_call(monkeypatch):
    """Production, 2026-09-25: four maths documents died on "'tuple' object has
    no attribute 'is_Symbol'"; the model diagnosed a camera scan, wrote a
    "please rescan" message, and the reindex path ran for nothing."""
    from support_agent import diagnose as dg

    class MockClient:
        def analyze(self, prompt, max_tokens=0, **k):
            raise AssertionError("no model call for a code error")

    monkeypatch.setattr(dg, "_gate_signals", lambda bundle, client: (_ for _ in ()).throw(AssertionError("no gate")))
    bundle = {"chapters": [], "issue": {"context": {"error": "'tuple' object has no attribute 'is_Symbol'"}},
              "recent_jobs": [{"status": "error", "error": "'tuple' object has no attribute 'is_Symbol'"}]}
    out = dg.diagnose(MockClient(), bundle)
    assert out["recommended_action"] == "escalate"
    assert out["category"] == "unknown"
    assert "is_Symbol" in out["staff_note"]
    assert "rescan" not in out["user_message"].lower()
    assert "nothing is wrong with your book" in out["user_message"]


def test_a_content_error_still_goes_to_the_model(monkeypatch):
    from support_agent import diagnose as dg
    calls = []

    class MockClient:
        def analyze(self, prompt, max_tokens=0, **k):
            calls.append(prompt)
            return {"data": {"category": "corrupt_pdf", "confidence": 0.9,
                             "recommended_action": "user_fix", "user_message": "u", "staff_note": "s"}}

    monkeypatch.setattr(dg, "_gate_signals", lambda bundle, client: {})
    bundle = {"chapters": [], "recent_jobs": [{"status": "error", "error": "No readable text in the PDF"}]}
    assert dg.internal_error(bundle) == ""
    out = dg.diagnose(MockClient(), bundle)
    assert calls and out["recommended_action"] == "user_fix"


def test_an_unreadable_model_reply_is_a_pipeline_error_too():
    from support_agent import diagnose as dg
    err = ("Script generation produced no segments for episode 1: 8297 chars, output_tokens=1965 billed "
           "across attempts (cap 30000), provider did NOT report truncation — so this is malformed JSON")
    assert dg.internal_error({"recent_jobs": [{"status": "error", "error": err}]}).startswith("Script generation")


# ── a scanned book is not an empty book ───────────────────────────────────────

_SCAN_BOOK = {"id": "b-scan", "title": "Class 6.1 To 6.4", "storage_path": "u/scan.pdf", "status": "ready",
              "chapters": [{"num": 0, "title": "Class 6.1 To 6.4", "start_page": 0, "end_page": 4}],
              "health": {"band": "fair", "score": 61,
                         "facts": {"pages": 5, "chapters": 1, "has_text_layer": False, "text_readable": True}}}


def _no_extraction(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the PDF must not be extracted on this path")
    monkeypatch.setattr("worker.client.download_book", lambda *a, **k: Path("/nonexistent.pdf"))
    monkeypatch.setattr("agent1_ingestion.extractor.extract_pdf", boom)


def test_a_cached_transcription_is_the_source_for_a_scanned_chapter(monkeypatch, tmp_path):
    from support_agent.bundle import _chapter_source_text
    _no_extraction(monkeypatch)
    sb = FakeSB({"chapter_grounding": [{"source_text": "Large numbers. Place value up to crores. 58,672 is ..."}]})
    text, meta = _chapter_source_text(sb, _SCAN_BOOK, "0", tmp_path)
    assert text.startswith("Large numbers. Place value")
    assert meta["source"] == "cached_transcription" and meta["scanned"] is True
    assert meta["stored_chapter"]["title"] == "Class 6.1 To 6.4"


def test_a_scanned_chapter_without_a_transcription_reads_as_nothing_not_as_noise(monkeypatch, tmp_path):
    """The failure of 2026-09-25: the text-layer extraction of a camera scan
    is scanner noise; handed to the gate it reads as 'No readable content,
    only scanner artifacts' and the model tells the teacher to rescan a
    book the pipeline generated from perfectly well."""
    from support_agent import diagnose as dg
    from support_agent.bundle import _chapter_source_text, _health_summary
    from types import SimpleNamespace
    monkeypatch.setattr("worker.client.download_book", lambda *a, **k: Path("/nonexistent.pdf"))
    monkeypatch.setattr("agent1_ingestion.extractor.extract_pdf",
                        lambda path: SimpleNamespace(items=[SimpleNamespace(text="|| ~~ .. ,, || scanner")],
                                                     total_pages=5))
    monkeypatch.setattr("agent1_ingestion.structurer.structure_book",
                        lambda **k: (_ for _ in ()).throw(AssertionError("noise must not be sliced")))
    sb = FakeSB({"chapter_grounding": []})
    text, meta = _chapter_source_text(sb, _SCAN_BOOK, "0", tmp_path)
    assert text == "" and meta["scanned"] is True and meta["source"] == "none"
    assert "not evidence" in meta["note"]
    # the gate stays silent, so nothing forces the reindex path
    bundle = {"chapters": _SCAN_BOOK["chapters"], "generation": {"chapter_ref": "0"},
              "source_text": text, "source_meta": meta, "book": {"health": _health_summary(_SCAN_BOOK)}}
    signals = dg._gate_signals(bundle, client=object())
    assert "source_matches_title" not in signals
    # and the model is told what it is looking at
    prompt = dg._compact(bundle)
    assert "'scanned': True" in prompt and "text_readable" in prompt


def test_a_book_with_a_text_layer_is_still_sliced(monkeypatch, tmp_path):
    from support_agent.bundle import _chapter_source_text
    from types import SimpleNamespace
    book = {**_SCAN_BOOK, "health": {"facts": {"has_text_layer": True, "text_readable": True}}}
    monkeypatch.setattr("worker.client.download_book", lambda *a, **k: Path("/nonexistent.pdf"))
    monkeypatch.setattr("agent1_ingestion.extractor.extract_pdf",
                        lambda path: SimpleNamespace(items=[SimpleNamespace(text="x" * 300)], total_pages=5))
    monkeypatch.setattr("agent1_ingestion.structurer.structure_book",
                        lambda **k: SimpleNamespace(model_dump=lambda: {"chapters": [
                            {"chapter_num": 0, "sections": [{"content": "Real chapter prose.", "subsections": []}]}]}))
    sb = FakeSB({"chapter_grounding": []})
    text, meta = _chapter_source_text(sb, book, "0", tmp_path)
    assert text == "Real chapter prose." and meta["source"] == "text_layer" and meta["scanned"] is False


# ── every resolution reaches the client ──────────────────────────────────────

def test_the_resolution_email_says_what_was_done_and_invites_a_reply():
    text = actions.resolution_text("worksheet", "The checker misread 58,672 as a decimal; fixed and regenerated.")
    assert text.startswith("Hi,\n\nThe problem with your worksheet on SketchCast has been addressed.")
    assert "misread 58,672" in text
    assert "reply to this email" in text and text.endswith("SketchCast AI")


def test_notify_owner_replies_to_a_person_not_to_noreply(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "test-key")
    monkeypatch.setenv("SUPPORT_STAFF_EMAIL", "team@example.com")
    import requests
    sent = {}
    monkeypatch.setattr(requests, "post", lambda url, headers=None, json=None, timeout=None: sent.update(json) or SimpleNamespace(status_code=200))

    class SB:
        class auth:
            class admin:
                @staticmethod
                def get_user_by_id(uid):
                    return SimpleNamespace(user=SimpleNamespace(email="teacher@example.com" if uid == "u1" else "kid@students.sketchcast.app"))

    assert actions.notify_owner(SB(), "u1", "Subject", "Body") is True
    assert sent["to"] == ["teacher@example.com"] and sent["reply_to"] == "team@example.com"
    assert sent["from"].endswith("<noreply@sketchcast.app>")
    sent.clear()
    assert actions.notify_owner(SB(), "student-1", "Subject", "Body") is False and not sent


class _AgentSB(FakeSB):
    """generations / books answer a single row, as maybe_single() does."""

    def __init__(self, gen):
        super().__init__({"platform_issues": [], "platform_audit_log": []})
        self._gen = gen

    def table(self, name):
        q = _Q(self, name)
        if name == "generations":
            q.execute = lambda: SimpleNamespace(data=self._gen)  # type: ignore[method-assign]
        elif name == "books":
            q.execute = lambda: SimpleNamespace(data=None)  # type: ignore[method-assign]
        return q


def _drive(monkeypatch, action, *, outcome="requeued"):
    pytest.importorskip("supabase")
    from support_agent import agent as agent_mod
    mails = []
    monkeypatch.setattr(agent_mod, "assemble_bundle", lambda sb, issue: {"chapters": []})
    monkeypatch.setattr(agent_mod, "diagnose", lambda client, bundle: {
        "category": "transient_error" if action == "retry_transient" else "corrupt_pdf", "confidence": 0.9,
        "user_message": "Please upload a text PDF of the same pages.", "staff_note": "s",
        "recommended_action": action, "gate_signals": {}})
    monkeypatch.setattr(agent_mod, "retry_transient", lambda sb, gen: outcome)
    monkeypatch.setattr(agent_mod, "notify_owner", lambda sb, owner, subject, text: mails.append((owner, subject, text)) or True)
    monkeypatch.setattr(agent_mod, "notify_staff", lambda issue, reason: None)
    gen = {"id": "g1", "kind": "exam_paper", "owner_id": "u1", "book_id": None, "status": "error"}
    sb = _AgentSB(gen)
    issue = {"id": "iss-9", "category": "generation_failed", "generation_id": "g1", "reporter_id": "u1"}
    agent_mod._run(sb, {"id": "j1"}, issue, client=None)
    return sb, mails


def test_a_self_heal_retry_tells_the_owner(monkeypatch):
    sb, mails = _drive(monkeypatch, "retry_transient")
    assert [m[0] for m in mails] == ["u1"]
    assert "rebuilding your test paper" in mails[0][1]
    assert "queued it again" in mails[0][2] and "reply to this email" in mails[0][2]
    assert any(t == "platform_issues" and r.get("status") == "resolved" for t, r in sb.updates)


def test_a_user_fix_reaches_the_owner_with_the_advice(monkeypatch):
    sb, mails = _drive(monkeypatch, "user_fix")
    assert len(mails) == 1 and mails[0][0] == "u1"
    assert "test paper" in mails[0][1]
    assert "Please upload a text PDF" in mails[0][2] and "reply to this email" in mails[0][2]


def test_a_refused_retry_escalates_and_does_not_claim_a_fix(monkeypatch):
    sb, mails = _drive(monkeypatch, "retry_transient", outcome="retry_cap_reached")
    assert mails == []
    assert any(t == "platform_issues" and r.get("agent_action") == "escalated" for t, r in sb.updates)
