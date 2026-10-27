"""Revision 2 deliverables (founder 2026-10-26): the three audit reports, as HTML
and PDF, from the records the audit and correction runs left behind.

  1  Scientific and assessment corrections log — every accepted correction:
     severity, category, location, who raised it (CONFIRMED by both validators
     or SINGLE and adjudicated), the problem, the fix, the verification result
     by round; then, per chapter, a before/after of every changed block against
     the first-draft snapshot (rev2/r1-snapshot).
  2  Curriculum coverage matrix — every 0893 objective the book targets or
     touches: where taught, where assessed, status; cross-subject reasons.
  3  Editorial and publishing QA report — totals, P0 resolutions, open items,
     rejected findings and why, Chapter 1's controlled revision for sign-off,
     layout and print checks, and what still needs a human expert.

    python -m publishing.tools.rev2_reports <slug>
"""
from __future__ import annotations

import argparse
import difflib
import html
import json
from pathlib import Path

CSS = """body{font:10pt/1.45 'Segoe UI',Arial,sans-serif;color:#1f2a24;margin:0}
@page{size:A4;margin:16mm 14mm} h1{font:600 20pt Georgia,serif;color:#1d4a38;margin:0 0 2mm}
h2{font:600 14pt Georgia,serif;color:#1d4a38;margin:8mm 0 2mm;border-bottom:.6pt solid #d6ddd2;padding-bottom:1mm;break-after:avoid}
h3{font:600 9pt 'Segoe UI';letter-spacing:.12em;text-transform:uppercase;color:#2e6b52;margin:5mm 0 1mm;break-after:avoid}
.lead{color:#5f6b64} table{width:100%;border-collapse:collapse;font-size:8pt;margin:2mm 0}
th{background:#e5ede3;text-align:left;padding:1.2mm 1.6mm;color:#1d4a38} td{padding:1.2mm 1.6mm;border-bottom:.4pt solid #d6ddd2;vertical-align:top}
tr{break-inside:avoid} .P0{color:#a3271b;font-weight:700} .P1{color:#a3542b;font-weight:600} .ok{color:#2e6b52;font-weight:600}
.no{color:#a3271b;font-weight:600} .kpi{display:inline-block;border:.6pt solid #d6ddd2;border-radius:4pt;padding:2mm 4mm;margin:0 2mm 2mm 0}
.kpi b{display:block;font:700 16pt Georgia,serif;color:#1d4a38} del{background:#fbe3dd;text-decoration:line-through}
ins{background:#dff0e2;text-decoration:none} .diff{font-size:8pt;border-left:2pt solid #d6ddd2;padding:1mm 3mm;margin:1mm 0 3mm}
.note{background:#f7ebdd;border-left:2.4pt solid #c47a3a;padding:2.5mm 4mm;margin:3mm 0}"""

esc = html.escape


def _rounds(corr: Path, n: int) -> list[tuple[int, dict]]:
    out = []
    for r in range(1, 10):
        f = corr / (f"ch{n:02d}.json" if r == 1 else f"ch{n:02d}-r{r}.json")
        if f.exists():
            out.append((r, json.loads(f.read_text(encoding="utf-8"))))
    return out


def _block_text(b: dict) -> str:
    t = b.get("type")
    if t == "misconception":
        return f"Wrong: {b.get('wrong')} — Right: {b.get('right')}"
    if t == "key_term":
        return f"{b.get('term')}: {b.get('text')}"
    if t == "list":
        return " / ".join(map(str, b.get("items") or []))
    if t == "table":
        return f"{b.get('headers') or b.get('columns')} {b.get('rows')}"
    if t in ("figure", "figure_group"):
        return f"[{t} {b.get('ref') or [i.get('ref') for i in b.get('items') or [] if isinstance(i, dict)]}] {b.get('caption')}"
    if t == "think":
        return f"{b.get('text')} — Answer: {b.get('answer')}"
    return str(b.get("text") or b.get("problem") or "")


def _x_text(x: dict) -> str:
    return " | ".join(str(x.get(k)) for k in ("stem", "options", "answer", "marks", "marking_scheme") if x.get(k))


def _diff(a: str, b: str) -> str:
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a.split(), b.split()).get_opcodes():
        if op == "equal":
            out.append(esc(" ".join(a.split()[i1:i2])))
        else:
            if i2 > i1:
                out.append(f"<del>{esc(' '.join(a.split()[i1:i2]))}</del>")
            if j2 > j1:
                out.append(f"<ins>{esc(' '.join(b.split()[j1:j2]))}</ins>")
    return " ".join(out)


def chapter_diff(old: dict, new: dict) -> tuple[str, int]:
    """Changed teaching blocks (matched by section, then by best text match) and changed questions."""
    parts, count = [], 0
    for s_new in new["sections"]:
        s_old = next((s for s in old["sections"] if s["sid"] == s_new["sid"]), {"blocks": []})
        olds = [_block_text(b) for b in s_old["blocks"]]
        used = set()
        for b in s_new["blocks"]:
            t = _block_text(b)
            if t in olds:
                used.add(olds.index(t))
                continue
            best = max(((difflib.SequenceMatcher(None, o, t).ratio(), i) for i, o in enumerate(olds) if i not in used),
                       default=(0, None))
            if best[1] is not None and best[0] >= 0.45:
                used.add(best[1])
                parts.append(f"<div class='diff'><b>{esc(s_new['sid'])} · {esc(b.get('type'))}</b> — "
                             f"{_diff(olds[best[1]], t)}</div>")
            else:
                parts.append(f"<div class='diff'><b>{esc(s_new['sid'])} · {esc(b.get('type'))} (added)</b> — <ins>{esc(t)}</ins></div>")
            count += 1
        for i, o in enumerate(olds):
            if i not in used:
                parts.append(f"<div class='diff'><b>{esc(s_new['sid'])} (removed)</b> — <del>{esc(o)}</del></div>")
                count += 1
    ox = {x["xid"]: _x_text(x) for x in old["exercises"]}
    for x in new["exercises"]:
        t = _x_text(x)
        if ox.get(x["xid"]) != t:
            parts.append(f"<div class='diff'><b>{esc(x['xid'].upper())}</b> — "
                         + (_diff(ox[x["xid"]], t) if x["xid"] in ox else f"<ins>{esc(t)}</ins>") + "</div>")
            count += 1
    return "".join(parts), count


def build(slug: str) -> list[Path]:
    from publishing.manuscript.render import _print
    from publishing.report import OUT
    out = OUT / "manuscripts" / slug
    rev2 = out / "rev2"
    audit, corr, snap = rev2 / "audit", rev2 / "corrections", rev2 / "r1-snapshot"
    reports = rev2 / "reports"
    reports.mkdir(exist_ok=True)
    docs = {n: json.loads((out / f"ch{n:02d}-draft.json").read_text(encoding="utf-8")) for n in range(1, 11)}
    from publishing.tools.academic_audit import curriculum as _curriculum
    cname = _curriculum(docs[1]["work"]["curriculum"])["name"]
    # ── 1. corrections log ──────────────────────────────────────────────────
    rows, diffs, tot = [], [], {"accepted": 0, "resolved": 0, "open": 0, "P0": 0, "P0_resolved": 0, "changed_units": 0}
    p0_list, open_items, rejected, ch1_changes = [], [], [], ""
    for n in range(1, 11):
        a = json.loads((audit / f"ch{n:02d}-audit.json").read_text(encoding="utf-8"))
        for i, f in enumerate(a["findings"]):
            f["id"] = f"ch{n:02d}-F{i:03d}"
        by_id = {f["id"]: f for f in a["findings"]}
        status: dict[str, str] = {}
        rulings = {}
        for r, st in _rounds(corr, n):
            by_id.update({f["id"]: f for f in st.get("round_findings") or []})
            rulings.update(st.get("rulings") or {})
            for fid in st.get("accepted") or []:
                status.setdefault(fid, "open")
            for fid in st.get("resolved") or []:
                status[fid] = f"resolved (round {r})"
            for fid in st.get("not_applicable") or []:
                status[fid] = "not applicable (about audit input, not the page)"
            for fid in st.get("rejected_bad_code") or []:
                status[fid] = "rejected (cites an objective code the framework does not have)"
        for fid, r in rulings.items():
            if r.get("verdict") == "disagree" and fid not in status:
                rejected.append((n, by_id.get(fid, {}), r))
        for fid, stt in sorted(status.items()):
            f = by_id.get(fid, {})
            sev = str(f.get("severity", ""))
            tot["accepted"] += 1
            tot["resolved"] += stt.startswith("resolved")
            if stt == "open":
                tot["open"] += 1
                open_items.append((n, f))
            if sev == "P0":
                tot["P0"] += 1
                tot["P0_resolved"] += stt.startswith("resolved")
                p0_list.append((n, f, stt))
            r = rulings.get(fid)
            who = f"{f.get('agreement', '')} — " + ", ".join(x.split("(")[0] for x in f.get("by") or [])
            if r:
                who += f"; ruled {r.get('verdict')} by {str(r.get('by', '')).split('(')[0]}"
            rows.append(f"<tr><td>{esc(fid)}</td><td class='{esc(sev)}'>{esc(sev)}</td><td>{esc(str(f.get('category')))}</td>"
                        f"<td>{esc(str(f.get('where')))}</td><td>{esc(who)}</td><td>{esc(str(f.get('problem')))}"
                        f"<br><i>Quote:</i> {esc(str(f.get('quote'))[:200])}</td><td>{esc(str(f.get('fix'))[:400])}</td>"
                        f"<td class='{'ok' if stt.startswith('resolved') else 'no'}'>{esc(stt)}</td></tr>")
        old = json.loads((snap / f"ch{n:02d}-draft.json").read_text(encoding="utf-8"))
        d, cnt = chapter_diff(old, docs[n])
        tot["changed_units"] += cnt
        block = f"<h3>Chapter {n}: {esc(docs[n]['chapter']['title'])} — {cnt} changed units</h3>{d or '<p>No changes.</p>'}"
        diffs.append(block)
        if n == 1:
            ch1_changes = block
    log_html = (f"<!doctype html><meta charset=utf-8><title>Corrections log</title><style>{CSS}</style>"
                f"<h1>Scientific and assessment corrections log</h1><p class=lead>" + esc(docs[1]['work']['title']) + ". Every correction accepted from the independent audit (Gemini 3.5 Flash at high reasoning, "
                f"Claude Opus 5.5 at high effort — never the model that wrote the text), how it was decided, and whether "
                f"both validators confirmed it resolved.</p>"
                f"<div class=kpi><b>{tot['accepted']}</b>corrections accepted</div><div class=kpi><b>{tot['resolved']}</b>"
                f"verified resolved</div><div class=kpi><b>{tot['P0_resolved']}/{tot['P0']}</b>P0 errors resolved</div>"
                f"<div class=kpi><b>{tot['changed_units']}</b>units changed</div>"
                "<h2>Corrections</h2><table><tr><th>ID</th><th>Sev</th><th>Category</th><th>Where</th><th>Raised by</th>"
                "<th>Problem</th><th>Correction</th><th>Validation</th></tr>" + "".join(rows) + "</table>"
                "<h2>Changes against the first draft</h2><p class=lead>Struck text was removed; highlighted text was "
                "added.</p>" + "".join(diffs))
    # ── 2. coverage matrix ──────────────────────────────────────────────────
    matrix = json.loads((rev2 / "coverage-matrix.json").read_text(encoding="utf-8"))
    coverage = json.loads((rev2 / "coverage.json").read_text(encoding="utf-8"))
    book = sorted({c for ch in coverage.values() for c in ch["blueprint"]})
    def mrows(codes):
        return "".join(f"<tr><td><b>{esc(c)}</b></td><td>{esc(matrix[c]['text'])}</td><td>{esc(', '.join(matrix[c]['taught']) or '—')}</td>"
                       f"<td>{esc(', '.join(matrix[c]['assessed']) or '—')}</td><td class='{'ok' if matrix[c]['status'] == 'taught and assessed' else 'no'}'>"
                       f"{esc(matrix[c]['status'])}</td></tr>" for c in codes)
    head = "<tr><th>Code</th><th>Objective</th><th>Taught (chapter.section)</th><th>Assessed</th><th>Status</th></tr>"
    cross = list({x.get("code"): x for ch in coverage.values() for x in (ch["map"].get("cross_subject") or [])}.values())
    chap_rows = "".join(f"<tr><td>{n}</td><td>{esc(c['title'])}</td><td>{esc(', '.join(c['blueprint']))}</td>"
                        f"<td>{esc(', '.join(docs[int(n)].get('tws') or []))}</td></tr>" for n, c in coverage.items())
    other = [c for c, v in matrix.items() if c not in book and v["status"] != "not covered"]
    missing_tws = [c for c, v in matrix.items() if ("TWS" in c or "SIC" in c) and v["status"] == "not covered"]
    cov_html = (f"<!doctype html><meta charset=utf-8><title>Curriculum coverage</title><style>{CSS}</style>"
                "<h1>Curriculum coverage matrix</h1><p class=lead>" + esc(cname) + ". Every section "
                "and question of the corrected chapters was mapped by an independent validator (Gemini 3.5 Flash, high "
                "reasoning); only codes in the framework are counted.</p>"
                "<h2>Chapters</h2><table><tr><th>Ch</th><th>Title</th><th>Content objectives (blueprint)</th>"
                f"<th>Thinking and Working Scientifically / Science in Context</th></tr>{chap_rows}</table>"
                f"<h2>Content objectives the book targets</h2><table>{head}{mrows(book)}</table>"
                + "".join(f"<div class=note><b>{esc(x.get('code'))} (cross-subject):</b> {esc(x.get('why'))}</div>" for x in cross)
                + f"<h2>Further objectives the book addresses</h2><table>{head}{mrows(other)}</table>"
                f"<h2>Thinking and Working Scientifically / Science in Context not yet addressed</h2><p>{esc(', '.join(missing_tws))}</p>")
    # ── 3. editorial and publishing QA ──────────────────────────────────────
    diag = {n: json.loads((out / f"ch{n:02d}-diagnostics.json").read_text()) for n in range(1, 11)
            if (out / f"ch{n:02d}-diagnostics.json").exists()}
    qa_rows = "".join(f"<tr><td>{n}</td><td>{d['page_count']}</td><td class='{'ok' if not d['hard'] else 'no'}'>{len(d['hard'])}</td>"
                      f"<td>{len(d['warnings'])}</td></tr>" for n, d in diag.items())
    p0_rows = "".join(f"<tr><td>{n}</td><td>{esc(str(f.get('where')))}</td><td>{esc(str(f.get('problem')))}</td>"
                      f"<td class='{'ok' if s.startswith('resolved') else 'no'}'>{esc(s)}</td></tr>" for n, f, s in p0_list)
    open_rows = "".join(f"<tr><td>{n}</td><td class='{esc(str(f.get('severity')))}'>{esc(str(f.get('severity')))}</td>"
                        f"<td>{esc(str(f.get('where')))}</td><td>{esc(str(f.get('problem')))}</td></tr>" for n, f in open_items)
    rej_rows = "".join(f"<tr><td>{n}</td><td>{esc(str(f.get('where')))}</td><td>{esc(str(f.get('problem'))[:220])}</td>"
                       f"<td>{esc(str(r.get('why'))[:260])}</td></tr>" for n, f, r in rejected)
    qa_html = (f"<!doctype html><meta charset=utf-8><title>QA report</title><style>{CSS}</style>"
               "<h1>Editorial and publishing QA report</h1><p class=lead>Revision 2 — academically audited, "
               "curriculum-mapped, editorially refined draft. Local only: nothing published or deployed.</p>"
               "<div class=note><b>What this is, and is not.</b> Two AI validators from different model families, neither "
               "of them the author, audited every chapter, ruled on each other's findings and verified the corrections. "
               "This is a strong check, not a substitute for a qualified subject teacher or examiner. Before publication, "
               "an independent subject expert should read the book, starting with the open items below.</div>"
               f"<h2>Totals</h2><div class=kpi><b>{tot['accepted']}</b>corrections accepted</div><div class=kpi><b>"
               f"{tot['resolved']}</b>verified resolved</div><div class=kpi><b>{tot['open']}</b>open</div>"
               f"<div class=kpi><b>{len(rejected)}</b>findings rejected on adjudication</div>"
               f"<h2>Scientific errors (P0) and how each was resolved</h2><table><tr><th>Ch</th><th>Where</th><th>Error</th>"
               f"<th>Result</th></tr>{p0_rows}</table>"
               f"<h2>Open items for the founder or a subject expert</h2><table><tr><th>Ch</th><th>Sev</th><th>Where</th>"
               f"<th>Problem</th></tr>{open_rows or '<tr><td colspan=4>None</td></tr>'}</table>"
               f"<h2>Findings rejected on adjudication (second validator disagreed)</h2><table><tr><th>Ch</th><th>Where</th>"
               f"<th>Finding</th><th>Why rejected</th></tr>{rej_rows or '<tr><td colspan=4>None</td></tr>'}</table>"
               "<h2>Chapter 1 — controlled revision of approved content (needs your sign-off)</h2>"
               "<p>Chapter 1 was approved at Gate D. The changes below were made only to correct audit findings; they are "
               "applied in Revision 2 and listed here so none is silent.</p>" + ch1_changes +
               "<h2>Book structure added</h2><ul><li>Preface (draft for you to edit)</li><li>Curriculum coverage table</li>"
               "<li>Alphabetical glossary from the chapters' key terms</li><li>Index from the teaching pages</li>"
               "<li>Sources and picture credits (curriculum framework; photo credits). No further-reading list was invented."
               "</li></ul>"
               f"<h2>Layout and print checks</h2><table><tr><th>Ch</th><th>Pages</th><th>Hard defects</th><th>Warnings</th>"
               f"</tr>{qa_rows}</table><ul><li>Labelled diagrams set at readable size (labels about 7 pt); leaders never cross.</li>"
               "<li>No internal production text in any caption, label or picture note (scanned).</li>"
               "<li>Print edition: A4 trim with 3 mm bleed; pictures at or above 150 dpi at printed size (warnings list any "
               "below the 300 dpi print target).</li><li class=no>Open: heading font (Sitka) embeds as Type 3; switch to an "
               "open-licence static serif before the print proof.</li><li>Picture credits: photographs are CC BY / CC BY-SA "
               "with attribution; confirm with the publisher whether any licence needs a commercial-use check.</li></ul>")
    files = []
    for name, body in (("1-corrections-log", log_html), ("2-curriculum-coverage-matrix", cov_html),
                       ("3-editorial-publishing-qa-report", qa_html)):
        h = reports / f"{name}.html"
        h.write_text(body, encoding="utf-8")
        files += [h, _print(h, reports / f"{name}.pdf")]
    (reports / "totals.json").write_text(json.dumps(tot, indent=1), encoding="utf-8")
    return files


def main() -> None:
    a = argparse.ArgumentParser()
    a.add_argument("slug")
    x = a.parse_args()
    for f in build(x.slug):
        print(f)


if __name__ == "__main__":
    main()
