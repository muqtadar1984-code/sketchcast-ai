"""catalogue.reply — the tolerant reading every catalogue job gives a model
reply, on the four shapes measured in the worker log on 2026-09-13 (three
``topic_article`` failures, one ``topic_questions``), plus the shapes the
client's wrapper and a nested reply take. Each fixture reproduces the slip
at the position the decoder reported, inside an otherwise valid document."""

from __future__ import annotations

import json

import pytest

from catalogue.reply import (
    MAX_MENDS, RAW_TEXT, ask_validated, parse_reply_text, reply_fault, strip_fences, unwrap_reply,
)

DOC = {
    "title": "Pressure",
    "objectives": [{"id": "o1", "text": "State the pressure formula."}],
    "sections": [
        {"id": "s1", "heading": "Pressure in Solids",
         "body_md": "Pressure rises with force.\n- \"quoted\" bullets in prose stay put\nAnd covers\": nothing here.",
         "figure_keys": ["pressure_formula_diagram"], "covers": ["8Pf.05"]},
    ],
    "figures": [{"figure_key": "pressure_formula_diagram", "caption": "P = F / A",
                 "spec": {"subject": "Pressure formula", "parts": ["force", "area"]}}],
}
TEXT = json.dumps(DOC, indent=2)
COVERS_AT = TEXT.index('"covers"')
PARTS_AT = TEXT.index('"parts"')


def _slip(text: str, before: str, after: str) -> str:
    assert text.count(before) == 1, before
    return text.replace(before, after)


# ── the measured shapes ─────────────────────────────────────────────────


def test_a_key_that_lost_its_opening_quote_is_restored_at_the_fault_position():
    # 07:49:43 UTC — ``covers": [`` at char 2857 of 19246.
    parsed, repairs = parse_reply_text(_slip(TEXT, '"covers": [', 'covers": ['))
    assert parsed == DOC
    assert repairs == [f"opening quote restored on key 'covers' at char {COVERS_AT}"]


def test_a_second_key_shape_spec_is_the_same_slip():
    # 07:48:59 UTC — ``spec": {`` at char 17552 of 18274.
    parsed, repairs = parse_reply_text(_slip(TEXT, '"spec": {', 'spec": {'))
    assert parsed == DOC and repairs[0].startswith("opening quote restored on key 'spec'")


def test_a_markdown_bullet_in_front_of_a_key_is_removed():
    # 07:48:59 UTC — ``- "parts": [`` at char 14089 of 15305.
    parsed, repairs = parse_reply_text(_slip(TEXT, '"parts": [', '- "parts": ['))
    assert parsed == DOC and repairs == [f"stray bullet removed before a key at char {PARTS_AT}"]


def test_a_pair_written_inside_an_array_is_dropped_not_hoisted():
    # 02:11:42 UTC — ``"misconception_ref": "m3"`` between an options array's
    # last element and its ``]``: "Expecting ',' delimiter" at char 7334 of 29010.
    items = {"items": [{"stem": "Why?", "options": [{"key": "A", "text": "x"}, {"key": "B", "text": "y"}],
                        "answer": "B"}]}
    text = json.dumps(items)
    last = '{"key": "B", "text": "y"}'
    assert text.count(last) == 1
    broken = text.replace(last, last + ',\n      "misconception_ref": "m3"\n    ')
    parsed, repairs = parse_reply_text(broken)
    assert parsed == items, "the options keep both elements; the stray pair is gone"
    assert len(repairs) == 1 and repairs[0].startswith("stray pair 'misconception_ref' dropped from an array at char")


def test_several_slips_in_one_reply_are_mended_one_after_another():
    text = _slip(_slip(_slip(TEXT, '"covers": [', 'covers": ['), '"spec": {', 'spec": {'), '"parts": [', '- "parts": [')
    parsed, repairs = parse_reply_text(text)
    assert parsed == DOC and len(repairs) == 3


# ── what must NOT be touched ────────────────────────────────────────────


def test_the_same_characters_inside_a_body_string_are_prose_and_stay():
    parsed, repairs = parse_reply_text(TEXT)
    assert parsed == DOC and repairs == []
    assert '- "quoted"' in parsed["sections"][0]["body_md"] and 'covers":' in parsed["sections"][0]["body_md"]


def test_a_value_followed_by_a_colon_inside_an_object_is_left_to_the_shared_salvage():
    # ``{"who": "who": "teacher"}`` is the duplicated-key slip the client's
    # own repair reads; the array rule must not consume the value as a key.
    parsed, repairs = parse_reply_text('{"items": [{"who": "who": "teacher"}]}')
    assert parsed == {"items": [{"who": "teacher"}]}
    assert repairs == ["salvaged by the shared JSON repair"]


def test_an_unknown_fault_is_not_mended_and_the_reply_is_reported_unreadable():
    # Severed after a comma: the shared salvage refuses to close a reply
    # that stopped mid-thought, and nothing here is a known slip.
    parsed, repairs = parse_reply_text('{"items": [1,')
    assert parsed is None and repairs == []
    assert reply_fault({RAW_TEXT: '{"items": [1,'}).startswith("Expecting value at line 1 col 14 (char 13 of 13)")


def test_mends_are_bounded():
    text = "{" + ", ".join(f'k{i}": {i}' for i in range(MAX_MENDS + 5)) + "}"
    parsed, repairs = parse_reply_text(text)
    assert parsed is None and len(repairs) == MAX_MENDS


# ── wrappers ────────────────────────────────────────────────────────────


def test_the_client_s_raw_text_wrapper_is_read_and_a_fenced_reply_is_unfenced():
    fenced = "```json\n" + _slip(TEXT, '"covers": [', 'covers": [') + "\n```"
    assert strip_fences("```json\n{}\n```") == "{}"
    parsed, repairs = unwrap_reply({RAW_TEXT: fenced}, "objectives")
    assert parsed == DOC and len(repairs) == 1
    parsed, repairs = unwrap_reply(fenced, "objectives")
    assert parsed == DOC, "a bare string is read the same way"


def test_a_reply_nested_under_one_wrapper_key_is_opened():
    parsed, repairs = unwrap_reply({"article": DOC}, "objectives")
    assert parsed == DOC and repairs == ["reply unwrapped from 'article'"]
    parsed, repairs = unwrap_reply({"items": [1]}, "items")
    assert parsed == {"items": [1]} and repairs == [], "a reply that carries the key is not unwrapped"
    parsed, _ = unwrap_reply({"article": DOC, "notes": "x"}, "objectives")
    assert parsed == {"article": DOC, "notes": "x"}, "two keys is not a wrapper"
    parsed, _ = unwrap_reply({"article": [DOC]}, "objectives")
    assert parsed == {"article": [DOC]}, "a wrapper around a list is not opened"


def test_an_unreadable_reply_stays_a_wrapper_with_the_decoder_s_reason():
    raw, repairs = unwrap_reply({RAW_TEXT: '{"objectives": [1,'}, "objectives")
    assert raw == {RAW_TEXT: '{"objectives": [1,'} and repairs == []
    assert reply_fault(raw).startswith("Expecting value at line 1 col 19 (char 18 of 18)")
    refusal = "Sorry, I cannot write this article."
    assert unwrap_reply(refusal, "objectives") == ({RAW_TEXT: refusal}, [])
    assert reply_fault({RAW_TEXT: refusal}).startswith("Expecting value at line 1 col 1 (char 0 of 35)")
    assert reply_fault({RAW_TEXT: "   "}) == "empty reply"
    assert reply_fault(DOC) is None and reply_fault("text") is None and reply_fault([]) is None


def test_anything_else_is_returned_as_it_came():
    for raw in ([], [1, 2], None, 3, {"a": 1, "b": 2}):
        assert unwrap_reply(raw, "items") == (raw, [])


# ── the retry ───────────────────────────────────────────────────────────


class Refused(RuntimeError):
    pass


def test_a_refused_reply_earns_exactly_one_more_call_and_the_second_answer_is_returned():
    replies = iter(["bad", "good"])
    calls = []

    def ask():
        calls.append(1)
        return next(replies)

    def validate(raw):
        if raw != "good":
            raise Refused(f"reply was {raw}")
        return raw.upper()

    stage: dict = {}
    assert ask_validated(ask, validate, invalid=Refused, what="article t1", stage=stage) == "GOOD"
    assert len(calls) == 2
    assert stage["reply_retries"] == ["article t1: Refused: reply was bad"]


def test_two_refusals_propagate_the_second_and_never_a_third_call():
    calls = []

    def ask():
        calls.append(1)
        return "bad"

    def validate(raw):
        raise Refused(f"attempt {len(calls)}")

    stage: dict = {"reply_retries": ["earlier: Refused: x"]}
    with pytest.raises(Refused, match="attempt 2"):
        ask_validated(ask, validate, invalid=Refused, what="questions t1", stage=stage)
    assert len(calls) == 2
    assert stage["reply_retries"] == ["earlier: Refused: x", "questions t1: Refused: attempt 1"], "appended, not overwritten"


def test_a_transport_error_is_not_the_validator_s_class_and_is_not_retried():
    calls = []

    def ask():
        calls.append(1)
        raise TimeoutError("upstream")

    with pytest.raises(TimeoutError):
        ask_validated(ask, lambda raw: raw, invalid=Refused, what="article t1")
    assert len(calls) == 1


def test_a_good_first_reply_is_one_call_and_leaves_no_trace_in_the_stage():
    stage: dict = {}
    assert ask_validated(lambda: "good", lambda raw: raw, invalid=Refused, what="x", stage=stage) == "good"
    assert stage == {}
