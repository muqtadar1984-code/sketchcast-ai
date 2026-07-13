"""Guard: the book-indexing + generation pipeline stays ROLE-AGNOSTIC.

Every adult who can upload a book / generate artifacts (teacher, principal,
coordinator, school_admin, parent-author) funnels through the SAME worker path:
a book insert → the `create_index_job_for_book` trigger → `index_book`; a
generation insert → the `on_generation_created` trigger → `process_generation`.
The worker job carries only book_id / generation_id — never the owner's role —
so a fix applied for one adult is applied for ALL of them.

These tests fail if someone (a) starts branching the pipeline on the owner's
role — which would silently give one role a different/worse pipeline — or
(b) drops one of the universal chapter-quality / recovery fixes. See
docs/PIPELINE_INVARIANTS.md (app repo).
"""

from __future__ import annotations

from pathlib import Path

_WORKER = Path(__file__).resolve().parent.parent / "worker"
_PROCESS = (_WORKER / "process.py").read_text(encoding="utf-8")
_RUN = (_WORKER / "run.py").read_text(encoding="utf-8")


def test_generation_pipeline_does_not_branch_on_role():
    # Ways you'd READ an owner/reporter role to branch on it. The processing path
    # must depend on book_id/generation_id + kind only, so every role gets the
    # same (fixed) pipeline. If you truly need a role here, that's a design change
    # — update docs/PIPELINE_INVARIANTS.md and this allowlist deliberately.
    forbidden = ('owner_role', 'reporter_role', '.get("role"', "['role']", '["role"]')
    hits = [tok for tok in forbidden if tok in _PROCESS]
    assert not hits, (
        f"process.py reads a role ({hits}) — the generation/index pipeline must stay "
        "role-agnostic so a fix for one adult reaches ALL adults. See docs/PIPELINE_INVARIANTS.md."
    )
    # Role-NAME string literals are the other tell of role-branching in this path.
    for role in ('"teacher"', '"parent"', '"principal"', '"coordinator"', '"school_admin"'):
        assert role not in _PROCESS, (
            f"process.py hard-codes the role {role} — role-branching in the pipeline. "
            "Keep it role-agnostic (see docs/PIPELINE_INVARIANTS.md)."
        )


def test_universal_chapter_fixes_stay_wired():
    # The scanned-book chapter fixes must remain in the shared path so every adult
    # keeps getting correct detection + self-heal, not just the teacher we tested.
    for fn in ("heal_chapter_boundaries", "verify_chapter_content", "relocate_chapter_for_generation"):
        assert fn in _PROCESS, (
            f"{fn} is no longer wired into process.py — a universal chapter-quality fix was dropped."
        )


def test_stale_job_reaper_stays_wired():
    # The orphaned-job recovery is pipeline-wide (any adult's stuck job recovers).
    assert "requeue_stale_jobs" in _RUN and "requeue_stale_sketches" in _RUN, (
        "the stale-job reaper is no longer wired into run.py — orphaned jobs would sit stuck again."
    )
