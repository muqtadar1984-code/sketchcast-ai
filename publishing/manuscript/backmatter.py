"""Book-level sections (founder 2026-10-26, Revision 2): preface, curriculum
coverage, alphabetical glossary, index, and sources and picture credits.

Everything here is DERIVED from the manuscript and its records, never invented:
  glossary   the chapters' own key-term boxes (first definition wins), with the
             chapters each term is defined or used in;
  index      the glossary terms and the chapters' bold vocabulary, with the
             pages (from the compiled chapters) where each appears in teaching
             pages — practice and answer pages are not indexed;
  coverage   rev2/coverage-matrix.json from the independent mapping;
  sources    the curriculum framework the book follows, and every photograph's
             credit from the chapters' picture credits. No further-reading list
             is invented: a reference that cannot be verified is not printed.
The preface is a short text drafted once (rev2/preface.json) from facts about
the book, for the founder to edit.
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path


def glossary(docs: list[tuple[int, dict]]) -> list[dict]:
    out: dict[str, dict] = {}
    for n, doc in docs:
        for s in doc["sections"]:
            for b in s.get("blocks") or []:
                if b.get("type") == "key_term" and b.get("term"):
                    k = b["term"].strip().lower()
                    e = out.setdefault(k, {"term": b["term"].strip(), "meaning": b.get("text", "").strip(), "chapters": []})
                    if n not in e["chapters"]:
                        e["chapters"].append(n)
    return sorted(out.values(), key=lambda e: e["term"].lower())


def index_terms(docs: list[tuple[int, dict]], gloss: list[dict]) -> list[str]:
    terms = {e["term"] for e in gloss}
    for _, doc in docs:
        for s in doc["sections"]:
            for b in s.get("blocks") or []:
                for w in re.findall(r"\*\*([^*]{3,40})\*\*", str(b.get("text") or "")):
                    if not w.lower().endswith("ly"):
                        terms.add(w.strip())
    seen, out = set(), []
    for t in sorted(terms, key=str.lower):
        k = t.lower().rstrip("s")
        if k not in seen:
            seen.add(k)
            out.append(t)
    return out


def index_pages(terms: list[str], chapter_pdfs: list[tuple[int, Path, int]]) -> dict[str, list[int]]:
    """term → book page numbers on TEACHING pages (opener to the page before Practice)."""
    import fitz
    pages: dict[str, list[int]] = {t: [] for t in terms}
    for n, pdf, start in chapter_pdfs:
        d = fitz.open(pdf)
        stop = next((i for i, pg in enumerate(d) if "Work through the questions in order" in pg.get_text()), len(d))
        for i in range(stop):
            text = d[i].get_text().lower().replace("\n", " ")
            for t in terms:
                if re.search(r"\b" + re.escape(t.lower()) + r"s?\b", text):
                    pages[t].append(start + i)
    return {t: sorted(set(p))[:6] for t, p in pages.items() if p}


def back_html(work: dict, theme: dict, gloss: list[dict], index: dict[str, list[int]], matrix: dict,
              coverage: dict, credits: list[str], first_page: int, edition: str) -> str:
    from publishing.manuscript.render import _pretty_curriculum
    from publishing.manuscript.theme import css_vars, page
    p = page(theme)
    b = p["bleed_mm"] if edition == "print" else 0
    W, H = p["trim"]["width_mm"] + 2 * b, p["trim"]["height_mm"] + 2 * b
    m = p["margins_mm"]
    folio = f"font: 9pt {p['folio']['font']}; color: {theme['brand_dark']};"
    esc = html.escape
    curriculum = _pretty_curriculum(work.get("curriculum"))
    # coverage: the book's own content objectives, any other code that is covered, then TWS / SIC
    book_codes = sorted({c for ch in coverage.values() for c in ch.get("blueprint") or []})
    def rows(codes):
        return "".join(
            f"<tr><td class='c'>{esc(c)}</td><td>{esc(matrix[c]['text'])}</td>"
            f"<td>{esc(', '.join(matrix[c]['taught']) or '—')}</td><td>{esc(', '.join(matrix[c]['assessed'][:8]) or '—')}"
            f"{' …' if len(matrix[c]['assessed']) > 8 else ''}</td><td class='st {matrix[c]['status'].split()[0]}'>"
            f"{esc(matrix[c]['status'])}</td></tr>" for c in codes if c in matrix)
    content = [c for c in book_codes if c in matrix]
    other_content = [c for c, v in matrix.items() if c not in content and "TWS" not in c and "SIC" not in c
                     and v["status"] != "not covered"]
    tws = [c for c, v in matrix.items() if "TWS" in c and v["status"] != "not covered"]
    sic = [c for c, v in matrix.items() if "SIC" in c and v["status"] != "not covered"]
    tws_missing = [c for c, v in matrix.items() if "TWS" in c and v["status"] == "not covered"]
    cross = list({x.get("code"): x for ch in reversed(list(coverage.values()))
                  for x in (ch.get("map") or {}).get("cross_subject") or []}.values())
    head = "<tr><th>Code</th><th>Objective</th><th>Taught in (chapter.section)</th><th>Assessed in</th><th>Status</th></tr>"
    cov = (f"<h2>Curriculum coverage</h2><p class='lead'>How this book covers the {esc(curriculum)} objectives it "
           "targets. 'Taught in' gives chapter.section; 'Assessed in' gives practice questions. Mapped independently "
           "of the book's author.</p>"
           f"<h3>Biology content objectives</h3><table class='cov'>{head}{rows(content)}</table>"
           + (f"<h3>Other objectives the book touches</h3><table class='cov'>{head}{rows(other_content)}</table>" if other_content else "")
           + ("<div class='note'><strong>Cross-subject links.</strong> " + " ".join(
               f"{esc(x.get('code'))}: {esc(x.get('why'))}" for x in cross) + "</div>" if cross else "")
           + f"<h3>Thinking and Working Scientifically</h3><table class='cov'>{head}{rows(tws)}</table>"
           + (f"<p class='small'>Not addressed in this book: {esc(', '.join(tws_missing))}.</p>" if tws_missing else "")
           + (f"<h3>Science in Context</h3><table class='cov'>{head}{rows(sic)}</table>" if sic else ""))
    gl = "".join(f"<div class='g'><span class='t'>{esc(e['term'])}</span> {esc(e['meaning'])} "
                 f"<span class='ch'>Ch {', '.join(map(str, e['chapters']))}</span></div>" for e in gloss)
    letters, idx = "", []
    for t in sorted(index, key=str.lower):
        L = t[0].upper()
        if L != letters:
            letters = L
            idx.append(f"<div class='L'>{esc(L)}</div>")
        idx.append(f"<div class='i'>{esc(t)}<span>{', '.join(map(str, index[t]))}</span></div>")
    src = ("<h2>Sources and picture credits</h2>"
           f"<p><strong>Curriculum.</strong> Cambridge Assessment International Education, <em>Cambridge Lower Secondary "
           f"Science Curriculum Framework (0893)</em>. Objective codes and wording in this book follow that framework.</p>"
           "<p><strong>Text and illustrations.</strong> The teaching text, questions and illustrations are original "
           "SketchCast work unless credited below.</p>"
           "<p><strong>Photographs.</strong> Used under the licences named, with their authors credited:</p><ul>"
           + "".join(f"<li>{c}</li>" for c in credits) + "</ul>")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Back matter</title><style>
{css_vars(theme)}
@page {{ size: {W}mm {H}mm; margin: {m['top'] + b}mm {m['outer'] + b}mm {m['bottom'] + b}mm {m['inner'] + b}mm;
  background-color: {p['paper']}; }}
@page :left {{ margin-left: {m['outer'] + b}mm; margin-right: {m['inner'] + b}mm; @bottom-left {{ content: counter(page); {folio} }} }}
@page :right {{ margin-left: {m['inner'] + b}mm; margin-right: {m['outer'] + b}mm; @bottom-right {{ content: counter(page); {folio} }} }}
html {{ counter-reset: page {first_page - 1}; print-color-adjust: exact; -webkit-print-color-adjust: exact; }}
body {{ margin: 0; font-family: var(--bfont); color: var(--ink); font-size: 9.6pt; line-height: 1.45; }}
section {{ break-before: page; }}
h2 {{ font: 600 20pt var(--hfont); color: var(--brand-dark); margin: 0 0 4mm; border-bottom: .8pt solid var(--line); padding-bottom: 2mm; }}
h3 {{ font: 600 8.6pt var(--bfont); letter-spacing: .14em; text-transform: uppercase; color: var(--brand); margin: 6mm 0 2mm; }}
.lead {{ color: var(--muted); }} .small {{ font-size: 8pt; color: var(--muted); }}
table.cov {{ width: 100%; border-collapse: collapse; font-size: 7.8pt; line-height: 1.35; }}
table.cov th {{ text-align: left; background: var(--soft); color: var(--brand-dark); padding: 1.4mm 2mm; }}
table.cov td {{ padding: 1.2mm 2mm; border-bottom: .4pt solid var(--line); vertical-align: top; }}
table.cov tr {{ break-inside: avoid; }} td.c {{ font-weight: 700; color: var(--brand-dark); white-space: nowrap; }}
td.st {{ white-space: nowrap; }} td.st.not {{ color: #a3542b; font-weight: 600; }} td.st.taught {{ color: var(--brand); }}
.note {{ background: var(--soft); border-left: 2.4pt solid var(--brand); padding: 2.5mm 4mm; margin: 4mm 0; font-size: 8.6pt; }}
.gl {{ columns: 2; column-gap: 8mm; }} .g {{ break-inside: avoid; margin: 0 0 2.2mm; }}
.g .t {{ font-weight: 700; color: var(--brand-dark); }} .g .ch {{ color: var(--muted); font-size: 8pt; white-space: nowrap; }}
.ix {{ columns: 3; column-gap: 6mm; font-size: 8.8pt; }} .ix .L {{ font: 700 11pt var(--hfont); color: var(--brand); margin: 3mm 0 1mm; break-after: avoid; }}
.ix .i {{ display: flex; justify-content: space-between; gap: 2mm; break-inside: avoid; }} .ix .i span {{ color: var(--muted); }}
ul {{ padding-left: 4mm; font-size: 8.6pt; }}
</style></head><body>
<section style="break-before: auto">{cov}</section>
<section><h2>Glossary</h2><div class="gl">{gl}</div></section>
<section><h2>Index</h2><p class="small">Page numbers refer to the teaching pages of each chapter.</p><div class="ix">{''.join(idx)}</div></section>
<section>{src}</section>
</body></html>"""


PREFACE = """Write the preface of a study guide. Facts (use only these; invent nothing):
Title: {title}. Curriculum: {curriculum}. Ages: {age}. Publisher of the guide: SketchCast.
Chapters: {chapters}.
Each chapter has short numbered sections, key-term boxes, key ideas, common mistakes, Think-about-it questions,
practice in three levels (Getting started, On your own, Challenge) with room to write, and answers with worked
solutions and mark points at the end of the chapter. The book ends with curriculum coverage, a glossary and an index.
Write about 250 words for learners and their teachers or parents: what the book is for, how it is built, and how
to use it well (read, try the Think questions before checking, practise, check answers honestly, use the glossary).
Plain, warm, direct. No headings. No claims about results or endorsements.
Reply with JSON: {{"preface": str}}"""


def preface(slug: str, docs: list[tuple[int, dict]], out: Path) -> str:
    f = out / "rev2" / "preface.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))["preface"]
    from publishing.blueprint import author_client
    from publishing.manuscript.render import _pretty_curriculum
    w = docs[0][1]["work"]
    got = author_client("anthropic").analyze(PREFACE.format(
        title=w["title"], curriculum=_pretty_curriculum(w.get("curriculum")), age=w["age_band"],
        chapters="; ".join(f"{n}. {d['chapter']['title']}" for n, d in docs)), max_tokens=1500)["data"]["preface"]
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"preface": got, "status": "draft for the founder to edit"}, indent=1, ensure_ascii=False),
                 encoding="utf-8")
    return got
