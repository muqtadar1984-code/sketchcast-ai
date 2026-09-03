"""Strip SSML markup out of text that must be plain.

The clean-text contract for narration segments: ``text`` is plain prose (spoken
by Edge TTS, shown as deck speaker notes and as the on-frame fallback), while
``elevenlabs_text`` carries the ``<break>`` markup (ElevenLabs honors it
natively). Gemini has put ``<break time='0.3s'/>`` tags into ``text``, where
Edge reads them ALOUD and the deck prints them — this module is the one place
that scrubs them.
"""

from __future__ import annotations

import re

# Allowlist of SSML tag NAMES only — deliberately NOT a generic <[^>]+> strip,
# because legitimate angle brackets in lesson text ("a < b", "x <notatag> y")
# must survive. Tolerates single/double/no quotes, self-closing or not, and
# closing tags; case-insensitive. The tag name must IMMEDIATELY follow "<" or
# "</": no whitespace tolerance there, or prose like
# "for frequencies < break frequency ... >" (a real filter-design phrase)
# would have the whole span eaten. No model emits "< break" with a space.
_SSML_TAG_RE = re.compile(
    r"</?(?:break|prosody|emphasis|say-as|speak|voice|phoneme)\b[^>]*?>",
    re.IGNORECASE,
)


# Fill-in-the-blank rules copied out of the textbook: "called a ____ .".
# Edge reads every underscore ALOUD ("underscore underscore underscore"), which
# is how a lesson shipped narrating punctuation. Two or more underscores, or a
# run of dots/dashes doing the same job, is a blank.
_BLANK_RE = re.compile(r"[ \t]*(?:_{2,}|\.{4,}|…{2,}|-{4,})[ \t]*")


def _speakable_prose(span: str) -> str:
    """The blank and punctuation rules, applied to a run of PROSE only. Does
    not strip tags and does not trim the ends — callers decide both."""
    out = _BLANK_RE.sub(" blank ", span)
    out = re.sub(r"\s+([,.;:!?])", r"\1", out)     # no space before punctuation
    return re.sub(r"[ \t]{2,}", " ", out)


def speakable(s) -> str:
    """Plain text prepared for a provider that reads tags ALOUD (Edge).

    A worksheet blank must not be SPOKEN, but it is legitimate PRINTED on a
    slide or in speaker notes, so this is deliberately separate from
    strip_ssml: only the audio path calls it. The blank becomes the word a
    teacher actually says, which keeps the sentence grammatical — dropping it
    outright leaves "called a ." and reads as a stumble.
    """
    out = strip_ssml(s)
    if not out:
        return ""
    return _speakable_prose(out).strip()


def speakable_ssml(s) -> str:
    """The MARKUP copy prepared for a provider that honours it (ElevenLabs
    today, Google next): blanks become the spoken word, prose is tidied, and
    every allow-listed tag survives byte for byte.

    This is the function the composer should have been given for the
    ``ssml_text`` argument. It was given ``speakable()`` instead, which begins
    with ``strip_ssml`` — so from commit e898f49 (2026-09-02) every
    ``<break time="…"/>`` the script generator writes, including the travel
    pause it injects at the top of each segment, was deleted before ANY
    provider saw it. ElevenLabs received plain prose; the "premium pauses"
    the prompt is built around never reached the voice. Measured: a real
    lesson carries 146 breaks (~90 s of intended silence), all dropped.

    The prose between tags is cleaned span by span, so a blank rule can never
    reach inside a tag, and a tag can never be mistaken for a blank.
    """
    if not s:
        return ""
    text = str(s)
    parts: list[str] = []
    last = 0
    for m in _SSML_TAG_RE.finditer(text):
        parts.append(_speakable_prose(text[last:m.start()]))
        parts.append(m.group(0))
        last = m.end()
    parts.append(_speakable_prose(text[last:]))
    out = re.sub(r"[ \t]{2,}", " ", "".join(parts))
    return out.strip()


def strip_ssml(s) -> str:
    """Return `s` with SSML tags removed and horizontal whitespace re-collapsed.

    Tags are replaced with a space (never glued: "a<break/>b" → "a b"), then
    runs of spaces/tabs collapse to one space. Newlines survive — multi-
    paragraph narration keeps its paragraph breaks in speaker notes and
    on-frame fallback text. Falsy input returns "".
    """
    if not s:
        return ""
    out = _SSML_TAG_RE.sub(" ", str(s))
    out = re.sub(r"[ \t]{2,}", " ", out)          # collapse space runs, keep \n
    out = re.sub(r"[ \t]*\n[ \t]*", "\n", out)    # trim spaces hugging newlines
    return out.strip()
