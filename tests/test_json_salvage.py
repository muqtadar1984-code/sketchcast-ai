"""The JSON salvage for model replies — the gaps found after the founder's
Cells Part 2 lesson (gen 16228b9e, 2026-09-04) failed with a complete,
54,119-character reply the repair could not parse. The raw reply died with
the container, so this pins every shape the repair could NOT handle at the
time (each measured FAILING before the fix) and the fault window that makes
the next failure diagnosable from the job error alone."""

from __future__ import annotations

import json
import logging

import pytest

from shared.claude_client import (ClaudeClient, _escape_inner_quotes, _fix_bad_escapes,
                                  _repair_json, json_fault)

X = ClaudeClient._extract_json


def seg(text, el=None):
    el = text if el is None else el
    return ('{"type": "hook", "text": "%s", "elevenlabs_text": "%s", '
            '"slide_heading": "H", "slide_points": ["a"]}' % (text, el))


def doc(*segs):
    return '{\n  "segments": [\n' + ",\n".join(segs) + '\n  ],\n  "visual_plan": {"chapters": []}\n}'


def ok(out) -> bool:
    return isinstance(out, dict) and bool(out.get("segments"))


@pytest.fixture(autouse=True)
def _quiet():
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


class TestProseQuotes:
    """A quoted word followed by a comma or colon is English, not JSON."""

    def test_quoted_word_then_comma(self):
        out = X(doc(seg('called "organelles", tiny structures inside.')))
        assert ok(out)
        assert out["segments"][0]["text"] == 'called "organelles", tiny structures inside.'

    def test_quoted_word_then_colon(self):
        out = X(doc(seg('Ask yourself "why?": because bones are strong.')))
        assert ok(out) and '"why?": because' in out["segments"][0]["text"]

    def test_nested_quotes_in_a_dialogue_line(self):
        raw = ('{"segments":[{"type":"hook","text":"x","dialogue":'
               '[{"who":"teacher","line":"She said "go", then left."}]}]}')
        out = X(raw)
        assert ok(out) and out["segments"][0]["dialogue"][0]["line"] == 'She said "go", then left.'

    def test_ssml_and_prose_quotes_together(self):
        out = X(doc(seg('<break time="0.3s"/> called "organelles", tiny.')))
        assert ok(out)

    def test_real_closers_still_close(self):
        # keys, string array elements, and a string before a closer all end
        # exactly where they always did — valid JSON is a no-op for the walk
        raw = ('{"a": "x", "b": ["p", "q", 3, true, null, {"n": "m"}], "c": {"d": "e", "f": [["g"]]},'
               ' "h": "", "i": "ends with \\\\", "j": "quote \\" inside", "k": -1.5, "l": false}')
        assert _escape_inner_quotes(raw) == raw
        assert json.loads(raw) == X(raw)

    def test_enumerations_in_prose(self):
        """The review's gap: '"cell", "tissue" and "organ"' — a quoted word
        followed by a comma and ANOTHER quoted word is not a key that follows."""
        out = X(doc(seg('Remember the words "cell", "tissue" and "organ" today.')))
        assert ok(out) and out["segments"][0]["text"] == 'Remember the words "cell", "tissue" and "organ" today.'
        # a quoted word, a colon, then something that could be a JSON value
        # ('"photosynthesis": 6CO2') is indistinguishable from a mangled
        # object ('"who":"alice":"bob"'); the layer refuses rather than guess
        # (see test_semantic_prompt: repairs never choose between readings)
        out = X(doc(seg('Recall "photosynthesis": 6CO2 + 6H2O makes sugar.')))
        assert not ok(out), "ambiguous — must stay a loud failure, not a guessed lesson"
        out = X(doc(seg('called "organelles", 2 of which matter.')))
        assert ok(out) and '"organelles", 2 of' in out["segments"][0]["text"]

    def test_maths_angle_brackets_across_strings_do_not_corrupt_structure(self):
        """The review's regression: a `<` in one string and a `>` in a later
        one must never be read as one tag — the structural quotes between
        them were being rewritten, dropping a key or merging two points."""
        raw = doc(seg('Since 3 < 5, three is smaller.', 'and 7 > 2 is also true.'),
                  '{"type": "hook", "text": "t", "elevenlabs_text": "t", "slide_heading": "H", "slide_points": ["a",]}')
        out = X(raw)
        assert ok(out)
        assert out["segments"][0]["elevenlabs_text"] == "and 7 > 2 is also true.", "the key survives"
        raw = doc('{"type": "hook", "text": "x <break time="0.3s"/> y", "elevenlabs_text": "x", '
                  '"slide_heading": "H", "slide_points": ["3 < 5", "7 > 2"]}')
        out = X(raw)
        assert ok(out) and out["segments"][0]["slide_points"] == ["3 < 5", "7 > 2"], "two points, not one"

    def test_terse_maths_letters_after_an_angle_bracket_are_not_tags(self):
        """Second review pass: `<p`, `<s`, `<w` matched single-letter SSML
        names and a tag spanned two strings again."""
        raw = doc(seg("Since 0<p and", "p>1 holds."),
                  '{"type": "hook", "text": "t", "elevenlabs_text": "t", "slide_heading": "H", "slide_points": ["a",]}')
        out = X(raw)
        assert ok(out)
        assert out["segments"][0]["text"] == "Since 0<p and"
        assert out["segments"][0]["elevenlabs_text"] == "p>1 holds."

    def test_trailing_comma_after_a_string_value_is_still_repairable(self):
        """Second review pass: the object branch accepted a comma only before a
        key, so the model's favourite slip — a trailing comma after the last
        dialogue line — swallowed the rest of the reply."""
        raw = ('{"segments": [{"type": "hook", "text": "x", "dialogue": '
               '[{"who": "teacher", "line": "Hi.",\n}]}], "visual_plan": {"chapters": []}}')
        out = X(raw)
        assert ok(out) and out["segments"][0]["dialogue"][0]["line"] == "Hi."

    def test_a_mis_nested_closer_after_a_string_still_reaches_rebalance(self):
        raw = '{"segments": [{"type": "hook", "text": "Hi."], "visual_plan": {"chapters": []}}'
        out = X(raw)
        assert ok(out) and out["segments"][0]["text"] == "Hi."


class TestControlCharsAndEscapes:
    def test_raw_newline_and_tab_inside_a_string(self):
        out = X(doc(seg("Line one.\nLine two.\tTabbed.")))
        assert ok(out) and "Line two." in out["segments"][0]["text"]

    def test_latex_backslash(self):
        out = X(doc(seg("Water is H\\(_2\\)O and 5\\% of mass.")))
        assert ok(out) and "H\\(_2\\)O" in out["segments"][0]["text"]

    def test_valid_escapes_are_left_alone(self):
        s = '{"a": "tab\\tnew\\nquote\\"slash\\\\ uni\\u00e9 pair-then-quote\\\\\\" end\\\\"}'
        assert _fix_bad_escapes(s) == s
        assert json.loads(s) == X(s)

    def test_over_escaped_apostrophe_and_bare_unicode_escape(self):
        out = X(doc(seg("It\\'s the cell\\'s job.")))
        assert ok(out) and out["segments"][0]["text"] == "It's the cell's job."
        out = X(doc(seg("Write \\underline{x} here.")))
        assert ok(out) and "\\underline{x}" in out["segments"][0]["text"]


class TestSsmlTags:
    @pytest.mark.parametrize("tag", [
        '<break time="0.3s"/>', '<break time="0.3s" />', '<break strength="medium"/>',
        '<prosody rate="slow">slow</prosody>', '<emphasis level="strong">x</emphasis>',
        '<say-as interpret-as="characters">DNA</say-as>',
    ])
    def test_every_tag_with_attribute_quotes_parses(self, tag):
        out = X(doc(seg(f"A {tag} b.")))
        assert ok(out)
        assert "'" in out["segments"][0]["text"], "attribute quotes become single quotes, valid SSML"

    def test_already_escaped_tags_are_untouched(self):
        raw = doc(seg('A <break time=\\"0.3s\\"/> b.'))
        out = X(raw)
        assert ok(out) and out["segments"][0]["text"] == 'A <break time="0.3s"/> b.'


class TestStillLoud:
    def test_a_truncated_reply_is_not_invented(self):
        # cut mid-value: the salvage must NOT hand back a plausible lesson
        raw = doc(seg("Complete."), seg("Half way")).rsplit('"slide_heading"', 1)[0]
        out = _repair_json(raw)
        assert out is None or not out.get("visual_plan"), "a severed reply stays a failure or a strict prefix"

    def test_garbage_stays_raw(self):
        out = X("not json at all")
        assert out == {"raw_text": "not json at all"}


class TestFaultWindow:
    def test_names_the_position_and_shows_the_text_around_it(self):
        raw = '{"segments": [{"text": "called "organelles", tiny"}]}'
        f = json_fault(raw)
        assert f and "char" in f and "organelles" in f
        assert f.startswith("Expecting") or "delimiter" in f

    def test_none_when_it_parses(self):
        assert json_fault('{"a": 1}') is None
        assert json_fault('{"a": "raw\nnewline"}') is None, "strict=False, like the salvage"

    def test_windows_never_carry_raw_control_characters(self):
        f = json_fault('{"a": "x\n\n\t\x0c\x00", "b": }')
        assert f and not any(ch in f for ch in "\n\t\x0c\x00")

    def test_bytes_do_not_raise(self):
        f = json_fault(b'{"a": }')
        assert isinstance(f, str) and "char" in f


class TestWrongCloser:
    """gen f0e65c2f (2026-09-04, the founder's retry): the action object was
    closed with `]` instead of `}` — `"layers": ["nucleus"] ] ],`. The
    omission reading over-closed and the reply still failed."""

    BAD = '''{
  "segments": [
    {
      "type": "hook", "text": "Cells.", "elevenlabs_text": "Cells.", "slide_heading": "H", "slide_points": ["a"],
      "scene": {
        "steps": [
          {
            "actions": [
              {
                "target": "cell_drawing",
                "layers": [
                  "nucleus"
                ]
              ]
            ],
            "key_point": "The nucleus is the control center, directing the cell."
          },
          {
            "actions": [ { "target": "x", "layers": ["y"] } ],
            "key_point": "Second step."
          }
        ]
      }
    }
  ],
  "visual_plan": {"chapters": [{"title": "c", "elements": ["particles"]}]}
}'''

    def test_the_measured_shape_parses_with_the_structure_intact(self):
        from shared.claude_client import _substitute_closers
        out = X(self.BAD)
        assert ok(out)
        steps = out["segments"][0]["scene"]["steps"]
        assert len(steps) == 2
        assert steps[0]["key_point"].startswith("The nucleus"), "key_point stays inside ITS step"
        assert steps[0]["actions"] == [{"target": "cell_drawing", "layers": ["nucleus"]}]
        assert out["visual_plan"]["chapters"][0]["title"] == "c"
        assert _substitute_closers(self.BAD) is not None

    def test_valid_json_and_truncated_replies_are_left_alone(self):
        from shared.claude_client import _substitute_closers
        assert _substitute_closers('{"a": [1, 2], "b": {"c": "d"}}') is None, "nothing to swap"
        assert _substitute_closers('{"a": [1, 2], "b": {"c": "d"') is None, "no swap, only missing closers"
        assert _substitute_closers('{"a": [1, 2}, "b": ') is None, "severed tail stays loud"

    def test_the_omission_reading_still_wins_where_it_is_right(self):
        # a } arriving while an array is open, with nothing wrong-typed —
        # the rebalancer's original case
        raw = '{"segments": [{"type": "hook", "text": "x", "slide_points": ["a"}], "visual_plan": {"chapters": []}}'
        out = X(raw)
        assert ok(out) and out["segments"][0]["slide_points"] == ["a"]
