"""SSML hygiene — the "text" field is contractually markup-free.

Gemini put <break time='0.3s'/> tags (single-quote style) into narration "text",
which Edge TTS reads ALOUD and the deck prints as speaker notes. Pinned here:

* shared.text_clean.strip_ssml scrubs SSML tags (allowlisted tag NAMES only —
  legitimate angle brackets in lesson text like "a < b" must survive);
* the script generator parses raw segments to a clean .text while
  .elevenlabs_text KEEPS its breaks (ElevenLabs honors them natively), with the
  quote style normalized to double quotes so the startswith() travel-break
  dedupe in process_director_manifest actually deduplicates.
"""

from __future__ import annotations

import pytest

from shared.text_clean import strip_ssml

# ── strip_ssml: what gets removed ─────────────────────────────────────────────


class TestStripSsmlRemovesTags:
    @pytest.mark.parametrize("tag", [
        "<break time='0.3s'/>",   # Gemini's single-quote style — the live bug
        '<break time="1s" />',    # double quotes, space before the slash
        "<break/>",               # bare self-closing
        "<break time = '2s' >",   # spaces inside the attributes, not self-closed
        "<BREAK TIME='1S'/>",     # case-insensitive
        "</break>",               # closing tag
        "<prosody rate='slow'>",  # other allowlisted SSML tags too
    ])
    def test_ssml_tag_is_stripped(self, tag):
        assert strip_ssml(f"Take a moment. {tag} Now continue.") == \
            "Take a moment. Now continue."

    def test_leftover_double_spaces_collapse(self):
        # Tags are replaced with a space (never glued), then runs collapse.
        assert strip_ssml("one<break/>two  <break time='1s'/>  three") == "one two three"

    def test_a_tag_alone_strips_to_empty(self):
        assert strip_ssml("<break time='2s'/>") == ""


class TestStripSsmlLeavesRealTextAlone:
    def test_plain_text_is_unchanged(self):
        assert strip_ssml("Photosynthesis turns light into food.") == \
            "Photosynthesis turns light into food."

    def test_maths_angle_brackets_survive(self):
        # DELIBERATELY not a generic <[^>]+> strip — comparison operators and
        # inequalities are legitimate lesson text.
        assert strip_ssml("a < b and c > d") == "a < b and c > d"

    def test_a_non_ssml_tag_name_survives(self):
        assert strip_ssml("x <notatag> y") == "x <notatag> y"

    def test_speech_rhythm_punctuation_survives(self):
        # "..." and "—" are the SANCTIONED rhythm devices in plain text.
        assert strip_ssml("It absorbs sunlight... but wait — it gives back.") == \
            "It absorbs sunlight... but wait — it gives back."

    def test_spaced_angle_bracket_prose_survives(self):
        # "< break" with a space is prose, not a tag — "break frequency" is a
        # real filter-design term. The tag name must hug the "<".
        assert strip_ssml("for frequencies < break frequency > the gain falls") == \
            "for frequencies < break frequency > the gain falls"
        assert strip_ssml("x < voice threshold > y") == "x < voice threshold > y"

    def test_paragraph_breaks_survive(self):
        # Only horizontal whitespace collapses — multi-paragraph narration keeps
        # its newlines in speaker notes and on-frame fallback text.
        assert strip_ssml("para one.<break time='1s'/>\n\npara two.") == \
            "para one.\n\npara two."

    def test_falsy_input_returns_empty_string(self):
        assert strip_ssml(None) == ""
        assert strip_ssml("") == ""


# ── the script generator applies the contract at parse time ───────────────────


class _Stub:
    """Claude/Gemini client stub replaying a fixed raw-segments reply."""

    model = "gemini-2.5-flash"

    def __init__(self, segments):
        self._segments = segments

    def analyze(self, prompt, system=None, max_tokens=0, **kw):
        return {"data": {"segments": self._segments}}


def _generate(raw_segments):
    from agent3_scripts.script_generator import generate_episode_script

    analysis = {
        "chapter_title": "Photosynthesis",
        "concepts": {"concepts": [], "dependencies": [], "prerequisites": []},
    }
    episode = {"episode_num": 1, "title": "Photosynthesis",
               "sections_covered": [], "key_concepts_introduced": []}
    return generate_episode_script(episode, analysis, 1, _Stub(raw_segments))


def test_dirty_text_parses_clean_while_elevenlabs_text_keeps_markup():
    # Gemini's exact failure: break tags inside "text" itself.
    script = _generate([{
        "type": "explore",
        "text": "Light lands on the leaf. <break time='0.3s'/> Then the magic starts.",
        "elevenlabs_text":
            'Light lands on the leaf. <break time="0.3s"/> Then the magic starts.',
    }])
    seg = script.segments[0]
    assert seg.text == "Light lands on the leaf. Then the magic starts."
    assert '<break time="0.3s"/>' in seg.elevenlabs_text  # premium pauses kept


def test_single_quoted_elevenlabs_text_is_normalized_to_double_quotes():
    script = _generate([{
        "type": "explore",
        "text": "Pause here.",
        "elevenlabs_text": "Pause here. <break time='0.5s'/> Then go on.",
    }])
    seg = script.segments[0]
    assert seg.elevenlabs_text == 'Pause here. <break time="0.5s"/> Then go on.'
    assert "time='" not in seg.elevenlabs_text


def test_missing_elevenlabs_text_falls_back_to_the_CLEAN_text():
    script = _generate([{
        "type": "explore",
        "text": "One idea. <break time='1s'/> Another idea.",
    }])
    seg = script.segments[0]
    assert seg.text == "One idea. Another idea."
    assert seg.elevenlabs_text == seg.text  # never inherits the dirty markup


VISUAL = {"prompt": "A continuous line drawing of a leaf, white background, minimalist"}


def test_travel_break_is_not_double_prepended_after_quote_normalization():
    # The DRAW_START dedupe is startswith('<break time="0.3s"/>') — before
    # normalization a single-quoted model break slipped past it and the
    # narration opened with TWO breaks.
    script = _generate([{
        "type": "explore",
        "text": "Watch the pen trace the leaf.",
        "elevenlabs_text": "<break time='0.3s'/> Watch the pen trace the leaf.",
        "visual_request": VISUAL,
        "visual_action": "DRAW_START",
    }])
    seg = script.segments[0]
    assert seg.elevenlabs_text.startswith('<break time="0.3s"/>')
    assert seg.elevenlabs_text.count("<break") == 1


def test_travel_break_is_still_inserted_when_the_model_omitted_it():
    script = _generate([{
        "type": "explore",
        "text": "Watch the pen trace the leaf.",
        "elevenlabs_text": "Watch the pen trace the leaf.",
        "visual_request": VISUAL,
        "visual_action": "DRAW_START",
    }])
    assert script.segments[0].elevenlabs_text == \
        '<break time="0.3s"/> Watch the pen trace the leaf.'


class TestSpeakableBlanks:
    """A shipped lesson read worksheet blanks aloud as "underscore underscore
    underscore". The blanks come from fill-in-the-blank exercises copied out
    of the textbook — legitimate PRINTED on a slide, never SPOKEN."""

    def test_underscore_blanks_are_not_spoken(self):
        from shared.text_clean import speakable
        out = speakable("A group of similar cells is called a ____ .")
        assert "_" not in out
        assert out == "A group of similar cells is called a blank."

    def test_the_printed_form_keeps_its_blanks(self):
        """strip_ssml still feeds the deck and the on-frame fallback, where a
        blank is the whole point of the exercise."""
        from shared.text_clean import strip_ssml
        assert "____" in strip_ssml("called a ____ .")

    def test_dot_and_dash_leaders_count_as_blanks(self):
        from shared.text_clean import speakable
        assert "blank" in speakable("The answer is ...... here")
        assert "blank" in speakable("The answer is ----- here")

    def test_ssml_is_still_stripped(self):
        from shared.text_clean import speakable
        assert speakable('Hi <break time="0.3s"/> there') == "Hi there"

    def test_ordinary_prose_is_untouched(self):
        from shared.text_clean import speakable
        s = "Cells group into tissues, tissues into organs."
        assert speakable(s) == s

    def test_a_single_underscore_is_not_a_blank(self):
        """snake_case in a term should not become "blank"."""
        from shared.text_clean import speakable
        assert speakable("the cell_wall layer") == "the cell_wall layer"
