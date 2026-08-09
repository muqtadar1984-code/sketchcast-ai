"""Book Health Score — a predictive quality read computed at index time from
signals the pipeline ALREADY produces (text-layer coverage, scanned-ness,
chapter-detection plausibility, page count). Surfaced to the teacher the
moment a book finishes indexing so bad scans are caught BEFORE they generate
failed lessons — prevention ahead of the support agent's diagnosis.

Pure function, no I/O: `compute_book_health(extraction, chapter_defs)` →
a JSON-able dict stored on `books.health`. Deliberately honest about scanned
books: they are handled well by the vision path, so they score "Good" with an
informational note, not "Poor" — we don't want to scare teachers off scans the
pipeline processes fine.

Text is measured for QUALITY as well as volume (2026-08-09). It used to be
volume only, and a 1,008-page book whose "text layer" read
``Thegreatestconstantofmoderntimesischange`` — 0.1% spaces, CJK mojibake in
the front matter — scored 96/100 "excellent". Nothing intervened, and 17
lesson kits were generated from unreadable source text. ``text_quality()``
below is the signal that was missing; ``text_layer_is_usable()`` is what
routes such a book to the vision/OCR path a scanned book already takes.
"""

from __future__ import annotations

import bisect
import statistics

from shared.scripts import SPACELESS_SCRIPTS, script_profile

# ── Text-layer QUALITY thresholds ────────────────────────────────────────────
# Measured against the samples in tests/test_languages.py (one per shipped
# locale) and against the production text that triggered this work:
#
#   sample            space_ratio   median token
#   English               0.195          4.0
#   Hindi                 0.212          3.0
#   Portuguese/Spanish    0.175          4.5
#   French                0.170          4.0
#   Marathi               0.169          4.0
#   Malay                 0.152          5.0
#   Arabic                0.147          5.0
#   Telugu                0.125          6.0    ← lowest / longest legitimate
#   Sterman ch.1 (bad)    0.012         28.0
#
# So: 0.06 is less than HALF the lowest legitimate ratio measured (Telugu's
# 0.125) and five times the broken book's 0.012 — a mean token length above
# ~15 characters, which no naturally spaced text reaches. 12 is double the
# longest legitimate median (Telugu's 6.0) and well under the broken book's 28.
_MIN_SPACE_RATIO = 0.06
_MAX_MEDIAN_TOKEN = 12
# Both must agree before word separation is called broken: the ratio is a
# whole-sample average and one giant unspaced blob inside good prose could drag
# it down on its own, while the MEDIAN token length only moves when most of the
# text is affected. Neither measure condemns a book alone.

# Script coherence — the second signal. A book is written in one writing
# system; a jumble is mojibake. It is measured DIFFERENTLY for the two kinds of
# script, because the same number means opposite things in each:
#
#   * where the script DOES use spaces, coherence only corroborates degraded
#     spacing. Genuinely bilingual textbooks ("गणित Mathematics Class 8", Malay
#     books with dense Quranic Arabic — measured at 0.95 family share) are
#     normal in this product's markets and must never be condemned for it.
#
#   * where the script has NO spaces (Chinese, Japanese, Thai) family share is
#     not usable at all, and thresholding it was a regression. Shares are taken
#     over LETTERS, and a Han character carries a whole word where a Latin word
#     costs 5-8 letters — so a handful of English technical terms swamps the
#     share of a book that is overwhelmingly Chinese by content. Measured on the
#     old rule, with English density rising left to right:
#         Chinese   0 blocks fam 1.00 ok | 1 block 0.56 CONDEMNED | 2+ 0.61-0.86 ok
#         Japanese  0 ok                 | 1 block 0.55 CONDEMNED | 2+ ok
#         Thai      1 / 2 / 3 blocks all CONDEMNED (0.73 / 0.57 / 0.53)
#     A verdict that flips back to "ok" as the supposed contamination INCREASES
#     is proof the measure is wrong, not the threshold — and the cost of the
#     false positive is real money: the book is scored "fair", told its PDF
#     can't be read, and routed to per-page vision OCR.
#
#     What actually marks the mojibake is the opposite of a share. Spaceless
#     prose is, by definition, nearly space-free; the triggering book's front
#     matter ("撃i i _ 3蚤妻 … S Sy 離em S T 的毒軸 岳 ng and") measures 31% spaces —
#     Han fragments sprinkled through spaced Latin debris. A Han-dominant sample
#     cannot honestly reach 15%: getting there needs ~1 space per 6 characters,
#     i.e. mostly spaced Latin words, at which point Latin dominates the letters
#     and the sample is judged by the SPACED branch instead. Real Chinese and
#     Japanese samples measure 0.00-0.03 even carrying English terms, and Thai
#     (which spaces phrases, not words) measures under 0.10.
#
#     …PROVIDED the spaces counted are the book's own. Measuring this ratio over
#     items joined with " " put the same Chinese text at 0.333 / 0.250 / 0.200 /
#     0.166 for items of 2 / 3 / 4 / 5 characters, all on the join alone — see
#     _MIN_SPACED_ITEM_CHARS, which is the answer to that and the reason the
#     numbers above still hold.
_MIN_FAMILY_SHARE_SPACED = 0.55
_DEGRADED_SPACE_RATIO = 0.10  # below the 0.125 floor of any shipped locale
_MAX_SPACE_RATIO_SPACELESS = 0.15

# WHICH branch a sample belongs to is not the dominant script either. Because a
# Latin word costs 5-8 letters against a Han character's one, a book that is
# overwhelmingly Chinese by content flips to dominant="latin" while still being
# mostly unspaced — measured, that book was then condemned by the SPACED rules
# for "words run together", which is just the letter-vs-word error arriving by a
# second route. So the branch is chosen by how much of the sample cannot carry
# spaces at all: above a quarter, the sample's space ratio and token lengths are
# governed by the unspaced part and say nothing about extraction quality.
# (Consequence, accepted knowingly: a Latin book with genuinely run-together
# words AND >25% CJK letters escapes the spacing rule. The book that triggered
# this work is not that shape — its 1,008 pages are run-together English with
# mojibake confined to the front matter, so a whole-book sample stays well under
# the gate and the spacing rule still fires. Verified by test.)
_SPACELESS_LETTER_SHARE = 0.25

# Enough text to measure, and enough to be worth measuring. Under ~400 chars a
# book has no text layer worth judging — that is the scanned-book branch's job,
# not this one. 40k chars is ~15 pages of prose, plenty for a stable ratio.
_MIN_QUALITY_CHARS = 400
_QUALITY_SAMPLE_CHARS = 40000
# …but taken from ACROSS the book, not from its front. The first 40k characters
# of a large book are ~15 pages of cover, copyright/CIP block, colophon and
# contents — systematically the least representative text in it, and the place
# where bilingual publisher data and ISBN blocks live. Ten windows spread over
# the body measure the book; one window over the front matter measures the
# imprint page. The first 5% is skipped for the same reason.
_QUALITY_WINDOWS = 10
_QUALITY_WINDOW_CHARS = _QUALITY_SAMPLE_CHARS // _QUALITY_WINDOWS
_QUALITY_SKIP_HEAD = 0.05

# Which extraction items can HONESTLY be asked about word spacing. An item is a
# line, a table cell, a figure caption, a vocabulary entry, an exercise stem or a
# ruby run — and a short one holds at most a word or two, so "it contains no
# space" is a fact about the extractor's SEGMENTATION, not about the text.
# Measuring the space ratio of the joined sample instead of the items therefore
# manufactured the very defect it was measuring: the injected join spaces alone,
# on identical monolingual Chinese body text, measured
#     items of 2 chars 0.333 | 3 -> 0.250 | 4 -> 0.200 | 5 -> 0.166 | 6 -> 0.142
# against _MAX_SPACE_RATIO_SPACELESS of 0.15, so a Chinese, Japanese or Thai book
# extracted in items of five characters or fewer was condemned as mojibake with
# the reason "characters from unrelated scripts are mixed through the text" — and
# extraction_has_text() then sent a whole textbook down the per-page vision/OCR
# route. Real money, long latency, wrong answer, and the shorter the items the
# worse the verdict, which is the signature of measuring an artefact.
#
# 20 characters is where an item is about the TEXT rather than the segmentation:
# the ten shipped locales measure a median whitespace-token of 3.0-6.0, so with
# its trailing space a word costs ~4-7 characters and 20 characters is three or
# four words; in a spaceless script 20 characters is ~20 Han/kana glyphs or ~7
# words, comfortably clear of the 2-6 char band above where the artefact lived.
_MIN_SPACED_ITEM_CHARS = 20


def _quality_sample(extraction) -> list[str]:
    """Up to _QUALITY_SAMPLE_CHARS of the extracted text, spread over the book,
    returned as the ITEMS (or item fragments) it came from — never pre-joined.

    The caller has to keep the boundaries. Joining with "" would manufacture the
    run-together text ``text_quality`` exists to detect, and joining with " "
    manufactures word spacing that isn't there (see _MIN_SPACED_ITEM_CHARS), so
    the two questions need two different views of the same sample and only the
    caller can build them.

    Long items are SLICED rather than taken whole: a book's front matter can
    arrive as one 47k-character item, and taking whole items would spend the
    entire budget on it — the exact document-order bias the windowing exists to
    avoid.
    """
    parts = [
        (getattr(item, "text", "") or "").strip()
        for item in getattr(extraction, "items", []) or []
    ]
    parts = [p for p in parts if p]
    total = sum(len(p) for p in parts)
    if total <= _QUALITY_SAMPLE_CHARS:
        return parts

    starts: list[int] = []
    at = 0
    for p in parts:
        starts.append(at)
        at += len(p)

    head = int(total * _QUALITY_SKIP_HEAD)
    step = (total - head) / _QUALITY_WINDOWS
    out: list[str] = []
    for k in range(_QUALITY_WINDOWS):
        lo = head + int(k * step)
        hi = lo + _QUALITY_WINDOW_CHARS
        i = max(0, bisect.bisect_right(starts, lo) - 1)
        while i < len(parts) and starts[i] < hi:
            fragment = parts[i][max(0, lo - starts[i]):hi - starts[i]]
            if fragment:
                out.append(fragment)
            i += 1
    return out


def text_quality(extraction) -> dict:
    """Is the extracted text READABLE, not merely present?

    Returns {checked, ok, reason, space_ratio, median_token, spacing_measured,
    script, family_share} — all JSON-able, all published into health.facts so
    support and the console can query them.

    ``checked`` is False (and ``ok`` True) when there is too little text to
    judge: a scanned book must keep falling into the scanned branch, not be
    condemned by a measurement taken over 40 characters of watermark. The same
    applies when the dominant script is ``other`` — a script this module does
    not know is a script whose spacing convention it also does not know, so
    every rule below would be guesswork. Unjudgeable is not the same as bad.

    ``spacing_measured`` says the same thing one level down, about the SPACING
    signal alone: it is False when the book arrives as items too short to carry
    word spacing at all, and then ``space_ratio`` is 0.0 and means nothing. The
    same principle — a book extracted a cell at a time has not told us anything
    about its word separation, so no rule may pretend it has.
    """
    parts = _quality_sample(extraction)
    # Two views of one sample, and the gap between them is the whole of this
    # module's second bug. TOKENS and the script profile are taken from the
    # space-JOINED text, because item boundaries are not word boundaries and
    # concatenating would manufacture the run-together text this function looks
    # for. The SPACE RATIO is taken from the items themselves, because the join
    # spaces are ours and not the book's — counting them condemned monolingual
    # CJK books whose text arrives in short items (see _MIN_SPACED_ITEM_CHARS),
    # and only items long enough to hold a word boundary are asked at all.
    sample = " ".join(parts)
    spaced = "".join(p for p in parts if len(p) >= _MIN_SPACED_ITEM_CHARS)
    profile = script_profile(sample)
    out = {
        "checked": False,
        "ok": True,
        "reason": None,
        "space_ratio": 0.0,
        "median_token": 0.0,
        "spacing_measured": False,
        "script": profile["dominant"],
        "family_share": round(profile["family_share"], 2),
    }
    if (
        sum(len(p) for p in parts) < _MIN_QUALITY_CHARS
        or not profile["dominant"]
        or profile["dominant"] == "other"
    ):
        return out

    out["checked"] = True
    out["spacing_measured"] = len(spaced) >= _MIN_QUALITY_CHARS
    if out["spacing_measured"]:
        out["space_ratio"] = round(sum(1 for ch in spaced if ch.isspace()) / len(spaced), 3)
    tokens = [t for t in sample.split() if any(ch.isalpha() for ch in t)]
    out["median_token"] = round(statistics.median(len(t) for t in tokens), 1) if tokens else 0.0

    # Every rule below thresholds the space ratio, so every rule below needs a
    # space ratio that was actually measurable. When it wasn't, the book is not
    # condemned on a number nobody took — the false negative (a genuinely broken
    # book that also happens to extract a cell at a time) is a lesson built from
    # poor grounding; the false positive it replaces was a whole good textbook
    # re-read page by page by vision.
    if not out["spacing_measured"]:
        return out

    letters = profile["letters"] or 1
    spaceless_share = sum(
        n for name, n in profile["counts"].items() if name in SPACELESS_SCRIPTS
    ) / letters
    spaceless = (
        profile["dominant"] in SPACELESS_SCRIPTS
        or spaceless_share >= _SPACELESS_LETTER_SHARE
    )
    if not spaceless and out["space_ratio"] < _MIN_SPACE_RATIO and out["median_token"] > _MAX_MEDIAN_TOKEN:
        out["ok"] = False
        out["reason"] = "words are run together — the text layer has almost no word spacing"
    elif spaceless and out["space_ratio"] > _MAX_SPACE_RATIO_SPACELESS:
        # Han/kana/Thai fragments scattered through spaced debris — see the
        # note on _MAX_SPACE_RATIO_SPACELESS. Never a share of letters.
        out["ok"] = False
        out["reason"] = "characters from unrelated scripts are mixed through the text"
    elif (
        not spaceless
        and profile["family_share"] < _MIN_FAMILY_SHARE_SPACED
        and out["space_ratio"] < _DEGRADED_SPACE_RATIO
    ):
        out["ok"] = False
        out["reason"] = "characters from unrelated scripts are mixed through the text"
    return out


def text_layer_is_usable(extraction) -> bool:
    """Whether the text layer can be TRUSTED as the book's content.

    ``vision_chapters.extraction_has_text`` consults this, so a book whose text
    layer is garbage routes down the same vision/OCR path a book with NO text
    layer already takes — instead of grounding every lesson in mojibake.
    """
    return bool(text_quality(extraction)["ok"])


# A page or two of colophon past the last chapter is not content anybody is
# missing; kept in step with structurer._MIN_REPORTABLE_UNMAPPED.
_MIN_REPORTABLE_UNMAPPED = 3
# …and past this share of the book it is not back matter at all, it is the book.
# Kept in step with structurer._MAX_UNMAPPED_TAIL_SHARE, which rejects such a map
# outright; a map that arrives here with a tail this large is one the structurer
# rejected and then had to restore because nothing finer validated either, so the
# teacher is the last line of defence and the score has to tell them. Measured:
# 96 of 301 pages mapped scored 82 band "good" with only the structure dimension
# capped — 68% of the book missing, reported as a good book.
_MAX_UNMAPPED_TAIL_SHARE = 0.25


def _unmapped_tail_pages(chapter_defs: list[dict], total_pages: int) -> int:
    """How many pages past the last chapter's end nothing covers.

    Silent when the map carries no page bounds at all — a caller that only has
    titles is not making a claim about coverage.
    """
    ends = []
    for c in chapter_defs or []:
        try:
            ends.append(int(c["end_page"]))
        except (KeyError, TypeError, ValueError):
            continue
    if not ends or total_pages <= 0:
        return 0
    gap = (total_pages - 1) - max(ends)
    return gap if gap >= _MIN_REPORTABLE_UNMAPPED else 0


def _band(score: int) -> str:
    if score >= 85:
        return "excellent"
    if score >= 70:
        return "good"
    if score >= 50:
        return "fair"
    return "poor"


def compute_book_health(extraction, chapter_defs: list[dict]) -> dict:
    """Return {score, band, dimensions, problems, recommendation, note}.

    dimensions each 0-100:
      text_layer — is the text machine-readable, and across how many pages?
      structure  — did chapter detection find a real, usable unit list?
    The overall score weights structure a little higher, because a wrong or
    single-chapter split hurts every downstream lesson more than a slightly
    sparse text layer (which the vision path backstops).
    """
    total_pages = int(getattr(extraction, "total_pages", 0) or 0)
    readability = float(getattr(extraction, "readability_score", 0.0) or 0.0)
    text_chars = sum(len(getattr(i, "text", "") or "") for i in getattr(extraction, "items", []))
    has_text = text_chars >= 200
    n_chapters = len(chapter_defs or [])
    quality = text_quality(extraction)
    unreadable_text = has_text and not quality["ok"]

    problems: list[str] = []
    note: str | None = None

    # ── text-layer / OCR dimension ────────────────────────────────────────────
    if not has_text:
        # Scanned book — no text layer, but the vision path reads it well.
        text_layer = 74
        note = "Scanned book — read by AI vision (works well, adds a little processing time)."
    elif unreadable_text:
        # There IS text and plenty of it — it just isn't readable. Volume and
        # page coverage (readability_score) both look great here, which is
        # exactly how this scored 96 before. Score the QUALITY instead, and say
        # what happens next: the book takes the same vision/OCR route a scan
        # takes (see text_layer_is_usable), so lessons still get built.
        text_layer = 30
        problems.append(
            f"The PDF's text layer can't be read — {quality['reason']}. "
            "The pages will be read by AI vision instead."
        )
        note = "Unreadable text layer — read by AI vision (works, adds processing time)."
    elif readability >= 0.75:
        text_layer = 96
    elif readability >= 0.5:
        text_layer = 84
    elif readability >= 0.3:
        text_layer = 68
        problems.append("Sparse text layer — many pages are images, so extraction may miss content.")
    else:
        text_layer = 52
        problems.append("Very little machine-readable text — the PDF may be a low-quality or partial scan.")

    # ── structure / chapter-detection dimension ───────────────────────────────
    if n_chapters >= 3:
        structure = 95
    elif n_chapters == 2:
        structure = 78
    elif n_chapters == 1:
        structure = 50
        problems.append("Only one unit was detected — chapter boundaries weren't found, so lessons can't be split by chapter.")
    else:
        structure = 40
        problems.append("No chapters detected.")

    # ── page-count sanity ─────────────────────────────────────────────────────
    if 0 < total_pages < 5:
        structure = min(structure, 55)
        problems.append("Very short document — there may not be enough content to teach from.")

    # ── pages no chapter covers ───────────────────────────────────────────────
    # The structurer bounds how far the LAST chapter may stretch, so when
    # detection stops early the tail is left unmapped rather than bolted onto a
    # 7-page chapter. That is the right trade, but it must never be silent:
    # unmapped pages are never extracted, never analysed and never taught, and
    # the teacher has no other way to find out.
    unmapped_tail = _unmapped_tail_pages(chapter_defs, total_pages)
    if unmapped_tail:
        structure = min(structure, 70)
        problems.append(
            f"The last {unmapped_tail} pages aren't covered by any chapter — chapter "
            "detection stopped before the end of the book, so those pages won't be taught."
        )

    # ── overall (structure weighted a touch higher) ───────────────────────────
    score = round(text_layer * 0.45 + structure * 0.55)

    # Honest caps so a strong signal can't mask a real weakness:
    #   * a scanned book works but is never "excellent" (a clean text PDF is);
    #   * an UNREADABLE text layer is worse than none — the vision path still
    #     rescues the lesson, so it isn't "poor", but it must never read as
    #     "excellent" the way 1,008 pages of mojibake did (score 95, band
    #     "excellent", 17 kits generated). 55 is one point under the "good"
    #     floor and below the scanned cap of 82, because a corrupt text layer
    #     can also leak into tutor grounding, which no scan can;
    #   * a single detected unit is a real structure problem;
    #   * no chapters / too short is a hard failure.
    #   * a book most of which no chapter covers is not a good book, whatever
    #     the pages that ARE mapped look like — capping the structure dimension
    #     alone left 96 of 301 mapped pages scoring 82 "good", because a 96
    #     text_layer carries 45% of the weight. 69 is one point under the "good"
    #     floor in _band, which is the specific lie being fixed; it stays above
    #     the unreadable-text cap because the mapped chapters do still teach;
    if not has_text:
        score = min(score, 82)
    if unreadable_text:
        score = min(score, 55)
    if unmapped_tail > _MAX_UNMAPPED_TAIL_SHARE * total_pages:
        score = min(score, 69)
    if n_chapters <= 1 and total_pages >= 20:
        score = min(score, 66)
    if n_chapters == 0:
        score = min(score, 45)
    if 0 < total_pages < 5:
        score = min(score, 55)
    band = _band(score)

    # ── recommendation: address the single worst signal. Driven by PROBLEMS,
    #    not the band, so a flagged issue always surfaces its fix. ─────────────
    if not problems and band in ("excellent", "good"):
        recommendation = None
    elif unreadable_text:
        recommendation = (
            "This PDF's text was saved in a way we can't read (often a re-scan or a "
            "foreign-language reprint). Lessons will be built by reading the pages, but a "
            "text-based PDF of the same book would produce noticeably better ones."
        )
    elif has_text and text_layer <= 68:
        recommendation = "Upload a higher-quality scan or a text-based PDF so the full content is captured."
    elif n_chapters <= 1:
        recommendation = "If this book has chapters, a version with a table of contents or clearer chapter headings will split it into per-chapter lessons."
    elif not has_text:
        recommendation = "This scan will work, but a clearer scan or a text-based PDF would produce the best lessons."
    else:
        recommendation = "Review the detected chapters before generating."

    return {
        "score": int(score),
        "band": band,
        "dimensions": {"text_layer": int(text_layer), "structure": int(structure)},
        "facts": {
            "pages": total_pages,
            "chapters": n_chapters,
            "has_text_layer": has_text,
            "text_coverage": round(readability, 2),
            # The measured quality signals, so a support/console query can tell
            # a mis-scored book from a genuinely bad one without re-extracting.
            "text_readable": not unreadable_text,
            "space_ratio": quality["space_ratio"],
            # …which is 0.0 and meaningless when this is False — a book that
            # arrives a table cell at a time never showed us its word spacing.
            "spacing_measured": quality["spacing_measured"],
            "median_token": quality["median_token"],
            "script": quality["script"],
            "unmapped_pages": unmapped_tail,
        },
        "problems": problems,
        "recommendation": recommendation,
        "note": note,
    }
