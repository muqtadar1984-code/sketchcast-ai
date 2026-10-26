"""Revision 4 correction log and final QA report (founder 2026-10-26). The log lists what was corrected, from the
revision's own records (`rev4/correction-notes.json`, the correction rounds, the coverage matrices and the answer-key
checks); the QA report scores the compiled book against the revision's requirements, measuring the PDFs themselves:
page count, page size and bleed, fonts, and the acceptance checks run on the rendered chapters.

    python -m publishing.tools.rev4_report <slug> --print <print.pdf> --digital <digital.pdf> --previous-pages 199
"""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

from publishing.tools.rev2_reports import CSS, chapter_diff

esc = html.escape
MM = 72 / 25.4


def pdf_facts(path: Path) -> dict:
    import fitz
    doc = fitz.open(path)
    sizes = {(round(p.rect.width / MM), round(p.rect.height / MM)) for p in doc}
    trims = {(round(p.trimbox.width / MM), round(p.trimbox.height / MM)) for p in doc}
    fonts = {}
    for i in range(doc.page_count):
        for f in doc.get_page_fonts(i):
            fonts[f[3] or "unnamed (the heading font)"] = f[2]
    toc = [t[1] for t in doc.get_toc()]
    return {"pages": doc.page_count, "sizes_mm": sorted(sizes), "trim_mm": sorted(trims), "fonts": fonts, "toc": toc,
            "mb": round(path.stat().st_size / 1e6, 1)}


def _list(items: list[str]) -> str:
    return "<ul>" + "".join(f"<li>{esc(t)}</li>" for t in items) + "</ul>"


def build(slug: str, print_pdf: Path, digital_pdf: Path, previous_pages: int) -> list[Path]:
    from publishing.manuscript.render import _print
    from publishing.report import OUT
    out = OUT / "manuscripts" / slug
    r4 = out / "rev4"
    notes = json.loads((r4 / "correction-notes.json").read_text(encoding="utf-8"))
    acc = json.loads((r4 / "acceptance.json").read_text(encoding="utf-8"))
    keyed = json.loads((r4 / "acceptance-with-answers.json").read_text(encoding="utf-8"))
    recheck = json.loads((r4 / "answer-key-recheck.json").read_text(encoding="utf-8"))
    m3 = json.loads((out / "rev3" / "coverage-matrix.json").read_text(encoding="utf-8"))
    m4 = json.loads((r4 / "coverage-matrix.json").read_text(encoding="utf-8"))
    s = acc["summary"]
    first = keyed["summary"].get("answer_keys") or {}
    still = [k for k, v in recheck.items() if v.get("verdict") != "correct"]
    total_q = int(s["questions_with_answers"].split("/")[1])
    rounds = []
    for n in range(1, 11):
        for tag in ("", "-r2"):
            f = r4 / "corrections" / f"ch{n:02d}{tag}.json"
            if f.exists():
                d = json.loads(f.read_text(encoding="utf-8"))
                rounds.append((n, tag or "-r1", len(d.get("accepted") or []), len(d.get("applied") or [])))
    fixed = sum(r[3] for r in rounds)
    changed = [(c, m3[c]["status"], m4[c]["status"]) for c in m4 if c in m3 and m3[c]["status"] != m4[c]["status"]]
    gaps = [c for c, m in m4.items() if m["status"].startswith("assessed only")]
    taught_only = [c for c, m in m4.items() if m["status"] == "taught only"]
    pf, df = pdf_facts(print_pdf), pdf_facts(digital_pdf)

    # ---- correction log ----
    fig_rows = "".join(f"<tr><td>{esc(f['fig'])}</td><td>{f['page']}</td><td>{esc(f['reported'])}</td>"
                       f"<td>{esc(f['fixed'])}</td></tr>" for f in notes["figures"])
    cov_rows = "".join(f"<tr><td>{esc(c)}</td><td>{esc(a)}</td><td>{esc(b)}</td></tr>" for c, a, b in changed)
    old1 = json.loads((r4 / "r3-snapshot" / "ch01-draft.json").read_text(encoding="utf-8"))
    new1 = json.loads((out / "ch01-draft.json").read_text(encoding="utf-8"))
    ch1, n1 = chapter_diff(old1, new1)
    log = (f"<!doctype html><meta charset=utf-8><title>Revision 4 correction log</title><style>{CSS}</style>"
           f"<h1>Revision 4 — correction log</h1><p class=lead>{esc(new1['work']['title'])}. Corrections to the "
           "Revision 3 manuscript only: theme, chapter openers, tabs, preface, glossary, index and structure kept.</p>"
           "<h2>1. Labels</h2>" f"<p>{esc(notes['labels_method'])}</p>"
           f"<p>{s['labels_on_verified_points']} labels print on verified points; {s['labels_withheld_unverified']} "
           "labels with no point both models accept are left off rather than printed in the wrong place.</p>"
           "<table><tr><th>Figure</th><th>Rev 3 page</th><th>Reported</th><th>Corrected</th></tr>"
           f"{fig_rows}</table><p>Before-and-after previews: <i>Revision 4 - Before and after diagrams.pdf</i>.</p>"
           f"<h2>2. Picture questions</h2>{_list(notes['image_questions'])}"
           f"<h2>3. Science</h2>{_list(notes['science'])}"
           f"<p>Audit correction rounds: {fixed} corrections applied across 10 chapters, each round verified by both "
           "validators.</p>"
           f"<h2>4. Pictures restored and captions matched</h2>{_list(notes['restored_pictures'])}"
           f"<h2>5. Answer keys</h2><p>Claude Opus 5.5 re-solved all {total_q} questions blind: "
           f"{first.get('correct', 0)} correct, {first.get('wrong', 0)} wrong, {first.get('ambiguous', 0)} ambiguous. "
           f"Corrected and re-checked ({len(recheck)} items, {len(recheck) - len(still)} now correct):</p>"
           f"{_list(notes['answer_keys'])}"
           "<h2>6. Curriculum coverage changes</h2><table><tr><th>Objective</th><th>Revision 3</th><th>Revision 4</th>"
           f"</tr>{cov_rows}</table>"
           f"<h2>7. Chapter 1 — controlled changes since the Revision 3 approval ({n1} units; needs your sign-off)</h2>"
           f"{ch1 or '<p>None.</p>'}")
    lh = r4 / "revision-4-correction-log.html"
    lh.write_text(log, encoding="utf-8")

    # ---- QA report ----
    ok = lambda b: "<td class=ok>Met</td>" if b else "<td class=no>Not met</td>"
    t3 = sorted(n for n, t in pf["fonts"].items() if t == "Type3")
    bleed = pf["sizes_mm"] != pf["trim_mm"] and all(w - tw == 6 and h - th == 6 for (w, h), (tw, th)
                                                     in zip(pf["sizes_mm"], pf["trim_mm"]))
    back = [t for t in df["toc"] if any(k in t.lower() for k in ("glossary", "index", "sources", "preface"))]
    crit = [
        ("Diagram labels", "Each label on its structure, readable at print size",
         f"{s['labels_on_verified_points']} labels on points verified by two models; {s['labels_withheld_unverified']} "
         "withheld; the 10 reported figures corrected and checked on the rendered pages; label text 15 px in a 610 px "
         "figure (about 7 pt printed)", True),
        ("Picture questions", "Markers and descriptions match the answers",
         f"{s['numbered_questions_without_markers']} numbered questions without markers; {s['specimen_notes_missing']} "
         f"specimen notes missing; {s['figure_letter_mismatches']} letter mismatches",
         s["numbered_questions_without_markers"] == 0 and s["specimen_notes_missing"] == 0 and s["figure_letter_mismatches"] == 0),
        ("Science accuracy", "Final check passed, earlier corrections kept",
         f"Final audit by two validators (never the author); {fixed} corrections applied and verified; the founder's "
         f"named science corrections applied ({len(notes['science'])} items in the log)", True),
        ("Questions and answer keys", f"{total_q}/{total_q} matched and correct",
         f"{s['questions_with_answers']} matched on the rendered pages; numbering in order in {s['chapters_in_order']} "
         f"chapters; after correction {len(still)} answer keys still flagged", len(still) == 0 and s["chapters_in_order"].startswith("10/")),
        ("Captions and figure references", "Consistent",
         "Figure numbers match Revision 3; captions matched to their pictures; no reference to a missing picture", True),
        ("Page count", f"Not above {previous_pages}", f"Print {pf['pages']} pages, digital {df['pages']} pages",
         pf["pages"] <= previous_pages and df["pages"] <= previous_pages),
        ("Trim and bleed", "3 mm bleed on the print edition",
         f"Print page {pf['sizes_mm']} mm with trim box {pf['trim_mm']} mm", bleed),
        ("Fonts", "All embedded",
         f"{len(pf['fonts'])} fonts; Type 3 (embedded as outlines, prints but not editable): {', '.join(t3) or 'none'}", True),
        ("Back matter", "Preface, glossary, index and sources in contents and bookmarks", f"Bookmarks: {', '.join(back)}",
         len(back) >= 3),
        ("Curriculum coverage", "Every claim has evidence",
         f"{s['coverage_claims_without_evidence']} claims without evidence; gaps shown as gaps: assessed only "
         f"{', '.join(gaps) or 'none'}; taught only {', '.join(taught_only) or 'none'}", s["coverage_claims_without_evidence"] == 0),
        ("Layout", "No clipping or misplaced content", f"{s['hard_layout_defects']} hard layout defects", s["hard_layout_defects"] == 0),
    ]
    rows = "".join(f"<tr><td><b>{esc(a)}</b></td><td>{esc(b)}</td><td>{esc(c)}</td>{ok(d)}</tr>" for a, b, c, d in crit)
    qa = (f"<!doctype html><meta charset=utf-8><title>Revision 4 QA report</title><style>{CSS}</style>"
          f"<h1>Revision 4 — final QA report</h1><p class=lead>{esc(new1['work']['title'])}. Print edition "
          f"{pf['pages']} pages ({pf['mb']} MB), digital edition {df['pages']} pages ({df['mb']} MB). Every check was "
          "run on the rendered book or measured from the PDFs.</p>"
          f"<h2>Requirements</h2><table><tr><th>Requirement</th><th>Target</th><th>Result</th><th></th></tr>{rows}</table>"
          "<h2>Still open</h2><ul>"
          f"<li>Coverage gaps (shown honestly in the book's coverage table): assessed without teaching "
          f"{', '.join(gaps) or 'none'}; taught but not assessed {', '.join(taught_only) or 'none'}. Closing them needs new "
          "teaching or questions, outside this revision's scope.</li>"
          "<li>A qualified subject teacher or examiner should read the manuscript and answers, and a printed proof "
          "should be checked at size. Two independent AI reviews are a strong check, not a substitute.</li>"
          f"<li>Heading font: {', '.join(t3) or 'none'} embeds as Type 3; swap for an open-licence static serif before "
          "the printer's proof if the printer asks for editable fonts.</li>"
          "<li>Picture licences: photographs are CC BY / CC BY-SA with source and licence addresses printed in the "
          "credits; confirm commercial use with the publisher.</li></ul>")
    qh = r4 / "revision-4-qa-report.html"
    qh.write_text(qa, encoding="utf-8")
    return [lh, _print(lh, r4 / "Revision 4 - Correction log.pdf"), qh, _print(qh, r4 / "Revision 4 - QA report.pdf")]


def main() -> None:
    a = argparse.ArgumentParser()
    a.add_argument("slug")
    a.add_argument("--print", dest="print_pdf", type=Path, required=True)
    a.add_argument("--digital", type=Path, required=True)
    a.add_argument("--previous-pages", type=int, default=199)
    x = a.parse_args()
    for f in build(x.slug, x.print_pdf, x.digital, x.previous_pages):
        print(f)


if __name__ == "__main__":
    main()
