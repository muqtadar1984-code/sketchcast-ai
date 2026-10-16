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

# A coefficient counts too ("2r", "3x"): only another digit or a point may
# touch a number, letters may follow it.
_NUMBER_RE = re.compile(r"(?<![\d.])-?\d[\d,]*(?:\.\d+)?(?![\d.])")
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


def numbers_equivalent(source: str, target: str) -> tuple[bool, list[str], list[str]]:
    """Same multiset of numeric VALUES on both sides. Returns (ok, missing,
    extra) — what the target lost and what it invented."""
    a, b = number_values(source), number_values(target)
    missing = list(a)
    extra: list[str] = []
    for v in b:
        if v in missing:
            missing.remove(v)
        else:
            extra.append(v)
    return (not missing and not extra), missing, extra


# ── scripts of the lesson languages ─────────────────────────────────────

_SCRIPT_RE = {
    "arab": re.compile(r"[؀-ۿݐ-ݿ]"),
    "deva": re.compile(r"[ऀ-ॿ]"),
    "telu": re.compile(r"[ఀ-౿]"),
}
_LATIN_RE = re.compile(r"[A-Za-z]")
_LANGUAGE_SCRIPT = {"ar": "arab", "ur": "arab", "ms-arab": "arab", "hi": "deva", "mr": "deva", "te": "telu"}
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


def _blob(segs: Iterable[dict]) -> str:
    return " ".join(_segment_text(s) + " " + " ".join(str(e.get("text") or "") for e in _scene_text_elements(s))
                    for s in segs)


def check_script(source: dict, target: dict, *, language: str,
                 glossary: Optional[list[tuple[str, str]]] = None,
                 protected: Optional[dict[str, list[str]]] = None,
                 strict: bool = True) -> dict:
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
        prot = [p for v in protected.values() for p in v] + [p for s in s_segs for p in _scene_protected(s)]
        for p in prot:
            if p and p not in dst_blob and p not in " ".join(p2 for s in t_segs for p2 in _scene_protected(s)):
                blocking.append(_finding("script", "protected expression altered", expression=p))
        ok, missing, extra = numbers_equivalent(src_blob, dst_blob)
        if not ok:
            # the recompiled board repeats a value where the English one
            # did not (or the reverse); a value that VANISHED is the defect
            lost = [v for v in missing if v not in number_values(dst_blob)]
            invented = [v for v in extra if v not in number_values(src_blob)]
            if lost or invented:
                blocking.append(_finding("script", "numbers differ", missing=lost, extra=invented))
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
LESSON_INVARIANT_KEYS = ("problem", "givens", "target", "before", "after", "final_answer",
                         "operation", "theorem", "uses", "figure_ops", "figure", "chart",
                         "from_state", "wrong_state", "kind", "task", "difficulty", "precision",
                         "answer")
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
        if isinstance(sv, str) and isinstance(tv, str):
            ok, missing, extra = numbers_equivalent(sv, tv)
            if not ok:
                blocking.append(_finding(path, "numbers differ", missing=missing, extra=extra))
            if sv.strip() and not tv.strip():
                blocking.append(_finding(path, "spoken text emptied"))
    return {"check": "A-maths", "ok": not blocking, "blocking": blocking, "flags": flags}


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


__all__ = ["DRIFT_FLAG_S", "LESSON_INVARIANT_KEYS", "LESSON_TEXT_KEYS", "check_maths_lesson", "check_script",
           "code_tokens", "english_leftover", "merge_reports", "normalize_numerals", "number_values",
           "numbers_equivalent", "post_render", "script_of"]
