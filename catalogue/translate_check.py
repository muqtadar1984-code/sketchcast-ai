"""Pre-checks for a TRANSLATED kit (Kit Translator plan, revision 3, 2026-10-16).

Pure functions over (source, target) script dicts and maths lessons. They
answer two questions a reviewer who cannot read the language cannot:

  * pre-check A — is the translation the SAME lesson? Same segments in the
    same order, the same speaker turns, every protected expression intact,
    every number and curriculum code equivalent (not identical: ``٣٫١٤`` is
    ``3.14``), nothing left in English, the glossary terms used where their
    English terms were;
  * pre-check C — did it RENDER as one? Narration that ends well before or
    after its clip, silent segments, text the acceptance report saw
    overlapping.

A check can only BLOCK or FLAG (decision D3): nothing here approves. A
``blocking`` finding fails the translation stage; a ``flag`` is shown on the
review screen and left to the reviewer. The report is one dict, written to
``topic_kits.precheck``.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Iterable, Optional

from shared.languages import NON_LATIN_SCRIPTS

# ── numerals ─────────────────────────────────────────────────────────────

# Digit runs of the scripts the lesson languages write in, each mapped onto
# ASCII. Fullwidth digits ride along: a Gemini reply has produced them.
_DIGIT_RUNS = (
    "٠١٢٣٤٥٦٧٨٩",   # Arabic-Indic
    "۰۱۲۳۴۵۶۷۸۹",   # Extended Arabic-Indic (Urdu, Persian)
    "०१२३४५६७८९",   # Devanagari
    "౦౧౨౩౪౫౬౭౮౯",   # Telugu
    "０１２３４５６７８９",   # fullwidth
)
_DIGIT_MAP = {ord(c): str(i) for run in _DIGIT_RUNS for i, c in enumerate(run)}
_DIGIT_MAP.update({
    ord("٫"): ".",   # Arabic decimal separator
    ord("٬"): ",",   # Arabic thousands separator
    ord("−"): "-",   # minus sign
    ord("–"): "-",
})

# A coefficient counts too ("2r", "3x"): only another digit or a decimal
# point may touch a number, letters may follow it. A full stop that ENDS the
# sentence ("x equals 6.") is not a decimal point — until 2026-10-17 the
# lookahead refused it and a number before a full stop was invisible to
# every check.
_NUMBER_RE = re.compile(r"(?<![\d.])-?\d[\d,]*(?:\.\d+)?(?!\d|\.\d)")
_THOUSANDS_RE = re.compile(r"^-?\d{1,3}(?:,\d{3})+(?:\.\d+)?$")
# "7Gg.12", "9Gg.10", "7Bs.01": an objective code; "cbse:7:GEO:01": a seeded
# node code. Both are invariant across languages.
_CODE_RE = re.compile(r"\b\d{1,2}[A-Za-z]{1,3}\.\d{1,3}\b|\b[a-z]+:\d{1,2}:[A-Za-z0-9_-]+(?::[A-Za-z0-9_-]+)*\b")


def normalize_numerals(text: str) -> str:
    """Every digit as ASCII, every decimal mark as ``.``; nothing else moves."""
    return str(text or "").translate(_DIGIT_MAP)


def number_values(text: str) -> list[str]:
    """The numbers a text states, as canonical decimal strings, in order.
    A comma inside a number is a thousands separator when it groups threes
    (``41,120``) and a decimal mark otherwise (``1,5`` in French prose)."""
    out: list[str] = []
    for m in _NUMBER_RE.finditer(normalize_numerals(text)):
        tok = m.group(0)
        if _THOUSANDS_RE.match(tok):
            tok = tok.replace(",", "")
        elif "," in tok:
            if "." in tok:
                tok = tok.replace(",", "")
            else:
                tok = tok.replace(",", ".")
        try:
            v = Decimal(tok)
        except InvalidOperation:
            continue
        out.append(format(v.normalize(), "f") if v != v.to_integral() else str(int(v)))
    return out


def code_tokens(text: str) -> list[str]:
    return sorted(set(_CODE_RE.findall(str(text or ""))))


# ── numbers the ENGLISH source spells out ─────────────────────────────────
# A maths lesson's spoken lines are written for the voice: "three hundred and
# sixty degrees", "three z", "two x minus ten" (shared/tts: a digit string is
# read unevenly). The Arabic translation of the Angles kit (2026-10-17, kit
# dee8ed26) wrote those as digits — ٣٦٠, ٣، ٢، ١٠ — and every one of them
# was reported as an invented number, twenty findings, the kit refused.
# These words are parsed to values and may ABSORB digits on the target side;
# they never count as missing (an English "one of the angles" is not a
# number the translation must keep), so this only ever removes false
# "extra" findings and never adds a finding of its own.
_UNITS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
          "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
          "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_SCALES = {"hundred": 100, "thousand": 1000, "million": 1_000_000}
_WORD_RE = re.compile(r"[A-Za-z]+")


def english_number_words(text: str) -> list[str]:
    """The values of the numbers ``text`` spells out in English words, as
    canonical decimal strings, in order: "one hundred and thirty-five" →
    "135", "three z" → "3", "two point five" → "2.5". An "and" counts only
    inside a number ("three hundred and sixty"); "a hundred" is not parsed."""
    words = [w.lower() for w in _WORD_RE.findall(str(text or "").replace("-", " "))]
    out: list[str] = []
    total = 0
    current = 0
    in_number = False
    decimals: list[str] = []
    in_decimals = False

    def flush():
        nonlocal total, current, in_number, decimals, in_decimals
        if in_number:
            value = total + current
            if in_decimals and decimals:
                out.append(f"{value}.{''.join(decimals)}".rstrip("0").rstrip(".") if any(d != "0" for d in decimals)
                           else str(value))
            else:
                out.append(str(value))
        total = current = 0
        in_number = in_decimals = False
        decimals = []

    i = 0
    while i < len(words):
        w = words[i]
        if in_decimals:
            if w in _UNITS and _UNITS[w] <= 9:
                decimals.append(str(_UNITS[w]))
                i += 1
                continue
            flush()
            continue
        if w in _UNITS:
            if in_number and current % 10 != 0:
                flush()                        # "three two": two numbers spoken in a row
            current += _UNITS[w]
            in_number = True
        elif w in _TENS:
            if in_number and current % 100 != 0:
                flush()                        # "three twenty": two numbers in a row
            current += _TENS[w]
            in_number = True
        elif w in _SCALES:
            if not in_number:
                i += 1
                continue                       # "a hundred", "hundreds of": not a value
            scale = _SCALES[w]
            if scale == 100:
                current = (current or 1) * 100
            else:
                total += (current or 1) * scale
                current = 0
        elif w == "and" and in_number and (total or current >= 100) and i + 1 < len(words) \
                and (words[i + 1] in _UNITS or words[i + 1] in _TENS):
            pass                               # "three hundred AND sixty"
        elif w == "point" and in_number and i + 1 < len(words) and words[i + 1] in _UNITS:
            in_decimals = True
        elif w in _POWERS:
            # "x squared" is written x² — the exponent is a digit on the board
            # and in a translation that writes the maths; it stands alone
            flush()
            out.append(_POWERS[w])
        else:
            flush()
        i += 1
    flush()
    return out


_POWERS = {"squared": "2", "cubed": "3"}


def _abs(value: str) -> str:
    """A number's value without its sign: "negative three" is spoken, "-3"
    is written, "x minus four" is spoken and "x − 4" (or "x-4") is written —
    the sign of a DIGIT in a translation follows the writing, not the
    speech, so an absorbed digit is matched by magnitude."""
    return str(value or "").lstrip("-")


def board_numbers(lesson: dict) -> list[str]:
    """Every number written on the lesson's board — the problems, the
    before/after lines, the answers (LESSON_INVARIANT_KEYS and
    LESSON_PROSE_MATHS_KEYS) — as values. A spoken line may quote any of
    them: "set each factor equal to zero" restated as "… = 0", "x squared
    minus two x" as "x² − 2x". The pool is a SET: the board is quoted, not
    counted."""
    out: list[str] = []
    for path, v in _walk(lesson or {}):
        key = _leaf_key(path)
        if isinstance(v, str) and (key in LESSON_INVARIANT_KEYS or key in LESSON_PROSE_MATHS_KEYS):
            for n in number_values(v):
                if n not in out:
                    out.append(n)
    return out


def number_samples(text: str, values: Iterable[str], width: int = 60, limit: int = 6) -> list[str]:
    """A short window of ``text`` around the first place each value in
    ``values`` is written (digits of any script, sign included) — what the
    reviewer, or the next fix, needs to see. Distinct values only."""
    out: list[str] = []
    norm = normalize_numerals(str(text or ""))
    seen: set[str] = set()
    for v in values:
        v = str(v)
        if v in seen or len(out) >= limit:
            continue
        seen.add(v)
        for m in _NUMBER_RE.finditer(norm):
            tok = m.group(0).replace(",", "")
            try:
                same = format(Decimal(tok).normalize(), "f").rstrip("0").rstrip(".") == v or tok == v or str(int(Decimal(tok))) == v
            except (InvalidOperation, ValueError):
                same = False
            if same:
                a, b = max(0, m.start() - width), min(len(norm), m.end() + width)
                out.append(("…" if a else "") + norm[a:b].replace(chr(10), " ") + ("…" if b < len(norm) else ""))
                break
    return out


def numbers_equivalent(source: str, target: str, board: Iterable[str] = ()) -> tuple[bool, list[str], list[str]]:
    """Same multiset of numeric VALUES on both sides. Returns (ok, missing,
    extra) — what the target lost and what it invented.

    A digit the target carries that the source does not is NOT invented when
    the source speaks it: a value the English spells out in words ("three
    hundred and sixty", "negative three", "x squared") stands for one digit
    on the target side, matched by magnitude (_abs) — the Quadratic kit
    (2026-10-17) wrote "negative three" as -3 and "x squared" as x². Nor
    when it is written on the ``board`` (the lesson's problems, working
    lines and answers): a spoken line may quote the board — "… = 0". A
    spoken value is never REQUIRED on the target side; a digit the source
    writes still is."""
    a, b = number_values(source), number_values(target)
    missing = list(a)
    extra: list[str] = []
    for v in b:
        if v in missing:
            missing.remove(v)
        else:
            extra.append(v)
    if extra:
        spelt = [_abs(x) for x in english_number_words(source)]
        pool = {_abs(x) for x in (board or ())}
        for v in list(extra):
            av = _abs(v)
            if av in spelt:
                spelt.remove(av)
                extra.remove(v)
            elif av in pool:
                extra.remove(v)
    return (not missing and not extra), missing, extra


# ── scripts of the lesson languages ─────────────────────────────────────

_SCRIPT_RE = {
    "arab": re.compile(r"[؀-ۿݐ-ݿ]"),
    "deva": re.compile(r"[ऀ-ॿ]"),
    "telu": re.compile(r"[ఀ-౿]"),
}
_LATIN_RE = re.compile(r"[A-Za-z]")
_LANGUAGE_SCRIPT = NON_LATIN_SCRIPTS      # shared.languages: one table for the check and the analyzer's budget
# A target in a non-Latin script may keep this share of Latin letters (a
# variable name, a unit, a proper noun) before the segment is flagged.
LATIN_SHARE_FLAG = 0.25
# English function words: a Latin-script target (fr, es, pt, ms) that still
# reads English shows them; a translation does not.
_EN_STOP = frozenset("the and of to is that with for are was this from have which their there".split())
EN_STOP_SHARE_FLAG = 0.08


def script_of(language: str) -> Optional[str]:
    return _LANGUAGE_SCRIPT.get(str(language or "").lower())


def english_leftover(text: str, language: str, protected: Iterable[str] = ()) -> Optional[str]:
    """A reason string when ``text`` still reads as English for a target in
    ``language``; None when it does not. Protected expressions are removed
    first — ``P = πr + 2r`` is Latin letters by design."""
    body = str(text or "")
    for p in protected:
        if p:
            body = body.replace(p, " ")
    letters_latin = len(_LATIN_RE.findall(body))
    script = script_of(language)
    if script:
        own = len(_SCRIPT_RE[script].findall(body))
        total = own + letters_latin
        if total >= 12 and letters_latin / total > LATIN_SHARE_FLAG:
            return f"{letters_latin} Latin letters against {own} in the target script"
        return None
    words = re.findall(r"[A-Za-z']+", body.lower())
    if len(words) >= 8:
        stop = sum(1 for w in words if w in _EN_STOP)
        if stop / len(words) > EN_STOP_SHARE_FLAG:
            return f"{stop} English function words in {len(words)}"
    return None


# ── the report ───────────────────────────────────────────────────────────


def _finding(where: str, what: str, **detail) -> dict:
    return {"where": where, "what": what, **{k: v for k, v in detail.items() if v not in (None, [], "")}}


def _dialogue_shape(seg: dict) -> list[str]:
    return [str((d or {}).get("who") or "teacher") for d in (seg.get("dialogue") or []) if isinstance(d, dict)]


def _segment_text(seg: dict) -> str:
    parts = [str(seg.get("text") or ""), str(seg.get("slide_heading") or "")]
    parts += [str(p) for p in (seg.get("slide_points") or [])]
    return " ".join(parts)


def _scene_text_elements(seg: dict) -> list[dict]:
    scene = seg.get("scene") if isinstance(seg.get("scene"), dict) else {}
    return [e for e in (scene.get("elements") or []) if isinstance(e, dict) and e.get("type") == "text"]


def _scene_protected(seg: dict) -> list[str]:
    """Expressions the renderer typesets (MathElement.expr): opaque to the
    translator, verbatim on both sides."""
    scene = seg.get("scene") if isinstance(seg.get("scene"), dict) else {}
    return [str(e.get("expr") or "") for e in (scene.get("elements") or [])
            if isinstance(e, dict) and e.get("type") == "math" and e.get("expr")]


def _compiled(seg: dict) -> bool:
    """Whether the segment's scene is the maths compiler's (maths/board.py
    stamps ``scene.compiled``), not a translated or model-written one."""
    scene = seg.get("scene") if isinstance(seg.get("scene"), dict) else {}
    return bool(scene.get("compiled"))


def _blob(segs: Iterable[dict]) -> str:
    return " ".join(_segment_text(s) + " " + " ".join(str(e.get("text") or "") for e in _scene_text_elements(s))
                    for s in segs)


def check_script(source: dict, target: dict, *, language: str,
                 glossary: Optional[list[tuple[str, str]]] = None,
                 protected: Optional[dict[str, list[str]]] = None,
                 strict: bool = True, board: Iterable[str] = ()) -> dict:
    """Pre-check A over two EpisodeScript dicts. ``glossary`` is
    ``[(term_en, term)]``; ``protected`` maps a segment id to expressions
    that must survive verbatim (the maths path adds the lesson's).

    ``strict`` pairs the segments by id and compares each pair. The maths
    path turns it OFF: its board is RECOMPILED in the language from the
    verified record, so a long line may split into one segment more or
    fewer — the record itself was checked field by field
    (check_maths_lesson), and here only the whole script's numbers, codes,
    leftovers and glossary use are compared."""
    blocking: list[dict] = []
    flags: list[dict] = []
    s_segs = [s for s in (source.get("segments") or []) if isinstance(s, dict)]
    t_segs = [s for s in (target.get("segments") or []) if isinstance(s, dict)]
    s_ids = [str(s.get("segment_id") or "") for s in s_segs]
    t_ids = [str(s.get("segment_id") or "") for s in t_segs]
    glossary = [(a, b) for a, b in (glossary or []) if a and b]
    protected = protected or {}

    if not strict:
        if abs(len(s_segs) - len(t_segs)) > max(2, len(s_segs) // 4):
            blocking.append(_finding("segments", "segment count differs too much",
                                     source=len(s_segs), target=len(t_segs)))
        src_blob, dst_blob = _blob(s_segs), _blob(t_segs)
        # The caller's protected expressions (the record's own problems and
        # answers) must survive; the ENGLISH board's typeset expressions are
        # another matter. The target board was recompiled from the verified
        # record — already checked field by field by check_maths_lesson — by
        # today's compiler, and a compiler that has moved on since the English
        # render typesets the same maths a little differently. So a typeset
        # expression the Arabic board no longer shows verbatim is a FLAG for
        # the reviewer, never a block (the Circle pilot, 2026-10-16: eight
        # such findings failed a translation whose record was intact).
        own = [p for v in protected.values() for p in v]
        dst_scene = " ".join(p2 for s in t_segs for p2 in _scene_protected(s))
        for p in own:
            if p and p not in dst_blob and p not in dst_scene:
                blocking.append(_finding("script", "protected expression altered", expression=p))
        typeset_lost = [p for s in s_segs for p in _scene_protected(s) if p and p not in dst_blob and p not in dst_scene]
        if typeset_lost:
            flags.append(_finding("script", "board expressions typeset differently from the English render",
                                  count=len(typeset_lost), examples=typeset_lost[:6]))
        prot = own + [p for s in s_segs for p in _scene_protected(s)]
        # NUMBERS: the SPOKEN lines are the translator's and are held strictly
        # (what the voice says must be what the English voice said). The
        # BOARD's text elements are the compiler's — recompiled in the
        # language from the verified record, which check_maths_lesson has
        # already compared field by field — and a compiler that has moved on
        # since the English render writes values the English board never
        # showed (Quadratic kit, 2026-10-17: today's chart labels the
        # parabola's vertex "(7/2, -9/4)"; the English render of 2026-09-28
        # predates that label). Such a difference is a FLAG for the reviewer,
        # with the target quoted, never a block.
        # A COMPILED segment (maths/board.py stamps scene.compiled) is the
        # compiler's through and through — its spoken text comes from the
        # record's speech fields, already checked by check_maths_lesson, and
        # today's compiler may speak a value the English compiler of the day
        # did not (the vertex label, read aloud). The translator's own words
        # are the FRAMING segments (recap, outro: _splice_framing), and those
        # are held strictly.
        src_speech = " ".join(_segment_text(s) for s in s_segs if not _compiled(s))
        dst_speech = " ".join(_segment_text(s) for s in t_segs if not _compiled(s))
        ok, missing, extra = numbers_equivalent(src_speech, dst_speech, board=board)
        if not ok:
            # the recompiled board repeats a value where the English one
            # did not (or the reverse); a value that VANISHED is the defect
            lost = [v for v in missing if v not in number_values(dst_speech)]
            invented = [v for v in extra if v not in number_values(src_speech)]
            if lost or invented:
                # QUOTE the target around each invented value: a refused
                # translation is deleted with its kit, so this report is the
                # only trace of what the model wrote.
                blocking.append(_finding("script", "numbers differ", missing=lost, extra=invented,
                                         target_samples=number_samples(dst_speech, invented)))
        b_ok, b_missing, b_extra = numbers_equivalent(src_blob, dst_blob, board=board)
        if not b_ok:
            b_lost = [v for v in b_missing if v not in number_values(dst_blob)]
            b_new = [v for v in b_extra if v not in number_values(src_blob)]
            if b_lost or b_new:
                flags.append(_finding("script", "board numbers differ from the English render", missing=b_lost,
                                      extra=b_new, target_samples=number_samples(dst_blob, b_new)))
        if code_tokens(src_blob) != code_tokens(dst_blob):
            blocking.append(_finding("script", "curriculum codes differ",
                                     source=code_tokens(src_blob), target=code_tokens(dst_blob)))
        why = english_leftover(dst_blob, language, prot)
        if why:
            flags.append(_finding("script", "still reads as English", detail=why))
        low_src, low_dst = src_blob.lower(), dst_blob.lower()
        for term_en, term in glossary:
            if re.search(r"\b" + re.escape(term_en.lower()) + r"\b", low_src) and term.lower() not in low_dst:
                flags.append(_finding("script", "glossary term not used", term_en=term_en, term=term))
        return {"check": "A", "ok": not blocking, "segments": len(s_segs), "target_segments": len(t_segs),
                "strict": False, "blocking": blocking, "flags": flags}

    if s_ids != t_ids:
        blocking.append(_finding("segments", "segment ids or order differ",
                                 source=s_ids[:40], target=t_ids[:40]))
    by_id = {str(s.get("segment_id") or ""): s for s in t_segs}
    pairs = [(s, by_id.get(str(s.get("segment_id") or ""))) for s in s_segs]

    for src, dst in pairs:
        sid = str(src.get("segment_id") or "")
        if dst is None:
            continue
        if _dialogue_shape(src) != _dialogue_shape(dst):
            blocking.append(_finding(sid, "speaker turns differ",
                                     source=_dialogue_shape(src), target=_dialogue_shape(dst)))
        if len(src.get("slide_points") or []) != len(dst.get("slide_points") or []):
            blocking.append(_finding(sid, "bullet count differs",
                                     source=len(src.get("slide_points") or []),
                                     target=len(dst.get("slide_points") or [])))
        if len(_scene_text_elements(src)) != len(_scene_text_elements(dst)):
            blocking.append(_finding(sid, "scene text elements differ in number"))
        prot = list(protected.get(sid) or []) + _scene_protected(src)
        dst_blob = _segment_text(dst) + " " + " ".join(str(e.get("text") or "") for e in _scene_text_elements(dst))
        src_blob = _segment_text(src) + " " + " ".join(str(e.get("text") or "") for e in _scene_text_elements(src))
        for p in prot:
            if p and p not in dst_blob and p not in " ".join(_scene_protected(dst)):
                blocking.append(_finding(sid, "protected expression altered", expression=p))
        ok, missing, extra = numbers_equivalent(src_blob, dst_blob)
        if not ok:
            blocking.append(_finding(sid, "numbers differ", missing=missing, extra=extra))
        if code_tokens(src_blob) != code_tokens(dst_blob):
            blocking.append(_finding(sid, "curriculum codes differ",
                                     source=code_tokens(src_blob), target=code_tokens(dst_blob)))
        why = english_leftover(dst_blob, language, prot)
        if why:
            flags.append(_finding(sid, "still reads as English", detail=why))
        low_src, low_dst = src_blob.lower(), dst_blob.lower()
        for term_en, term in glossary:
            if re.search(r"\b" + re.escape(term_en.lower()) + r"\b", low_src) and term.lower() not in low_dst:
                flags.append(_finding(sid, "glossary term not used", term_en=term_en, term=term))
    return {"check": "A", "ok": not blocking, "segments": len(s_segs),
            "blocking": blocking, "flags": flags}


# ── the maths lesson record ──────────────────────────────────────────────

# Fields of maths.schema.Lesson that are MATHS, not words: copied verbatim
# by the translator, verified identical here.
LESSON_INVARIANT_KEYS = ("givens", "target", "before", "after", "final_answer",
                         "theorem", "uses", "figure_ops", "figure", "chart",
                         "from_state", "wrong_state", "kind", "task", "difficulty", "precision",
                         "answer")
# Fields that are PROSE WITH MATHS INSIDE — a word problem: "Find the area
# of a circle with a radius of 6 cm. (Use π = 3.14)". Translated as words,
# then every maths span must survive verbatim (prose_maths_equivalent).
# The founder's review of the first Arabic video (2026-10-16): the question
# stayed English on the board.
# A step's `operation` ("Substitute y and a", "subtract 3 from both sides")
# is the muted note the board writes beside the working (maths/board.py
# `{operation} {explanation}`): words, with the odd maths span — translated
# like a problem, its spans and numbers checked the same way. Founder,
# 2026-10-17, on the Substitution Arabic video: "can the greyed out text also
# be written in Arabic?" The verifier reads it only to label a check.
LESSON_PROSE_MATHS_KEYS = ("problem", "operation")
# A maths span: a run of numbers, single-letter variables (Latin or Greek),
# operators, powers and brackets — "π = 3.14", "2x + 3 = 11", "r = 6" —
# that carries at least one digit or operator. Unit words ("cm") are two
# or more letters, so they end a span and are free to be translated.
_LETTER = r"A-Za-zͰ-Ͽ"
_SPAN_TOKEN = (rf"(?:\d+(?:[.,]\d+)?|(?<![{_LETTER}])[A-Za-zα-ωΑ-Ω](?![{_LETTER}])"
               r"|[=+−\-×÷*/^√<>≤≥≠]|[²³]|\(|\))")
_SPAN_RE = re.compile(rf"{_SPAN_TOKEN}(?:\s*{_SPAN_TOKEN})*")
_SPAN_CORE_RE = re.compile(r"\d|[=+−\-×÷*/^√<>≤≥≠]")
# A hyphen between two LETTERS joins a word ("circle-related", "x-axis",
# "well-known") and is no operator; the detector reads it as a plain hyphen
# (U+2010), which the operator class does not hold. "x - 2" and "x-2" keep
# their minus. (The second Circle re-run, 2026-10-16: the match item's stem
# "Match each circle-related term…" was refused twice for a lost "-".)
# The same for a NUMBER joined to a word of three or more letters ("the
# 90-degree angle", "a 3-step problem", "the 38-degree angle"): an adjective,
# not "90 minus degree". The translation writes the number and the unit word
# its own way ("sudut 90 darjah"), so reading "90-" as maths refused every
# trigonometry bank item that named an angle — five of the Trigonometric
# Ratios kit's items in Arabic AND in Malay (2026-10-19). A maths function
# after the hyphen keeps the minus ("1-cos x", "2-sin θ", "3-log x").
_WORD_HYPHEN_RE = re.compile(
    r"(?<=[^\W\d_])-(?=[^\W\d_])"
    r"|(?<=\d)-(?!(?:sin|cos|tan|sec|csc|cot|log|ln|exp|sqrt|arc)[^\W\d_]*\b)(?=[^\W\d_]{3,})",
    re.UNICODE)
_PROSE_WORD_RE = re.compile(r"[^\W\d_]{2,}", re.UNICODE)


def has_prose(text: str) -> bool:
    """Whether a question carries WORDS to translate — two or more words of
    two or more letters. ``4x + 3 = 19`` has none and stays as it is (the
    board typesets it as an equation); ``Solve 2x + 3 = 11 for x.`` does."""
    return len(_PROSE_WORD_RE.findall(str(text or ""))) >= 2


def _bracket_depths(span: str) -> tuple[list[int], int]:
    """The index of each ``(`` with no ``)`` after it, and how many ``)``
    close nothing."""
    open_at: list[int] = []
    unopened = 0
    for i, ch in enumerate(span):
        if ch == "(":
            open_at.append(i)
        elif ch == ")":
            if open_at:
                open_at.pop()
            else:
                unopened += 1
    return open_at, unopened


def _wrapped(span: str) -> bool:
    """Whether the span's first ``(`` is closed by its last ``)`` — the whole
    span in one pair, as in ``(r = 6)``; ``(a + b)(a - b)`` is not."""
    if not (span.startswith("(") and span.endswith(")")):
        return False
    depth = 0
    for i, ch in enumerate(span):
        depth += 1 if ch == "(" else -1 if ch == ")" else 0
        if depth == 0:
            return i == len(span) - 1
    return False


def _trim_brackets(span: str) -> str:
    """A span with the brackets that belong to the SENTENCE taken off its
    ends — a ``(`` or ``)`` that pairs with nothing inside the span ("(Use
    π = 3.14)" gives "π = 3.14)"), or one pair round the whole span — and
    every bracket that belongs to the MATHS kept: "2(x + 3)", "4x^2 - 3(y - 1)".
    A bracket in the middle is never trimmed, so a translation that drops
    one ("3(y - 1 apabila") keeps a span that no longer balances."""
    while True:
        span = span.strip()
        if _wrapped(span):
            span = span[1:-1]
            continue
        open_at, unopened = _bracket_depths(span)
        if span.startswith(")") and unopened:
            span = span[1:]
        elif span.endswith("(") and open_at and open_at[-1] == len(span) - 1:
            span = span[:-1]
        elif span.endswith(")") and unopened and _bracket_depths(span[:-1])[1] < unopened:
            span = span[:-1]
        elif span.startswith("(") and open_at and open_at[0] == 0:
            span = span[1:]
        else:
            return span


def balanced(span: str) -> bool:
    """Whether every bracket of a maths span pairs with another."""
    open_at, unopened = _bracket_depths(span)
    return not open_at and not unopened


def maths_spans(text: str) -> list[str]:
    """The maths spans of a sentence, whitespace-normalised, in order, with
    the sentence's brackets trimmed off and the maths' own kept."""
    out: list[str] = []
    for m in _SPAN_RE.finditer(_WORD_HYPHEN_RE.sub("‐", normalize_numerals(str(text or "")))):
        span = _trim_brackets(" ".join(m.group(0).split()))
        # maths carries a digit, or a variable WITH an operator — a bare
        # operator between words ("A - B" in prose, a stray dash) is not
        if span and _SPAN_CORE_RE.search(span) and (re.search(r"\d", span) or re.search(r"[A-Za-zα-ωΑ-Ω]", span)):
            out.append(span)
    return out


# A decimal COMMA between digits ("273,15", "3,14") is the French, Spanish
# and Portuguese way to write "273.15" — number_values already reads it so;
# the span comparison did not, and refused a Heat Transfer bank item in
# French whose stem said "subtracting 273.15" (2026-10-19). A comma that
# groups thousands ("41,120") is left alone, as number_values leaves it.
_DECIMAL_COMMA_RE = re.compile(r"(?<=\d),(?=\d)(?!\d{3}(?!\d))")


def _decimal_points(text: str) -> str:
    """``text`` with every decimal comma between digits written as a point."""
    return _DECIMAL_COMMA_RE.sub(".", text)


def _bracket_count(spans: Iterable[str]) -> int:
    return sum(s.count("(") + s.count(")") for s in spans)


def prose_maths_equivalent(source: str, target: str) -> tuple[bool, list[str]]:
    """Every maths span of the source appears in the target (whitespace,
    numerals and decimal commas normalised), every bracket the source's maths carries is still
    there and still pairs, and the numbers are equivalent. Returns (ok, the
    spans the target lost — and its spans whose brackets no longer pair).

    The Malay Substitution kit (2026-10-19) wrote "Nilaikan 4x^2 - 3(y - 1
    apabila x = -2": the ")" was gone, and the check, which stripped every
    bracket off every span, passed it."""
    tgt = _decimal_points(" ".join(normalize_numerals(str(target or "")).split()))
    tgt_compact = tgt.replace(" ", "")
    # the target's spans read from the same decimal-normalised text the
    # lost-span test reads
    src_spans, tgt_spans = maths_spans(source), maths_spans(tgt)
    lost = [s for s in src_spans if s not in tgt and s.replace(" ", "") not in tgt_compact]
    # a span the source writes unbalanced (a typo in the English) is no
    # fault of the translation
    src_unbalanced = {s.replace(" ", "") for s in src_spans if not balanced(s)}
    unbalanced = [f"unbalanced brackets {s}" for s in tgt_spans
                  if not balanced(s) and s.replace(" ", "") not in src_unbalanced]
    if not unbalanced and not src_unbalanced and _bracket_count(tgt_spans) != _bracket_count(src_spans):
        unbalanced.append(f"brackets {_bracket_count(src_spans)} → {_bracket_count(tgt_spans)}")
    ok_numbers, missing, extra = numbers_equivalent(source, target)
    return (not lost and not unbalanced and ok_numbers), lost + unbalanced + [f"number {m}" for m in missing]
# Fields that are SPOKEN or WRITTEN words: translated, then checked for
# numeric equivalence.
LESSON_TEXT_KEYS = ("line", "speech", "student", "explanation", "intro_speech", "student_question",
                    "reply", "answer_speech", "solution_speech", "why_wrong", "closing", "topic",
                    "title", "steps_text", "concept_points", "misconceptions", "label")


def _walk(obj, path: str = ""):
    """(path, value) for every leaf; lists index as ``[i]``."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}[{i}]")
    else:
        yield path, obj


def _leaf_key(path: str) -> str:
    tail = path.rsplit(".", 1)[-1]
    return re.sub(r"\[\d+\]$", "", tail)


def check_maths_lesson(source: dict, target: dict) -> dict:
    """The translated Lesson record against its source: identical structure,
    identical maths, equivalent numbers in every spoken line."""
    blocking: list[dict] = []
    flags: list[dict] = []
    s = dict(_walk(source))
    t = dict(_walk(target))
    board = board_numbers(source)
    if set(s) != set(t):
        only_s = sorted(set(s) - set(t))[:20]
        only_t = sorted(set(t) - set(s))[:20]
        blocking.append(_finding("lesson", "structure differs", source_only=only_s, target_only=only_t))
    for path, sv in s.items():
        if path not in t:
            continue
        tv = t[path]
        key = _leaf_key(path)
        if key in LESSON_INVARIANT_KEYS or key == "who":
            if sv != tv:
                blocking.append(_finding(path, "protected field altered", source=sv, target=tv))
            continue
        if key in LESSON_PROSE_MATHS_KEYS and isinstance(sv, str) and isinstance(tv, str):
            ok, lost = prose_maths_equivalent(sv, tv)
            if not ok:
                blocking.append(_finding(path, "maths in the question altered", lost=lost, source=sv, target=tv))
            elif sv.strip() and not tv.strip():
                blocking.append(_finding(path, "question emptied"))
            continue
        if isinstance(sv, str) and isinstance(tv, str):
            ok, missing, extra = numbers_equivalent(sv, tv, board=board)
            if not ok:
                blocking.append(_finding(path, "numbers differ", missing=missing, extra=extra))
            if sv.strip() and not tv.strip():
                blocking.append(_finding(path, "spoken text emptied"))
    return {"check": "A-maths", "ok": not blocking, "blocking": blocking, "flags": flags}


# ── question-bank items (D4) ─────────────────────────────────────────────


def _opt_list(options) -> list[dict]:
    return [o for o in (options if isinstance(options, list) else []) if isinstance(o, dict)]


def check_item(source: dict, target: dict) -> dict:
    """A translated bank item against its English source. The maths of an
    item is its SHAPE — type, mode, marks, option keys, the answer key, the
    label and pair counts — and must be identical; its words must carry the
    same numbers; and the translated options must still be distinct (two
    English distractors can collapse into one word in another language,
    which leaves a multiple-choice question with no single answer).
    Blocking findings mean the item is not staged."""
    blocking: list[dict] = []
    flags: list[dict] = []
    sid = str(source.get("id") or "item")
    for key in ("item_type", "answer_mode", "marks", "difficulty", "cognitive_level", "objective_ref"):
        if source.get(key) != target.get(key):
            blocking.append(_finding(sid, f"{key} changed", source=source.get(key), target=target.get(key)))
    s_opts, t_opts = _opt_list(source.get("options")), _opt_list(target.get("options"))
    if [o.get("key") for o in s_opts] != [o.get("key") for o in t_opts]:
        blocking.append(_finding(sid, "option keys differ"))
    texts = [" ".join(str(o.get("text") or "").split()).casefold() for o in t_opts]
    if len(texts) != len(set(t for t in texts if t)) and any(texts):
        blocking.append(_finding(sid, "translated options are no longer distinct", options=texts))
    s_ans, t_ans = source.get("answer") or {}, target.get("answer") or {}
    if isinstance(s_ans, dict) and isinstance(t_ans, dict):
        if s_ans.get("key") != t_ans.get("key"):
            blocking.append(_finding(sid, "answer key changed", source=s_ans.get("key"), target=t_ans.get("key")))
        if s_ans.get("value") is not None and s_ans.get("value") != t_ans.get("value"):
            blocking.append(_finding(sid, "numerical answer changed", source=s_ans.get("value"), target=t_ans.get("value")))
        for list_key in ("labels", "pairs"):
            if len(s_ans.get(list_key) or []) != len(t_ans.get(list_key) or []):
                blocking.append(_finding(sid, f"{list_key} count differs"))
    if len(source.get("marking_scheme") or []) != len(target.get("marking_scheme") or []):
        blocking.append(_finding(sid, "marking scheme length differs"))
    src_text = " ".join([str(source.get("stem") or "")] + [str(o.get("text") or "") for o in s_opts]
                        + [str(pt.get("point") or "") for pt in (source.get("marking_scheme") or []) if isinstance(pt, dict)])
    dst_text = " ".join([str(target.get("stem") or "")] + [str(o.get("text") or "") for o in t_opts]
                        + [str(pt.get("point") or "") for pt in (target.get("marking_scheme") or []) if isinstance(pt, dict)])
    ok, missing, extra = numbers_equivalent(src_text, dst_text)
    if not ok:
        blocking.append(_finding(sid, "numbers differ", missing=missing, extra=extra))
    ok_m, lost = prose_maths_equivalent(str(source.get("stem") or ""), str(target.get("stem") or ""))
    if not ok_m:
        blocking.append(_finding(sid, "maths in the stem altered", lost=lost))
    if not str(target.get("stem") or "").strip():
        blocking.append(_finding(sid, "stem emptied"))
    return {"check": "A-item", "ok": not blocking, "blocking": blocking, "flags": flags}


# ── a worksheet's questions (T1.5) ───────────────────────────────────────
# The maths worksheet's ``questions_json`` (docgen.questions): the sheet
# rendered again in the language from its own verified questions, words
# translated, every number and expression kept.


def is_maths(text: str) -> bool:
    """Whether a string is NOTATION rather than words — ``x = 5``, ``9π cm``,
    ``3x + 5 = 20``, ``7``: once its maths spans are taken out no word of two
    or more letters remains. Such a string is copied, never translated. A
    unit word (``cm``) alone does not make a string prose: ``9π cm`` is maths."""
    t = str(text or "").strip()
    if not t:
        return True
    from docgen.questions import numeric_forms
    if numeric_forms(t) is not None:
        return True
    rest = _SPAN_RE.sub(" ", _WORD_HYPHEN_RE.sub("‐", normalize_numerals(t)))
    return not _PROSE_WORD_RE.search(rest)


QUESTION_INVARIANT_KEYS = ("id", "type", "marks", "image")


def _q_strings(q: dict) -> list[str]:
    out = [str(q.get("prompt") or "")]
    if isinstance(q.get("answer"), str):
        out.append(q["answer"])
    out += [str(x) for x in (q.get("accept") or []) if isinstance(q.get("accept"), list)]
    out += [str(o) for o in (q.get("options") or []) if isinstance(q.get("options"), list)]
    out += [f"{p.get('left')} {p.get('right')}" for p in (q.get("pairs") or []) if isinstance(p, dict)]
    out += [str(w) for w in (q.get("working") or []) if isinstance(q.get("working"), list)]
    return out


def check_question(source: dict, target: dict) -> dict:
    """A translated worksheet question against its English source. The id,
    type, marks and picture are identical; a true/false answer is the same
    truth; the option, pair and working-line COUNTS match; every number
    across the question (prompt, answers, options, pairs, working) is
    equivalent; every maths span of the prompt and of each working line
    survives verbatim; the options stay distinct. Blocking findings drop the
    question from the translated sheet."""
    blocking: list[dict] = []
    qid = str(source.get("id") or "question")
    for key in QUESTION_INVARIANT_KEYS:
        if source.get(key) != target.get(key):
            blocking.append(_finding(qid, f"{key} changed"))
    if source.get("type") == "true_false" and bool(source.get("answer")) != bool(target.get("answer")):
        blocking.append(_finding(qid, "answer changed", source=source.get("answer"), target=target.get("answer")))
    for list_key in ("options", "pairs", "working", "accept"):
        if len(source.get(list_key) or []) != len(target.get(list_key) or []):
            blocking.append(_finding(qid, f"{list_key} count differs"))
    opts = [" ".join(str(o).split()).casefold() for o in (target.get("options") or [])]
    if opts and len(opts) != len(set(opts)):
        blocking.append(_finding(qid, "translated options are no longer distinct", options=opts))
    ok, missing, extra = numbers_equivalent(" ".join(_q_strings(source)), " ".join(_q_strings(target)))
    if not ok:
        blocking.append(_finding(qid, "numbers differ", missing=missing, extra=extra))
    ok_m, lost = prose_maths_equivalent(str(source.get("prompt") or ""), str(target.get("prompt") or ""))
    if not ok_m:
        blocking.append(_finding(qid, "maths in the question altered", lost=lost))
    for i, (sw, tw) in enumerate(zip(source.get("working") or [], target.get("working") or [])):
        ok_w, lost_w = prose_maths_equivalent(str(sw), str(tw))
        if not ok_w:
            blocking.append(_finding(f"{qid} working[{i}]", "maths in the working altered", lost=lost_w))
    if not str(target.get("prompt") or "").strip():
        blocking.append(_finding(qid, "question emptied"))
    return {"check": "A-question", "ok": not blocking, "blocking": blocking, "flags": []}


# ── pre-check B: meaning, by back-translation ────────────────────────────

_CONTENT_WORD_RE = re.compile(r"[^\W\d_]{3,}", re.UNICODE)
_STOP = frozenset("the and of to is that with for are was this from have which their there what when "
                  "how you we our its into than then them they will can let now here also each".split())
FIDELITY_FLAG = 0.30


def content_overlap(a: str, b: str) -> float:
    """Share of the source's content words that the back-translation keeps
    (case-folded, stop words out). A rough fidelity score: 1.0 is the same
    sentence, 0.3 and below is a different one."""
    wa = {w.casefold() for w in _CONTENT_WORD_RE.findall(str(a or ""))} - _STOP
    wb = {w.casefold() for w in _CONTENT_WORD_RE.findall(str(b or ""))} - _STOP
    if not wa:
        return 1.0
    return len(wa & wb) / len(wa)


def fidelity_report(samples: list[tuple[str, str, str]]) -> dict:
    """``samples`` = ``[(where, source, back_translation)]``. Flags the ones
    whose back-translation keeps too little of the source; never blocks
    (decision D3 — meaning is the reviewer's to judge)."""
    flags: list[dict] = []
    scores: list[float] = []
    for where, src, back in samples:
        score = content_overlap(src, back)
        scores.append(score)
        if score < FIDELITY_FLAG:
            flags.append(_finding(where, "back-translation keeps little of the source",
                                  score=round(score, 2), source=src[:200], back=back[:200]))
    return {"check": "B", "ok": True, "samples": len(samples),
            "mean_score": round(sum(scores) / len(scores), 2) if scores else None,
            "blocking": [], "flags": flags}


# ── pre-check C: after the render ────────────────────────────────────────

DRIFT_FLAG_S = 4.0
LONG_SILENCE_FLAG_S = 6.0


def post_render(script: dict, video: dict, *, overlaps: Optional[list] = None) -> dict:
    """The measured manifest against the script: a clip that runs more than
    DRIFT_FLAG_S past (or short of) its narration, a segment with no audio,
    and whatever text overlaps the acceptance report named."""
    blocking: list[dict] = []
    flags: list[dict] = []
    segs = {str(s.get("segment_id") or ""): s for s in (video.get("segments") or []) if isinstance(s, dict)}
    for sseg in (script.get("segments") or []):
        if not isinstance(sseg, dict):
            continue
        sid = str(sseg.get("segment_id") or "")
        v = segs.get(sid)
        if v is None:
            blocking.append(_finding(sid, "segment never rendered"))
            continue
        audio = float(v.get("audio_duration_seconds") or 0.0)
        clip = float(v.get("clip_duration_seconds") or 0.0)
        if audio <= 0.0 and str(sseg.get("text") or "").strip():
            blocking.append(_finding(sid, "no audio for a spoken segment"))
        if clip > 0.0 and audio > 0.0:
            drift = clip - audio
            if abs(drift) > DRIFT_FLAG_S:
                flags.append(_finding(sid, "clip and narration drift", seconds=round(drift, 1)))
            if drift > LONG_SILENCE_FLAG_S:
                flags.append(_finding(sid, "long silence after the narration", seconds=round(drift, 1)))
    for o in overlaps or []:
        flags.append(_finding(str((o or {}).get("segment_id") or "scene") if isinstance(o, dict) else "scene",
                              "text overlap reported by acceptance", detail=o))
    return {"check": "C", "ok": not blocking, "blocking": blocking, "flags": flags}


def merge_reports(*reports: dict) -> dict:
    """One ``precheck`` value for topic_kits: the parts, their union of
    findings, and the verdict."""
    parts = [r for r in reports if isinstance(r, dict)]
    blocking = [f for r in parts for f in (r.get("blocking") or [])]
    flags = [f for r in parts for f in (r.get("flags") or [])]
    return {"ok": not blocking, "blocking": blocking, "flags": flags,
            "checks": [{k: v for k, v in r.items() if k not in ("blocking", "flags")} for r in parts]}


__all__ = ["DRIFT_FLAG_S", "LESSON_INVARIANT_KEYS", "LESSON_PROSE_MATHS_KEYS", "LESSON_TEXT_KEYS",
           "check_item", "check_maths_lesson", "check_script", "code_tokens", "content_overlap", "english_leftover",
           "fidelity_report", "has_prose", "maths_spans", "merge_reports",
           "normalize_numerals", "number_values", "numbers_equivalent", "post_render", "prose_maths_equivalent",
           "script_of"]
