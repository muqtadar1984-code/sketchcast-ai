"""Deterministic checks on a drafted chapter (§7 diagram rule, §15.3).

Model review comes after these; these never call a model. Each finding is
{unit, check, verdict: fail|warn, detail}. A chapter with any `fail` is not
offered for Gate D until the finding is fixed or a person waives it.
"""
from __future__ import annotations

import json
import re

# A sentence that points at a picture. A section that says this must place a
# figure block of its own (the diagram rule: no orphan reference).
DEICTIC = re.compile(r"\b(diagram|figure|picture|illustration|shown (?:below|above|here)|look at the (?:image|drawing))\b",
                     re.I)
TIERS = ("guided", "independent", "challenge")


def _text(block: dict) -> str:
    return " ".join(str(v) for k, v in block.items() if k not in ("type", "source_refs", "ref"))


def check_sections(sections: list[dict], valid_ids: set[str], figure_ids: set[str]) -> list[dict]:
    out = []
    for s in sections:
        sid = s.get("sid", "?")
        has_fig = False
        for i, b in enumerate(s.get("blocks") or [], 1):
            refs = b.get("source_refs") or []
            where = f"{sid}.b{i}"
            if not refs:
                out.append({"unit": where, "check": "source_refs", "verdict": "fail",
                            "detail": f"{b.get('type')} block cites nothing"})
            bad = [r for r in refs if r not in valid_ids]
            if bad:
                out.append({"unit": where, "check": "source_refs", "verdict": "fail",
                            "detail": f"cites ids not in the dossier: {bad}"})
            if b.get("type") == "figure":
                has_fig = True
                if b.get("ref") not in figure_ids:
                    out.append({"unit": where, "check": "figure_exists", "verdict": "fail",
                                "detail": f"figure {b.get('ref')!r} is not a pinned figure or approved asset"})
            if b.get("type") == "figure_group":
                has_fig = True
                items = b.get("items") or []
                bad = [i.get("ref") for i in items if i.get("ref") not in figure_ids]
                if len(items) < 2 or bad:
                    out.append({"unit": where, "check": "figure_exists", "verdict": "fail",
                                "detail": f"figure group needs 2+ known pictures; unknown: {bad}"})
                for r in b.get("relations") or []:
                    if not all(isinstance(r.get(k), int) and 0 <= r[k] < len(items) for k in ("from", "to")):
                        out.append({"unit": where, "check": "figure_exists", "verdict": "fail",
                                    "detail": f"relation {r} points outside the group"})
            if b.get("type") == "think" and not str(b.get("answer") or "").strip():
                out.append({"unit": where, "check": "think", "verdict": "fail",
                            "detail": "a think box needs its answer (printed at the end of the chapter)"})
        if sum(1 for b in s.get("blocks") or [] if b.get("type") == "think") > 1:
            out.append({"unit": sid, "check": "think", "verdict": "fail", "detail": "more than one think box"})
        prose = " ".join(_text(b) for b in s.get("blocks") or [] if b.get("type") not in ("figure", "figure_group"))
        m = DEICTIC.search(prose)
        if m and not has_fig:
            out.append({"unit": sid, "check": "deictic", "verdict": "fail",
                        "detail": f"text says {m.group(0)!r} but the section places no figure"})
        words = len(prose.split())
        if words < 120:
            out.append({"unit": sid, "check": "length", "verdict": "warn", "detail": f"{words} words"})
    return out


def _norm(s) -> str:
    return re.sub(r"\W+", " ", str(s or "").lower()).strip()


def check_assessment(x: dict) -> list[dict]:
    """The deterministic half of the ASSESSMENT VALIDITY gate (ch 1 review,
    2026-10-25: a match task printed its terms beside their own definitions)."""
    xid, out = x.get("xid", "?"), []
    if x.get("item_type") == "match":
        pairs = x.get("pairs") or []
        if len(pairs) < 3:
            out.append({"unit": xid, "check": "assessment", "verdict": "fail",
                        "detail": "a match task needs structured pairs (3+) so the book can scramble them"})
        shown = _norm(f"{x.get('stem')} {' '.join(map(str, x.get('options') or []))}")
        leaked = [p for p in pairs if _norm(p.get("right")) and _norm(p.get("right")) in shown
                  and _norm(p.get("left")) in shown]
        if len(leaked) >= 2:
            out.append({"unit": xid, "check": "assessment", "verdict": "fail",
                        "detail": f"the stem/options already show {len(leaked)} correct pairings (answer given away)"})
        rights = [_norm(p.get("right")) for p in pairs]
        if len(set(rights)) < len(rights):
            out.append({"unit": xid, "check": "assessment", "verdict": "fail",
                        "detail": "a match task repeats an answer in column B; that is a sorting task — use distinct "
                                  "descriptions, or set it as a sort"})
    if x.get("item_type") == "mcq":
        opts = [_norm(o) for o in x.get("options") or []]
        ans = _norm(x.get("answer"))
        letter = re.fullmatch(r"[a-h]", ans or "")
        if not opts or not (letter and ord(ans) - 97 < len(opts) or any(ans == o or ans.endswith(o) for o in opts)):
            out.append({"unit": xid, "check": "assessment", "verdict": "fail",
                        "detail": f"the answer {x.get('answer')!r} is not one of the options"})
    if x.get("item_type") == "sort":
        cats = [_norm(c) for c in x.get("categories") or []]
        its = x.get("items") or []
        if len(cats) < 2 or len(its) < 4 or any(_norm(i.get("category")) not in cats for i in its):
            out.append({"unit": xid, "check": "assessment", "verdict": "fail",
                        "detail": "a sort task needs 2+ categories and 4+ items, each in one of the categories"})
    pics = ([x["figure_ref"]] if x.get("figure_ref") else []) + list(x.get("figure_refs") or [])
    if pics and len(x.get("figure_context") or {}) < len(pics):
        out.append({"unit": xid, "check": "assessment", "verdict": "fail",
                    "detail": "a question with pictures must say for each (A, B, …) where it came from (figure_context)"})
    for p in x.get("marking_scheme") or []:
        if len(str(p.get("point") or "").split()) < 4:
            out.append({"unit": xid, "check": "assessment", "verdict": "fail",
                        "detail": f"mark point {p.get('point')!r} does not say what earns the mark"})
    return out


def check_picture_reuse(sections: list[dict]) -> list[dict]:
    """In the teaching text each picture appears once (founder 2026-10-25: no
    repetition to add visual density). Exercises may reuse a picture."""
    seen, out = {}, []
    for s in sections:
        for b in s.get("blocks") or []:
            refs = ([b.get("ref")] if b.get("type") == "figure" else
                    [i.get("ref") for i in b.get("items") or []] if b.get("type") == "figure_group" else [])
            for r in refs:
                if r in seen:
                    out.append({"unit": s.get("sid", "?"), "check": "picture_reuse", "verdict": "fail",
                                "detail": f"{r} is already shown in {seen[r]}; refer back to it instead"})
                else:
                    seen[r] = s.get("sid", "?")
    return out


def check_exercises(exercises: list[dict], valid_ids: set[str], figure_ids: set[str], nodes: set[str],
                    photo_ids: set[str] = frozenset(), skills: set[str] = frozenset()) -> list[dict]:
    out = []
    for x in exercises:
        out += check_assessment(x)
        for r in x.get("figure_refs") or []:
            if r not in figure_ids:
                out.append({"unit": x.get("xid", "?"), "check": "figure_exists", "verdict": "fail", "detail": f"{r!r}"})
        if x.get("skill_code") and x["skill_code"] not in skills:
            out.append({"unit": x.get("xid", "?"), "check": "coverage", "verdict": "warn",
                        "detail": f"skill {x['skill_code']!r} is not one of {sorted(skills)}"})
    tf = [x for x in exercises if x.get("item_type") == "true_false"]
    if len(tf) > 1:
        out.append({"unit": "exercises", "check": "assessment", "verdict": "fail",
                    "detail": f"{len(tf)} true/false items; at most one"})
    closed = [_norm(x.get("answer")) for x in exercises if x.get("item_type") in ("true_false", "mcq")]
    if len(closed) >= 3 and len(set(closed)) == 1:
        out.append({"unit": "exercises", "check": "assessment", "verdict": "fail",
                    "detail": f"every closed item has the same answer ({closed[0]!r}); a guessable pattern"})
    # photo_ids are the real photographs this chapter may use (not shown in another chapter): when there are
    # two, an observation task must use them; when there are not, two or more drawings serve, and none is a warning
    if len(photo_ids) >= 2 and not any(len(set(x.get("figure_refs") or []) & photo_ids) >= 2 for x in exercises):
        out.append({"unit": "exercises", "check": "observation", "verdict": "fail",
                    "detail": "no observation exercise uses two or more of the chapter's real photographs"})
    elif len(photo_ids) < 2 and not any(len(x.get("figure_refs") or []) >= 2 for x in exercises):
        out.append({"unit": "exercises", "check": "observation", "verdict": "warn",
                    "detail": "no observation exercise compares two or more pictures"})
    for x in exercises:
        xid = x.get("xid", "?")
        for f in ("stem", "answer", "worked_solution"):
            if not str(x.get(f) or "").strip():
                out.append({"unit": xid, "check": "complete", "verdict": "fail", "detail": f"no {f}"})
        scheme = x.get("marking_scheme") or []
        if not scheme:
            out.append({"unit": xid, "check": "complete", "verdict": "fail", "detail": "no marking scheme"})
        elif sum(int(p.get("marks") or 0) for p in scheme) != int(x.get("marks") or 0):
            out.append({"unit": xid, "check": "marks", "verdict": "fail",
                        "detail": f"scheme totals {sum(int(p.get('marks') or 0) for p in scheme)}, marks {x.get('marks')}"})
        refs = list(x.get("source_refs") or []) + ([x["source_item_id"]] if x.get("source_item_id") else [])
        if not refs:
            out.append({"unit": xid, "check": "source_refs", "verdict": "fail", "detail": "exercise cites nothing"})
        bad = [r for r in refs if r not in valid_ids]
        if bad:
            out.append({"unit": xid, "check": "source_refs", "verdict": "fail", "detail": f"not in the dossier: {bad}"})
        if x.get("figure_ref") and x["figure_ref"] not in figure_ids:
            out.append({"unit": xid, "check": "figure_exists", "verdict": "fail", "detail": f"{x['figure_ref']!r}"})
        if not x.get("figure_ref") and not x.get("figure_refs") and DEICTIC.search(str(x.get("stem") or "")):
            out.append({"unit": xid, "check": "deictic", "verdict": "fail", "detail": "stem points at a picture it does not have"})
        if x.get("node_code") not in nodes:
            out.append({"unit": xid, "check": "coverage", "verdict": "fail",
                        "detail": f"node {x.get('node_code')!r} is not one of the chapter's {sorted(nodes)}"})
    for t in TIERS:
        if not any(x.get("tier") == t for x in exercises):
            out.append({"unit": "exercises", "check": "tiers", "verdict": "fail", "detail": f"no {t} exercise"})
    return out


def check_extension(sections: list[dict], exercises: list[dict]) -> list[dict]:
    """The extension box is optional reading and is never assessed: an exercise
    whose claims are taught ONLY inside the extension fails (ch 1, 2026-10-25:
    two questions marked the nucleoid). A section may not be extension alone."""
    ext_refs, core_refs, out = set(), set(), []
    for s in sections:
        kinds = {b.get("type") for b in s.get("blocks") or []}
        if kinds and kinds <= {"extension", "figure"}:
            out.append({"unit": s.get("sid", "?"), "check": "scope", "verdict": "fail",
                        "detail": "a section made only of extension material; fold it into a related section"})
        for b in s.get("blocks") or []:
            (ext_refs if b.get("type") == "extension" else core_refs).update(
                r for r in b.get("source_refs") or [] if r.startswith(("claim:", "sec:", "gloss:")))
    only_ext = ext_refs - core_refs
    for x in exercises:
        hit = only_ext & set(x.get("source_refs") or [])
        if hit:
            out.append({"unit": x.get("xid", "?"), "check": "assessment", "verdict": "fail",
                        "detail": f"assesses extension-only content {sorted(hit)}; the extension is never assessed"})
    return out


def check_coverage(sections: list[dict], exercises: list[dict], plan: dict, nodes: set[str]) -> list[dict]:
    taught = {n for s in plan.get("sections", []) for n in s.get("node_codes") or []}
    tested = {x.get("node_code") for x in exercises}
    out = []
    ext = [b for s in sections for b in s.get("blocks") or [] if b.get("type") == "extension"]
    if len(ext) > 1:
        out.append({"unit": "chapter", "check": "scope", "verdict": "fail",
                    "detail": f"{len(ext)} extension boxes; at most one per chapter"})
    for b in ext:
        if len(str(b.get("text") or "").split()) > 90:
            out.append({"unit": "chapter", "check": "scope", "verdict": "fail",
                        "detail": f"extension box of {len(str(b.get('text')).split())} words (max 80)"})
    for n in sorted(nodes):
        if n not in taught:
            out.append({"unit": "chapter", "check": "coverage", "verdict": "fail", "detail": f"{n} is taught by no section"})
        if n not in tested:
            out.append({"unit": "chapter", "check": "coverage", "verdict": "fail", "detail": f"{n} is tested by no exercise"})
    return out


LOCKED_FIELDS = ("type", "text", "term", "wrong", "right", "answer", "caption", "ref", "items", "relations")


def check_preserved(approved: list[dict], expanded: list[dict]) -> list[dict]:
    """An EXPANSION of an approved chapter (founder 2026-10-25) may add blocks but
    never change one: every approved block appears verbatim, in its order."""
    out = []
    new_by_sid = {s["sid"]: s for s in expanded}
    for old in approved:
        new = new_by_sid.get(old["sid"])
        if not new:
            out.append({"unit": old["sid"], "check": "preserved", "verdict": "fail", "detail": "section missing"})
            continue
        if new.get("heading") != old.get("heading"):
            out.append({"unit": old["sid"], "check": "preserved", "verdict": "fail",
                        "detail": f"heading changed: keep {old.get('heading')!r}"})
        key = lambda b: json.dumps({k: b.get(k) for k in LOCKED_FIELDS if k in b}, sort_keys=True, ensure_ascii=False)
        stream, at = [key(b) for b in new.get("blocks") or []], 0
        for b in old.get("blocks") or []:
            try:
                at = stream.index(key(b), at) + 1
            except ValueError:
                out.append({"unit": old["sid"], "check": "preserved", "verdict": "fail",
                            "detail": f"approved {b.get('type')} block changed, moved or dropped; restore it exactly: "
                                      + key(b)[:400]})
    return out


LOCKED_FIELDS = ("type", "text", "term", "wrong", "right", "answer", "caption", "ref", "items", "relations")


def check_preserved(approved: list[dict], expanded: list[dict]) -> list[dict]:
    """An EXPANSION of an approved chapter (founder 2026-10-25) may add blocks but
    never change one: every approved block appears verbatim, in its order."""
    out = []
    new_by_sid = {s["sid"]: s for s in expanded}

    def key(b):
        return json.dumps({k: b.get(k) for k in LOCKED_FIELDS if k in b}, sort_keys=True, ensure_ascii=False)

    for old in approved:
        new = new_by_sid.get(old["sid"])
        if not new:
            out.append({"unit": old["sid"], "check": "preserved", "verdict": "fail", "detail": "section missing"})
            continue
        if new.get("heading") != old.get("heading"):
            out.append({"unit": old["sid"], "check": "preserved", "verdict": "fail",
                        "detail": f"heading changed: keep {old.get('heading')!r}"})
        stream, at = [key(b) for b in new.get("blocks") or []], 0
        for b in old.get("blocks") or []:
            try:
                at = stream.index(key(b), at) + 1
            except ValueError:
                out.append({"unit": old["sid"], "check": "preserved", "verdict": "fail",
                            "detail": f"approved {b.get('type')} block changed, moved or dropped; restore it "
                                      f"exactly: {key(b)[:400]}"})
    return out


def restore_locked(approved: list[dict], expanded: list[dict]) -> list[dict]:
    """Put every locked block back, verbatim and in order (a model told to keep a
    block may still 'improve' it; the lock is enforced here, not by the prompt).
    A new block that is a reworded or re-pictured locked block is replaced by the
    locked one; a locked block that went missing is re-inserted after the locked
    block before it. New blocks keep their places."""
    from difflib import SequenceMatcher

    def key(b):
        return json.dumps({k: b.get(k) for k in LOCKED_FIELDS if k in b}, sort_keys=True, ensure_ascii=False)

    def body(b):
        return " ".join(str(b.get(k) or "") for k in ("text", "term", "wrong", "right", "caption"))

    old_by_sid = {s["sid"]: s for s in approved}
    out = []
    for sec in expanded:
        old = old_by_sid.get(sec["sid"])
        if not old:
            out.append(sec)
            continue
        O, at, blocks = old.get("blocks") or [], 0, []
        for b in sec.get("blocks") or []:
            j = next((i for i in range(at, len(O)) if key(O[i]) == key(b)), None)
            if j is None:
                j = next((i for i in range(at, len(O)) if O[i].get("type") == b.get("type") and body(b) and
                          SequenceMatcher(None, body(O[i]), body(b)).ratio() >= 0.6), None)
            if j is None:
                blocks.append(b)                     # genuinely new
                continue
            blocks.extend(O[at:j + 1])               # anything skipped, then the locked block itself
            at = j + 1
        blocks.extend(O[at:])
        out.append({**sec, "heading": old.get("heading", sec.get("heading")), "blocks": blocks})
    return out
