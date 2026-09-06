"""``canonical_key`` against the truth table the APP also runs.

The topic catalogue has ONE normalisation function and two implementations:
``src/utils/catalogue/key.ts`` in the app (the console writes
``topics.canonical_key`` and ``topic_aliases.normalized`` through it) and
``catalogue/key.py`` here (the harvest and the seed loader write
``topic_candidates.normalized`` through it). They are matched by SQL equality,
so a single input the two sides key differently is a candidate that never
finds its topic.

The cases live in tests/fixtures/catalogue_key_cases.json, a byte-identical
copy of sketchcast-app/src/utils/__tests__/fixtures/catalogue_key_cases.json.
Both suites pin the same sha256 of the LF-normalised bytes (the premium-voice
table set the precedent, and why LF-normalised: a Windows checkout rewrote
that file to CRLF and turned the suite red over nothing a test asserts), so
editing one copy alone turns the OTHER repo's suite red — which is the point.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from catalogue.key import canonical_key, singular_token

FIXTURE = Path(__file__).parent / "fixtures" / "catalogue_key_cases.json"
# Bump ONLY when the app's copy is changed to match, byte for byte.
CATALOGUE_KEY_CASES_SHA256 = "b392712ee147d8890410387daa5a475708e07391ae7943befbf3bab9bf7a782b"


def _canonical(raw: bytes) -> bytes:
    """The fixture's bytes with line endings normalised to LF (see module doc)."""
    return raw.replace(b"\r\n", b"\n")


_RAW = _canonical(FIXTURE.read_bytes())
TABLE = json.loads(_RAW.decode("utf-8"))
CASES = TABLE["cases"]


class TestSharedTable:
    def test_it_is_the_same_table_the_app_runs(self):
        assert hashlib.sha256(_RAW).hexdigest() == CATALOGUE_KEY_CASES_SHA256
        assert len(CASES) >= 28

    def test_every_case_has_the_agreed_shape(self):
        for c in CASES:
            assert set(c) >= {"in", "key"}, c
            assert isinstance(c["in"], str) and isinstance(c["key"], str), c

    def test_the_table_carries_the_mandated_invariants(self):
        """The cases the plan named must be IN the table, not only in this
        file — otherwise the two repos could agree on a table that omits the
        very inputs the rule was written for."""
        table = {c["in"]: c["key"] for c in CASES}
        for inp, key in {
            "The Cell": "cell",
            "Cells": "cell",
            "Acids, Bases & Salts": "acid_base_and_salt",
            "Light — Reflection and Refraction": "light_reflection_and_refraction",
            "Newton's Laws": "newton_s_law",
            "7Bs.01": "7bs_01",
            "  ": "",
        }.items():
            assert table.get(inp) == key, (inp, table.get(inp), key)


@pytest.mark.parametrize("case", CASES, ids=lambda c: repr(c["in"])[:40])
def test_canonical_key_matches_the_table(case):
    assert canonical_key(case["in"]) == case["key"]


class TestTheKeyIsAKey:
    """Properties the table implies but a single row cannot pin."""

    def test_idempotent(self):
        for c in CASES:
            assert canonical_key(c["key"]) == c["key"], c

    def test_only_key_characters_survive(self):
        for c in CASES:
            assert all(ch.islower() or ch.isdigit() or ch == "_" for ch in c["key"]), c
            assert not c["key"].startswith("_") and not c["key"].endswith("_"), c
            assert "__" not in c["key"], c

    def test_none_and_non_strings_are_tolerated(self):
        assert canonical_key(None) == ""
        assert canonical_key(7) == "7"
        assert canonical_key("  The  ") == ""

    def test_one_article_only(self):
        assert canonical_key("The A Team") == "a_team"
        assert canonical_key("An Atom") == "atom"


class TestSingularToken:
    """Rule 6 as the app states it: alphabetic, >= 4 letters, final s, and the
    letter before it not in {s, a, i, o, u}."""

    @pytest.mark.parametrize("token,expected", [
        ("cells", "cell"), ("atoms", "atom"), ("laws", "law"),
        ("bases", "base"), ("forces", "force"), ("waves", "wave"),
        ("glass", "glass"), ("gas", "gas"), ("bus", "bus"), ("this", "this"),
        ("its", "its"), ("chaos", "chaos"), ("7bs", "7bs"), ("s", "s"),
        ("physics", "physic"), ("photosynthesis", "photosynthesis"),
        ("gases", "gase"),  # the accepted casualty — both repos agree
    ])
    def test_cases(self, token, expected):
        assert singular_token(token) == expected
