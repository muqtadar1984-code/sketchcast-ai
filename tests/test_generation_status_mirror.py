"""A generation must say "processing" while it is being built.

THE INCIDENT (prod, 2026-08-25). A trial teacher generated a six-artifact kit and
received FIVE. The missing one was the presentation — the narrated video, the
flagship of the kit. Its credit was charged and never refunded.

generations.status was written ONLY by finish_job, and only ever to 'done' or
'error'. So the row read 'queued' for the whole build, however many minutes the
render took. Three things followed:

  * delete-lesson.tsx shows a CANCEL confirmation for a 'queued' row and is inert
    for a 'processing' one — so the ✕ invited a teacher to cancel a render that
    was already running, and the inert guard could never fire because nothing
    wrote the word it checks for;
  * credit_ledger_void_unconsumed then refused the refund, because
    claim_next_job had already set the job's progress to 1;
  * and the Library's progress ring is `pct={status === "processing" ? progress : 0}`,
    so it sat at ZERO while the job reported real progress, with no ETA label.

Measured: two teachers lost a credit this way, both on presentations (the slow
artifact, the one that looks stuck), one of them the founder.

These are SOURCE assertions rather than behavioural ones because worker/ cannot
be imported without the supabase package, which is the documented local env gap.
The repo already pins symbols this way (see the bookmark-rung absence test).
"""

from __future__ import annotations

import re
from pathlib import Path

CLIENT = Path(__file__).resolve().parent.parent / "worker" / "client.py"
SRC = CLIENT.read_text(encoding="utf-8")


def _body(fn: str) -> str:
    """The source of one top-level function."""
    m = re.search(rf"^def {fn}\(.*?(?=^def |\Z)", SRC, re.S | re.M)
    assert m, f"{fn} not found in worker/client.py"
    return m.group(0)


def test_the_mirror_helper_exists_and_cannot_raise():
    assert "def mirror_generation_status(" in SRC
    body = _body("mirror_generation_status")
    # A status label must never cost a teacher their generation.
    assert "try:" in body and "except Exception" in body
    # A job with no generation (index_book) must be a no-op, not a crash.
    assert "if not generation_id:" in body and "return" in body


def test_claiming_a_job_marks_its_generation_processing():
    body = _body("claim_next_job")
    assert 'mirror_generation_status(sb, claimed.get("generation_id"), "processing")' in body, (
        "claim_next_job must mark the generation as being built — this is what "
        "stops the ✕ offering a cancel that costs a credit and returns nothing"
    )
    # …and only after the claim actually won the race.
    assert body.index("if not upd.data:") < body.index("mirror_generation_status")


def test_a_requeued_job_goes_back_to_queued():
    """A crash-recovered job must not leave its generation stuck 'processing' —
    that would make the ✕ inert forever and strand the row."""
    body = _body("requeue_stale_jobs")
    assert 'mirror_generation_status(sb, j.get("generation_id"), "queued")' in body


def test_a_poison_pill_marks_its_generation_error():
    """Auto-failing at the JOB level left the generation 'queued' forever AND kept
    its credit: credit_ledger_sync only voids on generations.status = 'error'."""
    body = _body("requeue_stale_jobs")
    assert 'mirror_generation_status(sb, j.get("generation_id"), "error")' in body
    assert 'select("id,type,book_id,attempts,generation_id")' in body, (
        "the reaper must SELECT generation_id or it has nothing to mirror onto"
    )


def test_finish_job_still_writes_the_terminal_status():
    """The mirror ADDS the in-flight states; it must not have displaced the
    terminal one, which is what credit_ledger_sync keys on."""
    body = _body("finish_job")
    assert 'sb.table("generations").update({"status": status})' in body
    assert '"error" if error else "done"' in body


def test_every_lifecycle_edge_is_covered():
    """queued -> processing -> done/error, with the crash paths returning it."""
    assert SRC.count("mirror_generation_status(") == 4  # 1 def + 3 call sites
    for word in ('"processing"', '"queued"', '"error"'):
        assert f"mirror_generation_status(sb, " in SRC and word in SRC
