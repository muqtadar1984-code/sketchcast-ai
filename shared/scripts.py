"""Unicode script classification — the minimum needed to reason about text in
the ten locales this product ships (English, Malay, Jawi, Arabic, French,
Spanish, Portuguese, Hindi, Marathi, Telugu).

Two questions get asked of a book's text, and both are script-dependent:

  * "are the words separated?" — a space ratio near zero is broken extraction
    in EVERY script this product ships, but it is completely NORMAL Chinese,
    Japanese or Thai, which do not put spaces between words. Any rule about
    spacing MUST consult ``SPACELESS_SCRIPTS`` first or it condemns a real
    Chinese textbook.
  * "is the text coherent?" — a book is written in ONE script. A soup of
    Devanagari and Latin letters in text that has also lost its spacing is
    mojibake, not a book. Note what ``family_share`` does NOT support: it is a
    share of LETTERS, and a Han character carries a whole word where a Latin
    word costs 5-8 letters, so it cannot be thresholded for a spaceless script
    without condemning any CJK book that quotes a few English terms. See
    ``book_health._MAX_SPACE_RATIO_SPACELESS`` for what is measured instead.

Pure, no I/O, no dependencies. ``shared/languages.py`` keeps its own tighter
ranges for LANGUAGE detection; this module answers the coarser SCRIPT question
for every script we might meet in a teacher's upload, not just the ones we can
generate lessons in.
"""

from __future__ import annotations

# (script, first_codepoint, last_codepoint) — coarse blocks, ordered for
# readability rather than lookup speed (titles and samples are short).
_RANGES: tuple[tuple[str, int, int], ...] = (
    ("latin", 0x0041, 0x005A), ("latin", 0x0061, 0x007A),
    ("latin", 0x00C0, 0x024F), ("latin", 0x1E00, 0x1EFF),
    ("greek", 0x0370, 0x03FF), ("greek", 0x1F00, 0x1FFF),
    ("cyrillic", 0x0400, 0x052F),
    # Arabic script — also carries Jawi (Malay) and Urdu.
    ("arabic", 0x0600, 0x06FF), ("arabic", 0x0750, 0x077F), ("arabic", 0x08A0, 0x08FF),
    ("arabic", 0xFB50, 0xFDFF), ("arabic", 0xFE70, 0xFEFF),
    ("devanagari", 0x0900, 0x097F),   # Hindi + Marathi
    ("bengali", 0x0980, 0x09FF),
    ("gurmukhi", 0x0A00, 0x0A7F),
    ("gujarati", 0x0A80, 0x0AFF),
    ("oriya", 0x0B00, 0x0B7F),
    ("tamil", 0x0B80, 0x0BFF),
    ("telugu", 0x0C00, 0x0C7F),
    ("kannada", 0x0C80, 0x0CFF),
    ("malayalam", 0x0D00, 0x0D7F),
    ("sinhala", 0x0D80, 0x0DFF),
    ("thai", 0x0E00, 0x0E7F),
    ("lao", 0x0E80, 0x0EFF),
    ("tibetan", 0x0F00, 0x0FFF),
    ("myanmar", 0x1000, 0x109F),
    ("khmer", 0x1780, 0x17FF),
    ("hangul", 0x1100, 0x11FF), ("hangul", 0x3130, 0x318F), ("hangul", 0xAC00, 0xD7AF),
    ("kana", 0x3040, 0x30FF),
    # U+3005 々 (iteration mark) and U+3006 〆 / U+3007 〇 are categories Lm/Lo/Nl,
    # so ``isalpha()`` is True for them and they WOULD have counted — as "other",
    # which is its own family and therefore diluted every real Japanese book that
    # uses 人々 or 各々.
    ("han", 0x3005, 0x3007),
    ("han", 0x3400, 0x4DBF), ("han", 0x4E00, 0x9FFF), ("han", 0xF900, 0xFAFF),
    # Halfwidth and Fullwidth Forms (U+FF00-FFEF), split by sub-range rather
    # than treated as one block: a PDF that extracts halfwidth katakana ｱｲｳ and
    # fullwidth Latin ＡＢＣ is still a normal Japanese book, but classifying
    # either as "other" pushed the sample's dominant to "other" — which is not in
    # SPACELESS_SCRIPTS, so the book was then judged by the SPACED rules and its
    # (correctly) zero space ratio read as run-together words.
    ("latin", 0xFF21, 0xFF3A), ("latin", 0xFF41, 0xFF5A),
    # …up to U+FF9F, not U+FF9D. The two codepoints past the syllables are the
    # halfwidth voiced sound marks ﾞ and ﾟ (U+FF9E, U+FF9F) — category Lm, so
    # isalpha() is True for them and they counted as "other", which is its own
    # family. They are not decoration: halfwidth katakana writes ガ as ｶ + ﾞ and
    # パ as ﾊ + ﾟ, so roughly every other syllable of real halfwidth text carries
    # one, and stopping at U+FF9D diluted the CJK family share of every book that
    # extracts this way.
    ("kana", 0xFF66, 0xFF9F),
    ("hangul", 0xFFA0, 0xFFDC),
    # CJK Extension B and beyond (SIP). Rare per character, but a classical or
    # historical Chinese textbook carries enough of them to matter.
    ("han", 0x20000, 0x2FA1F),
)

# Scripts that do NOT separate words with spaces. A near-zero space ratio is
# normal here and says nothing about extraction quality. (Korean DOES space
# between words, so hangul is deliberately absent.)
SPACELESS_SCRIPTS = frozenset({"han", "kana", "thai", "lao", "khmer", "myanmar", "tibetan"})

# Scripts that legitimately co-occur inside ONE writing system, so that
# "coherent text" is measured over the family rather than the script. Japanese
# is the case that forces this: normal Japanese prose is ~45% hiragana, ~35%
# kanji and ~10% katakana, so its single most-common SCRIPT is under half the
# letters — measuring per-script would condemn a perfectly good Japanese book.
# Korean mixes hangul with hanja for the same reason.
_FAMILIES: dict[str, str] = {"han": "cjk", "kana": "cjk", "hangul": "cjk"}


def family_of(script: str | None) -> str | None:
    """The writing system a script belongs to (most scripts are their own)."""
    if script is None:
        return None
    return _FAMILIES.get(script, script)


def script_of(ch: str) -> str | None:
    """The script of a single character, or None when it isn't a letter.

    Digits, punctuation and whitespace are NOT letters — they appear in every
    script and would dilute any dominance measure taken over them.
    """
    if not ch.isalpha():
        return None
    cp = ord(ch)
    for name, lo, hi in _RANGES:
        if lo <= cp <= hi:
            return name
    return "other"


def script_profile(text: str) -> dict:
    """Script make-up of a text sample.

    Returns ``{letters, counts, dominant, dominant_share, family,
    family_share}``. Both shares are of the LETTERS (not of the characters), so
    a page of formulas or page numbers can't drag them down. ``dominant`` is
    the raw script (use it to ask "does this script use spaces?");
    ``family``/``family_share`` answer "is this text one coherent writing
    system?" and are the pair to threshold on.
    """
    counts: dict[str, int] = {}
    for ch in text or "":
        name = script_of(ch)
        if name:
            counts[name] = counts.get(name, 0) + 1
    letters = sum(counts.values())
    dominant = max(counts, key=lambda k: counts[k]) if counts else None

    fam_counts: dict[str, int] = {}
    for name, n in counts.items():
        fam = family_of(name)
        fam_counts[fam] = fam_counts.get(fam, 0) + n
    family = max(fam_counts, key=lambda k: fam_counts[k]) if fam_counts else None

    return {
        "letters": letters,
        "counts": counts,
        "dominant": dominant,
        "dominant_share": (counts[dominant] / letters) if dominant and letters else 0.0,
        "family": family,
        "family_share": (fam_counts[family] / letters) if family and letters else 0.0,
    }
