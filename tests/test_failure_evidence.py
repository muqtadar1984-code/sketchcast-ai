"""What a failure leaves behind for whoever reads it next.

THE INCIDENT (prod, 2026-09-05). Sara Hamaydeh's first lesson failed (gen
eb12963c, issue 24b6cadd). The script failure builds its message deliberately
long — preamble, JSON fault window, the reply's first 220 and last 160
characters, the path of the saved dump — because the reply itself dies with
the container and that line is all a later reader has. It came to 1,018
characters.

jobs.error kept the first 500, which stopped a few words past the
malformation. platform_issues.context, the row the console shows and the
diagnosis agent is filed against, kept the first 300 — which stopped BEFORE
it, mid-phrase on "… natural land". The reader with the LEAST evidence was the
one asked to explain the failure, and the incident was consequently diagnosed
by inference: the surviving window supported "a quote left bare in the prose",
while the malformation the 500-character row actually recorded was an assets
object closed twice.

Neither column bounds this. jobs.error is `text` and platform_issues.context
is `jsonb` (checked against prod, 2026-09-05); 300 and 500 were budgets we
picked. These pin the new one, and pin that a trim keeps BOTH ends — a
head-only cut on this message loses the fault, the excerpts and the dump path,
in that order.
"""

from __future__ import annotations

import pytest

import worker.run as run

# The real message, rebuilt to the shape script_generator emits, at the size it
# reached for Sara's third attempt (job 8e3d26dd, 2026-09-05 14:42 UTC).
FAULT = ("JSON fault: Expecting ',' delimiter at line 1 col 14381 "
         "(char 14380 of 22538): … natural landscape (mountains, oceans, "
         "diverse life), illustrating Allah's creation effortlessly.\"}}, "
         "\"semantic_regions\": [\"allah_central_script\", …")
REAL = ("Script generation produced no segments for episode 1: 22538 chars, "
        "output_tokens=6168 billed across attempts (cap 30000), provider did "
        "NOT report truncation — so this is malformed JSON, not a cut-off "
        "reply. " + FAULT + ". Reply began: " + "b" * 220 + " … ended: "
        + "e" * 160 + ". Raw reply saved to /tmp/sketchcast_reply_ep1.txt")


def test_the_measured_message_survives_whole():
    """~1,000 characters is not a pathological reply, it is the ordinary size
    of this failure — the real one was 1,018, and the reply it quoted is gone,
    so this is a faithful reconstruction rather than a copy. It used to be cut
    to 300."""
    assert 800 < len(REAL) < 1400, "the fixture drifted from the measured shape"
    assert run._evidence(REAL) == REAL


def test_the_fault_the_incident_lost_is_now_in_the_row():
    kept = run._evidence(REAL)
    assert "char 14380 of 22538" in kept
    assert '"}}, "semantic_regions"' in kept, "the malformation itself, not just its position"
    assert "/tmp/sketchcast_reply_ep1.txt" in kept, "and where the reply was saved"
    assert len(REAL[:300]) == 300 and '"}}' not in REAL[:300], \
        "the old cap stopped before the malformation — that is what this fixes"


def test_a_pathological_message_is_still_bounded():
    huge = run._evidence("x" * 500_000)
    assert len(huge) <= run._EVIDENCE_CHARS


def test_a_trim_keeps_BOTH_ends_and_says_what_it_dropped():
    """Middle-out, never head-only. The head carries what broke; the tail
    carries the reply excerpt and the path to the dump. A head-only cut of
    this message loses every one of them."""
    text = "HEAD" + "m" * 50_000 + "TAIL"
    kept = run._evidence(text)
    assert kept.startswith("HEAD") and kept.endswith("TAIL")
    assert "chars elided" in kept, "a trimmed message must not read as a short one"


def test_short_messages_and_empties_are_untouched():
    assert run._evidence("boom") == "boom"
    assert run._evidence(None) == ""
    assert run._evidence(RuntimeError("boom")) == "boom", "an exception object is fine too"


@pytest.mark.parametrize("limit", [1, 10, 60, 300, 500, 4000])
def test_the_result_never_exceeds_its_budget(limit):
    """Including limits smaller than the elision marker itself, which is where
    a middle-trim most easily goes off by the marker's length."""
    assert len(run._evidence("z" * 20_000, limit)) <= limit


def test_no_failure_path_in_the_worker_still_cuts_a_head():
    """The sibling check. Three call sites store a failure message — the
    support issue, the plan-tier give-up and the generic job failure — and all
    three had their own hard-coded slice."""
    import inspect
    src = inspect.getsource(run)
    assert "error[:300]" not in src
    assert "[:500]" not in src, "a bare head-slice on the failure path"
    assert src.count("_evidence(") >= 4, "helper plus all three call sites"
