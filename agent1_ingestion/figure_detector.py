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
_SKIP_BEFORE_PAGE = 2        # never scan the cover / title page (front matter, no teaching figures)
_MAX_FIGURES = 8            # keep at most this many per chapter
_CROP_TARGET_PX = 1100       # render the cropped region ~this wide
_CROP_MAX_ZOOM = 4.0
_MIN_CROP_W = 260            # reject slivers / mis-detections
_MIN_CROP_H = 170
_MIN_AREA = 0.015            # a real figure covers at least ~1.5% of the page
_MAX_AREA = 0.85             # ~whole page = probably not a single figure

# --- crop tightening + figure gate (run on the rendered region, at crop time) ---
# Detection boxes are loose: they sweep in the body-text line above a figure and the
# caption / "Questions" below. These trim the render back to just the diagram and
# reject regions that are not a labelled teaching figure at all.
_TRIM_GAP_FRAC = 0.028       # merge content bands across whitespace gaps up to this frac of height
_MIN_BAND_COLOR_PX = 1500    # a real colored figure has at least this many colored pixels...
_MIN_BAND_COLOR_FRAC = 0.010 # ...and they are a real fraction of its band, not stray specks
_MIN_COLOR_ROW_COVER = 0.30  # colour spread across >=30% of the band's rows (a header/"continued" bar fails)
_MIN_WHITE_FRAC = 0.45       # a labelled diagram sits on a LIGHT background...
_MAX_COLOR_FRAC = 0.35       # ...an unlabelled colour photo fills the frame — reject only when BOTH agree "photo"


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
    pages = [p for p in range(first, min(last, first + _MAX_PAGES - 1) + 1) if p >= _SKIP_BEFORE_PAGE]
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
                "Report ONLY the TEACHING FIGURES — the ones a student studies to understand a "
                "concept: a LABELLED diagram, an annotated scientific drawing, a chart/graph, a map, "
                "or a photograph that carries LABELS or a figure caption pointing out its parts. The "
                "key test: it has labels, callouts, or a caption naming what it shows.\n\n"
                "Do NOT report (these are NOT teaching figures), even when colourful:\n"
                "- the book COVER, or a unit/chapter OPENER's large background or decorative photo;\n"
                "- any PHOTOGRAPH with no leader-line labels naming its parts — a specimen or "
                "cells seen under a microscope, a magnified texture, a leaf, a landscape, a stock "
                "or watermarked photo — these teach nothing on their own, however vivid;\n"
                "- publisher logos, branding, edition/endorsement text, page numbers, headers/footers;\n"
                "- body text, headings, key-word lists, worked-example tables, and 'getting "
                "started' / 'Activity' / '...continued' / 'You will need' / question boxes.\n\n"
                "For each teaching figure, report:\n"
                f"- image_number: which image it is on (1..{n}) — the position in THIS set, never a "
                "printed page number.\n"
                "- bbox: [x0, y0, x1, y1] as fractions of the page from 0 to 1 (x0,y0 = top-left, "
                "x1,y1 = bottom-right), tight around the figure and its leader-line labels ONLY — "
                "do NOT include the body-text line above it, or the caption / question numbers below.\n"
                "- caption: a short description (<=12 words) of what the figure shows.\n"
                "- label: the printed figure label if visible (e.g. \"Fig. 4.2\"), otherwise null.\n\n"
                'Return ONLY JSON: {"figures": [{"image_number": <int>, "bbox": [x0,y0,x1,y1], '
                '"caption": "<text>", "label": "<text or null>"}]}. Empty list if a page has no '
                "labelled teaching figure. When unsure whether an image teaches or just decorates, SKIP it."
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


def _tighten_to_figure(img):
    """Trim a rendered figure region down to just the labelled diagram, or return
    None to drop it (better no figure than the wrong one).

    Detection boxes are loose — they include the body-text line above a figure and
    the caption / "Questions" heading below. Purely from pixels we (1) split the
    region into content bands by whitespace gaps, (2) keep the band carrying the
    actual coloured diagram, (3) trim it tight left/right to its leader-line labels,
    then reject anything that is not a labelled teaching diagram: an activity /
    "...continued" box (colour only in a header bar) or an unlabelled colour photo
    (fills the frame, no light background). Validated on a real book — see
    scratchpad/test_figures.py.
    """
    import numpy as np

    a = np.asarray(img.convert("RGB")).astype(np.int16)
    h, w, _ = a.shape
    if h < 40 or w < 40:
        return None
    lum = a.mean(2)
    mx, mn = a.max(2), a.min(2)
    ink = lum < 175                       # any dark mark: text, strokes, dark fills
    color = (mx - mn > 42) & (mx > 60)    # saturated = graphical, not black text

    # 1. content bands: runs of inked rows, merged across small whitespace gaps.
    nonwhite = ink.mean(1) >= 0.002
    gap = max(6, int(_TRIM_GAP_FRAC * h))
    bands: list[tuple[int, int]] = []
    y = 0
    while y < h:
        if not nonwhite[y]:
            y += 1
            continue
        y0 = y
        while y < h and nonwhite[y]:
            y += 1
        y1 = y
        while y1 < h:                     # absorb the next run if only a small gap splits them
            look = y1
            while look < h and look < y1 + gap and not nonwhite[look]:
                look += 1
            if look < h and look < y1 + gap and nonwhite[look]:
                while look < h and nonwhite[look]:
                    look += 1
                y1 = look
            else:
                break
        bands.append((y0, y1))
        y = y1
    if not bands:
        return None

    # 2. keep the band with the most coloured (diagram) pixels.
    b0, b1, best = bands[0][0], bands[0][1], -1.0
    for c0, c1 in bands:
        cc = float(color[c0:c1].sum())
        if cc > best:
            best, b0, b1 = cc, c0, c1
    cc = float(color[b0:b1].sum())
    area = max(1, (b1 - b0) * w)
    if cc < _MIN_BAND_COLOR_PX or cc / area < _MIN_BAND_COLOR_FRAC:
        return None                       # no real coloured figure — plain text / mono region
    row_has_color = color.sum(1) > 0.006 * w
    if float(row_has_color[b0:b1].mean()) < _MIN_COLOR_ROW_COVER:
        return None                       # colour only in a bar/header (activity / "continued" box)

    # 3. trim tight left/right to inked columns (keeps the leader-line labels).
    col_has = ink[b0:b1].mean(0) >= 0.004
    cols = np.where(col_has)[0]
    if len(cols) == 0:
        return None
    m = max(6, int(0.02 * w))
    x0 = max(0, int(cols[0]) - m)
    x1 = min(w, int(cols[-1]) + 1 + m)
    yy0 = max(0, b0 - m)
    yy1 = min(h, b1 + m)
    crop = img.crop((x0, yy0, x1, yy1))

    # 4. a labelled diagram sits on a LIGHT background with flat colour fills; an
    #    unlabelled colour photo fills the frame edge-to-edge. Reject only when both
    #    signals agree "photo", so a legitimately dense diagram still survives.
    ca = np.asarray(crop.convert("RGB")).astype(np.int16)
    clum = ca.mean(2)
    cmx, cmn = ca.max(2), ca.min(2)
    white_frac = float((clum > 236).mean())
    color_frac = float(((cmx - cmn > 42) & (cmx > 60)).mean())
    if white_frac < _MIN_WHITE_FRAC and color_frac > _MAX_COLOR_FRAC:
        return None
    return crop


def crop_figure(pdf_path: str | Path, page_num: int, bbox, out_path: str | Path) -> Path | None:
    """Render a figure's page region (normalised bbox), TRIM it to just the diagram,
    and save a crisp PNG — or None.

    Rejects slivers, near-page-size crops, near-blank regions, and (via
    :func:`_tighten_to_figure`) loose boxes and non-figure regions (activity boxes,
    unlabelled photos), so only a real, tightly-cropped figure reaches a slide.
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

    # Trim the loose detection box down to just the diagram (and drop non-figures).
    try:
        tight = _tighten_to_figure(img)
    except Exception as exc:  # noqa: BLE001 — trimming must never break a lesson
        logger.warning("figure tighten failed (p%d): %s", page_num, exc)
        tight = img
    if tight is None:
        return None
    img = tight

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
