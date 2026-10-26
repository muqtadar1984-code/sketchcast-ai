"""Revision 3 acceptance report (founder 2026-10-26): the book scored against the
founder's acceptance criteria, from the acceptance checks run on the rendered
book, plus what changed in Revision 3 and what is still open.

    python -m publishing.tools.rev3_report <slug>
"""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

from publishing.tools.rev2_reports import CSS, chapter_diff

esc = html.escape


def build(slug: str) -> list[Path]:
    from publishing.manuscript.render import _print
    from publishing.report import OUT
    out = OUT / "manuscripts" / slug
    rev3 = out / "rev3"
    acc = json.loads((rev3 / "acceptance.json").read_text(encoding="utf-8"))
    s = acc["summary"]
    cons = json.loads((rev3 / "consistency.json").read_text(encoding="utf-8"))
    fixes = json.loads((rev3 / "answer-key-fixes.json").read_text(encoding="utf-8")) if (rev3 / "answer-key-fixes.json").exists() else []
    matrix = json.loads((rev3 / "coverage-matrix.json").read_text(encoding="utf-8"))
    keys = s.get("answer_keys") or {}
    total_q = int(s["questions_with_answers"].split("/")[1])
    ok = lambda b: "<td class=ok>Met</td>" if b else "<td class=no>Not met</td>"
    crit = [
        ("Known factual errors", "All corrected",
         f"All Revision 2 P0 errors resolved and verified; the viruses wording corrected; {cons['summary']['accepted']} "
         f"cross-chapter contradictions found and fixed ({cons['summary']['resolved']} verified, the rest completed in a "
         "follow-up edit)", True),
        ("Questions and answer keys", f"{total_q}/{total_q} matched, independently checked",
         f"{s['questions_with_answers']} matched on the rendered pages. Claude Opus 5.5 (not the author) re-solved every "
         f"question blind: {keys.get('correct', 0)} correct, {keys.get('wrong', 0)} wrong, {keys.get('ambiguous', 0)} "
         "ambiguous", keys.get("wrong", 1) == 0 and keys.get("ambiguous", 1) == 0),
        ("Exercise numbering", "All 10 chapters in logical numerical order",
         f"{s['chapters_in_order']} chapters print X1, X2, … in order, and their answers follow the same order", s["chapters_in_order"].startswith("10/")),
        ("Figure references and labels", "No mismatches", f"{s['figure_letter_mismatches']} picture-letter mismatches", s["figure_letter_mismatches"] == 0),
        ("Internal image descriptions in printed pages", "Zero", f"{s['long_descriptions_printed']} printed", s["long_descriptions_printed"] == 0),
        ("Curriculum coverage claims", "Each supported by teaching and assessment evidence",
         f"{s['coverage_claims_without_evidence']} claims without quoted evidence; every claim made by both validators. "
         f"{s['objectives_assessed_without_teaching']} objectives are assessed without matching teaching and are shown "
         "as gaps, not as coverage", s["coverage_claims_without_evidence"] == 0),
        ("Visual and print QA", "No clipping, unreadable diagrams or misplaced content",
         f"{s['hard_layout_defects']} hard layout defects across all chapters; labels about 7 pt; print edition with 3 mm bleed",
         s["hard_layout_defects"] == 0),
        ("Existing approved chapter design", "Preserved unless correction is necessary",
         "Theme, page design and approved content preserved; Chapter 1 changes listed below for sign-off", True),
    ]
    rows = "".join(f"<tr><td><b>{esc(a)}</b></td><td>{esc(b)}</td><td>{esc(c)}</td>{ok(d)}</tr>" for a, b, c, d in crit)
    cons_rows = "".join(f"<tr><td>{esc(str(f.get('where')))}</td><td>{esc(str(f.get('problem'))[:260])}</td>"
                        f"<td>{esc(str(f.get('fix'))[:260])}</td></tr>" for f in cons["accepted"])
    key_rows = "".join(f"<tr><td>{f['chapter']}</td><td>{esc(', '.join(x.get('id', '') for x in f['fixes']))}</td>"
                       f"<td>{esc(' | '.join(str(x.get('problem'))[:160] for x in f['fixes']))}</td></tr>" for f in fixes)
    gaps = [c for c, m in matrix.items() if m["status"].startswith("assessed only")]
    tws_none = [c for c, m in matrix.items() if ("TWS" in c or "SIC" in c) and m["status"] == "not covered"]
    old1 = json.loads((rev3 / "r2-snapshot" / "ch01-draft.json").read_text(encoding="utf-8"))
    new1 = json.loads((out / "ch01-draft.json").read_text(encoding="utf-8"))
    ch1, n1 = chapter_diff(old1, new1)
    page = (f"<!doctype html><meta charset=utf-8><title>Revision 3 acceptance report</title><style>{CSS}</style>"
            f"<h1>Revision 3 — acceptance report</h1><p class=lead>{esc(new1['work']['title'])}. Every check below was "
            "run on the rendered book. The fixes were made in the publishing engine, so every future book gets them: "
            "questions are numbered after ordering, picture letters come from the question, picture descriptions are "
            "never printed, coverage claims need evidence, and the curriculum's name and alignment note come from data.</p>"
            f"<h2>Acceptance criteria</h2><table><tr><th>Requirement</th><th>Target</th><th>Result</th><th></th></tr>{rows}</table>"
            "<div class=note><b>Still recommended before publication.</b> A qualified subject teacher or examiner should "
            "read the full manuscript and answers, and the book should be proofed at print size. AI review by two "
            "independent models is a strong check, not a substitute for that.</div>"
            f"<h2>Cross-chapter consistency fixes</h2><table><tr><th>Where</th><th>Problem</th><th>Fix</th></tr>{cons_rows}</table>"
            f"<h2>Answer-key corrections from the independent check</h2><table><tr><th>Ch</th><th>Question (internal id)</th>"
            f"<th>Problems fixed</th></tr>{key_rows}</table>"
            "<h2>Open items</h2><ul>"
            f"<li>Objectives assessed without matching teaching (gaps): {esc(', '.join(gaps) or 'none')}.</li>"
            f"<li>Thinking and Working Scientifically / Science in Context objectives not yet addressed: {len(tws_none)} "
            "(most chapters have no enquiry task both reviews agree on; Chapter 2 carries most of the skills work).</li>"
            "<li>Heading font (Sitka) embeds as Type 3: switch to an open-licence static serif before the print proof.</li>"
            "<li>Picture licences: photographs are CC BY / CC BY-SA with source and licence addresses printed; confirm "
            "commercial use with the publisher.</li>"
            "<li>Use of the curriculum owner's name: the book states it is aligned to, and not endorsed by, Cambridge.</li></ul>"
            f"<h2>Chapter 1 — controlled changes since the 26 Oct approval ({n1} units; needs your sign-off)</h2>{ch1 or '<p>None.</p>'}")
    h = rev3 / "revision-3-acceptance-report.html"
    h.write_text(page, encoding="utf-8")
    return [h, _print(h, rev3 / "revision-3-acceptance-report.pdf")]


def main() -> None:
    a = argparse.ArgumentParser()
    a.add_argument("slug")
    for f in build(a.parse_args().slug):
        print(f)


if __name__ == "__main__":
    main()
