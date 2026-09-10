"""A delimiter written in the script the lesson is written IN.

THE INCIDENT (prod, 2026-09-10 08:04 UTC, gen 1307931a). Asmaa's Arabic KG2
lesson, re-run minutes after the streaming fix let its script call complete for
the first time. The reply arrived whole — 21,052 characters, 12,425 output
tokens against a 30,000 cap, and the provider did not report truncation — and
then failed to parse:

    Expecting ',' delimiter at line 1 col 12469

    …"cue":"الْإِجَّاصُ أَصْفَرُ أَيْضاً"}]}]}，{"id":"chapter_2",…
                                              ^ U+FF0C FULLWIDTH COMMA

The character between the two chapter objects IS a comma to read, and is not
the comma JSON accepts. A model writing Arabic prose slipped into non-ASCII
punctuation at a structural position. The salvage had rules for wrong closers,
surplus closers, bad escapes, prose quotes and SSML — none of them can see a
delimiter that is simply the wrong codepoint.

THE RULE IS DECIDABLE, which is the bar every rule in this salvage has to
clear: outside a string none of these characters is ever valid JSON, so
swapping one loses nothing. INSIDE a string an Arabic comma is ordinary prose —
this lesson is full of it — and rewriting it would put words in the teacher's
mouth. The walker tracks string state for exactly that reason.
"""

from __future__ import annotations

import json

from shared.claude_client import (
    _ascii_json_punctuation,
    _repair_json,
)

FW = "，"      # ，fullwidth comma
AR = "،"      # ،Arabic comma
IDEO = "、"    # 、ideographic comma
FWCOLON = "："  # ：fullwidth colon


# ── the production reply's exact shape ────────────────────────────────────────

PROD = (
    '{"segments":[{"type":"hook","text":"","dialogue":'
    '[{"who":"teacher","line":"هَلْ تُحِبُّ الْمَوْزَ؟"},'
    '{"who":"student","line":"الْمَوْزُ أَصْفَرُ وَطَعْمُهُ حُلْوٌ!"}]}],'
    '"visual_plan":[{"id":"chapter_1","concept":"الفواكه"}'
    + FW +
    '{"id":"chapter_2","concept":"الخضراوات وأسماؤها وألوانها"}]}'
)


def test_the_production_reply_now_parses():
    # The fixture must still reproduce the fault, or the test below proves
    # nothing: a repair that "works" on already-valid JSON is not a repair.
    try:
        json.loads(PROD)
        raise AssertionError("the fixture no longer reproduces the fault")
    except json.JSONDecodeError as exc:
        assert "delimiter" in str(exc)

    out = _repair_json(PROD)
    assert out is not None, "the salvage must recover the reply"
    assert len(out["segments"]) == 1
    assert len(out["visual_plan"]) == 2, "both chapters survive"
    assert out["visual_plan"][1]["id"] == "chapter_2"


def test_the_arabic_words_are_returned_unchanged():
    """The whole point of walking string state. A lesson that ships words the
    model never wrote is worse than one that fails."""
    out = _repair_json(PROD)
    assert out["segments"][0]["dialogue"][1]["line"] == "الْمَوْزُ أَصْفَرُ وَطَعْمُهُ حُلْوٌ!"
    assert out["visual_plan"][1]["concept"] == "الخضراوات وأسماؤها وألوانها"


# ── the walker ────────────────────────────────────────────────────────────────


def test_an_arabic_comma_inside_a_string_is_preserved():
    """Arabic narration legitimately contains ، — rewriting it would corrupt
    the teacher's words. Only a delimiter POSITION is rewritten."""
    src = '{"line":"أَحْمَرُ' + AR + ' أَصْفَرُ' + AR + ' أَخْضَرُ","n":1}'
    assert _ascii_json_punctuation(src) is None, "nothing outside a string: no change"
    assert json.loads(src)["line"].count(AR) == 2


def test_a_delimiter_outside_a_string_is_swapped():
    src = '{"a":1' + FW + '"b":2}'
    fixed = _ascii_json_punctuation(src)
    assert fixed == '{"a":1,"b":2}'
    assert json.loads(fixed) == {"a": 1, "b": 2}


def test_both_positions_at_once():
    """The realistic mixture: prose commas inside, a stray delimiter between."""
    src = '{"line":"وَاحِد' + AR + ' اِثْنَان"' + FW + '"n":2}'
    fixed = _ascii_json_punctuation(src)
    out = json.loads(fixed)
    assert out["n"] == 2
    assert out["line"] == "وَاحِد" + AR + " اِثْنَان", "the prose comma survived"


def test_every_look_alike_in_the_table():
    for ch, ascii_ in ((FW, ","), (AR, ","), (IDEO, ","), (FWCOLON, ":")):
        if ascii_ == ",":
            src = '{"a":1' + ch + '"b":2}'
            assert json.loads(_ascii_json_punctuation(src)) == {"a": 1, "b": 2}, ch
        else:
            src = '{"a"' + ch + '1}'
            assert json.loads(_ascii_json_punctuation(src)) == {"a": 1}, ch


def test_a_clean_reply_is_left_alone():
    """None means 'nothing to do', so a healthy reply never enters the
    candidate list twice and the richest-parse comparison is unaffected."""
    assert _ascii_json_punctuation('{"a":1,"b":"مَرْحَبًا"}') is None


def test_an_escaped_quote_does_not_break_string_tracking():
    """A \\" inside a string must not be read as the string ending, or every
    delimiter after it would be judged in the wrong state."""
    src = '{"line":"قَالَ \\"نَعَمْ\\"' + AR + ' ثُمَّ مَشَى"' + FW + '"n":3}'
    fixed = _ascii_json_punctuation(src)
    out = json.loads(fixed)
    assert out["n"] == 3
    assert out["line"].count(AR) == 1, "the prose comma inside the string survived"
    assert '"نَعَمْ"' in out["line"]


def test_the_rule_composes_with_the_other_repairs():
    """A reply with BOTH a fullwidth delimiter and a trailing comma."""
    src = '{"a":[1,2,]' + FW + '"b":2}'
    out = _repair_json(src)
    assert out == {"a": [1, 2], "b": 2}


# ── the desync, found by adversarial review 2026-09-10 ────────────────────────
#
# The first cut of this rule ran BEFORE _escape_inner_quotes and REPLACED the
# working text instead of branching. _repair_json only ever runs on replies
# that already failed to parse, which is exactly the population where a bare
# inner quote is likeliest — and one bare quote inverts the walker's idea of
# "inside a string" for the whole rest of the document. Every Arabic comma
# after it was then rewritten to ASCII: measured 8 of 8 on a reply carrying no
# look-alike at all, so the rule did pure damage. Worse, it was a REGRESSION —
# the same reply salvaged with its punctuation intact before the rule existed —
# and because candidate 0b reassigned the base text, the richest-parse arbiter
# had no un-rewritten reading left to choose.


def _desync_reply() -> str:
    """A realistic malformed Arabic reply: one BARE inner quote (a measurement
    in prose, `5" سم`), then ordinary Arabic commas in later narration."""
    return (
        '{"segments":[{"narration":"الطول 5" سم' + AR + ' وهذا"},'
        '{"narration":"حمراء' + AR + ' صفراء' + AR + ' أخضر"}]}'
    )


def test_a_bare_inner_quote_does_not_cost_the_prose_its_punctuation():
    src = _desync_reply()
    assert src.count(AR) == 3
    out = _repair_json(src)
    assert out is not None, "the reply must still be salvaged"
    narrations = [s["narration"] for s in out["segments"]]
    assert sum(n.count(AR) for n in narrations) == 3, (
        f"every Arabic comma must survive; got {narrations}"
    )
    assert sum(n.count(",") for n in narrations) == 0, (
        f"no Arabic comma may become an ASCII one; got {narrations}"
    )


def test_the_walker_refuses_when_its_own_walk_ends_inside_a_string():
    """If the walk finishes mid-string its notion of 'outside' was never
    trustworthy, so every swap it made is suspect and the whole rewrite is
    withdrawn — the same refusal _substitute_closers makes."""
    assert _ascii_json_punctuation('{"a":"x' + AR + 'y') is None
    assert _ascii_json_punctuation('{"a":1' + FW + '"b":"unterminated') is None


def test_the_unrewritten_reading_is_still_offered():
    """The rule BRANCHES: reassigning the base text meant a false positive
    always won, because the only candidate carrying the original punctuation
    was the one that by construction does not parse."""
    import inspect

    from shared import claude_client as cc

    src = inspect.getsource(cc._repair_json)
    assert "bases = [fixed]" in src, "the un-rewritten reading must stay a base"
    assert "fixed = ascii_punct" not in src, "the rule must not replace the base text"
    # …and it must sit after the inner-quote repair, not before it.
    assert src.index("_escape_inner_quotes") < src.index("_ascii_json_punctuation")


def test_the_table_holds_only_script_variants_of_the_same_mark():
    """A fullwidth SEMICOLON mapped to a comma changes the mark, not just its
    script — a guess about intent, which this layer must never make. It and the
    ratio sign were dropped after review; neither was ever measured."""
    from shared.claude_client import _UNICODE_DELIMITERS

    assert "；" not in _UNICODE_DELIMITERS
    assert "∶" not in _UNICODE_DELIMITERS
    for ch, ascii_ in _UNICODE_DELIMITERS.items():
        assert ascii_ in ",:", f"{ch!r} maps to {ascii_!r}"


def test_every_entry_in_the_table_is_actually_exercised():
    """The earlier version of this file hand-listed four of six entries, so two
    shipped untested. Iterate the table itself so it cannot under-cover again."""
    from shared.claude_client import _UNICODE_DELIMITERS

    for ch, ascii_ in _UNICODE_DELIMITERS.items():
        src = ('{"a":1' + ch + '"b":2}') if ascii_ == "," else ('{"a"' + ch + '1}')
        fixed = _ascii_json_punctuation(src)
        assert fixed is not None and json.loads(fixed), f"{ch!r} not repaired"
