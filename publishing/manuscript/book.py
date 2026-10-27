"""Compile a whole book from its rendered chapters: cover, title page, imprint,
contents, an "about this book" page, then every chapter with page numbers
running on through the book, each chapter opening on a right-hand page.

Each chapter is re-printed with its book page numbers from its saved draft,
in the layout the chapter's own layout search chose — nothing is re-written.
The front matter is drawn from the work record and the theme; a manuscript
label (e.g. "First draft manuscript") is printed on the title page and imprint.

    python -m publishing.manuscript.book <slug> 1 10 --label "First draft manuscript" --prepared-for "…"
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import logging
from datetime import date
from pathlib import Path

log = logging.getLogger("publishing.book")


def _chapter_pdf(doc: dict, full: dict, env_file: Path, out: Path, first_page: int, edition: str) -> Path:
    from publishing.manuscript.render import _alternatives, _figures, _print, _underlay, annotations_for, chapter_html
    n = doc["chapter"]["number"]
    chosen = (doc.get("layout_choice") or {}).get("chosen", "as authored")
    cand = next((c for name, c in _alternatives(doc) if name == chosen), doc)
    page = out / f"book-ch{n:02d}-{edition}.html"
    page.write_text(chapter_html(cand, _figures(cand, full, env_file, annotations_for(doc["work"]["slug"])), edition,
                                 first_page), encoding="utf-8")
    pdf = _print(page, out / f"book-ch{n:02d}-{edition}.pdf")
    _underlay(pdf, doc["work"]["slug"], n, edition)
    return pdf


def _front_html(work: dict, theme: dict, chapters: list[dict], label: str, prepared_for: str,
                edition: str, hero: str | None, preface: str = "", back: list[tuple[str, int]] = ()) -> str:
    from publishing.manuscript.render import _pretty_curriculum
    from publishing.manuscript.theme import css_vars, page
    p = page(theme)
    b = p["bleed_mm"] if edition == "print" else 0
    W, H = p["trim"]["width_mm"] + 2 * b, p["trim"]["height_mm"] + 2 * b
    m = p["margins_mm"]
    title, _, sub = str(work["title"]).replace(" - ", " – ").partition(":")
    today = date.today().strftime("%B %Y")
    toc = "".join(f'<tr><td class="n">{c["number"]}</td><td>{html.escape(c["title"])}</td>'
                  f'<td class="pg">{c["start"]}</td></tr>' for c in chapters)
    toc += "".join(f'<tr><td class="n"></td><td>{html.escape(t)}</td><td class="pg">{pg}</td></tr>' for t, pg in back)
    pre = ("<section class='preface'><h2>Preface</h2>" + "".join(
        f"<p>{html.escape(x)}</p>" for x in preface.splitlines() if x.strip()) + "</section>") if preface else ""
    hero_img = f'<img class="cover-hero" src="{hero}" alt="">' if hero else ""
    curriculum = html.escape(_pretty_curriculum(work.get("curriculum")))
    from publishing.tools.academic_audit import curriculum as _cur
    note = _cur(work.get("curriculum")).get("alignment_note")
    alignment = f"<p>{html.escape(note)}</p>" if note else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{html.escape(work['title'])}</title><style>
{css_vars(theme)}
@page {{ size: {W}mm {H}mm; margin: {m['top'] + b}mm {m['outer'] + b}mm {m['bottom'] + b}mm {m['inner'] + b}mm;
  background-color: {p['paper']}; }}
@page cover {{ margin: 0; background-color: {p['paper_deep']}; }}
html {{ print-color-adjust: exact; -webkit-print-color-adjust: exact; }}
body {{ margin: 0; font-family: var(--bfont); color: var(--ink); font-size: 10.5pt; line-height: 1.55; }}
section {{ break-after: page; }}
.cover {{ page: cover; height: {H}mm; position: relative; overflow: hidden; }}
.cover .band {{ position: absolute; left: 0; right: 0; bottom: 0; height: {92 + b}mm; background: var(--brand-dark); }}
.cover .inner {{ position: absolute; left: {22 + b}mm; right: {22 + b}mm; top: {30 + b}mm; }}
.cover .kicker {{ font: 600 9pt var(--bfont); letter-spacing: .26em; text-transform: uppercase; color: var(--brand); }}
.cover h1 {{ font: 700 40pt/1.08 var(--hfont); color: var(--brand-dark); margin: 4mm 0 3mm; }}
.cover .sub {{ font: italic 17pt/1.3 var(--hfont); color: var(--ink); margin: 0 0 6mm; max-width: 150mm; }}
.cover .meta {{ font-size: 10pt; color: var(--muted); }}
.cover-hero {{ position: absolute; right: {16 + b}mm; top: {96 + b}mm; width: 104mm; }}
.cover .foot {{ position: absolute; left: {22 + b}mm; right: {22 + b}mm; bottom: {16 + b}mm; color: {p['paper']};
  display: flex; justify-content: space-between; align-items: flex-end; font-size: 10pt; }}
.cover .foot .brand {{ font: 700 15pt var(--hfont); letter-spacing: .02em; }}
.cover .draft {{ border: .8pt solid {p['paper']}; border-radius: 10pt; padding: 1mm 4mm; font-size: 8.6pt;
  letter-spacing: .14em; text-transform: uppercase; }}
.title h1 {{ font: 700 28pt/1.15 var(--hfont); color: var(--brand-dark); margin: 40mm 0 4mm; }}
.title .sub {{ font: italic 15pt/1.35 var(--hfont); margin: 0 0 12mm; }}
.title .rule {{ width: 40mm; border-top: 2pt solid var(--accent); margin: 0 0 8mm; }}
.title p {{ color: var(--muted); margin: 0 0 2mm; }}
.label {{ display: inline-block; margin-top: 22mm; border: .8pt solid var(--brand); color: var(--brand); border-radius: 10pt;
  padding: 1mm 4mm; font-size: 8.6pt; letter-spacing: .14em; text-transform: uppercase; }}
.imprint {{ font-size: 8.8pt; color: var(--muted); padding-top: 150mm; }}
.imprint p {{ margin: 0 0 3mm; }}
h2 {{ font: 600 20pt var(--hfont); color: var(--brand-dark); margin: 0 0 8mm; border-bottom: .8pt solid var(--line); padding-bottom: 3mm; }}
table.toc {{ width: 100%; border-collapse: collapse; font-size: 11pt; }}
table.toc td {{ padding: 3mm 0; border-bottom: .5pt solid var(--line); vertical-align: baseline; }}
table.toc td.n {{ width: 14mm; font: 700 14pt var(--hfont); color: var(--brand); }}
table.toc td.pg {{ width: 14mm; text-align: right; color: var(--brand-dark); font-weight: 600; }}
.about h3 {{ font: 600 9pt var(--bfont); letter-spacing: .16em; text-transform: uppercase; color: var(--brand); margin: 6mm 0 2mm; }}
.about .row {{ display: grid; grid-template-columns: 42mm 1fr; gap: 4mm; margin: 0 0 3mm; align-items: start; }}
.preface p {{ font-size: 10.5pt; line-height: 1.6; margin: 0 0 3mm; }}
.chip {{ font-size: 8.4pt; font-weight: 600; letter-spacing: .1em; text-transform: uppercase; padding: 1.4mm 3mm;
  border-radius: 3pt; }}
</style></head><body>
<section class="cover"><div class="band"></div><div class="inner">
<div class="kicker">{html.escape(title.strip())}</div><h1>{html.escape(sub.strip() or title.strip())}</h1>
<p class="sub">A study guide for ages {html.escape(str(work.get('age_band')).replace('-', '–'))}</p>
<div class="meta">{curriculum}</div></div>{hero_img}
<div class="foot"><span class="brand">SketchCast</span><span class="draft">{html.escape(label)}</span></div></section>
<section></section>
<section class="title"><h1>{html.escape(title.strip())}</h1><p class="sub">{html.escape(sub.strip())}</p>
<div class="rule"></div><p>{curriculum}</p><p>Ages {html.escape(str(work.get('age_band')).replace('-', '–'))}</p>
<p>SketchCast</p><div class="label">{html.escape(label)} · {today}</div></section>
<section class="imprint">
<p><strong>{html.escape(str(work['title']).replace(' - ', ' – '))}</strong></p>
<p>{html.escape(label)}, {today}. Prepared by SketchCast{(' for ' + html.escape(prepared_for)) if prepared_for else ''}.
This is a working draft shared for review and discussion. It is not for sale or further distribution, and its
text, pictures and page design will change before publication.</p>
<p>© {date.today().year} SketchCast. All rights reserved.</p>
<p>Illustrations are original SketchCast artwork. Photographs are used under the licences named in each chapter's
picture credits, with their authors credited.</p>
<p>Curriculum references: {curriculum}. Learning objective codes are shown at the start of each chapter.</p>{alignment}</section>
{pre}
<section><h2>Contents</h2><table class="toc">{toc}</table></section>
<section class="about"><h2>How this book works</h2>
<p>Each chapter teaches one part of the curriculum, in short numbered sections, and ends with practice and answers.</p>
<h3>In each chapter</h3>
<div class="row"><span class="chip" style="background:var(--soft);color:var(--brand-dark)">Key term</span>
<span>A word you need, with its meaning, the first time it is used.</span></div>
<div class="row"><span class="chip" style="border:.8pt solid var(--accent);color:var(--accent)">Key idea</span>
<span>One sentence worth remembering.</span></div>
<div class="row"><span class="chip" style="background:var(--warn);color:var(--warn-edge)">Common mistake</span>
<span>A wrong idea many learners have, crossed out, and the correct idea.</span></div>
<div class="row"><span class="chip" style="border:.8pt dashed var(--think);color:var(--think)">Think about it</span>
<span>A question to stop and think about. Its answer is at the end of the chapter.</span></div>
<div class="row"><span class="chip" style="background:var(--ext);color:#5d7280">Going further</span>
<span>A short, optional look beyond the curriculum. It is not assessed.</span></div>
<h3>Practice</h3>
<p>Questions come in three levels: <strong>Getting started</strong> ●○○, <strong>On your own</strong> ●●○ and
<strong>Challenge</strong> ●●●. Written answers have lines to work on, sized to the marks. Answers, worked solutions
and mark points follow each chapter.</p></section>
</body></html>"""


def compile_book(slug: str, first: int, last: int, env_file: Path, label: str, prepared_for: str,
                 edition: str = "digital") -> Path:
    import fitz
    from publishing.manuscript.render import _print, hero_image
    from publishing.manuscript.theme import for_work, page
    from publishing.report import OUT
    out = OUT / "manuscripts" / slug
    build = out / "book"
    build.mkdir(exist_ok=True)
    theme = for_work(slug)
    docs = []
    for n in range(first, last + 1):
        f = out / f"ch{n:02d}-draft.json"
        if f.exists():
            docs.append((n, json.loads(f.read_text(encoding="utf-8")),
                         json.loads((out / f"ch{n:02d}-dossier-full.json").read_text(encoding="utf-8"))))
        else:
            log.warning("chapter %d has no draft yet; left out", n)
    # chapters first (their lengths set the contents page numbers); each opens on a right-hand (odd) page
    parts, chapters, at = [], [], 1
    seen_videos: set[str] = set()
    for n, doc, full in docs:
        # a lesson video is printed with the first chapter it belongs to, never repeated (founder 2026-10-26)
        doc = {**doc, "videos": [v for v in doc.get("videos") or [] if v not in seen_videos]}
        seen_videos |= set(doc["videos"])
        if at % 2 == 0:
            parts.append("blank")
            at += 1
        pdf = _chapter_pdf(doc, full, env_file, build, at, edition)
        pages = len(fitz.open(pdf))
        chapters.append({"number": n, "title": doc["chapter"]["title"], "start": at, "pages": pages})
        parts.append(pdf)
        at += pages
    work = docs[0][1]["work"]
    # back matter: coverage, glossary, index, sources — derived from the chapters (see backmatter.py)
    from publishing.manuscript import backmatter as BM
    from publishing.manuscript.render import credit_line
    pairs = [(n, d) for n, d, _ in docs]
    gloss = BM.glossary(pairs)
    pdf_of = {c["number"]: next(Path(x) for x in parts if x != "blank" and Path(x).name.startswith(f"book-ch{c['number']:02d}"))
              for c in chapters}
    index = BM.index_pages(BM.index_terms(pairs, gloss), [(c["number"], pdf_of[c["number"]], c["start"]) for c in chapters])
    # the current revision's coverage (PUBLISH_REV), else the latest earlier one (founder 2026-10-26: Revision 4
    # printed Revision 3's table because this was fixed to rev3)
    rev2 = next((out / r for r in (os.getenv("PUBLISH_REV", ""), "rev4", "rev3", "rev2")
                 if r and (out / r / "coverage-matrix.json").exists()), out / "rev2")
    matrix = json.loads((rev2 / "coverage-matrix.json").read_text(encoding="utf-8")) if (rev2 / "coverage-matrix.json").exists() else {}
    coverage = json.loads((rev2 / "coverage.json").read_text(encoding="utf-8")) if (rev2 / "coverage.json").exists() else {}
    credits = []
    for n, d, full in docs:
        used = set()
        for s_ in d["sections"]:
            for blk in s_["blocks"]:
                used.add(blk.get("ref"))
                used |= {i.get("ref") for i in blk.get("items") or [] if isinstance(i, dict)}
        for x in d["exercises"]:
            used |= {x.get("figure_ref"), *(x.get("figure_refs") or [])}
        for a_ in full.get("library_assets") or []:
            if a_["id"] in used and a_.get("credit") and a_.get("kind") == "photo":
                title = (a_.get("identity") or {}).get("source_title") or a_["credit"].get("title")
                cl = credit_line({**a_["credit"], "title": title})
                if cl not in credits:
                    credits.append(cl)
    back_start = at + (1 if at % 2 == 0 else 0)
    back_pdf, back = None, []
    if matrix:
        bh = build / f"back-{edition}.html"
        bh.write_text(BM.back_html(work, theme, gloss, index, matrix, coverage, credits, back_start, edition),
                      encoding="utf-8")
        back_pdf = fitz.open(_print(bh, build / f"back-{edition}.pdf"))
        for title in ("Curriculum coverage", "Glossary", "Index", "Sources and picture credits"):
            # a page's text begins with its folio ("183 Curriculum coverage …"), so the number is skipped first
            i = next((k for k, pg in enumerate(back_pdf)
                      if re.sub(r"^\s*\d+\s*", "", pg.get_text()).startswith(title)), None)
            if i is not None:
                back.append((title, back_start + i))
    pref = BM.preface(slug, pairs, out) if matrix else ""
    front_html = build / f"front-{edition}.html"
    front_html.write_text(_front_html(work, theme, chapters, label, prepared_for, edition, hero_image(slug, 1),
                                      pref, back), encoding="utf-8")
    front = fitz.open(_print(front_html, build / f"front-{edition}.pdf"))

    p = page(theme)
    book = fitz.open()
    book.insert_pdf(front)
    if len(book) % 2:                       # chapter 1 opens on a right-hand page
        _blank(book, p, edition)
    contents_at = next(i for i, pg in enumerate(front) if pg.get_text().lstrip().startswith("Contents")) + 1
    preface_at = next((i + 1 for i, pg in enumerate(front) if pg.get_text().lstrip().startswith("Preface")), None)
    toc = [[1, "Cover", 1]] + ([[1, "Preface", preface_at]] if preface_at else []) + [[1, "Contents", contents_at]]
    toc.sort(key=lambda t: t[2])
    for part in parts:
        if part == "blank":
            _blank(book, p, edition)
            continue
        start = len(book) + 1
        book.insert_pdf(fitz.open(part))
        c = next(c for c in chapters if Path(part).name.startswith(f"book-ch{c['number']:02d}"))
        toc.append([1, f"Chapter {c['number']}: {c['title']}", start])
    if back_pdf is not None:
        if len(book) % 2:                   # the back matter opens on a right-hand page too
            _blank(book, p, edition)
        offset = len(book) + 1 - back_start
        book.insert_pdf(back_pdf)
        toc += [[1, t, pg + offset] for t, pg in back]
    book.set_toc(toc)
    if edition == "print":                  # declare the trim inside the bleed, so a printer's preflight sees both
        bl = p["bleed_mm"] * 72 / 25.4
        for pg in book:
            pg.set_bleedbox(pg.mediabox)
            pg.set_trimbox(fitz.Rect(pg.mediabox.x0 + bl, pg.mediabox.y0 + bl, pg.mediabox.x1 - bl, pg.mediabox.y1 - bl))
    book.set_metadata({"title": work["title"], "author": "SketchCast", "subject": label,
                       "keywords": "; ".join(k for k in (label, prepared_for) if k)})
    name = f"{slug}-{label.lower().replace(' ', '-')}-{edition}.pdf"
    dest = out / name
    book.save(dest, garbage=3, deflate=True)
    (build / "chapters.json").write_text(json.dumps(chapters, indent=1), encoding="utf-8")
    log.info("book: %d pages → %s", len(book), dest)
    return dest


def _blank(book, p: dict, edition: str) -> None:
    """A plain paper page (no folio), so the next chapter opens on a right-hand page."""
    import fitz
    b = p["bleed_mm"] if edition == "print" else 0
    mm = 72 / 25.4
    W, H = (p["trim"]["width_mm"] + 2 * b) * mm, (p["trim"]["height_mm"] + 2 * b) * mm
    pg = book.new_page(width=W, height=H)
    c = tuple(int(p["paper"][i:i + 2], 16) / 255 for i in (1, 3, 5))
    pg.draw_rect(pg.rect, color=None, fill=c)


def main() -> None:
    from publishing.cli import DEFAULT_ENV
    a = argparse.ArgumentParser()
    a.add_argument("slug")
    a.add_argument("first", type=int)
    a.add_argument("last", type=int)
    a.add_argument("--label", default="First draft manuscript")
    a.add_argument("--prepared-for", default="")
    a.add_argument("--edition", default="digital", choices=("digital", "print"))
    a.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    x = a.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    print(compile_book(x.slug, x.first, x.last, x.env_file, x.label, x.prepared_for, x.edition))


if __name__ == "__main__":
    main()
