"""Jawi (``ms-arab``) as a Kit Translator target — derived from the topic's
APPROVED MALAY kit, not from English (plan §5.11, 2026-10-19).

Jawi is Malay written in the Arabic-derived script. The Malay voices (g-ms-f
WaveNet, edge-yasmin) read Rumi only, so a plain translation into ms-arab —
narration in Jawi handed to the Malay voice — cannot be spoken. A Jawi kit
is therefore TWO scripts of the same Malay words:

  SPOKEN   ``text``, ``elevenlabs_text`` and every ``dialogue[i].line`` stay
           the Malay kit's Rumi, word for word — the voice says exactly what
           the approved Malay video says;
  WRITTEN  every on-screen field — slide heading and points, slide visual,
           the scene's text elements, the maths board — is the same Malay in
           Jawi, and the speech bubbles show it through two display fields:
           ``display_text`` on the segment and ``display`` on each dialogue
           line (agent6_animation.video_composer renders the bubbles from
           them while the TTS reads ``text`` / ``line``).

The article, glossary, bank items, quiz, worksheet and documents are written
surfaces: transliterated (the bank, quiz, article, worksheet) or authored in
Jawi from the Jawi article (activity, case study, lesson plan, deck) exactly
as for any other language.

Lineage: the Jawi kit's ``source_kit_id`` is the Malay kit and the job's
``params.source_kit_id`` names the Malay kit too, so every place that keys a
translation on (source kit, language) — the one-live-job index, the
orphan-resume claim, ``find_translation_kit`` — works unchanged, and the
supersede cascade (catalogue.supersede) follows Malay → Jawi.

The maths board is compiled TWICE from the same verified record: once from
the Malay record in ``ms`` (what the voice says, and the cue phrases the
board's drawing is timed to) and once from the transliterated record in
``ms-arab`` (what the board shows). Segment ids come from the record's
structure, never from text length, so the two compiles pair one to one.
"""

from __future__ import annotations

import copy
import re
from typing import Optional

from shared.languages import prompt_directive

TARGET = "ms-arab"
SOURCE = "ms"

# Said to the model with every ms-arab call (translate.translate_strings):
# the input is already Malay, and only the SCRIPT changes.
RULE = (
    "The strings are ALREADY Malay (Bahasa Melayu) written in Rumi (Latin letters). Rewrite each one in the JAWI "
    "script: the SAME Malay words, in the same order — a transliteration, not a new translation; do not "
    "paraphrase, shorten, add or explain. Numbers stay as written in Western digits (0-9) — never turn a digit "
    "into a word or a number word into digits. EVERY Malay word is written in Jawi — including loanwords, "
    "borrowed scientific terms (inframerah → اينفراميره, joule → جول, rod → رود) and abbreviations "
    "(lwn → لاون). Only these stay in Latin characters, exactly as written: single variable letters (x, y), "
    "unit SYMBOLS (J, °C, m, kg, cm), chemical formulas (H2O, NaCl), equations, expressions and the "
    "expressions you are told to keep unchanged."
    + prompt_directive(TARGET)
)
# The re-ask for a string that still carries Latin words (translate.
# translate_strings' Jawi repair pass): ``{words}`` names them.
REPAIR_RULE = ("A previous Jawi version of these strings left these Malay words in Latin letters: {words}. Write "
               "EVERY Malay word in Jawi script — loanwords, scientific terms and abbreviations included; only "
               "variable letters, unit symbols, chemical formulas and the expressions to keep unchanged stay in "
               "Latin.")

# Latin words of three letters or more that are NOT Malay words left in
# Rumi: maths function names and unit symbols, which a Jawi page writes in
# Latin like any other (lower-cased for the comparison).
LATIN_KEEP = frozenset(
    "sin cos tan sec csc cot arcsin arccos arctan asin acos atan sinh cosh tanh log ln exp lim max min mod gcd lcm "
    "soh cah toa "
    "mol kpa mpa hpa kwh mwh khz mhz ghz mah kcal cal rpm".split())
_LATIN_WORD_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{3,}(?![A-Za-z])")
# H2O, NaCl, HCl, NaOH, CO2: element symbols with a digit or a two-letter
# element in them — a formula, not a word ("Rod" and "DNA" are words here)
_FORMULA_RE = re.compile(r"^(?:[A-Z][a-z]?\d*)+$")


def _formula(word: str) -> bool:
    return bool(_FORMULA_RE.match(word)) and (any(c.isdigit() for c in word)
                                              or bool(re.search(r"[A-Z][a-z]", word)) and sum(c.isupper() for c in word) >= 2)


def latin_leftovers(text: str, protected=()) -> list[str]:
    """The Latin words of three letters or more that a Jawi string still
    carries — a Malay word the model left in Rumi — once the protected
    expressions are taken out; maths function names, unit symbols and
    chemical formulas are not leftovers. In order, each once."""
    body = str(text or "")
    for p in sorted((str(x) for x in protected or () if str(x or "").strip()), key=len, reverse=True):
        body = body.replace(p, " ")
    out: list[str] = []
    for m in _LATIN_WORD_RE.finditer(body):
        w = m.group(0)
        if w.lower() in LATIN_KEEP or _formula(w):
            continue
        # a figure's points ("triangle ABC", "PQR", "XYZ") — capitals in
        # alphabet order — stay Latin in a Jawi textbook too (first Jawi
        # Trig kit, 2026-10-19); an acronym ("DNA") is still a word
        if w.isupper() and len(w) <= 4 and all(ord(b) - ord(a) == 1 for a, b in zip(w, w[1:])):
            continue
        # a formula with digits ("CO2") reaches here as its letters only
        tail = body[m.end():m.end() + 1]
        if tail.isdigit() and _FORMULA_RE.match(w):
            continue
        if w not in out:
            out.append(w)
    return out
BACK_RULE = ("These are Malay sentences written in the JAWI script. Write each in RUMI (Latin) Malay, word for word, "
             "as literally as you can; do not translate it into English, improve or shorten it.")

_ARABIC_LETTER_RE = re.compile(r"[؀-ۿݐ-ݿ]")
_LATIN_LETTER_RE = re.compile(r"[A-Za-z]")


def is_derived(language: Optional[str]) -> bool:
    """Whether ``language`` is translated from another TRANSLATION (Jawi from
    Malay) rather than from the English original."""
    return str(language or "").strip().lower() == TARGET


def source_language_for(language: Optional[str]) -> str:
    """The language a kit in ``language`` is translated FROM: Malay for Jawi,
    English for everything else."""
    return SOURCE if is_derived(language) else "en"


def context_for(language: Optional[str], context: str = "") -> str:
    """``context`` with the transliteration rule in front for a Jawi call."""
    if not is_derived(language):
        return context
    return (RULE + ("\n" + context if context else "")).strip()


# ── the two scripts of one segment ─────────────────────────────────────


def written_items(segment: dict) -> list[tuple[str, str]]:
    """A segment's ON-SCREEN words, by path: the heading, the bullets, the
    slide visual's words and the scene's text elements. Transliterated in
    place."""
    items: list[tuple[str, str]] = []
    if str(segment.get("slide_heading") or "").strip():
        items.append(("slide_heading", str(segment["slide_heading"])))
    for i, p in enumerate(segment.get("slide_points") or []):
        if str(p or "").strip():
            items.append((f"slide_points[{i}]", str(p)))
    sv = segment.get("slide_visual")
    if isinstance(sv, dict):
        for k in ("heading", "caption", "title"):
            if str(sv.get(k) or "").strip():
                items.append((f"slide_visual.{k}", str(sv[k])))
        for i, it in enumerate(sv.get("items") or []):
            if isinstance(it, str) and it.strip():
                items.append((f"slide_visual.items[{i}]", it))
            elif isinstance(it, dict):
                for k in ("text", "label", "caption"):
                    if str(it.get(k) or "").strip():
                        items.append((f"slide_visual.items[{i}].{k}", str(it[k])))
    scene = segment.get("scene")
    if isinstance(scene, dict):
        for i, e in enumerate(scene.get("elements") or []):
            if isinstance(e, dict) and e.get("type") == "text" and str(e.get("text") or "").strip():
                items.append((f"scene.elements[{i}].text", str(e["text"])))
    return items


def spoken_items(segment: dict) -> list[tuple[str, str]]:
    """A segment's SPOKEN words, keyed by the DISPLAY field they become:
    ``display_text`` from ``text`` and ``dialogue[i].display`` from each
    line. The spoken fields themselves are never rewritten. A segment with
    dialogue sends its lines only: its ``display_text`` is their
    concatenation (finish_segment), as ``text`` is the lines'."""
    items: list[tuple[str, str]] = []
    for i, d in enumerate(segment.get("dialogue") or []):
        if isinstance(d, dict) and str(d.get("line") or "").strip():
            items.append((f"dialogue[{i}].display", str(d["line"])))
    if not items and str(segment.get("text") or "").strip():
        items.append(("display_text", str(segment["text"])))
    return items


def finish_segment(seg: dict, scene_text_max: int) -> dict:
    """After the Jawi is written back: scene text keeps the schema's length;
    the spoken fields, the timing and the turns are the Malay kit's."""
    scene = seg.get("scene")
    if isinstance(scene, dict):
        for e in scene.get("elements") or []:
            if isinstance(e, dict) and e.get("type") == "text" and isinstance(e.get("text"), str):
                e["text"] = e["text"].strip()[:scene_text_max]
    dialogue = [d for d in (seg.get("dialogue") or []) if isinstance(d, dict) and str(d.get("line") or "").strip()]
    if dialogue and all(str(d.get("display") or "").strip() for d in dialogue):
        # the bubbles' copy of the narration IS the lines' display copies —
        # as ``text`` is the lines' concatenation
        seg["display_text"] = " ".join(str(d.get("display") or "").strip() for d in dialogue
                                       if str(d.get("display") or "").strip())
    return seg


# ── the maths board, compiled twice ─────────────────────────────────────


def _phrase(at) -> bool:
    return isinstance(at, dict) and bool(at.get("phrase"))


def _keyed(actions: list) -> list[tuple[tuple[str, str, int], dict]]:
    """Each action keyed by (verb, target, how many times that pair came
    before) — the same drawing step in two compiles of one board."""
    seen: dict[tuple[str, str], int] = {}
    out = []
    for a in actions or []:
        if not isinstance(a, dict):
            continue
        vt = (str(a.get("verb") or ""), str(a.get("target") or ""))
        out.append(((*vt, seen.get(vt, 0)), a))
        seen[vt] = seen.get(vt, 0) + 1
    return out


def remap_cues(jawi_scene: dict, rumi_scene: dict) -> tuple[int, bool]:
    """Point the Jawi board's spoken cues at the RUMI narration. The board's
    drawing is cued to the opening words of a spoken line (maths.board._cue);
    the Jawi compile cued it to the Jawi words, which the Malay voice's word
    timings never contain. Each Jawi action takes the cue of the SAME step of
    the Rumi compile — the same verb on the same element, counted in order
    — so it fires exactly when it does in the Malay video. The two boards
    can differ by a few steps (the Rumi one may move the method card's
    highlight, which maths.board.method_step_for finds by Latin word stems
    and the Jawi one does not); a Jawi phrase cue with no Rumi counterpart
    stays, and the renderer finds it by proportion in the displayed (Jawi)
    narration (spike.scene_engine.timing). Returns ``(cues remapped, whether
    every phrase cue found its counterpart)``."""
    rumi = dict(_keyed(rumi_scene.get("actions") or []))
    n, lost = 0, 0
    for key, ja in _keyed(jawi_scene.get("actions") or []):
        ra = rumi.get(key)
        if ra is None:
            lost += int(_phrase(ja.get("at")))
            continue
        if not (_phrase(ja.get("at")) or _phrase(ra.get("at"))):
            continue
        if "at" in ra:
            ja["at"] = copy.deepcopy(ra["at"])
        else:
            ja.pop("at", None)
        n += 1
    return n, lost == 0


class Unpaired(ValueError):
    """The two compiles of one lesson do not pair (segment ids or speaker
    turns differ): the Jawi board cannot be given the Malay voice."""


def merge_compiles(rumi: dict, jawi: dict) -> tuple[dict, dict]:
    """One script from the two compiles of the same record: the Jawi
    compile's board, headings and points; the Rumi compile's spoken fields,
    holds and durations; the Jawi spoken copy as the display fields.
    Returns ``(script, report)``; raises ``Unpaired``."""
    r_segs = [s for s in (rumi.get("segments") or []) if isinstance(s, dict)]
    j_segs = [s for s in (jawi.get("segments") or []) if isinstance(s, dict)]
    r_ids = [str(s.get("segment_id") or "") for s in r_segs]
    j_ids = [str(s.get("segment_id") or "") for s in j_segs]
    if r_ids != j_ids:
        raise Unpaired(f"segment ids differ between the Rumi and Jawi boards: {r_ids[:12]} vs {j_ids[:12]}")
    out = copy.deepcopy(jawi)
    remapped, unpaired_scenes = 0, []
    segs = []
    for r, j in zip(r_segs, j_segs):
        sid = str(r.get("segment_id") or "")
        rd = [d for d in (r.get("dialogue") or []) if isinstance(d, dict)]
        jd = [d for d in (j.get("dialogue") or []) if isinstance(d, dict)]
        if [d.get("who") for d in rd] != [d.get("who") for d in jd]:
            raise Unpaired(f"{sid}: speaker turns differ between the Rumi and Jawi boards")
        seg = copy.deepcopy(j)
        seg["text"] = r.get("text") or ""
        seg["elevenlabs_text"] = r.get("elevenlabs_text") or r.get("text") or ""
        seg["display_text"] = j.get("text") or ""
        seg["dialogue"] = ([{**copy.deepcopy(rl), "display": str(jl.get("line") or "")} for rl, jl in zip(rd, jd)]
                           or None)
        for k in ("hold_secs", "estimated_duration_seconds", "pause_for_question"):
            if k in r:
                seg[k] = r[k]
        if isinstance(seg.get("scene"), dict) and isinstance(r.get("scene"), dict):
            n, paired = remap_cues(seg["scene"], r["scene"])
            remapped += n
            if not paired:
                unpaired_scenes.append(sid)
        segs.append(seg)
    out["segments"] = segs
    out["total_estimated_duration_seconds"] = rumi.get("total_estimated_duration_seconds") or out.get(
        "total_estimated_duration_seconds")
    return out, {"segments": len(segs), "cues_remapped": remapped, "scenes_unpaired": unpaired_scenes}


# ── checks only a two-script kit needs ──────────────────────────────────


def check_two_scripts(script: dict) -> dict:
    """The Jawi pre-check beside check A: every SPOKEN field is Rumi (an
    Arabic letter there would reach the Malay voice — blocking), and every
    segment that speaks has a Jawi display copy (missing — blocking: its
    bubbles would show Rumi). A display copy that still reads as Rumi is a
    flag (check A's leftover rule reads the same thing per segment)."""
    blocking: list[dict] = []
    flags: list[dict] = []
    for seg in script.get("segments") or []:
        if not isinstance(seg, dict):
            continue
        sid = str(seg.get("segment_id") or "")
        spoken = [("text", str(seg.get("text") or "")), ("elevenlabs_text", str(seg.get("elevenlabs_text") or ""))]
        spoken += [(f"dialogue[{i}].line", str(d.get("line") or "")) for i, d in enumerate(seg.get("dialogue") or [])
                   if isinstance(d, dict)]
        for where, text in spoken:
            if _ARABIC_LETTER_RE.search(text):
                blocking.append({"where": sid, "what": "spoken text is not Rumi", "field": where,
                                 "sample": text[:120]})
                break
        if str(seg.get("text") or "").strip() and not str(seg.get("display_text") or "").strip():
            blocking.append({"where": sid, "what": "no Jawi display copy of the narration"})
        for i, d in enumerate(seg.get("dialogue") or []):
            if isinstance(d, dict) and str(d.get("line") or "").strip() and not str(d.get("display") or "").strip():
                blocking.append({"where": sid, "what": "no Jawi display copy of a dialogue line", "line": i})
                break
        shown = str(seg.get("display_text") or "")
        own, latin = len(_ARABIC_LETTER_RE.findall(shown)), len(_LATIN_LETTER_RE.findall(shown))
        if own + latin >= 12 and latin > own:
            flags.append({"where": sid, "what": "display text still reads as Rumi", "sample": shown[:120]})
        # word by word: a few Malay words left in Rumi ("لوݢم Rod") pass the
        # share rule above, so every written surface is read for them —
        # the bubbles' copies and the board, heading, points and visual
        left = written_leftovers(seg)
        if left:
            flags.append({"where": sid, "what": LEFTOVER_FLAG,
                          "words": sorted({w for ws in left.values() for w in ws}, key=lambda w: (w.lower(), w)),
                          "fields": {k: v for k, v in left.items()}})
    return {"check": "A-jawi", "ok": not blocking, "blocking": blocking, "flags": flags}


LEFTOVER_FLAG = "Latin words left in the Jawi"


def written_leftovers(seg: dict) -> dict[str, list[str]]:
    """``{field path: [Latin words]}`` over every surface of a segment the
    viewer READS in Jawi: ``display_text``, each line's ``display`` and the
    written fields (written_items). The scene's typeset expressions are
    protected."""
    scene = seg.get("scene") if isinstance(seg.get("scene"), dict) else {}
    protected = [str(e.get("expr")) for e in (scene.get("elements") or [])
                 if isinstance(e, dict) and e.get("type") == "math" and str(e.get("expr") or "").strip()]
    fields: list[tuple[str, str]] = [("display_text", str(seg.get("display_text") or ""))]
    fields += [(f"dialogue[{i}].display", str(d.get("display") or "")) for i, d in enumerate(seg.get("dialogue") or [])
               if isinstance(d, dict)]
    fields += written_items(seg)
    out: dict[str, list[str]] = {}
    for path, text in fields:
        words = latin_leftovers(text, protected)
        if words:
            out[path] = words
    return out


__all__ = ["BACK_RULE", "LATIN_KEEP", "LEFTOVER_FLAG", "REPAIR_RULE", "RULE", "SOURCE", "TARGET", "Unpaired",
           "check_two_scripts", "context_for", "finish_segment", "is_derived", "latin_leftovers", "merge_compiles",
           "remap_cues", "source_language_for", "spoken_items", "written_items", "written_leftovers"]
