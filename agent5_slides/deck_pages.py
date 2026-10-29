"""A deck's pages for the Classroom Board (founder, 2026-10-28).

"The pptx loaded on the Board, each slide as 1 page … and later a teacher
highlights a portion and asks SK to explain it — for that the document must
be loaded." So every deck gets, next to its .pptx:

* one PNG per slide (artifact kind ``slide_png``, in the enum since 0001), and
* ONE text map per deck, ``deck_pages/pages.json`` (artifact kind
  ``deck_pages_json``, app migration 0166): every slide's title, speaker
  notes, words in reading order and the box of each word and text block.

The boxes come from the RENDERED PDF, not from the pptx shapes, so they are
where the text actually landed (autofit, wrapping, the renderer's own font
metrics). They are normalised 0..1 to the PDF page, which is the same slide
the PNG shows, so the Board maps a highlight on the PNG to words at any
display size.

One renderer only: ``director.qa.render_images`` (PowerPoint COM on Windows,
LibreOffice → PDF → pdftoppm on Railway). It is injected so tests never need
a real one.

Layout in the ``artifacts`` bucket, beside the deck::

    {owner_id}/{generation_id}/deck.pptx               (deck_pptx, unchanged)
    {owner_id}/{generation_id}/deck_pages/slide_01.png (slide_png, one per slide)
    {owner_id}/{generation_id}/deck_pages/pages.json   (deck_pages_json)
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

MAP_VERSION = 1
MAX_NOTES = 4000
MAX_TEXT = 6000
MAX_WORDS = 1500
MAX_BLOCKS = 300
MAX_BLOCK_TEXT = 2000
MAX_TITLE = 300

PAGES_DIR = "deck_pages"
PAGES_JSON = "pages.json"
KIND_PNG = "slide_png"
KIND_MAP = "deck_pages_json"     # app migration 0166

# The build's budget for the best-effort export (the deck job must never wait
# long for a bonus); the render-only job gets longer.
BUILD_RENDER_SECONDS = 120
JOB_RENDER_SECONDS = 300


class DeckRenderUnavailable(RuntimeError):
    """No PNGs came out of the renderer (none installed, it failed, or it ran
    past its time budget). Carries the renderer's own reason."""


# ── the text map ──────────────────────────────────────────────────────


def _norm_box(x0: float, y0: float, x1: float, y1: float, rect) -> list[float]:
    w = float(rect.width) or 1.0
    h = float(rect.height) or 1.0
    ox, oy = float(rect.x0), float(rect.y0)

    def c(v: float) -> float:
        return round(min(1.0, max(0.0, v)), 4)

    return [c((x0 - ox) / w), c((y0 - oy) / h), c((x1 - ox) / w), c((y1 - oy) / h)]


def _png_size(path: Path) -> tuple[int, int]:
    from PIL import Image

    with Image.open(path) as im:
        return int(im.width), int(im.height)


def _clean(s: str) -> str:
    return re.sub(r"[ \t\r\f\v]+", " ", str(s or "")).strip()


def _pptx_slides(pptx_path: Optional[Path]) -> list[dict]:
    """Title + notes of every slide the renderer shows (hidden slides are
    left out of a PDF export, so they are left out here too)."""
    if not pptx_path or not Path(pptx_path).exists():
        return []
    from pptx import Presentation

    out = []
    for slide in Presentation(str(pptx_path)).slides:
        if slide._element.get("show") == "0":
            continue
        title = ""
        try:
            t = slide.shapes.title
            if t is not None and t.has_text_frame:
                title = _clean(t.text_frame.text.replace("\n", " ").replace("\x0b", " "))
        except Exception:  # noqa: BLE001 — a malformed placeholder is just no title
            title = ""
        notes = ""
        if slide.has_notes_slide:
            try:
                notes = (slide.notes_slide.notes_text_frame.text or "").strip()
            except Exception:  # noqa: BLE001
                notes = ""
        out.append({"title": title, "notes": notes})
    return out


def _largest_line(page) -> str:
    """The slide's title when the pptx has no title placeholder (the
    storyboard and Director decks draw theirs as text boxes): the line set
    in the largest type, the first such line on a tie."""
    best, best_size = "", 0.0
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            text = _clean("".join(s.get("text", "") for s in spans))
            if not text:
                continue
            size = max(float(s.get("size") or 0) for s in spans)
            if size > best_size + 0.01:
                best, best_size = text, size
    return best


def build_text_map(pdf_path: Path, pptx_path: Optional[Path], png_paths: list[Path],
                   renderer: str = "", part: int = 1, first_n: int = 1) -> dict:
    """The deck's text map, pure over the files. ``png_paths[i]`` is page
    ``i`` of the PDF; a count mismatch means the two do not show the same
    slides, and a map that points at the wrong picture is worse than none."""
    import fitz  # PyMuPDF

    pngs = [Path(p) for p in png_paths]
    doc = fitz.open(str(pdf_path))
    try:
        if doc.page_count != len(pngs):
            raise ValueError(f"deck pages: the PDF has {doc.page_count} page(s) but the renderer "
                             f"made {len(pngs)} PNG(s)")
        meta = _pptx_slides(pptx_path)
        if meta and len(meta) != doc.page_count:
            logger.warning("deck pages: the pptx shows %d slide(s), the PDF %d page(s); titles and "
                           "notes are taken from the PDF only", len(meta), doc.page_count)
            meta = []
        slides = []
        for i, page in enumerate(doc):
            rect = page.rect
            raw = page.get_text("words", sort=True)
            words, lines, cur, key = [], [], [], None
            for x0, y0, x1, y1, w, block_no, line_no, _ in raw:
                if not str(w).strip():
                    continue
                if key is not None and (block_no, line_no) != key and cur:
                    lines.append(" ".join(cur))
                    cur = []
                key = (block_no, line_no)
                cur.append(str(w))
                if len(words) < MAX_WORDS:
                    words.append({"t": str(w), "b": _norm_box(x0, y0, x1, y1, rect)})
            if cur:
                lines.append(" ".join(cur))
            blocks = []
            for b in page.get_text("blocks", sort=True):
                x0, y0, x1, y1, text = b[0], b[1], b[2], b[3], b[4]
                btype = b[6] if len(b) > 6 else 0
                text = "\n".join(_clean(t) for t in str(text or "").splitlines() if _clean(t))
                if btype != 0 or not text:
                    continue
                blocks.append({"t": text[:MAX_BLOCK_TEXT], "b": _norm_box(x0, y0, x1, y1, rect)})
                if len(blocks) >= MAX_BLOCKS:
                    break
            m = meta[i] if meta else {}
            title = (m.get("title") or _largest_line(page) or "")[:MAX_TITLE]
            w_px, h_px = _png_size(pngs[i])
            slides.append({
                "n": first_n + i,
                "part": int(part),
                "png": f"slide_{first_n + i:02d}.png",
                "w": w_px,
                "h": h_px,
                "title": title,
                "notes": (m.get("notes") or "")[:MAX_NOTES],
                "text": "\n".join(lines)[:MAX_TEXT],
                "words": words,
                "blocks": blocks,
            })
    finally:
        doc.close()
    return {"v": MAP_VERSION, "renderer": renderer, "slides": slides}


# ── render ────────────────────────────────────────────────────────────


def _default_renderer(timeout: int) -> Callable:
    from agent5_slides.director.qa import render_images

    def run(pptx, out_dir, pdf=None):
        return render_images(pptx, out_dir, pdf=pdf, timeout=timeout)

    return run


def reuse_qa_render(pptx_path: Path) -> Optional[tuple[list[Path], Path, str]]:
    """The Director's last QA render of THIS pptx, if it is still on disk:
    ``{deck dir}/director/qa_N/`` holding the slide PNGs and the PDF
    LibreOffice made on the way (PowerPoint makes none there). Used only
    when the PDF is NEWER than the pptx: a storyboard fallback rewrites the
    pptx after QA, and then the QA pictures show a different deck."""
    pptx_path = Path(pptx_path)
    qa_root = pptx_path.parent / "director"
    if not qa_root.is_dir() or not pptx_path.exists():
        return None
    rounds = sorted((d for d in qa_root.glob("qa_*") if d.is_dir() and d.name[3:].isdigit()),
                    key=lambda d: int(d.name[3:]))
    if not rounds:
        return None
    last = rounds[-1]
    pdf = last / (pptx_path.stem + ".pdf")
    pngs = sorted(last.glob("slide_*.png"))
    if not pdf.exists() or not pngs:
        return None
    if pdf.stat().st_mtime < pptx_path.stat().st_mtime:
        return None
    return pngs, pdf, "libreoffice (director qa)"


def export_deck_pages(pptx_path: Path, work_dir: Path, renderer: Optional[Callable] = None, *,
                      timeout: int = JOB_RENDER_SECONDS, part: int = 1, first_n: int = 1,
                      reuse_qa: bool = False) -> dict:
    """Render one pptx to PNGs + PDF and build its text map.

    Returns ``{"pngs": [Path], "pdf": Path, "map": dict, "renderer": str,
    "seconds": float}``. Raises DeckRenderUnavailable when no PNG came out,
    or when the render outran ``timeout`` (a renderer that ignores its
    timeout is still held to it here)."""
    pptx_path = Path(pptx_path)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    reused = reuse_qa_render(pptx_path) if reuse_qa else None
    if reused:
        pngs, pdf, how = reused
        logger.info("deck pages: reusing the Director's QA render (%d slide(s))", len(pngs))
    else:
        render = renderer or _default_renderer(timeout)
        pdf = work_dir / "deck.pdf"
        pngs, how = render(pptx_path, work_dir / "png", pdf=pdf)
        pngs = [Path(p) for p in (pngs or [])]
        if not pngs:
            raise DeckRenderUnavailable(f"no slide images: {how}")
        if not pdf.exists():
            raise DeckRenderUnavailable(f"the renderer ({how}) made no PDF to read the text from")
    seconds = round(time.monotonic() - t0, 2)
    if seconds > timeout:
        raise DeckRenderUnavailable(f"render took {seconds}s, over the {timeout}s budget")
    text_map = build_text_map(pdf, pptx_path, pngs, renderer=str(how), part=part, first_n=first_n)
    return {"pngs": pngs, "pdf": pdf, "map": text_map, "renderer": str(how), "seconds": seconds}


def export_deck_parts(parts: list[tuple[int, Path]], work_dir: Path, renderer: Optional[Callable] = None, *,
                      timeout: int = JOB_RENDER_SECONDS, reuse_qa: bool = False) -> dict:
    """Every part of a deck (``deck.pptx`` = part 1, ``deck_partN.pptx`` =
    part N) as ONE run of pages: numbering continues across parts and each
    slide says which part it came from. Same return shape as
    ``export_deck_pages``."""
    work_dir = Path(work_dir)
    pngs: list[Path] = []
    slides: list[dict] = []
    renderers: list[str] = []
    seconds = 0.0
    for part, path in sorted(parts, key=lambda p: int(p[0])):
        res = export_deck_pages(path, work_dir / f"part_{int(part)}", renderer, timeout=timeout,
                                part=int(part), first_n=len(slides) + 1, reuse_qa=reuse_qa)
        pngs.extend(res["pngs"])
        slides.extend(res["map"]["slides"])
        seconds += float(res.get("seconds") or 0)
        if res["renderer"] not in renderers:
            renderers.append(res["renderer"])
    how = ", ".join(renderers)
    return {"pngs": pngs, "pdf": None, "map": {"v": MAP_VERSION, "renderer": how, "slides": slides},
            "renderer": how, "seconds": round(seconds, 2)}


# ── publish ───────────────────────────────────────────────────────────


def _is_missing_enum(exc: BaseException) -> bool:
    s = f"{exc!r} {getattr(exc, 'message', '')} {getattr(exc, 'code', '')}".lower()
    return ("22p02" in s or "invalid input value for enum" in s
            or ("enum" in s and KIND_MAP in s))


def publish_deck_pages(sb, generation_id: str, owner_id: str, pptx_storage_path, result: dict) -> dict:
    """Upload the pages and register them. Idempotent: files are upserted at
    fixed paths, and only this generation's previous ``slide_png`` /
    ``deck_pages_json`` rows are replaced — never ``clear_artifacts``, which
    would take the deck_pptx (and a presentation's videos) with them.

    The ``deck_pages_json`` kind arrives with app migration 0166. Until it is
    applied the insert is refused (invalid enum value): the map is uploaded
    anyway, the slide rows still go in, and a warning says so."""
    from worker import client as db

    base = f"{owner_id}/{generation_id}"
    prefix = f"{base}/{PAGES_DIR}"
    text_map = dict(result["map"])
    decks = pptx_storage_path if isinstance(pptx_storage_path, (list, tuple)) else [pptx_storage_path]
    text_map["deck"] = [str(p) for p in decks if p]
    pngs = [Path(p) for p in result["pngs"]]
    slides = text_map.get("slides") or []
    if len(slides) != len(pngs):
        raise ValueError(f"deck pages: {len(slides)} slide(s) in the map but {len(pngs)} PNG(s)")

    # 1. files first: a row never points at a file that is not there yet
    new_paths = []
    for png, slide in zip(pngs, slides):
        dest = f"{prefix}/{slide['png']}"
        db.upload_artifact(sb, png, dest)
        new_paths.append(dest)
    local_map = pngs[0].parent / PAGES_JSON if pngs else Path(PAGES_JSON)
    local_map.write_text(json.dumps(text_map, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    map_path = f"{prefix}/{PAGES_JSON}"
    db.upload_artifact(sb, local_map, map_path)

    # 2. the previous pages' rows go — these two kinds only, one filter each
    #    (an IN list naming deck_pages_json is itself refused before 0166)
    old_paths = []
    try:
        old = (sb.table("artifacts").select("storage_path").eq("generation_id", generation_id)
               .eq("kind", KIND_PNG).execute())
        old_paths = [r.get("storage_path") for r in (getattr(old, "data", None) or [])]
    except Exception as exc:  # noqa: BLE001 — only used to tidy storage
        logger.warning("deck pages %s: previous slide rows unreadable (%s)", generation_id, exc)
    sb.table("artifacts").delete().eq("generation_id", generation_id).eq("kind", KIND_PNG).execute()
    try:
        sb.table("artifacts").delete().eq("generation_id", generation_id).eq("kind", KIND_MAP).execute()
    except Exception as exc:  # noqa: BLE001
        if not _is_missing_enum(exc):
            raise

    # 3. the new rows: every slide in one insert, then the map
    sb.table("artifacts").insert([
        {"generation_id": generation_id, "kind": KIND_PNG, "storage_path": p} for p in new_paths
    ]).execute()
    registered = True
    try:
        db.add_artifact_row(sb, generation_id, KIND_MAP, map_path)
    except Exception as exc:  # noqa: BLE001
        if not _is_missing_enum(exc):
            raise
        registered = False
        logger.warning("deck pages %s: 0166 not applied: text map uploaded but not registered (%s)",
                       generation_id, str(exc)[:200])

    # 4. a deck that got shorter leaves PNGs nobody points at: remove them
    stale = sorted({p for p in old_paths if p and p.startswith(prefix + "/") and p not in new_paths})
    if stale:
        try:
            sb.storage.from_("artifacts").remove(stale)
        except Exception as exc:  # noqa: BLE001 — tidying, never a failure
            logger.warning("deck pages %s: %d stale PNG(s) not removed (%s)", generation_id, len(stale), exc)
    logger.info("deck pages %s: %d slide(s) published (%s), map %s", generation_id, len(new_paths),
                result.get("renderer"), "registered" if registered else "UNREGISTERED (0166)")
    return {"slides": len(new_paths), "map_path": map_path, "map_registered": registered,
            "png_paths": new_paths, "stale_removed": stale}


# ── which pptx files make up a deck ───────────────────────────────────

_PART_RE = re.compile(r"(?:^|/)deck(?:_part(\d+))?\.pptx$")


def deck_part_of(storage_path: str) -> Optional[int]:
    """``…/deck.pptx`` → 1, ``…/deck_part3.pptx`` → 3, anything else → None."""
    m = _PART_RE.search(str(storage_path or ""))
    if not m:
        return None
    return int(m.group(1)) if m.group(1) else 1
