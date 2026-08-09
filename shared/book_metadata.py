"""Book title/author hygiene — the deterministic cleanup around the metadata
Claude detects at index time.

Lives in shared/ because BOTH repos need the same rules and have already
drifted once with real consequences: app commit 15dbe57 moved
``cleanBookTitle()`` into the INSERT (so the stored title is pre-cleaned),
while worker commit e9b034e, the same day, added a gate that only accepts the
model's title when the stored one still "looks like a filename". Since then no
upload can produce a title that passes the gate — measured at 0 of 19
production books — so a month of paid-for metadata detection was discarded
every single time, and a book stayed named "文字BUSINESS DYNAMICS" through 17
generations.

Everything here is pure and SCRIPT-AWARE. The product ships Arabic, Jawi,
Telugu, Marathi, Hindi and Malay titles: a rule that "strips non-Latin
characters" — the obvious reading of the ticket — would delete five locales'
book titles outright.
"""

from __future__ import annotations

import re
import unicodedata

from shared.scripts import script_of

# Download-site junk. Mirrors the app's src/utils/book.ts.
_DOMAIN_HEAD = re.compile(r"^[a-z0-9-]+\.(?:com|net|org|pub|io|in|co|info|xyz)[._-]+", re.I)
_JUNK_TAIL = re.compile(r"[\s._-]*(pdf[\s._-]*free|free[\s._-]*pdf|ebook|pdf|free|download)\s*$", re.I)

# A leading run of >=5 digits is a download-site record id, not a book: real
# titles that open with a number open with a SHORT one ("1000 Solved Problems",
# "21 Lessons"). Both production cases were 9 and 10 digits.
_ID_PREFIX = re.compile(r"^\d{5,}[\s._-]+(?=\S)")
# The browser's duplicate-download suffix: "… (2)", "… (3)".
_DUP_SUFFIX = re.compile(r"\s*\((?:[1-9]\d?)\)\s*$")

# Blocks that are safe to NFKC-fold. Folding the WHOLE string would decompose
# Arabic presentation forms and could alter Jawi text, so only these two:
# Number Forms (U+2150-U+218F — the Ⅰ Ⅱ Ⅲ roman numerals machine-built
# bookmarks use) and the fullwidth ASCII block (U+FF01-U+FF5E).
_FOLDABLE = re.compile(r"[⅐-↏！-～]")
# "PartI" → "Part I": after folding, a roman numeral is glued to the word.
_GLUED_ROMAN = re.compile(r"(?<=[a-z])(?=(?:I{1,3}|IV|VI{0,3}|IX|XI{0,3}|XIV|XV|XI?X|XX)\b)")

_NULLISH = frozenset({"null", "none", "unknown", "n/a", "unspecified", "-"})

# Edge-trim safety valves — see sanitise_title.
_EDGE_RUN_MAX = 4
_EDGE_MIN_LETTERS_KEPT = 3
_EDGE_MIN_SHARE_KEPT = 0.75


def looks_like_filename(s: str) -> bool:
    """The original filename test, plus extension-less export slugs.

    Kept as ONE branch of ``title_needs_repair`` rather than as the whole
    decision — that promotion is what made it dead code.

    "Has no space" is deliberately NOT a signature on its own. It used to be,
    and it broke the module's own invariant: ``pick_book_title('IGCSE', None)``
    returned ``'Igcse'`` and ``pick_book_title('BIOLOGY', None)`` returned
    ``'Biology'``, because a spaceless title fell through to
    ``clean_title_fallback``, which ends in ``.title()``. A teacher who names a
    book "IGCSE" had it silently renamed on the next re-index — and this is the
    first change that lets the write path fire at all, so nothing had caught it.
    A single word is only a file name when it also carries FILE punctuation.
    """
    s = (s or "").strip()
    if not s:
        return True
    return (
        s.lower().endswith(".pdf")
        or bool(_DOMAIN_HEAD.search(s))
        or bool(re.search(r"[\s._-](pdf|free)", s, re.I))
        # "esl_cie_asl_genpaper_1ed_tr_ch1.3_wksht1.3a_TOR" — a file name with
        # the extension already stripped. Four-plus underscore-joined segments
        # and no spaces is not a book title.
        or (" " not in s and s.count("_") >= 3)
    )


def clean_title_fallback(s: str) -> str:
    """Best-effort tidy of a filename-derived title when the model gave none."""
    t = re.sub(r"\.pdf$", "", s or "", flags=re.I)
    t = _DOMAIN_HEAD.sub("", t)
    t = _JUNK_TAIL.sub("", _JUNK_TAIL.sub("", t))
    t = re.sub(r"[_-]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t.title() if t else "Untitled book"


def _edge_trim_foreign_run(s: str) -> str:
    """Drop a SHORT run of foreign-script letters glued to the start or end.

    This is the rule that has to be safe in ten locales, so it is defined by
    script MAJORITY, never by "non-Latin". A Telugu, Hindi, Marathi, Arabic or
    Jawi title is 100% one script, so that script IS the majority, the
    difference set is empty, and this function is provably a no-op on it.

    It only fires when ALL of these hold, which together describe
    "文字BUSINESS DYNAMICS" and essentially nothing a real book is called:
      * the run is at an EDGE (interior characters are never touched),
      * it is <= 4 letters,
      * it is GLUED — no whitespace separates it from the rest, so the
        genuinely bilingual titles common in these markets ("गणित Mathematics
        Class 8", "العربية Grade 5") are out of scope,
      * >= 3 letters and >= 75% of the letters survive.
    """
    letters = [(i, ch) for i, ch in enumerate(s) if script_of(ch)]
    if not letters:
        return s
    counts: dict[str, int] = {}
    for _, ch in letters:
        name = script_of(ch)
        counts[name] = counts.get(name, 0) + 1
    dominant = max(counts, key=lambda k: counts[k])

    for at_start in (True, False):
        run = []
        order = letters if at_start else list(reversed(letters))
        for i, ch in order:
            if script_of(ch) == dominant:
                break
            run.append(i)
        if not run or len(run) > _EDGE_RUN_MAX:
            continue
        cut = (max(run) + 1) if at_start else min(run)
        # GLUED: the character on the other side of the cut must not be
        # whitespace. A space-separated foreign word is a bilingual title
        # ("गणित Mathematics Class 8"), not extraction noise, and must survive.
        neighbour = s[cut:cut + 1] if at_start else s[cut - 1:cut]
        if not neighbour or neighbour.isspace():
            continue
        candidate = s[cut:] if at_start else s[:cut]
        kept = sum(1 for ch in candidate if script_of(ch))
        if kept >= _EDGE_MIN_LETTERS_KEPT and kept >= _EDGE_MIN_SHARE_KEPT * len(letters):
            s = candidate.strip()
            letters = [(i, ch) for i, ch in enumerate(s) if script_of(ch)]
            if not letters:
                break
    return s


def sanitise_title(s: str) -> str:
    """Deterministic title cleanup, applied to the model's title AND the
    filename-derived fallback. Case is left alone — ALL-CAPS is the model's
    call, not a regex's."""
    t = (s or "").strip()
    if not t:
        return ""
    # 1. Fold only the two safe blocks, then re-space a glued roman numeral so
    #    "PartⅠ Perspective" reads "Part I Perspective".
    t = _FOLDABLE.sub(lambda m: unicodedata.normalize("NFKC", m.group(0)), t)
    t = _GLUED_ROMAN.sub(" ", t)
    # 2. Script-majority edge trim ("文字BUSINESS DYNAMICS").
    t = _edge_trim_foreign_run(t)
    # 3./4. Download-site id prefix and browser duplicate suffix.
    if len(t.split()) >= 2:
        t = _ID_PREFIX.sub("", t)
    t = _DUP_SUFFIX.sub("", t)
    # 5. Collapse whitespace.
    return " ".join(t.split()).strip()


def title_needs_repair(s: str) -> bool:
    """Whether the STORED title carries a junk signature we can see.

    Deliberately conservative: a title a teacher typed by hand shows none of
    these, so it is never clobbered. (The app knowing whether a human typed the
    title would be better still — see the ``title_source`` column proposed in
    the diagnosis — but that needs a migration, and this closes the hole today.)
    """
    t = (s or "").strip()
    if not t:
        return True
    return (
        looks_like_filename(t)
        or bool(_ID_PREFIX.match(t))
        or bool(_DUP_SUFFIX.search(t))
        or sanitise_title(t) != t
    )


def _tokens(s: str) -> set[str]:
    return {w for w in re.split(r"[^\w]+", (s or "").casefold()) if len(w) > 2}


def pick_book_title(current: str, detected: str | None) -> str | None:
    """The title to WRITE, or None to keep what is stored.

    Two guards, because this is the first time the model's title actually
    reaches ``books.title`` in production — a month of detection ran with its
    output discarded, so the write path is untested:
      * only replace a stored title that shows a junk signature;
      * only accept a detected title that SHARES a meaningful word with the
        stored one, so a hallucinated title cannot rename a teacher's book.
        Every real case passes it ("文字BUSINESS DYNAMICS" → "Business
        Dynamics: Systems Thinking and Modeling for a Complex World" shares
        both words).
    """
    stored = (current or "").strip()
    if not title_needs_repair(stored):
        return None
    clean_detected = sanitise_title(detected or "")
    if clean_detected and (not stored or _tokens(clean_detected) & _tokens(stored)):
        return clean_detected
    fallback = sanitise_title(clean_title_fallback(stored))
    if not fallback:
        return None
    # A change of CASE alone is not a repair. ``clean_title_fallback`` ends in
    # ``.title()``, which is right for a filename slug and wrong for anything
    # else, and process.py's `if new_title == current_title: new_title = None`
    # guard does not catch it because the string genuinely differs. Belt to the
    # ``looks_like_filename`` braces: whatever else changes here, "IGCSE" must
    # never come back as "Igcse".
    if fallback.casefold() == stored.casefold():
        return None
    return fallback


# ── Author ───────────────────────────────────────────────────────────────────
# The cover of the book that triggered this reads "John D. Sterman's Business
# Dynamics", and the model copied the possessive verbatim into the author
# field: books.author became "John D. Sterman's". The prompt now asks for a
# bare name; this is the belt to that braces, because models regress.
_POSSESSIVE = re.compile(r"[’']s$")
_BY_PREFIX = re.compile(r"^by\s+", re.I)


def sanitise_author(value, title: str = "") -> str | None:
    """A bare author/publisher name, or None."""
    a = " ".join(str(value or "").split())
    if not a or a.casefold() in _NULLISH:
        return None
    a = _BY_PREFIX.sub("", a).strip()
    a = _POSSESSIVE.sub("", a).strip(" ,;:-–—")
    if not a or a.casefold() in _NULLISH:
        return None
    # An "author" containing the book's title is a copied cover line, not a
    # name — the exact shape "X's <Title>" degrades into once the possessive
    # is gone.
    clean_title = " ".join((title or "").split()).casefold()
    if clean_title and len(clean_title) > 3 and clean_title in a.casefold():
        return None
    return a
