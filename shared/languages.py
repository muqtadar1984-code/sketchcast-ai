"""Language registry + detection — the single source of supported languages.

Six launch languages: English, Bahasa Melayu, Arabic (RTL), French, Spanish,
Portuguese. Everything language-aware reads THIS module:

  - index_book detects a book's language (pure heuristic, $0) → books.language
  - generation resolves params.language → book.language → "en" and injects a
    prompt directive into analysis, scripts and document authoring
  - the TTS registry picks a matching free Edge voice by language
  - the slide/deck builders mirror layout when direction == "rtl"

Adding a language = one entry here + one voice in shared/tts/registry.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Language:
    code: str        # ISO 639-1 — stored in books.language / params.language
    name: str        # English name (console/ops)
    native: str      # what the teacher sees in pickers
    direction: str   # "ltr" | "rtl"
    default_voice: str  # voice_id in shared/tts/registry.py


LANGUAGES: dict[str, Language] = {
    "en": Language("en", "English", "English", "ltr", "edge-aria"),
    "ms": Language("ms", "Malay", "Bahasa Melayu", "ltr", "edge-yasmin"),
    "ar": Language("ar", "Arabic", "العربية", "rtl", "edge-zariyah"),
    "fr": Language("fr", "French", "Français", "ltr", "edge-denise"),
    "es": Language("es", "Spanish", "Español", "ltr", "edge-elvira"),
    "pt": Language("pt", "Portuguese", "Português", "ltr", "edge-francisca"),
    # Indian languages (immediate markets). Hindi and Marathi SHARE Devanagari;
    # detection disambiguates them by function-word vote (see _devanagari_variant).
    "te": Language("te", "Telugu", "తెలుగు", "ltr", "edge-shruti"),
    "mr": Language("mr", "Marathi", "मराठी", "ltr", "edge-aarohi"),
    "hi": Language("hi", "Hindi", "हिन्दी", "ltr", "edge-swara"),
    # Jawi — Malay written in the Arabic-derived script (RTL). SAME spoken
    # language as ms, so the voice is the Malay one; the SCRIPT is Arabic, so
    # it reuses the Arabic RTL rendering. Written artifacts only for now (the
    # narrated video/deck stay Rumi until the Jawi video phase). NOT a
    # detection target — books are in Rumi; teachers SELECT Jawi as output.
    "ms-arab": Language("ms-arab", "Malay (Jawi)", "بهاس ملايو (جاوي)", "rtl", "edge-yasmin"),
}


def get_language(code: str | None) -> Language:
    return LANGUAGES.get((code or "").strip().lower(), LANGUAGES["en"])


def is_rtl(code: str | None) -> bool:
    return get_language(code).direction == "rtl"


# ── Detection (pure, deterministic, $0) ──────────────────────────────────────
# Arabic is identified by script; the Latin-script languages by distinctive
# stopword frequency. Words chosen to SEPARATE the pairs that share vocabulary
# (es/pt, fr/es) — each set avoids words common in the others.
_AR_RE = re.compile(r"[؀-ۿݐ-ݿ]")
_DEVA_RE = re.compile(r"[ऀ-ॿ]")
_TELU_RE = re.compile(r"[ఀ-౿]")
_LATIN_RE = re.compile(r"[A-Za-zÀ-ÿ]")
_STOPWORDS: dict[str, frozenset[str]] = {
    "en": frozenset("the and of to is that with for are was this from have".split()),
    "ms": frozenset("dan yang untuk dengan ialah adalah kepada dalam ini itu tidak boleh murid bahagian soalan".split()),
    "fr": frozenset("le les des est dans pour avec une sur qui pas vous nous être cette".split()),
    "es": frozenset("el los las es y en una para por con del muy pero como está".split()),
    "pt": frozenset("o os às é em uma para não com do da você são também mais".split()),
}


def detect_language(text: str) -> str | None:
    """Best-guess language of a text sample, or None when too ambiguous.

    Callers treat None as "leave books.language unset" — generation then
    defaults to English exactly as before, so detection can only ever help.
    """
    sample = (text or "")[:30000]
    if not sample.strip():
        return None
    # Script beats statistics — but only when the script DOMINATES the letters.
    # (An Islamic-Education book in Malay quotes plenty of Quranic Arabic; the
    # dominance check keeps it Malay.)
    letters = sum(1 for ch in sample if ch.isalpha())
    latin = len(_LATIN_RE.findall(sample))
    if letters:
        ar = len(_AR_RE.findall(sample))
        if ar / letters > 0.3 and ar > latin:
            return "ar"
        deva = len(_DEVA_RE.findall(sample))
        if deva / letters > 0.3 and deva > latin:
            return _devanagari_variant(sample)
        telu = len(_TELU_RE.findall(sample))
        if telu / letters > 0.3 and telu > latin:
            return "te"

    words = re.findall(r"[a-zà-ÿ']+", sample.lower())
    if len(words) < 30:
        return None
    scores = {code: sum(1 for w in words if w in sw) for code, sw in _STOPWORDS.items()}
    best = max(scores, key=lambda c: scores[c])
    runner = max((c for c in scores if c != best), key=lambda c: scores[c])
    # Confidence: the winner must be a real presence AND clearly ahead — the
    # tight margins also make UNSUPPORTED languages (Italian, German…) come
    # back None (→ English default) instead of a near-miss like es/fr.
    if scores[best] < max(8, len(words) * 0.025):
        return None
    if scores[runner] > 0 and scores[best] < scores[runner] * 2.0:
        return None
    return best


# Hindi vs Marathi share the script but not their function words — the copula
# alone (है/हैं vs आहे/आहेत) is nearly decisive; the rest widens the margin.
_HI_WORDS = frozenset("है हैं का की के में से को और नहीं लिए यह वह पर इस उस करने द्वारा".split())
_MR_WORDS = frozenset("आहे आहेत आणि मध्ये ची चे चा म्हणून येथे तसेच किंवा असते होते ते हे".split())
_DEVA_WORD_RE = re.compile(r"[ऀ-ॿ]+")


def _devanagari_variant(sample: str) -> str:
    words = _DEVA_WORD_RE.findall(sample)
    hi = sum(1 for w in words if w in _HI_WORDS)
    mr = sum(1 for w in words if w in _MR_WORDS)
    # Ties and silence default to Hindi — the majority Devanagari language.
    return "mr" if mr > hi else "hi"


def prompt_directive(code: str | None) -> str:
    """The language instruction injected into analysis/script/doc prompts."""
    lang = get_language(code)
    if lang.code == "en":
        return ""
    if lang.code == "ms-arab":
        # Jawi: Malay in the Arabic script. Spell it out — models default to
        # Rumi for Malay, so the instruction must be explicit and name the
        # Jawi-specific letters.
        return (
            "\n\nLANGUAGE & SCRIPT: write ALL output in Malay (Bahasa Melayu) using the "
            "JAWI script — the Arabic-derived script for Malay — NOT Rumi (Latin). Use "
            "correct Jawi orthography, including the Jawi-specific letters where they belong: "
            "چ (ca), ڠ (nga), ڤ (pa), ݢ (ga), ۏ (va), ڽ (nya). Keep religious and technical "
            "terms in their standard Jawi/Arabic spelling. Do NOT output any Rumi/Latin "
            "transliteration — Jawi only."
        )
    return (
        f"\n\nLANGUAGE: this chapter is written in {lang.name} ({lang.native}). "
        f"Write ALL output — names, explanations, narration, questions, answers — in {lang.name}, "
        "matching the register of the source text. Keep religious, technical and proper-noun terms "
        "exactly as they appear in the chapter (including any Arabic-script quotations). "
        "Do not translate the content into English."
    )
