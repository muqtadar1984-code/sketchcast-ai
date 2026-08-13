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
