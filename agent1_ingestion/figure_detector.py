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
_REFINE_WIDTH = 1500         # single-page render for the precise per-figure box refinement (higher res)
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
# The PALETTE-AGNOSTIC fallback: vision refine (refine_figure_box) is the authority on
# tight boxes; this only trims residual margins and rejects two things ANY book agrees
# are not a figure — an unlabelled colour photo, and colour confined to a header bar
# (an activity / "Learn" / "Practise" panel). It NEVER rejects for "not colourful
# enough", so monochrome line diagrams, greyscale screenshots and low-colour tables all
# survive (validated on maths / computing / scanned books, not just the colourful one).
_TRIM_GAP_FRAC = 0.028       # merge content bands across whitespace gaps up to this frac of height
_COLOR_SELECT_MIN = 2000     # pick the band by COLOUR only when colour is genuinely present, else by ink size
_BAR_COLOR_MIN = 1500        # a coloured region this big whose colour sits in a thin bar = activity/header box...
_BAR_COV_MAX = 0.30          #   ...colour spread across <30% of the band's rows (mono figures skip this — no colour)
_PHOTO_WHITE_MAX = 0.45      # a crop with little light background...
_PHOTO_COLOR_MIN = 0.35      #   ...AND heavy colour is an unlabelled photo — reject (BOTH required, so a mono
                             #      screenshot (low colour) or a tinted diagram (some white) is kept)


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


def refine_figure_box(pdf_path, page_num, rough_bbox, caption, label, client):
    """VISION authority: render the figure's single page at high resolution and ask
    the model for a TIGHT box around just that figure — or to DROP it if the region is
    not really a labelled teaching figure.

    Palette-independent (the model judges by meaning, not colour), so it tightens the
    box the same way on a monochrome maths book, a screenshot-heavy computing book, or a
    colour-diagram science book — the generalisation the pixel fallback can't guarantee.
    Returns a tight normalised bbox, None to DROP, or the ROUGH bbox unchanged on any
    failure (best-effort — never worse than detection alone). Never raises.
    """
    rough = _clean_bbox(rough_bbox)
    if client is None:
        return rough
    tmp = Path(tempfile.mkdtemp(prefix="fig_refine_"))
    paths: list = []
    try:
        try:
            paths = _render_pages(pdf_path, range(int(page_num), int(page_num) + 1), _REFINE_WIDTH, tmp)
        except Exception as exc:  # noqa: BLE001
            logger.warning("figure refine render failed (p%d): %s", page_num, exc)
            return rough
        if not paths:
            return rough
        desc = (caption or label or "a labelled diagram").strip()
        prompt = (
            "This is ONE page of a school textbook. A figure detected on it is described "
            f'as: "{desc}".\n\n'
            "Return the TIGHT bounding box around ONLY that teaching figure and its own "
            "leader-line labels. EXCLUDE the body-text lines above or below it, its caption "
            "line, section headings, page numbers, and any surrounding activity-panel chrome "
            "(a 'Learn' / 'Practise' / 'Activity' / '...continued' bar or border).\n"
            "The figure may be a labelled diagram, a chart or graph, a number line, a worked "
            "table or grid, a flow chart, or a software screenshot — COLOUR IS NOT REQUIRED, "
            "a black-and-white line diagram counts.\n"
            "If the described region is NOT actually a self-contained teaching figure — it is "
            "running text, an activity / question box, a decorative or unlabelled photo, or you "
            "cannot find it on this page — return null.\n"
            "bbox is [x0, y0, x1, y1] as fractions of the page (0..1; x0,y0 = top-left, "
            "x1,y1 = bottom-right).\n"
            'Return ONLY JSON: {"bbox": [x0,y0,x1,y1]} or {"bbox": null}.'
        )
        try:
            result = client.analyze_images_batch(paths, prompt, max_tokens=300)
            data = result.get("data", {}) if isinstance(result, dict) else {}
        except Exception as exc:  # noqa: BLE001 — refine must never break indexing
            logger.warning("figure refine failed (p%d): %s", page_num, exc)
            return rough
        if isinstance(data, dict) and "bbox" in data:
            raw = data.get("bbox")
            if raw is None:
                return None                       # model: not a real figure → drop the candidate
            tight = _clean_bbox(raw)
            if tight is not None:
                return tight
        return rough                              # unparseable answer → keep the rough box
    finally:
        for p in paths:
            try:
                p.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(tmp, ignore_errors=True)


def _tighten_to_figure(img):
    """Trim a rendered figure region to just the figure, or None to drop it — the
    PALETTE-AGNOSTIC fallback (vision refine is the authority; this is what runs when
    refine is unavailable or on already-indexed books).

    Purely from layout structure (no "must be colourful" assumption): (1) split the
    region into content bands by whitespace gaps, (2) keep the figure band — the most
    COLOURED band when colour is present, else the largest INKED band (so a monochrome
    diagram or a greyscale screenshot is kept, not dropped), (3) trim tight left/right
    to its content, then reject only the two things every book agrees are not a figure:
    an unlabelled colour PHOTO (fills the frame, little light background AND heavy
    colour) and colour confined to a HEADER BAR (an activity / "Learn" panel).
    Validated on colourful-diagram, monochrome-maths, screenshot-computing and scanned
    books — see scratchpad/test_figures.py.
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

    # 2. keep the figure band: by COLOUR when colour is genuinely present (a coloured
    #    diagram beats the text bands around it), otherwise by INK size — so a
    #    monochrome line diagram or a greyscale screenshot is kept, never rejected.
    band_color = lambda b: float(color[b[0]:b[1]].sum())   # noqa: E731
    band_ink = lambda b: float(ink[b[0]:b[1]].sum())       # noqa: E731
    if max((band_color(b) for b in bands), default=0.0) >= _COLOR_SELECT_MIN:
        b0, b1 = max(bands, key=band_color)
    else:
        b0, b1 = max(bands, key=band_ink)

    # 3. trim tight left/right to inked columns (keeps leader-line labels / row labels).
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

    # 4. reject only what every palette agrees is not a figure.
    cc = float(color[b0:b1].sum())
    cov = float((color.sum(1) > 0.006 * w)[b0:b1].mean())
    if cc >= _BAR_COLOR_MIN and cov < _BAR_COV_MAX:
        return None                       # colour lives only in a header bar → activity/"Learn" panel
    ca = np.asarray(crop.convert("RGB")).astype(np.int16)
    clum = ca.mean(2)
    cmx, cmn = ca.max(2), ca.min(2)
    white_frac = float((clum > 236).mean())
    color_frac = float(((cmx - cmn > 42) & (cmx > 60)).mean())
    if white_frac < _PHOTO_WHITE_MAX and color_frac > _PHOTO_COLOR_MIN:
        return None                       # little light background AND heavy colour → unlabelled photo
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
