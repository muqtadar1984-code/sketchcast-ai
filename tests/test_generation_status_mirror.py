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
    assert 'mirror_generation_status(sb, generation_to_mirror(claimed), "processing")' in body, (
        "claim_next_job must mark the generation as being built — this is what "
        "stops the ✕ offering a cancel that costs a credit and returns nothing"
    )
    # …and only after the claim actually won the race.
    assert body.index("if not upd.data:") < body.index("mirror_generation_status")


def test_a_requeued_job_goes_back_to_queued():
    """A crash-recovered job must not leave its generation stuck 'processing' —
    that would make the ✕ inert forever and strand the row."""
    body = _body("requeue_stale_jobs")
    assert 'mirror_generation_status(sb, owned_gen, "queued")' in body


def test_a_poison_pill_marks_its_generation_error():
    """Auto-failing at the JOB level left the generation 'queued' forever AND kept
    its credit: credit_ledger_sync only voids on generations.status = 'error'."""
    body = _body("requeue_stale_jobs")
    assert 'mirror_generation_status(sb, owned_gen, "error")' in body
    assert 'select("id,type,book_id,attempts,generation_id")' in body, (
        "the reaper must SELECT generation_id or it has nothing to mirror onto"
    )


# ── The second incident: an OBSERVER job relabelled the row it reported on ──
#
# Prod, 2026-09-03 11:50. A worksheet's storage download was reset by the peer;
# the worker marked the job AND the generation 'error' (credit refunded) and,
# with the support agent on, filed a platform_issue plus a support_diagnose job
# carrying the SAME generation_id. Two seconds later a worker thread claimed
# that diagnosis job — and claim_next_job mirrored 'processing' onto the
# worksheet. finish_job for a support job deliberately passes no generation,
# so nothing ever wrote a terminal state again: spinner forever, ✕ inert,
# and the book could not be deleted ("a kit from this book is still being
# built"). The manual report path is worse: a teacher reporting a HEALTHY,
# assigned lesson would have had it flipped to 'processing' under students.


def test_observer_jobs_are_named_and_the_resolver_exists():
    # topic_harvest joined 2026-09 (catalogue): it owns no generation either;
    # topic_derive (Phase 2a) owns no generation and no book; topic_article
    # and figure_render (Phase 2b) write the knowledge base and own neither.
    assert ('OBSERVER_JOB_TYPES = frozenset({"support_diagnose", "topic_harvest", "topic_derive", '
            '"topic_article", "figure_render"})') in SRC
    body = _body("generation_to_mirror")
    assert "OBSERVER_JOB_TYPES" in body and "return None" in body
    assert 'return job.get("generation_id")' in body


def test_claim_never_mirrors_through_a_raw_generation_id():
    """Every mirror in the claim path must go through the resolver, so a
    support_diagnose claim cannot relabel the generation it reports on."""
    body = _body("claim_next_job")
    assert 'claimed.get("generation_id")' not in body
    assert "generation_to_mirror(claimed)" in body


def test_reaper_never_mirrors_through_a_raw_generation_id():
    """Both reaper edges (requeue → 'queued', poison pill → 'error') resolve the
    generation ONCE per job through the same rule. Auto-failing a stuck
    diagnosis must not mark a healthy lesson 'error' and void its credit."""
    body = _body("requeue_stale_jobs")
    assert 'mirror_generation_status(sb, j.get("generation_id")' not in body
    assert "owned_gen = generation_to_mirror(j)" in body
    # Resolved before either branch uses it.
    assert body.index("owned_gen = generation_to_mirror(j)") < body.index("if att >= max_attempts:")


def test_finish_job_is_the_one_place_a_raw_generation_id_is_written():
    """run.py already passes finish_job `None` for a support job; the helper
    itself takes an explicit generation_id and must keep doing so — it is the
    builder's terminal write, and credit_ledger_sync keys on it."""
    body = _body("finish_job")
    assert "generation_to_mirror" not in body
    assert "if generation_id:" in body


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
    # …and every call site names its generation through the ownership rule.
    calls = re.findall(r"mirror_generation_status\(sb, (\w+(?:\([^)]*\))?), ", SRC)
    assert sorted(calls) == ["generation_to_mirror(claimed)", "owned_gen", "owned_gen"], calls
