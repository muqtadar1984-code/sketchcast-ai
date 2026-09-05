"""The JSON salvage for model replies — the gaps found after the founder's
Cells Part 2 lesson (gen 16228b9e, 2026-09-04) failed with a complete,
54,119-character reply the repair could not parse. The raw reply died with
the container, so this pins every shape the repair could NOT handle at the
time (each measured FAILING before the fix) and the fault window that makes
the next failure diagnosable from the job error alone."""

from __future__ import annotations

import json
import logging
from pathlib import Path

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
        # A quoted word, a colon, then something that could be a JSON value
        # ('"photosynthesis": 6CO2') used to be refused as indistinguishable
        # from a mangled object ('"who":"alice":"bob"'). It is not: the quote
        # count inside the string separates them. Here it is ODD — the pair
        # opened at "photo… is still open, so its second half cannot also end
        # the string. This is the shape that cost Sara Hamaydeh her first
        # lesson (gen eb12963c, 2026-09-05) as an Arabic gloss.
        out = X(doc(seg('Recall "photosynthesis": 6CO2 + 6H2O makes sugar.')))
        assert ok(out) and out["segments"][0]["text"] == 'Recall "photosynthesis": 6CO2 + 6H2O makes sugar.'
        # The mangled object it was confused with keeps its EVEN count (zero
        # inner quotes) and still fails loudly — see test_semantic_prompt's
        # "repairs never choose between two readings", which pins it.
        assert _repair_json('{"d": [{"who":"alice":"bob"}]}') is None
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

class TestSaraLostLesson:
    """gen eb12963c / issue 24b6cadd, 2026-09-05. Sara Hamaydeh signed up at
    14:05 UTC, clicked Generate at 14:16 on "Islamic y6", and the lesson never
    arrived. Her third attempt (job 8e3d26dd, 14:42) came back COMPLETE —
    22,538 chars, 6,168 output tokens against a 30,000 cap, the provider
    reporting no truncation — and would not parse at char 14,380.

    Two shapes are pinned here because the incident had two readings. The
    stored evidence (300 characters, see tests/test_failure_evidence.py) ended
    on "… natural land" and could only support the INFERENCE that a quote had
    been left bare in the prose; the 500-character jobs.error row, read back
    off the live table, showed the actual malformation — an assets object
    closed twice. Both are real, both failed the whole lesson, and both are
    fixed; the fixtures keep them apart so a future reader is not misled the
    same way.
    """

    FIX = Path(__file__).resolve().parent / "fixtures"

    def _shapes(self):
        return json.loads((self.FIX / "inner_quote_shapes.json").read_text(encoding="utf-8"))

    def _value(self, case, out):
        """The one field the shape was built around, or None if nothing parsed."""
        if not isinstance(out, dict) or set(out) == {"raw_text"}:
            return None
        segs = out.get("segments") or []
        if not segs:
            return None
        if case["kind"] == "array":
            return segs[0].get("slide_points")
        dialogue = segs[0].get("dialogue") or []
        return dialogue[0].get("line") if dialogue else None

    def test_the_reply_that_failed_now_yields_the_lesson(self):
        """The whole 22,538-char reply, not a reduction. Ten segments and the
        visual plan — a repair that produced segments and dropped the plan
        would render every one of them as a plain card."""
        raw = (self.FIX / "sara_islamic_y6_script_reply.txt").read_text(encoding="utf-8")
        assert len(raw) == 22538, "the fixture is the measured size"
        out = X(raw)
        assert ok(out) and len(out["segments"]) == 10
        assert out["visual_plan"]["chapters"], "the plan must survive with the segments"
        lines = [d["line"] for s in out["segments"] for d in (s.get("dialogue") or [])]
        assert any('"khalifah": "a caretaker who answers for the keys"' in ln for ln in lines), \
            "the gloss reaches the lesson with the model's own quotation marks"

    def test_the_ab_control_still_parses(self):
        """Same length, same fault offset, same window — only the sub-shape at
        the bare quote differs. It repaired before the fix and must still, or
        the change traded one failure for another."""
        raw = (self.FIX / "sara_islamic_y6_plain_quote_reply.txt").read_text(encoding="utf-8")
        out = X(raw)
        assert ok(out) and len(out["segments"]) == 10 and out["visual_plan"]["chapters"]

    def test_no_shape_is_ever_silently_rewritten(self):
        """THE acceptance rule, over all sixteen measured shapes: each one is
        either repaired to exactly what the model wrote, or refused outright.
        A parse carrying different words is the outcome none of them may have
        — a teacher would ship it without ever knowing."""
        for case in self._shapes()["cases"]:
            got = self._value(case, X(case["raw_reply"]))
            assert got is None or got == case["intended"], \
                f"{case['name']}: silently rewritten to {got!r}"

    def test_the_shapes_that_used_to_fail_now_repair(self):
        """S5/S6 were LOUD before the fix — a complete lesson thrown away over
        a colon. S15 was CORRUPTED, which is worse, and is now refused."""
        by_name = {c["name"]: c for c in self._shapes()["cases"]}
        for name in ("S5_quoted_word_comma_quoted_word_colon",
                     "S6_quoted_word_colon_then_quoted_gloss"):
            case = by_name[name]
            assert self._value(case, X(case["raw_reply"])) == case["intended"], name

    def test_an_arabic_gloss_survives_the_colon(self):
        """The sub-shape the 300-character evidence pointed at, in miniature:
        a transliterated name, a colon, and a quoted translation. Prose, not a
        mangled object — the quote count inside the string says so."""
        raw = ('{"segments":[{"type":"hook","text":"x","dialogue":[{"who":"teacher",'
               '"line":"The name "Ar-Rahman": "the Most Merciful" is the one we say most."}]}]}')
        out = X(raw)
        assert ok(out)
        assert out["segments"][0]["dialogue"][0]["line"] == \
            'The name "Ar-Rahman": "the Most Merciful" is the one we say most.'

    def test_an_enumeration_in_an_array_element_is_refused_not_split(self):
        """The only SILENT corruption in the class, and the reason the walk
        refuses instead of preferring its own reading: both readings parse.
        Splitting the pair invents two slide points out of one, in words no
        model wrote, and every downstream check waves them through."""
        raw = ('{"segments":[{"type":"hook","text":"x","slide_heading":"H",'
               '"slide_points":["remember "amanah", "khalifah" today","then pray"]}]}')
        out = X(raw)
        assert not ok(out), "two readings both parse — this must stay a loud failure"
        assert _repair_json(raw) is None, "and the refusal ends the WHOLE repair"
        assert 'khalifah" today' not in json.dumps(out), "the split words never reach a caller"

    def test_a_legitimately_escaped_quote_at_a_strings_end_is_left_alone(self):
        """The walk counts the quotes the MODEL escaped as well as its own, so
        a value that properly ends on \\" has an even count and closes where it
        always did. Valid JSON stays a no-op, byte for byte."""
        raw = ('{"segments":[{"type":"hook","text":"x","dialogue":[{"who":"teacher",'
               '"line":"The whole class answered together, \\"Ameen.\\""}]}]}')
        assert _escape_inner_quotes(raw) == raw, "nothing to escape — a no-op"
        out = X(raw)
        assert ok(out)
        assert out["segments"][0]["dialogue"][0]["line"] == \
            'The whole class answered together, "Ameen."'

    def test_parity_never_overrules_a_reading_that_actually_parses(self):
        """Parity is a claim about prose, so it loses to evidence. Here the
        model really did drop the closing half of its pair: reading the comma
        as prose swallows the rest of the object and terminates nothing, so the
        OLD reading is the only one that is a document and it wins — keeping
        `b`, and the lesson, at the cost of one quotation mark."""
        assert _escape_inner_quotes('{"a":"say "hi","b":2}') == '{"a":"say \\"hi","b":2}'
        assert X('{"a":"say "hi","b":2}') == {"a": 'say "hi', "b": 2}

    def test_where_both_readings_are_documents_the_repair_refuses(self):
        """The other half of the same rule. Both readings of this object parse
        — one keeps `slide_heading` as a key, the other reads it as prose the
        text swallowed — so there is nothing to choose between and the reply
        fails loudly instead of shipping one of them."""
        raw = '{"segments":[{"type":"hook","text":"The "Big Idea","slide_heading":"H"}]}'
        assert _repair_json(raw) is None
        assert set(X(raw)) == {"raw_text"}


class TestSuperfluousCloser:
    """The malformation the live jobs.error row recorded for Sara's third
    attempt, which the 300-character issue context had cut away:

        "assets": {"creation_scene": "…Allah's creation effortlessly."}},
        "semantic_regions": ["allah_central_script", …

    The assets object closed twice. The second `}` shut the CHAPTER, leaving
    its remaining keys stranded in the chapters ARRAY, and json.loads stopped
    at the colon after "semantic_regions" — char 14,380 of 22,538.
    """

    BAD = ('{"segments":[{"type":"hook","text":"Allah made all of this.",'
           '"slide_heading":"The names of Allah"}],'
           '"visual_plan":{"chapters":[{"id":"chapter_1","concept":"asmaa_ul_husna",'
           '"assets":{"creation_scene":"A natural landscape, illustrating creation."}},'
           '"semantic_regions":["allah_central_script"],'
           '"steps":[{"segment":1,"decision":"EXTEND","actions":[]}]}]}}')

    def test_the_measured_shape_parses_with_the_chapter_intact(self):
        out = X(self.BAD)
        assert ok(out)
        chapter = out["visual_plan"]["chapters"][0]
        assert chapter["id"] == "chapter_1"
        assert chapter["assets"]["creation_scene"].startswith("A natural landscape")
        assert chapter["semantic_regions"] == ["allah_central_script"], \
            "the stranded keys go back INSIDE the chapter, not beside it"
        assert chapter["steps"][0]["segment"] == 1

    def test_the_neighbouring_rules_cannot_reach_it(self):
        """Pins WHY a fourth rule exists: the closer is present, matched and
        well-formed, so the omission and substitution readings both decline."""
        from shared.claude_client import _rebalance_json, _substitute_closers
        for rule in (_rebalance_json, _substitute_closers):
            out = rule(self.BAD)
            if out is None:
                continue
            with pytest.raises(json.JSONDecodeError):
                json.loads(out, strict=False)  # a candidate, but never a document

    def test_it_fires_only_where_no_other_reading_exists(self):
        from shared.claude_client import _drop_superfluous_closers
        assert _drop_superfluous_closers('{"a": {"b": 1}, "c": 2}') is None, \
            "a closer returning into an OBJECT is ordinary, valid JSON"
        assert _drop_superfluous_closers('{"a": [{"b": 1}, {"c": 2}]}') is None, \
            "a closer returning into an array followed by an ELEMENT is fine"
        assert _drop_superfluous_closers('{"a": [{"b": 1}], "c": 2}') is None, \
            "nothing dropped, nothing to report"
        assert _drop_superfluous_closers('{"a": [{"b": 1}}, "c": 2') is None, \
            "a severed tail stays loud"
        assert _drop_superfluous_closers('{"a": [1, 2}, "b": 3') is None, \
            "a MIS-nested closer belongs to _substitute_closers"

    def test_valid_json_is_untouched(self):
        payload = {"segments": [{"type": "hook"}],
                   "visual_plan": {"chapters": [{"id": "c1", "assets": {"k": "v"}}]}}
        assert X(json.dumps(payload)) == payload
