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
    sample = (text or "")[:20000]
    if not sample.strip():
        return None
    # Arabic: script beats statistics.
    letters = sum(1 for ch in sample if ch.isalpha())
    if letters and len(_AR_RE.findall(sample)) / letters > 0.3:
        return "ar"

    words = re.findall(r"[a-zà-ÿ']+", sample.lower())
    if len(words) < 30:
        return None
    scores = {code: sum(1 for w in words if w in sw) for code, sw in _STOPWORDS.items()}
    best = max(scores, key=lambda c: scores[c])
    runner = max((c for c in scores if c != best), key=lambda c: scores[c])
    # Confidence: the winner must be a real presence AND clearly ahead.
    if scores[best] < max(5, len(words) * 0.02):
        return None
    if scores[runner] > 0 and scores[best] < scores[runner] * 1.5:
        return None
    return best


def prompt_directive(code: str | None) -> str:
    """The language instruction injected into analysis/script/doc prompts."""
    lang = get_language(code)
    if lang.code == "en":
        return ""
    return (
        f"\n\nLANGUAGE: this chapter is written in {lang.name} ({lang.native}). "
        f"Write ALL output — names, explanations, narration, questions, answers — in {lang.name}, "
        "matching the register of the source text. Keep religious, technical and proper-noun terms "
        "exactly as they appear in the chapter (including any Arabic-script quotations). "
        "Do not translate the content into English."
    )
