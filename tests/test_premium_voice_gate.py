"""The premium-voice gate, against the truth table the APP also runs.

Founder decision 2026-09-05: premium voices go to paid plans and to comp
overrides of 100000 or more — NOT to every comp override, which is what both
gates asked for before migration 0105. On prod that was 18 accounts against 7.

`paid` and `premium` are two different questions from here on:
    paid    = exempt from the credit gate. A comp of ANY size. UNCHANGED.
    premium = may hear the premium voices. The database decides, once, in
              premium_voices_allowed(); the app reads the same answer through
              my_fair_use().premium_voices.

The cases live in tests/fixtures/premium_voice_cases.json, a byte-identical
copy of sketchcast-app/src/utils/__tests__/fixtures/premium-voice-cases.json.
Both suites pin the same sha256, so editing one copy alone turns the OTHER
repo's suite red — which is the point: the worker must never refuse a voice the
picker offered, nor render one it did not.

No network, no model, no live Supabase: the client is the scripted stand-in
from test_tts_gate.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from shared.tts import PAID_TIERS
from tests.test_tts_gate import _ABSENT, _APIError, _SB, NO_COMP, wc  # noqa: F401  (wc is a fixture)

FIXTURE = Path(__file__).parent / "fixtures" / "premium_voice_cases.json"
# Bump ONLY when the app's copy is changed to match, byte for byte.
PREMIUM_VOICE_CASES_SHA256 = "5edb7d740ce81082c718fb8392b06ba4f1d7094af16eb35217d57b6ac5f2c3e3"

def _canonical(raw: bytes) -> bytes:
    """The fixture's bytes with line endings normalised to LF.

    The pin proves the two repos run the SAME TABLE, and it used to hash the
    file exactly as it sat on disk. That made it fail on every Windows
    checkout — measured 2026-09-05: the git blob hashes to 403e9119…, the
    working copy to 9d1a74aa…, because the checkout rewrote 142 lines to CRLF.
    The table was identical; only the line endings were not, and the suite went
    red for a difference that cannot change what a single test asserts.

    Normalising is not a weakening. Byte-identity ON DISK was a proxy for the
    property actually wanted, and a proxy that reports a platform's checkout
    convention as cross-repo drift is the wrong proxy. .gitattributes now also
    pins the file to LF on checkout, so the two halves agree.
    """
    return raw.replace(b"\r\n", b"\n")


_RAW = _canonical(FIXTURE.read_bytes())
TABLE = json.loads(_RAW.decode("utf-8"))
CASES = TABLE["cases"]


def _sb(case, **kw):
    """The scripted client for one fixture row: the profile caps and the tier
    the row describes, with premium_voices_allowed answering as 0105's SQL
    would (the stand-in derives it, so the row cannot disagree with itself)."""
    return _SB(tier=case["tier"],
               override={"max_books": case["max_books"], "max_chapters": case["max_chapters"]},
               **kw)


class TestSharedTable:
    def test_it_is_the_same_table_the_app_runs(self):
        assert hashlib.sha256(_RAW).hexdigest() == PREMIUM_VOICE_CASES_SHA256
        assert len(CASES) >= 15

    def test_the_fixture_encodes_the_rule_and_nothing_else(self):
        """db_premium must BE "big override or paid tier" — otherwise the table
        could drift into describing some other rule than the migration's."""
        for c in CASES:
            big = max(c["max_books"] or 0, c["max_chapters"] or 0) >= TABLE["threshold"]
            assert c["db_premium"] is bool(big or c["tier"] in TABLE["paid_tiers"]), c["name"]
            assert c["expect_premium"] is c["db_premium"], c["name"]

    def test_the_workers_paid_tiers_are_the_fixtures(self):
        assert sorted(PAID_TIERS) == sorted(TABLE["paid_tiers"])


class TestResolveTierPremium:
    @pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
    def test_every_case(self, wc, case):
        r = wc.resolve_tier(_sb(case), "u")
        assert r["premium"] is case["expect_premium"], case["name"]
        assert r["premium_note"] is None

    def test_a_100000_override_gets_premium(self, wc):
        r = wc.resolve_tier(_SB(tier="trial",
                                override={"max_books": 100000, "max_chapters": None}), "u")
        assert r["premium"] is True and r["paid"] is True and r["override"] is True

    def test_a_20_book_override_keeps_unlimited_generation_but_loses_premium(self, wc):
        """The eleven seeded accounts. `paid` — the credit-gate exemption — must
        NOT move; only the voice does."""
        r = wc.resolve_tier(_SB(tier="trial",
                                override={"max_books": 20, "max_chapters": None}), "u")
        assert r["paid"] is True, "still exempt from the credit gate"
        assert r["override"] is True
        assert r["premium"] is False, "but not the premium voice"

    @pytest.mark.parametrize("tier", sorted(PAID_TIERS))
    def test_a_paid_tier_gets_premium(self, wc, tier):
        r = wc.resolve_tier(_SB(tier=tier, override=NO_COMP), "u")
        assert r["premium"] is True and r["paid"] is True

    @pytest.mark.parametrize("tier", ["trial", "promo", "school_trial", "school_expired",
                                      "school_suspended", "banana"])
    def test_an_unpaid_tier_does_not(self, wc, tier):
        r = wc.resolve_tier(_SB(tier=tier, override=NO_COMP), "u")
        assert r["premium"] is False and r["paid"] is False

    def test_the_threshold_is_not_written_in_the_worker(self):
        """The number lives in the migration. If it ever appears in worker code
        the two halves of the product can drift, which is the bug 0105 fixes."""
        root = Path(__file__).resolve().parents[1]
        bare = re.compile(r"(?<!\d)100000(?!\d)")   # not GOOGLE_TTS_CHAR_CAP's 1000000
        for pkg in ("worker", "shared"):
            for f in (root / pkg).rglob("*.py"):
                assert not bare.search(f.read_text(encoding="utf-8")), f


class TestUnmigratedDatabase:
    """0105 may land AFTER this deploy. That window must degrade, never fail
    and never over-grant."""

    @pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
    def test_it_degrades_to_paid_tiers_only(self, wc, case):
        r = wc.resolve_tier(_sb(case, premium_exc=_ABSENT), "u")
        assert r["premium"] is (case["tier"] in PAID_TIERS), case["name"]
        assert r["premium_note"] == "unavailable"

    def test_the_over_grant_cannot_come_back_through_the_fallback(self, wc):
        """The exact regression: before 0105 an override of any size meant
        premium. The fallback must reach for the PAID TIERS, never `override`."""
        r = wc.resolve_tier(_SB(tier="trial", override={"max_books": 20, "max_chapters": None},
                                premium_exc=_ABSENT), "u")
        assert r["paid"] is True and r["premium"] is False
        # and not even for the big ones — the fallback cannot know the size
        r = wc.resolve_tier(_SB(tier="trial", override={"max_books": 2147483647, "max_chapters": None},
                                premium_exc=_ABSENT), "u")
        assert r["paid"] is True and r["premium"] is False

    def test_a_paid_teacher_still_gets_premium_before_the_migration(self, wc):
        r = wc.resolve_tier(_SB(tier="pro", override=NO_COMP, premium_exc=_ABSENT), "u")
        assert r["premium"] is True and r["premium_note"] == "unavailable"

    def test_a_missing_function_is_not_retried(self, wc):
        sb = _SB(tier="pro", override=NO_COMP, premium_exc=_ABSENT)
        wc.resolve_tier(sb, "u")
        assert sb.premium_calls == 1, "an APIError will not get better by waiting"

    def test_it_never_raises(self, wc):
        for case in CASES:
            wc.resolve_tier(_sb(case, premium_exc=_ABSENT), "u")


class TestFailedRead:
    def test_a_comped_account_is_requeued_when_the_premium_read_times_out(self, wc):
        """Review finding, and the behaviour this replaces.

        The first draft returned premium=False here: a comped account whose
        premium_voices_allowed call TIMED OUT rendered with the free voice and
        nothing but a log line. That is the same failure the tier read has
        always requeued on — TransientTierError's own docstring says "a paying
        customer must not receive the free voice because of a timeout" — and it
        lands on exactly the accounts whose entitlement lives ONLY in this
        answer: all seven of the founder's 100k comps are plan_tier='trial', so
        the paid-tier fallback cannot cover them.

        Requeue is bounded at 3 attempts by worker/run.py, and it is reached
        only for a TIMEOUT, never for a database without 0105.
        """
        sb = _SB(tier="trial", override={"max_books": 2147483647, "max_chapters": None},
                 premium_exc=wc._Timeout("t"))
        with pytest.raises(wc.TransientTierError):
            wc.resolve_tier(sb, "u")
        assert sb.premium_calls == 2, "a transient error IS retried before requeueing"

    def test_an_uncomped_account_is_not_requeued_for_it(self, wc):
        """Narrow on purpose. A paid tier already has its answer from the
        fallback, and a free account's answer is False either way — neither
        needs a requeue, so neither gets one."""
        r = wc.resolve_tier(_SB(tier="pro", override=NO_COMP, premium_exc=wc._Timeout("t")), "u")
        assert r["premium"] is True and r["premium_note"] == "unread"
        r = wc.resolve_tier(_SB(tier="trial", override=NO_COMP, premium_exc=wc._Timeout("t")), "u")
        assert r["premium"] is False and r["paid"] is False

    def test_a_known_missing_0105_never_requeues_a_comped_account(self, wc, monkeypatch):
        """The precondition of this whole branch: the deploy may land BEFORE
        the migration. An absent function must degrade, never requeue, whatever
        the boot probe found."""
        for probe in (False, None, True):
            monkeypatch.setattr(wc, "_PREMIUM_PROBE_OK", probe)
            r = wc.resolve_tier(_SB(tier="trial", override={"max_books": 100000,
                                                            "max_chapters": None},
                                    premium_exc=_ABSENT), "u")
            assert r["premium"] is False and r["premium_note"] == "unavailable"
            assert r["paid"] is True

    def test_a_comp_still_needs_no_rpc_at_all(self, wc):
        """The property test_a_comp_needs_no_rpc_to_succeed pins, restated for
        the premium read — and the reason the requeue also demands that
        plan_tier ANSWERED. When the whole RPC surface is down a comped account
        keeps rendering (it needs no RPC to be `paid`); requeueing it would
        spend the 3-attempt cap and then fail the lesson outright, which is
        strictly worse than the free voice it would otherwise get."""
        r = wc.resolve_tier(_SB(tier=None, override={"max_books": 2147483647,
                                                     "max_chapters": None},
                                rpc_exc=wc._Timeout("t")), "u")
        assert r["paid"] is True and r["override"] is True and r["tier"] is None
        assert r["premium"] is False and r["premium_note"] == "unread"

    def test_a_probe_that_found_it_broken_stops_the_requeue(self, wc, monkeypatch):
        """The guard that makes the requeue safe: if the RPC is known broken,
        a comped account renders free rather than burning the attempt cap."""
        monkeypatch.setattr(wc, "_PREMIUM_PROBE_OK", False)
        r = wc.resolve_tier(_SB(tier="trial", override={"max_books": 100000, "max_chapters": None},
                                premium_exc=wc._Timeout("t")), "u")
        assert r["premium"] is False and r["premium_note"] == "unread" and r["paid"] is True

    def test_a_shape_we_do_not_understand_is_not_an_answer(self, wc):
        """A null or a string back from the RPC must read as 'unknown', not as
        a confident False that silently downgrades a paying account — and NOT
        as a requeue either: asking again gets the same reply, so three
        attempts would just fail the lesson. That is why it has its own note."""
        r = wc.resolve_tier(_SB(tier="pro", override=NO_COMP, premium="not-a-bool"), "u")
        assert r["premium"] is True, "the paid-tier fallback caught it"
        assert r["premium_note"] == "unreadable"
        r = wc.resolve_tier(_SB(tier="trial", override={"max_books": 100000, "max_chapters": None},
                                premium="not-a-bool"), "u")
        assert r["premium"] is False, "refused, never granted, on an unreadable answer"
        assert r["premium_note"] == "unreadable"

    def test_the_whole_rpc_surface_down_still_requeues_the_way_it_did(self, wc):
        """Unchanged behaviour: an unresolvable TIER is still a requeue, and the
        premium read does not turn that into a silent free render."""
        sb = _SB(tier=None, override=NO_COMP, rpc_exc=wc._Timeout("t"))
        with pytest.raises(wc.TransientTierError):
            wc.resolve_tier(sb, "u")
        assert sb.premium_calls == 0, "and it is raised before paying for a third read"

    def test_a_known_broken_rpc_renders_free_and_says_why(self, wc, monkeypatch):
        monkeypatch.setattr(wc, "_PLAN_TIER_PROBE_OK", False)
        r = wc.resolve_tier(_SB(tier=None, override=NO_COMP, rpc_exc=_APIError("x")), "u")
        assert r["paid"] is False and r["premium"] is False
        assert r["error"] == "tier_unread"


class TestBootProbeForPremium:
    """probe_premium_voices_allowed mirrors probe_plan_tier's tri-state, and is
    what lets an un-migrated database say so ONCE at boot instead of once per
    job — and what stops resolve_tier requeueing against an RPC that is gone."""

    def test_success_is_true(self, wc):
        assert wc.probe_premium_voices_allowed(_SB(tier="trial", override=NO_COMP)) is True
        assert wc._PREMIUM_PROBE_OK is True

    def test_a_database_without_0105_is_false(self, wc):
        assert wc.probe_premium_voices_allowed(_SB(premium_exc=_ABSENT)) is False
        assert wc._PREMIUM_PROBE_OK is False

    def test_a_timeout_is_inconclusive_not_broken(self, wc):
        assert wc.probe_premium_voices_allowed(_SB(premium_exc=wc._Timeout("t"))) is None
        assert wc._PREMIUM_PROBE_OK is None

    def test_it_never_raises(self, wc):
        wc.probe_premium_voices_allowed(_SB(premium_exc=RuntimeError("boom")))

    def test_the_worker_runs_it_at_boot_beside_the_tier_probe(self):
        src = (Path(__file__).resolve().parents[1] / "worker" / "run.py").read_text(
            encoding="utf-8")
        assert "db.probe_plan_tier(sb)" in src
        assert "db.probe_premium_voices_allowed(sb)" in src


class TestProcessUsesPremiumNotPaid:
    def test_the_gate_reads_tier_info_premium(self):
        """process.py must hand `premium` to allow_premium. Reading `paid`
        there is the whole bug: it is True for all 18 comped accounts."""
        src = (Path(__file__).resolve().parents[1] / "worker" / "process.py").read_text(
            encoding="utf-8")
        assert 'allow_premium = bool(tier_info.get("premium"))' in src
        assert 'allow_premium = bool(tier_info["paid"])' not in src
        # and the seeded default must be premium-off
        assert '"premium": False' in src
