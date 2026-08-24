"""Chapter-list OUTCOME validation — is the WINNING chapter list actually a
list of chapters?

The incident this module exists for (2026-08-23, Sara Junaidi's scanned
"Science Learner's Book 8", book e0459f87): a KamiHQ-produced scan carried 13
human-typed junk bookmarks — 'LEANERS BOOK 8 1' … 'LEANERS BOOK 8 12', the
book's own misspelled cover title plus a serial — and every existing guard
waved them through. ``_looks_like_file_bookmark`` models FILE slugs, not
human-typed labels; ``_toc_is_usable``'s trivial-title rule needs a majority
of bare digits and saw only one; ``_map_shape`` judges PAGE GEOMETRY and its
span rule is an AND that missed by 0.0007 of max_share. 13 chapters whose
boundaries described nothing shipped at health 82 "good", problems [], with a
36-page hole and 11/12-part credit estimates on garbage ranges.

The repo's own history says a stronger point filter is the class of fix that
fails ("fixing the regex is never enough — it changes which rung wins"), so
this is the missing layer instead: a rung-independent judgement of whatever
chapter list WON, on signals that need no language and no model:

  * a near-duplicate TITLE FAMILY — most titles are one shared stem plus a
    serial. Chapter titles name topics; only a cover title stamped per
    bookmark repeats like that.
  * DECIMAL-SECTION numbers as top-level chapters with numbering that skips
    (1.3 → 2.4 → 4.1) — sections cherry-picked as chapters, the stored
    incident list's exact shape after the heal glued real section headings
    onto wrong boundaries.
  * EXTREME SPAN VARIANCE — a 2-page "chapter" beside an 85-page one. The
    complement of ``_map_shape``'s span rule, whose AND with
    ``_MAX_CHAPTER_SHARE`` is the near-miss that let the incident through.
  * TRIVIAL TITLES — no letters at all ('1', '٣', '—').

Everything here is PURE over titles + ranges and script-agnostic BY
CONSTRUCTION: Unicode categories, never ``\\w+`` (which shatters
Devanagari/Telugu — a lesson already paid for in the coverage tokenizer),
``str.casefold`` never ``.lower``, and digits mean category Nd in any script
(Arabic-Indic '١٢' included) plus Nl for the U+2160 roman numerals that
machine-built bookmarks glue onto words.

Consumed from BOTH sides of the pipeline, deliberately:
  * ``structurer._chapters_plausible`` — so a junk list FAILS the outcome
    check no matter which rung produced it, is demoted, and the cascade
    continues to the Claude/vision rung the ``chapters<=1`` fence used to
    keep permanently shut;
  * ``book_health.compute_book_health`` — so the STORED list is judged again
    independently of the cascade (pre-fix books re-indexed, healed lists,
    restored coarse fallbacks) and a suspect list gates and scores honestly.
No imports from other agent1 modules, so both can import this without cycles.
"""

from __future__ import annotations

import unicodedata

from shared.scripts import SPACELESS_SCRIPTS, script_of

# ── thresholds ───────────────────────────────────────────────────────────────
# A family needs enough members to be a pattern rather than a coincidence: two
# chapters called "Revision 1" / "Revision 2" in a real book are ordinary.
_FAMILY_MIN_MEMBERS = 3
# What makes a shared stem a BOOK TITLE rather than a generic label word: two
# or more whitespace-separated tokens ('LEANERS BOOK 8'), or — because
# spaceless scripts write a whole cover title in one unbroken run — a run of
# spaceless-script characters ('科学课本'). A single word in a SPACED script
# ('chapter', 'Experiment', 'الوحدة', 'अध्याय') is how those languages label
# chapters generically and is never a family stem, whatever its length.
# The spaceless bar is 4 because real CJK cover titles measure 4-7 characters
# ('科学课本' = "science textbook" is 4) — a 10-char bar missed all of them —
# while the common generic spaceless labels stem shorter ('บทที่' loses its
# combining marks to the serial strip and measures 3).
_STEM_MIN_TOKENS = 2
_STEM_MIN_CHARS_SPACELESS = 4
# Letterless titles ('1', '٤', '—'). The structurer's own digit-majority rule
# keeps its majority bar; here a third is one SIGNAL, never a verdict alone.
_TRIVIAL_SHARE = 1 / 3
# Decimal-led titles ("1.3 Breathing") as top-level chapters: three is enough
# to see the numbering scheme, and only NON-CONTIGUOUS numbering fires — a
# deliberate section-level split (1.1, 1.2, 2.1…) is a different question and
# already _depth_is_sections' job at outline level.
_DECIMAL_MIN = 3
# The span-variance complement to _map_shape's AND rule. Same 4.0 ratio, but
# corroborated by EITHER a big max_share OR a sliver chapter — the incident sat
# at max_share 0.2493 against the existing 0.25 with a 2-page chapter beside an
# 85-page one. Never a verdict alone (see _WEAK_SIGNALS_FOR_SUSPECT): a real
# book with one long unit and short intros must not be condemned by geometry
# without a second, independent smell.
_SPAN_MIN_CHAPTERS = 4
_SPAN_RATIO = 4.0
_SPAN_MAX_SHARE = 0.20
_SPAN_SLIVER_PAGES = 2
# Any TWO independent signals condemn; NO signal ever condemns alone — the
# family included. A near-duplicate family alone is exactly what a legitimate
# serial labelling scheme looks like ('Practice Test 1..6', 'Kegiatan Belajar
# 1..12', 'الوحدة الدراسية ١..٨', 'หน่วยการเรียนรู้ที่ ๑..๘' — all healthy,
# all one stem plus a serial with even spans), so a family-alone verdict
# demoted CORRECT maps and gated healthy books in every non-English locale
# while the single-word English equivalents passed. Junk bookmark families
# come from a scanner splitting at arbitrary points, so they carry a second
# smell: the incident's raw list measured family 12/13 PLUS span variance
# (2 pages beside 85), and its cosmetically-healed stored list decimal-skips
# PLUS span variance. Corroboration keeps both caught and the healthy books
# clean.
_WEAK_SIGNALS_FOR_SUSPECT = 2

# ── HARD signals (2026-08-24 fleet audit) ────────────────────────────────────
# The corroboration rule above is for AMBIGUOUS smells — every weak signal has
# a legitimate lookalike, so none may condemn alone. The full-library audit
# (14 live defective books, most at score 95) surfaced a second class with NO
# legitimate reading at all: an EMPTY title is not a label anyone typed, a
# U+FFFD replacement character is a decode failure by definition, a
# fill-in-the-blank underscore line (': _______') is a worksheet answer slot,
# a title that is a FILE name is the scanner's export slug, a combining mark
# with no base letter is OCR debris, and end_page < start_page describes no
# pages. Each of these condemns alone — demanding a second smell here just let
# '948a9494 Grade 12 Physics' ship 9 empty titles at score 95 and burn 6
# generations. Thresholds still demand a PATTERN where one stray could be a
# fluke; where a single instance is already impossible in a real book (the
# replacement char, a negative range) one is enough.
_EMPTY_TITLE_SHARE = 1 / 3        # 948a9494: 9 of 10 titles empty
_BLANK_LINE_MIN = 2               # f22b8b64: two ':______' Jawi worksheet lines
_GARBLED_MIN = 2                  # 81c94f98: harakat glued to punctuation, األ
_FILENAME_SHARE = 0.5             # 2ea65b58: 4/4 '031-072-C606'; 994b8238: 5/5
_GLUED_HARD_SHARE = 0.5           # a13a45e2 / 4e66897c: heading+body majority
_GLUED_WEAK_SHARE = 0.25          # …a minority still counts as one weak smell
_FRAGMENT_MIN = 2                 # bb68dec6: 'things', 'enough' as titles


def _is_alpha(ch: str) -> bool:
    return unicodedata.category(ch).startswith("L")


def _is_serial_char(ch: str) -> bool:
    # Nd = decimal digits in every script (0-9, ٠-٩, ०-९ …); Nl = letter
    # numbers, i.e. the U+2160 roman numerals bookmarks glue onto words
    # ("PartⅠ") that already blinded _LABEL_RE once. Beyond those categories,
    # any character with a defined Unicode NUMERIC VALUE counts: ideographic
    # numerals 一二三…十 are category Lo, not Nd/Nl, yet Chinese/Japanese
    # scanner-typed bookmarks conventionally serialize with them — treating
    # them as letters hid a '…ブック 一..十三' family completely.
    if unicodedata.category(ch) in ("Nd", "Nl"):
        return True
    return unicodedata.numeric(ch, None) is not None


def _is_word_letter(ch: str) -> bool:
    # A letter that is NOT also a numeral. 一二三 are category Lo (letters),
    # but a title made only of them is a bare number, not words.
    return _is_alpha(ch) and not _is_serial_char(ch)


def _norm(title: str) -> str:
    return " ".join(str(title or "").split()).casefold()


def _stem(title: str) -> str:
    """The title with ONE serial run stripped from EACH end: 'LEANERS BOOK
    8 10' → 'leaners book 8', '1 - LEANERS BOOK 8' → 'leaners book' (the
    leading '1' AND the trailing '8' each cost one run). One run per side only
    — the '8' inside 'LEANERS BOOK 8 10' is the grade on the book's cover and
    stripping it too would merge unrelated families. The LEADING strip exists
    because 'N - Cover Title' is the other common human enumeration style (and
    the natural digit-first order for RTL bookmarks): without it the family
    was invisible whenever the scanner typed the serial first."""
    t = _norm(title)
    end = len(t)
    while end > 0 and not (_is_alpha(t[end - 1]) or _is_serial_char(t[end - 1])):
        end -= 1  # trailing space / punctuation
    serial_end = end
    while end > 0 and _is_serial_char(t[end - 1]):
        end -= 1
    if end == serial_end:  # no trailing serial at all
        end = len(t)
    else:
        while end > 0 and not (_is_alpha(t[end - 1]) or _is_serial_char(t[end - 1])):
            end -= 1
    start = 0
    while start < end and not (_is_alpha(t[start]) or _is_serial_char(t[start])):
        start += 1  # leading punctuation
    serial_start = start
    while start < end and _is_serial_char(t[start]):
        start += 1
    if start == serial_start:  # no leading serial at all
        start = 0
    else:
        while start < end and not (_is_alpha(t[start]) or _is_serial_char(t[start])):
            start += 1
    return t[start:end]


def _stem_is_substantial(stem: str) -> bool:
    if not stem:
        return False
    if len(stem.split()) >= _STEM_MIN_TOKENS:
        return True
    # Single token. In a SPACED script that is a generic label word
    # ('Chapter', 'Experiment', 'Hoofdstuk', 'الوحدة') — never a family stem,
    # whatever its length: 'Experiment' is 10 letters and still just how a lab
    # manual labels its chapters. Spaceless scripts are the one place a whole
    # COVER TITLE arrives as a single token ('科学课本 1..13'), so a run whose
    # letters are mostly spaceless-script counts from a much lower bar.
    letters = [ch for ch in stem if _is_alpha(ch)]
    if not letters:
        return False
    spaceless = sum(1 for ch in letters if script_of(ch) in SPACELESS_SCRIPTS)
    return spaceless * 2 > len(letters) and len(stem) >= _STEM_MIN_CHARS_SPACELESS


def _leading_decimal(title: str) -> tuple[int, int] | None:
    """(major, minor) when the title LEADS with a decimal section number —
    '1.3 Breathing', '٢.٤ الفصل'. Unicode digits, int() understands Nd."""
    t = _norm(title)
    i = 0
    major_start = i
    while i < len(t) and unicodedata.category(t[i]) == "Nd":
        i += 1
    # Separators: ASCII dot, Arabic decimal ٫, and the fullwidth ．(U+FF0E) /
    # ideographic 。(U+3002) that CJK typography sets section numbers with.
    if i == major_start or i >= len(t) or t[i] not in ".٫．。":
        return None
    dot = i
    i += 1
    minor_start = i
    while i < len(t) and unicodedata.category(t[i]) == "Nd":
        i += 1
    if i == minor_start:
        return None
    if i < len(t) and _is_alpha(t[i]) and script_of(t[i]) == "latin":
        # '1.3rd' style — a LATIN ordinal suffix glued to the number is not a
        # section number. Only Latin: CJK and Thai glue the TOPIC directly to
        # the number ('1.3光合作用', '1.3การหายใจ'), and treating any letter
        # as the guard made this signal structurally dead in those scripts.
        return None
    try:
        return int(t[major_start:dot]), int(t[minor_start:i])
    except ValueError:
        return None


# ── per-title defect predicates (fleet audit 2026-08-24) ─────────────────────

_FILENAME_EXT_RE = None  # built lazily below — keep the module import-light


def _is_blank_line_title(title: str) -> bool:
    """A fill-in-the-blank worksheet line read as a chapter title
    (': _______________' — the Jawi Tajwid book f22b8b64, the founder's
    other-languages fear made real). Underscore runs never appear in a real
    chapter name in any script."""
    t = _norm(title)
    return "___" in t and not any(_is_alpha(ch) for ch in t)


def _looks_like_filename(title: str) -> bool:
    """A title that is a FILE name or asset code, not a chapter name.

    Descended from the structurer's ``_looks_like_file_bookmark``, which died
    with the bookmark rung (2026-08-24) — but the CLASS did not: filename
    titles live on in stored rows and can arrive from any detector, and the
    point filter missed a whole production book anyway ('031-072-C606', page
    ranges with the extension already stripped — the علوم book 2ea65b58, the
    exact class the filter existed for). Three shapes:
      * a literal extension ('.pdf', '.indd');
      * 4+ underscore-joined segments with no spaces (export slugs —
        'esl_cie_asl_genpaper_1ed_tr_ch1.3_wksht1.3a_TOR', book 994b8238);
      * digits with at most a short code attached ('000 C606',
        '031-072-C606') — no token carrying three letters anywhere. Guarded
        to LATIN-only letters: a spaceless-script title packs a word per
        character ('第7章' has two letters and is a perfectly good chapter),
        so the no-real-word rule is only meaningful where words cost letters.
    """
    import re
    global _FILENAME_EXT_RE
    if _FILENAME_EXT_RE is None:
        _FILENAME_EXT_RE = re.compile(r"\.(pdf|docx?|epub|indd|ai|png|jpe?g)$", re.I)
    t = str(title or "").strip()
    if not t:
        return False
    if _FILENAME_EXT_RE.search(t):
        return True
    if " " not in t and t.count("_") >= 3:
        return True
    letters = [ch for ch in t if _is_alpha(ch)]
    if not letters or not any(ch.isdigit() for ch in t):
        return False
    if any(script_of(ch) != "latin" for ch in letters):
        return False  # only Latin slugs are judged by the no-real-word rule
    has_word = any(
        sum(1 for ch in tok if _is_alpha(ch)) >= 3 for tok in t.split()
    )
    return not has_word


def _is_garbled(title: str) -> bool:
    """OCR/mojibake corruption INSIDE a title (book 81c94f98, 24 generations
    burned on an active user's رياضيات book). Two shapes, verified against the
    live titles' codepoints:
      * a combining mark with no letter to combine with — 'ةُ؟ُ' puts a damma
        on the question mark, and 'ِأُجرِيَ' opens the title with a kasra.
        Unicode-category based, so it is script-agnostic by construction.
      * the lam-alef mojibake signature — bare ALEF immediately followed
        (combining marks aside) by a hamza-carrying alef: 'األأنماط' for
        'الأنماط'. Real Arabic orthography never writes اأ / اإ / اآ inside a
        word; the cp1256 round-trip does."""
    t = str(title or "")
    prev_base: str | None = None  # last non-combining char seen
    for ch in t:
        cat = unicodedata.category(ch)
        if cat == "Mn":
            if prev_base is None or not _is_alpha(prev_base):
                return True  # mark with nothing to sit on
            continue  # marks stack — the base stays the base
        if prev_base == "ا" and ch in "أإآ":
            return True  # ا followed by أ/إ/آ — the lam-alef mojibake
        prev_base = ch
    return False


def _carries_body_text(title: str) -> bool:
    """A heading with its first body sentence glued on, stored as the title
    ('Enterprise This chapter covers syllabus section AS Level 1.1' — books
    a13a45e2 and 4e66897c, 37 and 8 'chapters' of it). Three markers, each
    calibrated against the live titles:
      * a sentence ENDS mid-title — terminal punctuation with a following
        space and 10+ more characters ('…geographical expanse. In the');
        digit-adjacent dots ('AS Level 1.1') don't count;
      * a long many-worded run — 9+ words over 55+ characters is prose, not a
        label (the structurer's own ``_clean_title`` bounds labels at 64);
      * cased chaos — an ALL-CAPS word of 5+ letters after a lowercase word
        ('…for assessment LEARNING intentions'); 5+ so that ordinary acronyms
        (DNA, HTML, JSON) never trip it.

    The length floor and the sentence-end windows are counted in WORDS-worth,
    not characters: a Latin word costs 5-8 characters where a Han character
    carries a whole word, so the Latin-calibrated 30-char floor made this
    signal structurally dead in CJK — '光合作用 …変える。次の節では' is a
    glued heading + sentence at 22 characters — which is the same
    letter-vs-word error the decimal guard already paid for once. A
    spaceless-majority title uses floors scaled ~2.5x down (30→12, 8→3,
    10→4), the measured character-per-word ratio between the two families."""
    t = " ".join(str(title or "").split())
    letters = [ch for ch in t if _is_alpha(ch)]
    spaceless = sum(1 for ch in letters if script_of(ch) in SPACELESS_SCRIPTS)
    dense = bool(letters) and spaceless * 2 > len(letters)
    min_len, lead, trail = (12, 3, 4) if dense else (30, 8, 10)
    if len(t) < min_len:
        return False
    for i, ch in enumerate(t):
        if ch in ".!?؟۔。！？" and lead <= i <= len(t) - trail:
            before = t[i - 1] if i else ""
            after = t[i + 1: i + 3]
            if ch == "." and before.isdigit() and after.strip()[:1].isdigit():
                continue  # '1.1' — a section number, not a sentence
            if ch in "。！？" or after.startswith(" "):
                return True
    words = t.split()
    if len(words) >= 9 and len(t) >= 55:
        return True
    saw_lower = False
    for w in words:
        letters = [ch for ch in w if _is_alpha(ch)]
        if len(letters) >= 3 and all(ch.islower() for ch in letters):
            saw_lower = True
        elif (saw_lower and len(letters) >= 5
              and all(ch.isupper() for ch in letters)):
            return True
    return False


def _is_lowercase_fragment(title: str) -> bool:
    """A bare mid-sentence word stored as a title ('things', 'enough' — book
    bb68dec6). Only meaningful in a CASED script, and only for very short
    titles: real chapter names start with a capital or a digit."""
    t = str(title or "").strip()
    if not t or len(t.split()) > 2:
        return False
    first = t[0]
    return first.islower() and first.isalpha()


def _label_leading_number(title: str) -> int | None:
    """The integer of a leading '<label word(s)> N' pattern — 'Unit 2: …',
    'الوحدة ٢' — or None. Unicode digits (Nd) in any script; at most two
    leading word tokens so a number deep inside a title never counts."""
    tokens = _norm(title).split()
    for tok in tokens[:3]:
        digits = "".join(ch for ch in tok if unicodedata.category(ch) == "Nd")
        if digits and len(digits) <= 2 and not any(_is_alpha(ch) for ch in tok.rstrip(":.-–—)]")):
            try:
                return int(digits)
            except ValueError:
                return None
        if not all(_is_alpha(ch) or ch in "()[]" for ch in tok.rstrip(":.-–—")):
            return None
    return None


def _strip_label_prefix(title: str) -> str:
    """'unit 2: communication' → 'communication' — the duplicate-content shape
    of book 682eedf5, where 'Communication' and 'Unit 2: Communication' were
    BOTH stored as chapters. Only fires when a label word + number visibly
    leads; anything else comes back unchanged."""
    t = _norm(title)
    tokens = t.split()
    if len(tokens) >= 3 and _label_leading_number(t) is not None:
        for i, tok in enumerate(tokens[:3]):
            if any(unicodedata.category(ch) == "Nd" for ch in tok):
                return " ".join(tokens[i + 1:]).lstrip(":.-–— ")
    return t


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    return float(ordered[mid]) if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


def _spans(chapters: list[dict]) -> list[int]:
    out = []
    for c in chapters or []:
        try:
            start = int(c["start_page"])
            end = int(c["end_page"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append(max(0, end - start + 1))
    return out


def assess_chapter_map(chapters: list[dict], total_pages: int | None = None) -> dict:
    """Judge a chapter list's TITLES + SHAPE, whichever rung produced it.

    Returns ``{"suspect": bool, "reasons": [str], "signals": {...}}`` —
    JSON-able; reasons are teacher-readable fragments the caller may embed in
    health.problems[]. Page-less lists (titles only) are judged on titles
    alone; lists too short for a signal to mean anything stay quiet.
    """
    titles = [str(c.get("title") or "") for c in (chapters or [])]
    n = len(titles)
    signals: dict = {
        "duplicate_family": 0.0,
        "family_members": 0,
        "trivial_share": 0.0,
        "decimal_nonseq": False,
        "span_variance": False,
        # fleet-audit signals (2026-08-24)
        "empty_titles": 0,
        "blank_line_titles": 0,
        "replacement_char": False,
        "garbled_titles": 0,
        "filename_titles": 0,
        "glued_titles": 0,
        "fragment_titles": 0,
        "duplicate_titles": 0,
        "label_number_gaps": False,
        "mixed_levels": False,
        "invalid_ranges": False,
    }
    reasons: list[str] = []
    if n == 0:
        return {"suspect": False, "reasons": reasons, "signals": signals}

    # ── near-duplicate title family ──────────────────────────────────────────
    stems: dict[str, int] = {}
    for t in titles:
        s = _stem(t)
        if _stem_is_substantial(s):
            stems[s] = stems.get(s, 0) + 1
    family_stem, members = "", 0
    if stems:
        family_stem, members = max(stems.items(), key=lambda kv: kv[1])
    if members >= _FAMILY_MIN_MEMBERS:
        signals["family_members"] = members
        signals["duplicate_family"] = round(members / n, 3)
        reasons.append(
            f"{members} of {n} chapter titles are near-copies of one label "
            f"('{family_stem}' plus a number) — usually a cover title stamped "
            "per bookmark rather than the book's real chapter names"
        )

    # ── trivial (letterless) titles ──────────────────────────────────────────
    # "Letterless" via _is_word_letter, so a bare ideographic numeral ('一',
    # '十三') counts exactly as a bare '1' or '13' does.
    trivial = sum(1 for t in titles if not any(_is_word_letter(ch) for ch in t))
    signals["trivial_share"] = round(trivial / n, 3)
    if trivial / n >= _TRIVIAL_SHARE and n >= 3:
        reasons.append(f"{trivial} of {n} chapter titles carry no words at all")
    else:
        trivial = 0  # below the bar it is not a signal

    # ── decimal sections as top-level chapters, numbering that skips ─────────
    decimals = [d for d in (_leading_decimal(t) for t in titles) if d]
    if len(decimals) >= _DECIMAL_MIN:
        majors = sorted({d[0] for d in decimals})
        major_gaps = (majors[-1] - majors[0] + 1) != len(majors)
        minor_gaps = False
        for major in majors:
            minors = sorted({d[1] for d in decimals if d[0] == major})
            # A unit's sections start at .1. A major whose minors begin at .2
            # or .4 lost its .1 to a promoted plain title — the Cambridge LB7
            # shape (f455c5bd: 'Cells' then '1.4 Cells, tissues and organs';
            # '2.2..2.6' with 2.1 missing), which the min-to-max contiguity
            # check alone could not see because 2..6 IS contiguous.
            if minors[0] != 1 or (
                len(minors) > 1 and (minors[-1] - minors[0] + 1) != len(minors)
            ):
                minor_gaps = True
        if major_gaps or minor_gaps:
            signals["decimal_nonseq"] = True
            reasons.append(
                "chapter numbering skips — section numbers like "
                f"{'.'.join(str(x) for x in decimals[0])} appear as whole chapters "
                "with gaps in the sequence, so boundaries likely fell on the "
                "wrong pages"
            )

    # ── extreme span variance ────────────────────────────────────────────────
    spans = _spans(chapters)
    if len(spans) >= _SPAN_MIN_CHAPTERS and total_pages and total_pages > 0:
        med = _median(spans)
        if (
            med > 0
            and max(spans) > _SPAN_RATIO * med
            and (max(spans) / total_pages > _SPAN_MAX_SHARE or min(spans) <= _SPAN_SLIVER_PAGES)
        ):
            signals["span_variance"] = True
            reasons.append(
                f"chapter sizes vary wildly ({min(spans)} to {max(spans)} pages) "
                "— boundaries this uneven rarely match a book's real units"
            )

    # ── fleet-audit defect classes (2026-08-24) ──────────────────────────────
    # Empty titles: not a label anyone typed. 948a9494 shipped 9 of 10 empty.
    empty = sum(1 for t in titles if not t.strip())
    signals["empty_titles"] = empty
    if n >= 3 and empty / n >= _EMPTY_TITLE_SHARE:
        reasons.append(f"{empty} of {n} chapters have no title at all")

    # U+FFFD: a decode failure by definition — the cheapest possible check,
    # and eb51a014 carried one at score 95.
    signals["replacement_char"] = any("�" in t for t in titles)
    if signals["replacement_char"]:
        reasons.append(
            "a chapter title contains the � replacement character — "
            "the text was corrupted when the PDF was made"
        )

    # Worksheet blank lines read as titles (f22b8b64).
    blanks = sum(1 for t in titles if _is_blank_line_title(t))
    signals["blank_line_titles"] = blanks
    if blanks >= _BLANK_LINE_MIN:
        reasons.append(
            f"{blanks} chapter titles are fill-in-the-blank underscore lines, "
            "not chapter names"
        )

    # OCR mojibake inside titles (81c94f98 — 24 generations burned).
    garbled = sum(1 for t in titles if _is_garbled(t))
    signals["garbled_titles"] = garbled
    if garbled >= _GARBLED_MIN:
        reasons.append(
            f"{garbled} chapter titles contain garbled text (misplaced accents "
            "or broken letters) — the titles were damaged in scanning"
        )

    # File names as chapters (2ea65b58, 994b8238).
    filenames = sum(1 for t in titles if _looks_like_filename(t))
    signals["filename_titles"] = filenames
    if n >= 2 and filenames / n >= _FILENAME_SHARE:
        reasons.append(
            f"{filenames} of {n} chapter titles look like file names, "
            "not the book's chapter names"
        )

    # Heading + first body sentence glued into the title (a13a45e2, 4e66897c).
    named = [t for t in titles if t.strip()]
    glued = sum(1 for t in named if _carries_body_text(t))
    signals["glued_titles"] = glued
    glued_hard = len(named) >= 2 and glued / max(1, len(named)) >= _GLUED_HARD_SHARE
    glued_weak = glued >= 2 and glued / max(1, len(named)) >= _GLUED_WEAK_SHARE
    if glued_weak or glued_hard:
        reasons.append(
            f"{glued} chapter titles run on into body text — the heading and "
            "its first sentence were glued together"
        )

    # Bare mid-sentence words as titles (bb68dec6: 'things', 'enough'). Only a
    # MINORITY counts — a book styled in all-lowercase is styling, not junk.
    fragments = sum(1 for t in titles if _is_lowercase_fragment(t))
    signals["fragment_titles"] = fragments
    fragment_hit = fragments >= _FRAGMENT_MIN and fragments * 2 < n
    if fragment_hit:
        reasons.append(
            f"{fragments} chapter titles are bare lowercase words — fragments "
            "of sentences, not chapter names"
        )

    # Repeated titles — exactly, or modulo a 'Unit N:' prefix (682eedf5 stored
    # 'Communication' AND 'Unit 2: Communication'; bb68dec6 stored one title
    # three times).
    seen: dict[str, int] = {}
    for t in named:
        key = _norm(t)
        seen[key] = seen.get(key, 0) + 1
    dupes = sum(cnt - 1 for cnt in seen.values() if cnt > 1)
    stripped = {}
    for t in named:
        s = _strip_label_prefix(t)
        if s and s != _norm(t):
            stripped[s] = stripped.get(s, 0) + 1
    prefix_dupes = sum(1 for s, cnt in stripped.items() if s in seen)
    signals["duplicate_titles"] = dupes + prefix_dupes
    if signals["duplicate_titles"]:
        reasons.append(
            "the same chapter title appears more than once — duplicated "
            "entries usually mean boundaries fell on the wrong pages"
        )

    # Labelled numbering that skips ('Unit 2' then 'Unit 4', no Unit 3 —
    # 682eedf5). Contiguity only, from wherever the run starts: a book whose
    # stored list begins at Chapter 2 is detection missing the front, not junk.
    label_nums = [x for x in (_label_leading_number(t) for t in named) if x is not None]
    if len(label_nums) >= 2:
        uniq = sorted(set(label_nums))
        if (uniq[-1] - uniq[0] + 1) != len(uniq):
            signals["label_number_gaps"] = True
            reasons.append(
                "the chapter numbering skips — numbered units are missing "
                "from the sequence"
            )

    # Two altitudes in one list: plain unit titles interleaved with decimal
    # SECTION titles (f455c5bd: 'Cells' beside '1.4 Cells, tissues and
    # organs' — 15 'chapters' for a 9-unit book).
    plain_titled = sum(
        1 for t in named
        if _leading_decimal(t) is None and any(_is_word_letter(ch) for ch in t)
    )
    if len(decimals) >= _DECIMAL_MIN and 2 <= plain_titled <= len(decimals):
        signals["mixed_levels"] = True
        reasons.append(
            "unit titles and numbered section titles are mixed in one list — "
            "two levels of the book promoted side by side"
        )

    # Corrupt page ranges IN STORED DATA. The structurer repairs before it
    # judges, so a freshly detected map never shows these — but stored rows
    # born before the repair existed do (9c36b003 stored end_page -1;
    # 994b8238 stored [115,1] and a [2,127] overlapping everything), and
    # book_health judges stored lists forever.
    ranged = []
    for c in chapters or []:
        try:
            ranged.append((int(c["start_page"]), int(c["end_page"])))
        except (KeyError, TypeError, ValueError):
            continue
    if ranged:
        bad = any(
            s < 0 or e < s or (total_pages and total_pages > 0 and e >= total_pages)
            for s, e in ranged
        )
        ordered = sorted(ranged)
        overlap = any(ordered[i][0] <= ordered[i - 1][1] for i in range(1, len(ordered)))
        if bad or overlap:
            signals["invalid_ranges"] = True
            reasons.append(
                "chapter page ranges are corrupt (backwards, negative or "
                "overlapping) — lessons would be built from the wrong pages"
            )

    # ── verdict ──────────────────────────────────────────────────────────────
    # HARD signals condemn alone: each names a defect with no legitimate
    # reading (see the threshold block up top). WEAK signals keep the
    # corroboration rule — the family in particular is indistinguishable from
    # a legitimate serial labelling scheme, and condemning it alone gated
    # healthy books in every non-English locale.
    hard = any([
        signals["replacement_char"],
        n >= 3 and empty / n >= _EMPTY_TITLE_SHARE,
        blanks >= _BLANK_LINE_MIN,
        garbled >= _GARBLED_MIN,
        n >= 2 and filenames / n >= _FILENAME_SHARE,
        glued_hard,
        signals["invalid_ranges"],
    ])
    weak = sum([
        signals["family_members"] >= _FAMILY_MIN_MEMBERS,
        bool(trivial),
        signals["decimal_nonseq"],
        signals["span_variance"],
        bool(signals["duplicate_titles"]),
        signals["label_number_gaps"],
        fragment_hit,
        signals["mixed_levels"],
        glued_weak and not glued_hard,
    ])
    return {
        "suspect": bool(hard or weak >= _WEAK_SIGNALS_FOR_SUSPECT),
        "reasons": reasons,
        "signals": signals,
    }


# ── page coverage, holes included ────────────────────────────────────────────
# BOTH existing unmapped-page probes (structurer._unmapped_tail and
# book_health._unmapped_tail_pages) measure only pages past the LAST chapter's
# end. The incident's 36-page hole (pages 26-61, opened AFTER every validator
# had run, by a heal relocation + _clamp_overlaps) therefore reported
# facts.unmapped_pages = 0. These helpers count the interval UNION, so a hole
# anywhere counts.


def covered_page_count(chapters: list[dict], total_pages: int) -> int:
    """Pages of [0, total_pages) that at least one chapter covers."""
    if total_pages <= 0:
        return 0
    ivs = []
    for c in chapters or []:
        try:
            start = max(0, int(c["start_page"]))
            end = min(total_pages - 1, int(c["end_page"]))
        except (KeyError, TypeError, ValueError):
            continue
        if end >= start:
            ivs.append((start, end))
    ivs.sort()
    covered = 0
    cur_s = cur_e = None
    for s, e in ivs:
        if cur_e is None or s > cur_e + 1:
            if cur_e is not None:
                covered += cur_e - cur_s + 1
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        covered += cur_e - cur_s + 1
    return covered


def uncovered_pages(
    chapters: list[dict], total_pages: int, apparatus: list[dict] | None = None
) -> dict:
    """``{"head", "holes", "tail"}`` page counts no chapter covers.

    ``head`` (front matter before chapter 1) is reported separately and NOT
    summed into anyone's verdict: a cover, contents and preface before the
    first chapter is normal book anatomy, and condemning it would regress
    every healthy book. ``holes`` (gaps BETWEEN chapters — the incident's
    36 orphaned pages) and ``tail`` are the pages a teacher silently loses.
    Silent when the map carries no page bounds at all — a caller with only
    titles is not making a claim about coverage.

    ``apparatus`` (2026-08-24, "apparatus is not a chapter"): recorded
    non-chapter ranges — glossary, index, contents, answer keys — that were
    EXCLUDED from the chapter list deliberately. Pages they cover are neither
    holes nor tail: a deliberate exclusion is not a detection hole, and
    counting a recorded glossary as "pages that won't be taught" would gate
    every book the trim worked correctly on. Only the RECORD buys the pardon —
    unrecorded gaps keep counting exactly as before.
    """
    out = {"head": 0, "holes": 0, "tail": 0}
    if total_pages <= 0:
        return out
    starts, ends = [], []
    for c in chapters or []:
        try:
            starts.append(max(0, int(c["start_page"])))
            ends.append(min(total_pages - 1, int(c["end_page"])))
        except (KeyError, TypeError, ValueError):
            continue
    if not starts:
        return out
    first, last = min(starts), max(ends)
    # Per-page marks beat interval arithmetic here: the apparatus subtraction
    # needs "covered by chapters OR apparatus" per page, and a book is at most
    # a few thousand pages.
    cov = bytearray(total_pages)
    for group in (chapters or []), (apparatus or []):
        for c in group:
            try:
                s = max(0, int(c["start_page"]))
                e = min(total_pages - 1, int(c["end_page"]))
            except (KeyError, TypeError, ValueError):
                continue
            for p in range(s, e + 1):
                cov[p] = 1
    out["head"] = first
    out["tail"] = sum(1 for p in range(last + 1, total_pages) if not cov[p])
    out["holes"] = sum(1 for p in range(first, last + 1) if not cov[p])
    return out


# ── apparatus is not a chapter (founder decision, 2026-08-24) ────────────────
# "A chapter is a unit of the book's TEACHING SEQUENCE." Cover, contents,
# copyright, acknowledgements, glossary, index, answer keys and reference/
# skills sections (Cambridge "Science Skills" — founder explicit) are never
# chapters: never listed, never gated as chapters, never part-split into
# credit rows. The dokumen.pub LB8 scan stored ALL of them — Cover Page,
# Contents, Science Skills, Glossary and Index as top-level chapters, the
# Glossary split into THREE one-credit parts.
#
# Two routes implement the ruling, and this is the conservative one: the
# vision/Claude rungs classify unit-vs-apparatus SEMANTICALLY as part of
# detection (any language, any wording), while this lexical+structural trim
# covers the non-LLM paths — obvious cover/contents/index shapes only. When
# in doubt, KEEP: over-trimming a real chapter costs a teacher content;
# showing one glossary costs a shrug. Hence every lexeme is position-gated
# (a front-matter word must sit near the front, a back-matter word near the
# back) and matched against the WHOLE title, never a substring — "Answers to
# big questions" is a chapter, "Answers" on the last pages is not.
#
# The excluded ranges are RECORDED, not vanished (see uncovered_pages): the
# structurer returns them on StructuredBook.apparatus, the worker stamps them
# into books.health.facts.apparatus, and coverage accounting treats them as
# deliberate. 'مقدمة'/'Introduction' are deliberately ABSENT from the front
# lexicon — an introduction is routinely the first real teaching unit.

_APPARATUS_FRONT: dict[str, frozenset[str]] = {
    "cover": frozenset({
        "cover", "cover page", "front cover", "الغلاف", "غلاف", "封面", "表紙",
        "muka depan", "kulit buku", "kapak", "portada", "couverture",
    }),
    "contents": frozenset({
        "contents", "content", "table of contents", "contents page",
        "المحتويات", "الفهرس", "فهرس", "فهرست", "جدول المحتويات", "قائمة المحتويات",
        "目录", "目錄", "目次", "contenido", "tabla de contenido", "índice",
        "indice", "sommaire", "table des matières", "inhalt",
        "inhaltsverzeichnis", "içindekiler", "isi kandungan", "kandungan",
        "daftar isi", "विषय सूची", "विषय-सूची", "अनुक्रमणिका", "सारणी",
        "สารบัญ", "mục lục", "оглавление", "содержание",
    }),
    "imprint": frozenset({
        "copyright", "copyright page", "imprint", "حقوق النشر", "حقوق الطبع",
        "版权", "版權", "奥付", "impressum",
    }),
    "acknowledgements": frozenset({
        "acknowledgements", "acknowledgments", "acknowledgement", "credits",
        "شكر وتقدير", "agradecimientos", "remerciements", "謝辞", "致谢",
        "penghargaan", "teşekkür",
    }),
    "preface": frozenset({
        "preface", "foreword", "how to use this book", "how to use this series",
        "about this book", "تمهيد", "prólogo", "prefacio", "avant-propos",
        "vorwort", "önsöz", "kata pengantar", "prakata", "序", "序文", "はじめに",
    }),
}
_APPARATUS_BACK: dict[str, frozenset[str]] = {
    "glossary": frozenset({
        "glossary", "glossary of terms", "مسرد", "مسرد المصطلحات",
        "قاموس المصطلحات", "المصطلحات", "glosario", "glossaire", "glossar",
        "sözlük", "词汇表", "詞彙表", "用語集", "glosari", "senarai istilah",
        "शब्दावली", "शब्दकोश", "อภิธานศัพท์", "глоссарий", "словарь терминов",
    }),
    "index": frozenset({
        "index", "الفهرس", "فهرس الموضوعات", "فهرس", "索引", "índice", "indice",
        "índice alfabético", "dizin", "indeks", "अनुक्रमणिका", "ดัชนี",
        "указатель", "предметный указатель",
    }),
    "answers": frozenset({
        "answers", "answer key", "answer keys", "الإجابات", "مفتاح الإجابات",
        "الأجوبة", "respuestas", "soluciones", "corrigés", "解答", "答案",
        "jawapan", "kunci jawaban", "उत्तर", "उत्तरमाला", "เฉลย", "ответы",
    }),
    "references": frozenset({
        "references", "bibliography", "المراجع", "قائمة المراجع", "参考文献",
        "bibliografía", "bibliografia", "références", "kaynakça", "rujukan",
        "संदर्भ", "литература", "список литературы",
    }),
}
# Words that join two back-matter names into one entry ("Glossary and Index").
_APPARATUS_CONNECTORS = frozenset({"and", "&", "y", "e", "et", "und", "dan", "ve", "و"})

# Position gates. Front lexemes only near the front (a mid-book chapter that
# happens to be called "Contents" in some language this list is wrong about is
# kept); back lexemes only in the last stretch. The reference/skills gate is
# tighter still because its lexeme is genuinely ambiguous — "Science Skills"
# is apparatus by explicit founder ruling, but a "* Skills" title anywhere
# else in a book could be a real unit.
_FRONT_MAX_SHARE = 0.15
_FRONT_MAX_PAGE = 12
_BACK_MIN_SHARE = 0.60
_SKILLS_MIN_SHARE = 0.85


def _apparatus_title_key(title: str) -> str:
    """The title as the lexicons store it: normalised, outer punctuation and a
    trailing bare serial stripped ('Contents ......... 4' → 'contents').

    'İ' (Turkish dotted capital, U+0130) casefolds to 'i' + a COMBINING DOT
    ABOVE — so 'İçindekiler' failed to match the lexicon's plain
    'içindekiler' and the one Turkish contents word this module ships was
    unreachable from any real Turkish book. The fleet's healthy Türkiye
    Anayasası fixture is dotted-İ throughout; same trap, other direction."""
    t = _norm(title).replace("i̇", "i")
    t = t.strip(" .·…:-–—0123456789٠١٢٣٤٥٦٧٨٩")
    return t.strip()


def apparatus_kind(title: str, start_page: int, total_pages: int) -> str | None:
    """The apparatus kind of a chapter entry, or None to KEEP it as a chapter.

    Lexical + positional, deliberately conservative — see the block comment.
    """
    if total_pages <= 0:
        return None
    key = _apparatus_title_key(title)
    if not key:
        return None
    front_limit = max(_FRONT_MAX_PAGE, int(_FRONT_MAX_SHARE * total_pages))
    in_front = start_page <= front_limit
    in_back = start_page >= _BACK_MIN_SHARE * total_pages
    if in_front:
        for kind, lexemes in _APPARATUS_FRONT.items():
            if key in lexemes:
                return kind
    if in_back:
        for kind, lexemes in _APPARATUS_BACK.items():
            if key in lexemes:
                return kind
        # Composite back matter: every joined part must itself be back matter
        # ("Glossary and Index"), or the entry is kept.
        parts = [p for p in key.replace("&", " & ").split() if p not in _APPARATUS_CONNECTORS]
        if len(parts) >= 2:
            joined_kinds = []
            for p in parts:
                k = next((kind for kind, lex in _APPARATUS_BACK.items() if p in lex), None)
                joined_kinds.append(k)
            if all(joined_kinds):
                return joined_kinds[0]
        # Reference/skills sections — the founder-explicit Cambridge "Science
        # Skills" shape: one or two words ending in "skills", hard against the
        # back of the book.
        words = key.split()
        if (
            start_page >= _SKILLS_MIN_SHARE * total_pages
            and 1 <= len(words) <= 2
            and words[-1] == "skills"
        ):
            return "reference"
    return None


def split_apparatus(
    chapters: list[dict], total_pages: int
) -> tuple[list[dict], list[dict]]:
    """``(teaching_units, apparatus)`` — the chapter list with apparatus
    entries moved out and RECORDED.

    An entry is apparatus when the detector said so (``kind == "apparatus"``,
    stamped by the vision/text-LLM rungs, which classify semantically in any
    language) or when the conservative lexical trim recognises it. Kept units
    are renumbered 0..n-1 — every consumer keys on contiguous chapter_num —
    and any detector ``kind`` marker is dropped from them, so the stored shape
    is byte-compatible with what consumers already read.

    Refuses to empty the list: a map that is ALL apparatus is a detection
    failure, and returning it untrimmed keeps the failure visible to the
    validator instead of storing a book with no chapters.
    """
    kept: list[dict] = []
    cut: list[dict] = []
    for c in chapters or []:
        try:
            start = int(c.get("start_page", 0))
            end = int(c.get("end_page", start))
        except (TypeError, ValueError):
            start, end = 0, 0
        title = str(c.get("title") or "")
        explicit = str(c.get("kind") or "").strip().lower() == "apparatus"
        kind = (
            (apparatus_kind(title, start, total_pages) or "apparatus")
            if explicit
            else apparatus_kind(title, start, total_pages)
        )
        if kind:
            cut.append({"title": title.strip(), "start_page": start,
                        "end_page": end, "kind": kind})
        else:
            kept.append({k: v for k, v in c.items() if k != "kind"})
    if not kept:
        return [dict(c) for c in chapters or []], []
    for i, c in enumerate(kept):
        c["chapter_num"] = i
    return kept, cut
