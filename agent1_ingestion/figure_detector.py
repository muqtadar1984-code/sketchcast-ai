"""Phase 3 — real textbook figures via VISION (works for vector + scanned books).

``extract_images`` only recovers embedded raster XObjects, which misses the two
common cases: modern textbooks draw figures as VECTOR art (no raster to pull),
and scanned books store each whole page as one image. So instead of pulling
objects, we RENDER the pages and have Claude point at the figures — then crop the
region straight out of the PDF at high resolution. That captures a figure however
it was authored.

Positions use the SAME image-number trick as vision chapter detection: the model
reports which image (1..N) a figure is on, never a page number it might read off
the page, and we map that back to the physical page ourselves. Bounding boxes are
fractions of the page, so they survive any render resolution.

Cost guardrails: low-res detection, a page cap, a figure cap. All best-effort —
detection or a crop failing must never break a lesson (the slide falls back to a
diagram or bullets).
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

from .vision_chapters import _is_int, _page_of, _render_pages

logger = logging.getLogger(__name__)

_DETECT_WIDTH = 900          # a touch higher than chapter detection — figures are smaller than banners
_DETECT_BATCH = 8
_MAX_PAGES = 16              # scan at most this many of a chapter's pages
_MAX_FIGURES = 8            # keep at most this many per chapter
_CROP_TARGET_PX = 1100       # render the cropped region ~this wide
_CROP_MAX_ZOOM = 4.0
_MIN_CROP_W = 260            # reject slivers / mis-detections
_MIN_CROP_H = 170
_MIN_AREA = 0.015            # a real figure covers at least ~1.5% of the page
_MAX_AREA = 0.85             # ~whole page = probably not a single figure


def _clean_bbox(bbox) -> tuple[float, float, float, float] | None:
    """Validate a normalised [x0,y0,x1,y1] (0..1) with a sane area, or None."""
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in bbox)
    except (TypeError, ValueError):
        return None
    x0, x1 = sorted((max(0.0, min(1.0, x0)), max(0.0, min(1.0, x1))))
    y0, y1 = sorted((max(0.0, min(1.0, y0)), max(0.0, min(1.0, y1))))
    area = (x1 - x0) * (y1 - y0)
    if x1 - x0 < 0.06 or y1 - y0 < 0.05 or not (_MIN_AREA <= area <= _MAX_AREA):
        return None
    return (x0, y0, x1, y1)


def detect_figures(pdf_path: str | Path, start_page: int, end_page: int, client) -> list[dict]:
    """Have Claude point at the figures in a chapter's pages.

    Returns ``[{page_num, bbox:(x0,y0,x1,y1), caption, label}]`` (0-indexed page,
    bbox as page fractions), or ``[]`` on any trouble. Never raises.
    """
    if client is None:
        return []
    first = max(0, int(start_page))
    last = max(first, int(end_page))
    pages = list(range(first, min(last, first + _MAX_PAGES - 1) + 1))
    if not pages:
        return []

    tmp = Path(tempfile.mkdtemp(prefix="fig_detect_"))
    figures: list[dict] = []
    try:
        for i in range(0, len(pages), _DETECT_BATCH):
            batch = pages[i:i + _DETECT_BATCH]
            paths = _render_pages(pdf_path, range(batch[0], batch[-1] + 1), _DETECT_WIDTH, tmp)
            if not paths:
                continue
            page_numbers = [_page_of(p) for p in paths]
            n = len(paths)
            prompt = (
                f"You are shown {n} consecutive pages of a school textbook, in order — "
                f"image 1 (first) through image {n} (last).\n\n"
                "Find the FIGURES on these pages: diagrams, illustrations, labelled drawings, "
                "photographs, charts, graphs and maps — the visual teaching aids a student would "
                "look at. Do NOT report body text, headings, plain text tables, exercises, page "
                "numbers, running headers/footers, publisher logos, or decorative borders.\n\n"
                "For each figure, report:\n"
                f"- image_number: which image it is on (1..{n}) — the position in THIS set, never a "
                "printed page number.\n"
                "- bbox: [x0, y0, x1, y1] as fractions of the page from 0 to 1 (x0,y0 = top-left "
                "corner, x1,y1 = bottom-right), tightly around the figure AND its caption.\n"
                "- caption: a short description (<=12 words) of what the figure shows.\n"
                "- label: the printed figure label if one is visible (e.g. \"Fig. 4.2\", "
                "\"Figure 3\"), otherwise null.\n\n"
                'Return ONLY JSON: {"figures": [{"image_number": <int>, "bbox": [x0,y0,x1,y1], '
                '"caption": "<text>", "label": "<text or null>"}]}. Empty list if there are no '
                "real figures. Prefer quality over quantity — skip anything you are unsure is a figure."
            )
            try:
                result = client.analyze_images_batch(paths, prompt, max_tokens=1500)
                data = result.get("data", {}) if isinstance(result, dict) else {}
                items = data.get("figures", []) if isinstance(data, dict) else data
                for it in items if isinstance(items, list) else []:
                    if not isinstance(it, dict):
                        continue
                    idx = it.get("image_number")
                    if not _is_int(idx) or not (1 <= int(idx) <= n):
                        continue  # out of range → a page number the model read, not a position
                    bbox = _clean_bbox(it.get("bbox"))
                    if not bbox:
                        continue
                    caption = str(it.get("caption") or "").strip()[:140]
                    label = str(it.get("label") or "").strip()[:40]
                    figures.append({
                        "page_num": page_numbers[int(idx) - 1],
                        "bbox": bbox,
                        "caption": caption,
                        "label": label if label.lower() not in ("null", "none", "") else "",
                    })
            except Exception as exc:  # noqa: BLE001 — one bad batch must not lose the rest
                logger.warning("figure detection batch at page %d failed: %s", batch[0] + 1, exc)
            finally:
                for p in paths:
                    p.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001 — detection must never break generation
        logger.error("figure detection failed: %s", exc)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # Keep the strongest few (largest area first — big figures read best on a slide).
    figures.sort(key=lambda f: (f["bbox"][2] - f["bbox"][0]) * (f["bbox"][3] - f["bbox"][1]), reverse=True)
    logger.info("figure detection: %d figures across pages %d-%d", len(figures), first + 1, last + 1)
    return figures[:_MAX_FIGURES]


def crop_figure(pdf_path: str | Path, page_num: int, bbox, out_path: str | Path) -> Path | None:
    """Render a figure's page region (normalised bbox) to a crisp PNG, or None.

    Rejects slivers, near-page-size crops, and near-blank regions (a mis-detected
    empty area), so only a real figure ever reaches a slide.
    """
    import fitz
    from PIL import Image

    box = _clean_bbox(bbox)
    if box is None:
        return None
    x0, y0, x1, y1 = box
    try:
        doc = fitz.open(str(pdf_path))
        if page_num < 0 or page_num >= doc.page_count:
            doc.close()
            return None
        page = doc.load_page(page_num)
        r = page.rect
        clip = fitz.Rect(r.x0 + x0 * r.width, r.y0 + y0 * r.height,
                         r.x0 + x1 * r.width, r.y0 + y1 * r.height)
        zoom = min(_CROP_MAX_ZOOM, _CROP_TARGET_PX / max(1.0, clip.width))
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        doc.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("figure crop failed (p%d): %s", page_num, exc)
        return None

    if img.width < _MIN_CROP_W or img.height < _MIN_CROP_H:
        return None
    # Near-blank guard: a crop that is almost entirely white is a mis-detection.
    small = img.resize((48, 48)).convert("L")
    px = list(small.getdata())
    if sum(1 for v in px if v > 244) / len(px) > 0.97:
        return None

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(out_path), "PNG")
    return out_path
