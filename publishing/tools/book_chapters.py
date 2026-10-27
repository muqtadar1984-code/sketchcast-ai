"""Draft a run of chapters end to end, one at a time, each to its own Gate D
packet (founder 2026-10-25: "draft Biology chapters 2-10 locally with the same
pipeline — draft, expand to 6-8 teaching pages with images, theme, render").

    draft → opener hero → expand (locked draft + new teaching + pictures) → render → packet

Every stage is skipped when its output already exists, so a run that stops (a
rate limit, a busy production queue) is resumed by running it again. A failed
chapter is recorded and the run moves on. Local only: the only production
traffic is reads and the image/text model calls the pipeline already makes.

    python -m publishing.tools.book_chapters <slug> 2 10 [--max-busy 3]
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import time
import traceback
from pathlib import Path

from publishing import localdb
from publishing.cli import DEFAULT_ENV
from publishing.report import OUT
from publishing.schemas import ROOT

log = logging.getLogger("publishing.book_chapters")
ASSETS = ROOT.parent.parent / "study-guide-sprint1" / "book-assets"
LONG_DRAFT_WORDS = 1700

HERO = """You art-direct the opening illustration of a chapter in a premium study guide for ages {age}.
Chapter {n}: {title}. What the chapter teaches: {purpose}
Describe ONE hero illustration for its opening page: beautiful, scientifically accurate, the chapter's central idea
at a glance, no text, letters, numbers or labels anywhere, nothing the chapter does not teach, nothing gory or
medical. One or two sentences an illustrator can follow exactly.
Reply with JSON: {{"subject": str}}"""


def hero(conn, doc: dict, env_file: Path, max_busy: int) -> dict:
    from publishing.blueprint import author_client
    from publishing.guard import production_queue
    from publishing.manuscript import book_assets
    from publishing.manuscript.theme import for_work
    ch, w = doc["chapter"], doc["work"]
    key = f"book:hero_ch{ch['number']:02d}"
    if conn.execute("select 1 from pub.book_assets where key=%s", (key,)).fetchone():
        return {"key": key, "existing": True}
    got = author_client("anthropic").analyze(
        HERO.format(age=w["age_band"], n=ch["number"], title=ch["title"],
                    purpose=ch.get("purpose") or doc["plan"].get("opening")), max_tokens=600)
    subject = ((got or {}).get("data") or {}).get("subject")
    if not subject:
        return {"key": key, "error": "no hero brief"}
    q = production_queue(env_file)
    if q["busy"] > max_busy:
        return {"key": key, "error": f"production busy ({q['busy']} {q['types']}); hero skipped"}
    return book_assets.generate(conn, key, f"chapter opener artwork: {ch['title']}",
                                subject + ", as a hero image for a chapter opening", ASSETS,
                                f"founder:book-run-ch{ch['number']:02d}", theme=for_work(w["slug"]))


def packet(out: Path, n: int, doc: dict, result: dict) -> Path:
    """One folder per chapter for Gate D: the PDFs, a contact sheet and a summary."""
    import fitz
    from PIL import Image
    p = out / f"ch{n:02d}-gate-d"
    p.mkdir(exist_ok=True)
    shutil.copy(result["pdf"], p / f"ch{n:02d}-digital.pdf")
    shutil.copy(result["print_pdf"], p / f"ch{n:02d}-print-bleed.pdf")
    shutil.copy(result["review"], p / f"ch{n:02d}-gate-d-review.html")
    pdf = fitz.open(result["pdf"])
    ims = []
    for pg in pdf:
        px = pg.get_pixmap(dpi=45)
        ims.append(Image.frombytes("RGB", (px.width, px.height), px.samples))
    w, h = ims[0].size
    cols, gap = 5, 12
    rows = -(-len(ims) // cols)
    sheet = Image.new("RGB", (cols * (w + gap) + gap, rows * (h + gap) + gap), (90, 96, 92))
    for i, im in enumerate(ims):
        sheet.paste(im, (gap + (i % cols) * (w + gap), gap + (i // cols) * (h + gap)))
    sheet.save(p / f"ch{n:02d}-contact-sheet.png")
    rv, g = doc["review"], doc["review"].get("gates") or {}
    words = sum(len(str(b.get(k, "")).split()) for s in doc["sections"] for b in s["blocks"]
                for k in ("text", "wrong", "right", "caption"))
    figs = sum(1 for s in doc["sections"] for b in s["blocks"] if b.get("type") in ("figure", "figure_group"))
    serious = [f for f in rv.get("findings") or [] if f.get("severity") in ("blocker", "major")]
    fails = [f for f in doc.get("checks") or [] if f.get("verdict") == "fail"]
    x = doc.get("expansion") or {}
    md = [f"# Chapter {n}: {doc['chapter']['title']} — Gate D packet", "",
          f"Local draft, not published. Pages: {result['pages']}. Hard layout defects: {result['hard_defects']}.",
          f"Teaching text: {words} words in {len(doc['sections'])} sections; {figs} figures; "
          f"{len(doc['exercises'])} exercises.", "",
          "## Gates", "",
          f"- Content: {g.get('content_gate')}  ·  Assessment: {g.get('assessment_gate')}  ·  "
          f"Visual: {g.get('visual_gate')}",
          f"- Ready for Gate D by the pipeline's own rule: **{rv.get('ready_for_gate_d')}**",
          f"- Deterministic check failures: {len(fails)}", ""]
    for f in fails:
        md.append(f"  - {f['unit']} {f['check']}: {str(f.get('detail'))[:200]}")
    md += ["", "## Reviewer blockers and majors (for your judgement)", ""]
    md += [f"- {f.get('unit')} [{f.get('severity')}] {str(f.get('detail'))[:300]}" for f in serious] or ["- none"]
    md += ["", "## Pictures needing a person", ""]
    md += [f"- {h}" for h in rv.get("human_review") or []] or ["- none"]
    md += ["", "## New illustrations", ""]
    md += [f"- {m.get('key')} (score {m.get('score')}): {m.get('teaches')}" for m in x.get("pictures_made") or []]
    md += [f"- REFUSED {m.get('key')}: {str(m.get('error'))[:200]}" for m in x.get("pictures_refused") or []]
    md += ["", f"Model cost for this chapter: ${sum(c.get('usd') or 0 for c in doc.get('costs') or []):.2f}"]
    (p / "SUMMARY.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    return p


def run(slug: str, first: int, last: int, env_file: Path, max_busy: int) -> list[dict]:
    from publishing.manuscript.author import draft_chapter, expand_chapter
    from publishing.manuscript.render import render
    out = OUT / "manuscripts" / slug
    status_file = out / f"book-run-ch{first:02d}-{last:02d}.json"
    status = json.loads(status_file.read_text(encoding="utf-8")) if status_file.exists() else {}
    for n in range(first, last + 1):
        st = status.setdefault(str(n), {})
        if st.get("done"):
            continue
        t0 = time.time()
        try:
            conn = localdb.connect()
            draft = out / f"ch{n:02d}-draft.json"
            if not draft.exists():
                log.info("ch%d: drafting", n)
                draft_chapter(conn, slug, n, env_file, out)
            doc = json.loads(draft.read_text(encoding="utf-8"))
            if "hero" not in st:
                st["hero"] = hero(conn, doc, env_file, max_busy)
                conn.commit()
            if not doc.get("expansion"):
                log.info("ch%d: expanding", n)
                words = sum(len(str(b.get(k, "")).split()) for sec in doc["sections"] for b in sec["blocks"]
                            for k in ("text", "wrong", "right", "caption"))
                # ~330 words fill a teaching page with its pictures: a draft already near 6 pages gets its
                # pictures and only the explanation they need, so no chapter runs far past 8 pages
                target = "6 to 8" if words < LONG_DRAFT_WORDS else (
                    "no more than 8 — the draft is already long, so add the new pictures and only the short "
                    "explanation each needs; add no other text")
                st["draft_words"] = words
                doc = expand_chapter(conn, slug, n, env_file, out, target, 3, max_busy)
            full = json.loads((out / f"ch{n:02d}-dossier-full.json").read_text(encoding="utf-8"))
            result = render(doc, full, env_file, out)
            doc = json.loads(draft.read_text(encoding="utf-8"))
            st.pop("error", None)
            st.pop("trace", None)
            st.update(done=True, packet=str(packet(out, n, doc, result)), pages=result["pages"],
                      hard_defects=result["hard_defects"], ready=doc["review"].get("ready_for_gate_d"),
                      minutes=round((time.time() - t0) / 60, 1))
            conn.close()
        except Exception as e:  # noqa: BLE001 — one chapter's failure must not stop the book
            st["error"] = f"{type(e).__name__}: {e}"[:500]
            st["trace"] = traceback.format_exc()[-2000:]
            log.error("ch%d failed: %s", n, st["error"])
        status_file.write_text(json.dumps(status, indent=1, default=str), encoding="utf-8")
        log.info("ch%d: %s", n, {k: v for k, v in st.items() if k != "trace"})
    return status


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("slug")
    p.add_argument("first", type=int)
    p.add_argument("last", type=int)
    p.add_argument("--max-busy", type=int, default=0)
    p.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    print(json.dumps(run(a.slug, a.first, a.last, a.env_file, a.max_busy), indent=1, default=str))


if __name__ == "__main__":
    main()
