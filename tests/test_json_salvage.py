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

from shared.claude_client import (ClaudeClient, _closes_string, _fix_bad_escapes,
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
        # exactly where they always did
        raw = '{"a": "x", "b": ["p", "q"], "c": {"d": "e"}, "f": "g"}'
        assert json.loads(raw) == X(raw)
        # the index points at the punctuation AFTER the candidate quote
        assert _closes_string(', "x"', 0) is True          # `,` then a string
        assert _closes_string(': "v"', 0) is True          # `:` then a value
        assert _closes_string(': true', 0) is True
        assert _closes_string(', then', 0) is False        # prose
        assert _closes_string(': because', 0) is False     # prose
        assert _closes_string('}', 0) is True
        assert _closes_string('', 0) is True


class TestControlCharsAndEscapes:
    def test_raw_newline_and_tab_inside_a_string(self):
        out = X(doc(seg("Line one.\nLine two.\tTabbed.")))
        assert ok(out) and "Line two." in out["segments"][0]["text"]

    def test_latex_backslash(self):
        out = X(doc(seg("Water is H\\(_2\\)O and 5\\% of mass.")))
        assert ok(out) and "H\\(_2\\)O" in out["segments"][0]["text"]

    def test_valid_escapes_are_left_alone(self):
        s = '{"a": "tab\\tnew\\nquote\\"slash\\\\ uni\\u00e9"}'
        assert _fix_bad_escapes(s) == s
        assert json.loads(s) == X(s)


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

    def test_windows_never_carry_raw_newlines(self):
        f = json_fault('{"a": "x\n\n\n", "b": }')
        assert f and "\n" not in f
