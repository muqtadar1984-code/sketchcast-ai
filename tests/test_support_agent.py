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
