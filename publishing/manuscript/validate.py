"""Deterministic checks on a drafted chapter (§7 diagram rule, §15.3).

Model review comes after these; these never call a model. Each finding is
{unit, check, verdict: fail|warn, detail}. A chapter with any `fail` is not
offered for Gate D until the finding is fixed or a person waives it.
"""
from __future__ import annotations

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
        prose = " ".join(_text(b) for b in s.get("blocks") or [] if b.get("type") != "figure")
        m = DEICTIC.search(prose)
        if m and not has_fig:
            out.append({"unit": sid, "check": "deictic", "verdict": "fail",
                        "detail": f"text says {m.group(0)!r} but the section places no figure"})
        words = len(prose.split())
        if words < 120:
            out.append({"unit": sid, "check": "length", "verdict": "warn", "detail": f"{words} words"})
    return out


def check_exercises(exercises: list[dict], valid_ids: set[str], figure_ids: set[str], nodes: set[str]) -> list[dict]:
    out = []
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
        if not x.get("figure_ref") and DEICTIC.search(str(x.get("stem") or "")):
            out.append({"unit": xid, "check": "deictic", "verdict": "fail", "detail": "stem points at a picture it does not have"})
        if x.get("node_code") not in nodes:
            out.append({"unit": xid, "check": "coverage", "verdict": "fail",
                        "detail": f"node {x.get('node_code')!r} is not one of the chapter's {sorted(nodes)}"})
    for t in TIERS:
        if not any(x.get("tier") == t for x in exercises):
            out.append({"unit": "exercises", "check": "tiers", "verdict": "fail", "detail": f"no {t} exercise"})
    return out


def check_coverage(sections: list[dict], exercises: list[dict], plan: dict, nodes: set[str]) -> list[dict]:
    taught = {n for s in plan.get("sections", []) for n in s.get("node_codes") or []}
    tested = {x.get("node_code") for x in exercises}
    out = []
    for n in sorted(nodes):
        if n not in taught:
            out.append({"unit": "chapter", "check": "coverage", "verdict": "fail", "detail": f"{n} is taught by no section"})
        if n not in tested:
            out.append({"unit": "chapter", "check": "coverage", "verdict": "fail", "detail": f"{n} is tested by no exercise"})
    return out
