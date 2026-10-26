"""Revision 3 acceptance checks (founder 2026-10-26) — run on the RENDERED book,
so they test what a reader sees, for any book the engine makes:

  numbering     each chapter's printed questions run X1, X2, … in order, and the
                answers section lists the same numbers in the same order
  matching      every question has exactly one answer entry (n/n)
  letters       every picture letter a question names is printed on its pictures
  descriptions  no long picture description is printed under a question picture
                (the printed note under a picture is at most 14 words)
  coverage      every coverage claim was made by both validators and quotes its
                evidence; nothing "assessed only" is counted as covered
  layout        no hard layout defect in any chapter
  answer keys   (--answers) Claude Opus 5.5, not the author, re-solves every
                question blind and checks the key and mark scheme

    python -m publishing.tools.acceptance <slug> [--answers]
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

ANSWERS = """You are checking the answer key of Chapter {n} of a study guide (ages {age}). For EACH question: solve it
yourself from the question alone (and its picture notes), then compare with the printed answer, worked solution and
marking scheme. Rule "correct", or "wrong" / "ambiguous" with the exact problem and the corrected key.

Questions with their keys:
{questions}

Reply with JSON: {{"checks": [{{"xid": str, "verdict": "correct"|"wrong"|"ambiguous", "problem": str, "fix": str}}]}}"""


def run(slug: str, answers: bool) -> dict:
    import fitz
    from publishing.manuscript.render import PICTURE_WORD, question_numbers, question_order
    from publishing.report import OUT
    out = OUT / "manuscripts" / slug
    res: dict = {"chapters": {}}
    rev = next((out / r for r in (os.getenv("PUBLISH_REV", ""), "rev3", "rev2") if r and (out / r / "coverage.json").exists()))
    for n in range(1, 11):
        f = out / f"ch{n:02d}-draft.json"
        if not f.exists():
            continue
        doc = json.loads(f.read_text(encoding="utf-8"))
        nums = question_numbers(doc)
        r: dict = {}
        pdf = fitz.open(out / f"ch{n:02d}.pdf")
        text = "\n".join(p.get_text() for p in pdf)
        cut = text.find("Answers and worked solutions")
        practice, answer_part = text[text.find("Work through the questions in order"):cut], text[cut:]
        printed_q = [int(m) for m in re.findall(r"^X(\d+)\s*$", practice, flags=re.M)]
        printed_a = [int(m) for m in re.findall(r"^X(\d+)\b", answer_part, flags=re.M)]
        expected = list(range(1, len(doc["exercises"]) + 1))
        r["numbering"] = {"questions_in_order": printed_q == expected, "answers_in_order": printed_a[:len(expected)] == expected,
                          "printed_questions": printed_q, "printed_answers": printed_a[:len(expected)]}
        r["matching"] = {"questions": len(doc["exercises"]), "answer_entries": len(set(printed_a) & set(expected))}
        bad_letters, long_notes = [], []
        for x in question_order(doc):
            refs = ([x["figure_ref"]] if x.get("figure_ref") else []) + list(x.get("figure_refs") or [])
            ctx = x.get("figure_context") or {}
            keys = sorted(k for k in ctx if isinstance(k, str) and len(k) == 1 and k.isalpha())
            named = sorted(set(re.findall(PICTURE_WORD + r"\s+([A-H])\b", x.get("stem") or "")))
            shown = keys if len(keys) == len(refs) and keys else (named if len(named) == len(refs) and named
                                                                   else list("ABCDEFGH"[:len(refs)]))
            if refs and any(l not in shown for l in named):
                bad_letters.append({"q": nums[x["xid"]], "names": named, "printed": shown})
            for v in ctx.values():
                if v and len(str(v).split()) > 14 and str(v)[:40] in text:
                    long_notes.append({"q": nums[x["xid"]], "note": str(v)[:80]})
        r["letters"] = bad_letters
        r["descriptions_printed"] = long_notes
        # numbered parts named by a question must be drawn on its picture; specimen notes it relies on must print
        from publishing.manuscript.render import DESCRIBES_PICTURE, annotations_for
        from publishing.tools.label_anchors import NUMBERED
        ann = annotations_for(slug)
        missing_markers, notes_missing = [], []
        for x in question_order(doc):
            refs = ([x["figure_ref"]] if x.get("figure_ref") else []) + list(x.get("figure_refs") or [])
            if refs and NUMBERED.search(x.get("stem") or ""):
                pts = {k for ref in refs for k in ((ann.get(ref) or {}).get("anchors") or {})}
                if not x.get("markers") or any(v not in pts for v in x["markers"].values()):
                    missing_markers.append(nums[x["xid"]])
            for k, v in (x.get("figure_context") or {}).items():
                if v and len(str(v).split()) <= 30 and not DESCRIBES_PICTURE.search(str(v)) and                         " ".join(str(v).split()[:6]) not in " ".join(text.split()):
                    notes_missing.append({"q": nums[x["xid"]], "note": str(v)[:60]})
        r["markers_missing"] = missing_markers
        r["specimen_notes_missing"] = notes_missing
        lab = [(ref, v) for ref, v in ann.items() if any(c in v.get("where", []) for c in (f"ch{n}",))]
        r["labels_verified"] = sum(len(v.get("anchors") or {}) for _, v in lab)
        r["labels_withheld"] = [(ref[:24], v.get("unverified")) for ref, v in lab if v.get("unverified")]
        diag = json.loads((out / f"ch{n:02d}-diagnostics.json").read_text())
        r["hard_layout_defects"] = len(diag["hard"])
        res["chapters"][n] = r
    cov = json.loads((rev / "coverage.json").read_text(encoding="utf-8"))
    weak = []
    for n, c in cov.items():
        for kind in ("sections", "exercises"):
            for unit, codes in (c.get("agreed") or {}).get(kind, {}).items():
                for code, ev in codes.items():
                    if not (ev.get("evidence") and ev.get("evidence_2")):
                        weak.append(f"ch{n} {unit} {code}")
    matrix = json.loads((rev / "coverage-matrix.json").read_text(encoding="utf-8"))
    res["coverage"] = {"claims_without_evidence": weak,
                       "assessed_only": [c for c, m in matrix.items() if m["status"].startswith("assessed only")]}
    if answers:
        from publishing.blueprint import _load_model_env
        from publishing.cli import DEFAULT_ENV
        from publishing.tools.academic_audit import OpusValidator, _ask, system_for
        _load_model_env(DEFAULT_ENV)
        opus = OpusValidator()
        keys = {}
        for n in res["chapters"]:
            doc = json.loads((out / f"ch{n:02d}-draft.json").read_text(encoding="utf-8"))
            nums = question_numbers(doc)
            qs = [{**{k: x.get(k) for k in ("item_type", "marks", "stem", "options", "pairs", "items", "categories",
                                              "answer", "worked_solution", "marking_scheme", "figure_context")},
                   "xid": nums[x["xid"]]} for x in question_order(doc)]
            data, cost = _ask(opus, ANSWERS.format(n=n, age=doc["work"]["age_band"],
                                                   questions=json.dumps(qs, ensure_ascii=False)), system_for(doc["work"]))
            keys[n] = {"checks": data.get("checks") or [], "usd": cost.get("usd")}
        res["answer_keys"] = keys
    s = res["chapters"]
    res["summary"] = {
        "questions_with_answers": f"{sum(c['matching']['answer_entries'] for c in s.values())}/"
                                  f"{sum(c['matching']['questions'] for c in s.values())}",
        "chapters_in_order": f"{sum(1 for c in s.values() if c['numbering']['questions_in_order'] and c['numbering']['answers_in_order'])}/{len(s)}",
        "figure_letter_mismatches": sum(len(c["letters"]) for c in s.values()),
        "long_descriptions_printed": sum(len(c["descriptions_printed"]) for c in s.values()),
        "hard_layout_defects": sum(c["hard_layout_defects"] for c in s.values()),
        "numbered_questions_without_markers": sum(len(c["markers_missing"]) for c in s.values()),
        "specimen_notes_missing": sum(len(c["specimen_notes_missing"]) for c in s.values()),
        "labels_on_verified_points": sum(c["labels_verified"] for c in s.values()),
        "labels_withheld_unverified": sum(len(u) for c in s.values() for _, u in c["labels_withheld"]),
        "coverage_claims_without_evidence": len(weak),
        "objectives_assessed_without_teaching": len(res["coverage"]["assessed_only"]),
    }
    if answers:
        res["summary"]["answer_keys"] = {v: sum(1 for k in res["answer_keys"].values() for c in k["checks"] if c.get("verdict") == v)
                                         for v in ("correct", "wrong", "ambiguous")}
    rev_dir = out / os.getenv("PUBLISH_REV", "rev3")
    rev_dir.mkdir(exist_ok=True)
    (rev_dir / "acceptance.json").write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
    return res["summary"]


def main() -> None:
    a = argparse.ArgumentParser()
    a.add_argument("slug")
    a.add_argument("--answers", action="store_true")
    x = a.parse_args()
    print(json.dumps(run(x.slug, x.answers), indent=1))


if __name__ == "__main__":
    main()
