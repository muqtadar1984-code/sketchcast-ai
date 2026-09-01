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

import ast
from pathlib import Path

_WORKER = Path(__file__).resolve().parent.parent / "worker"
_PROCESS = (_WORKER / "process.py").read_text(encoding="utf-8")
_RUN = (_WORKER / "run.py").read_text(encoding="utf-8")

_ROLES = {"teacher", "parent", "principal", "coordinator", "school_admin",
          "student"}


def _branch_tests(source: str):
    """Every expression the module uses to DECIDE something."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.While, ast.IfExp)):
            yield node.test
        elif isinstance(node, ast.Compare):
            yield node
        elif isinstance(node, ast.match_case):
            yield node.pattern


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
    # A role NAME reaching a DECISION is the other tell. Checked against the
    # parse tree rather than the raw text, because the avatar roster uses
    # "teacher"/"student" as slot names — those name a character on the
    # whiteboard, not the owner of the book, and branch nothing.
    for test in _branch_tests(_PROCESS):
        named = {n.value for n in ast.walk(test)
                 if isinstance(n, ast.Constant) and n.value in _ROLES}
        assert not named, (
            f"process.py branches on the role name(s) {sorted(named)} at line "
            f"{test.lineno} — role-branching in the pipeline. Keep it "
            "role-agnostic (see docs/PIPELINE_INVARIANTS.md)."
        )


def test_the_role_guard_still_bites():
    """The check moved from raw text to the parse tree to stop a false
    positive; prove it did not stop catching the real thing."""
    branching = 'if book["owner"] == "teacher":\n    x = 1\n'
    caught = [t for t in _branch_tests(branching)
              if any(isinstance(n, ast.Constant) and n.value in _ROLES
                     for n in ast.walk(t))]
    assert caught, "the guard no longer detects a role comparison"

    # ...and that the shape it now tolerates really is inert.
    roster = 'a = {"teacher": pick(voice), "student": pick(grade)}\n'
    assert not [t for t in _branch_tests(roster)
                if any(isinstance(n, ast.Constant) and n.value in _ROLES
                       for n in ast.walk(t))]


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
