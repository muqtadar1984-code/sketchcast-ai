"""Which provider serves which language.

Routing is by SCRIPT FAMILY, not by language, because that is what the evidence
is actually about. Two measurements drove this:

  * Kimi and DeepSeek both collapsed on Arabic during the India pricing work.
  * Haiku's Jawi orthography was unreliable enough that worker/process.py has
    carried a hard-coded `if jawi` exception since 2026-07-19.

The second one matters more than it looks: Jawi is MALAY written in the
Arabic script. A model's Arabic score told us nothing about its Jawi
orthography, and a model's Malay score told us nothing either. The thing that
predicted failure was the script the glyphs are drawn in — so that is what the
table keys on.

Anything not named here falls to the default. That is deliberate: a new
language should get the well-tested majority path, not an error.
"""

from __future__ import annotations

# Arabic-derived scripts. Includes languages the app does not serve yet, so the
# policy is complete before the language ships rather than after it breaks.
#   ar  Arabic        ur  Urdu         fa  Persian
#   ps  Pashto        ckb Sorani       sd  Sindhi
#   ms-arab  Jawi — Malay in the Arabic script
_ARABIC_SCRIPT = frozenset({"ar", "ur", "fa", "ps", "ckb", "sd", "ms-arab"})

# Han-derived scripts. RESERVED — no CJK language is served today and no client
# is implemented, so routing here raises rather than silently falling back to a
# provider we have not evaluated for these scripts.
#   zh  Chinese (incl. -hans/-hant)    ja  Japanese
# Korean is deliberately ABSENT: Hangul is alphabetic, not Han-derived, and
# behaves far more like the default path than like Chinese or Japanese.
_CJK_SCRIPT = frozenset({"zh", "zh-hans", "zh-hant", "zh-cn", "zh-tw", "ja"})

ANTHROPIC = "anthropic"
GEMINI = "gemini"
KIMI = "kimi"

#: Providers with a working client. KIMI is routable policy but not implemented.
IMPLEMENTED = frozenset({ANTHROPIC, GEMINI})


def script_family(language: str | None) -> str:
    """`arabic`, `cjk`, or `default` for a language code."""
    if not language:
        return "default"
    code = language.strip().lower().replace("_", "-")
    if code in _ARABIC_SCRIPT:
        return "arabic"
    if code in _CJK_SCRIPT:
        return "cjk"
    # Match a bare primary subtag too, so "ar-EG" or "zh-Hans-CN" route the
    # same as "ar" and "zh" without needing every regional variant enumerated.
    primary = code.split("-", 1)[0]
    if primary in _ARABIC_SCRIPT:
        return "arabic"
    if primary in _CJK_SCRIPT:
        return "cjk"
    return "default"


def provider_for(language: str | None) -> str:
    """The provider that should serve `language`.

    arabic  -> anthropic   Claude is the only model measured as reliable on
                           Arabic script, and the only one that gets Jawi right.
    cjk     -> kimi        Reserved. Not implemented — see `IMPLEMENTED`.
    default -> gemini      Latin and Indic scripts. Paid for by GCP credits,
                           which Vertex confirmed cover Google models 100%.
    """
    family = script_family(language)
    if family == "arabic":
        return ANTHROPIC
    if family == "cjk":
        return KIMI
    return GEMINI


def provider_is_available(provider: str) -> bool:
    return provider in IMPLEMENTED
