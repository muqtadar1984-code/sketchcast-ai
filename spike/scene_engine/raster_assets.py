"""AI-generated raster line-art assets: generate -> post-process -> cache -> trace.

The cost architecture (§9): the image model creates the difficult VISUAL once;
our renderer does the drawing, movement, labels and camera forever after. An
asset is cached on disk by key — a school's hundredth photosynthesis lesson
pays $0 for the leaf.

Transport is raw REST with `requests` (the exact pattern of
shared/gemini_client.py — no new dependency):
  1. Vertex (aiplatform.googleapis.com) when VERTEX_PROJECT_ID + google creds
     exist — bills the credited GCP project, like prod. TRIED FIRST.
  2. AI Studio (generativelanguage.googleapis.com) with GOOGLE_AI_API_KEY /
     GEMINI_API_KEY — the local-dev fallback (real money, pennies).

Failure of ANY step returns None and the caller falls back to the authored
vector tier — a lesson never fails because an asset did (§20).
"""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import requests
from PIL import Image

from .trace import drawing_order

logger = logging.getLogger(__name__)

IMAGE_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")
CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "storage" / "scene_assets"
NOMINAL_WORLD_W = 700.0  # an illustration at element scale 1.0 spans ~700 world px

_COLOR_SUFFIX = (
    " Friendly flat-colour cartoon illustration with clean black outlines and "
    "simple cel shading, warm natural skin tones, colourful clothing, pure "
    "white background, waist-up, centred. ABSOLUTELY NO TEXT OF ANY KIND: no "
    "letters, words, labels, numbers or captions. No background scenery, no "
    "frame, no watermark."
)

_STYLE_SUFFIX = (
    " Black ink line drawing, hand-drawn whiteboard sketch style, clean confident "
    "strokes, pure white background. ABSOLUTELY NO TEXT OF ANY KIND anywhere in "
    "the image: no letters, no words, no labels, no numbers, no captions, no "
    "arrows pointing at parts — the diagram is UNLABELED (labels are added "
    "separately by software). No shading, no color fill, no watermark."
)


@dataclass
class RasterAsset:
    key: str
    ink: Image.Image            # RGBA, ink on transparency
    trace: list[tuple[float, float]]
    stamp_r: float
    world_scale: float          # asset px -> world px at element scale 1.0
    # vision-annotated named part regions, asset pixel coords: name -> list of
    # [x0, y0, x1, y1] boxes (several boxes = several instances, e.g. three
    # mitochondria). The keystone for layer anchors, arrow routing and
    # narration-ordered drawing on generated art.
    regions: dict[str, list[list[float]]] = None
    baked_text: bool = False    # vision saw text in the art (validation warns)

    def __post_init__(self):
        if self.regions is None:
            self.regions = {}


# ── transport ────────────────────────────────────────────────────────────────

def _vertex_call(prompt: str) -> bytes | None:
    project = os.getenv("VERTEX_PROJECT_ID", "").strip()
    if not project:
        return None
    try:
        # same credential chain as prod: materialises the Railway-style
        # GOOGLE_APPLICATION_CREDENTIALS_JSON string into a file, else falls
        # through to a file path or ambient ADC (gcloud login)
        from shared.claude_client import _ensure_google_credentials
        _ensure_google_credentials()
        import google.auth
        import google.auth.transport.requests
        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(google.auth.transport.requests.Request())
        region = os.getenv("VERTEX_REGION", "global").strip() or "global"
        host = ("aiplatform.googleapis.com" if region == "global"
                else f"{region}-aiplatform.googleapis.com")
        url = (f"https://{host}/v1/projects/{project}/locations/{region}"
               f"/publishers/google/models/{IMAGE_MODEL}:generateContent")
        def _go():
            res = requests.post(url,
                                headers={"Authorization": f"Bearer {creds.token}"},
                                json=_body(prompt), timeout=120)
            res.raise_for_status()
            return res.json()
        return _image_from(_with_backoff(_go, "Vertex image"))
    except Exception as e:
        logger.warning("Vertex image call failed (%s); trying AI Studio", e)
        return None


def _aistudio_call(prompt: str) -> bytes | None:
    key = os.getenv("GOOGLE_AI_API_KEY", "") or os.getenv("GEMINI_API_KEY", "")
    if not key:
        return None
    try:
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{IMAGE_MODEL}:generateContent")
        def _go():
            res = requests.post(url, headers={"x-goog-api-key": key},
                                json=_body(prompt), timeout=120)
            res.raise_for_status()
            return res.json()
        return _image_from(_with_backoff(_go, "AI Studio image"))
    except Exception as e:
        logger.warning("AI Studio image call failed: %s", e)
        return None


def _is_rate_limited(exc: Exception) -> bool:
    r = getattr(exc, "response", None)
    return getattr(r, "status_code", None) in (429, 503)


def _with_backoff(fn, what: str, tries: int = 4):
    """Retry a transport call through rate limits.

    A 429 used to be swallowed as "no image" — the asset silently vanished
    AND, when it was the vision annotator, the whole region schedule
    degraded to uniform slices, so labels stopped matching the narration.
    A burst limit is a wait, not a failure.
    """
    import random
    import time as _t
    delay = 6.0
    for attempt in range(tries):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — transport errors only
            if not _is_rate_limited(exc) or attempt == tries - 1:
                raise
            wait = delay + random.uniform(0, 2.0)
            logger.warning("%s rate-limited; retrying in %.0fs (%d/%d)",
                           what, wait, attempt + 1, tries - 1)
            _t.sleep(wait)
            delay *= 2.4
    return None


def _body(prompt: str) -> dict:
    return {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
    }


def _image_from(payload: dict) -> bytes | None:
    for cand in payload.get("candidates", []):
        for part in cand.get("content", {}).get("parts", []):
            data = part.get("inlineData") or part.get("inline_data") or {}
            if str(data.get("mimeType") or data.get("mime_type", "")).startswith("image/"):
                return base64.b64decode(data["data"])
    return None


def _vision_json(prompt: str, png_bytes: bytes) -> dict | None:
    """One image + prompt -> parsed JSON, via the same Vertex-first transport."""
    img_part = {"inlineData": {"mimeType": "image/png",
                               "data": base64.b64encode(png_bytes).decode()}}
    body = {"contents": [{"role": "user",
                          "parts": [img_part, {"text": prompt}]}]}
    vision_model = os.getenv("GEMINI_VISION_MODEL", "gemini-2.5-flash")

    def call(url, headers):
        def _go():
            res = requests.post(url, headers=headers, json=body, timeout=120)
            res.raise_for_status()
            return res.json()
        payload = _with_backoff(_go, "vision") or {}
        for cand in payload.get("candidates", []):
            txt = "".join(p.get("text", "")
                          for p in cand.get("content", {}).get("parts", []))
            m = __import__("re").search(r"\{.*\}", txt, __import__("re").S)
            if m:
                return json.loads(m.group(0))
        return None

    project = os.getenv("VERTEX_PROJECT_ID", "").strip()
    if project:
        try:
            from shared.claude_client import _ensure_google_credentials
            _ensure_google_credentials()
            import google.auth
            import google.auth.transport.requests
            creds, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"])
            creds.refresh(google.auth.transport.requests.Request())
            region = os.getenv("VERTEX_REGION", "global").strip() or "global"
            host = ("aiplatform.googleapis.com" if region == "global"
                    else f"{region}-aiplatform.googleapis.com")
            return call(f"https://{host}/v1/projects/{project}/locations/{region}"
                        f"/publishers/google/models/{vision_model}:generateContent",
                        {"Authorization": f"Bearer {creds.token}"})
        except Exception as e:
            logger.warning("Vertex vision call failed (%s); trying AI Studio", e)
    key = os.getenv("GOOGLE_AI_API_KEY", "") or os.getenv("GEMINI_API_KEY", "")
    if not key:
        return None
    try:
        return call(f"https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{vision_model}:generateContent", {"x-goog-api-key": key})
    except Exception as e:
        logger.warning("AI Studio vision call failed: %s", e)
        return None


def part_names_from_prompt(prompt: str) -> list[str]:
    """The 'Name the layer groups exactly: a, b, c' tail of an asset prompt."""
    import re
    m = re.search(r"name the layer groups exactly:\s*([^.\n]+)", prompt,
                  re.IGNORECASE)
    if not m:
        return []
    return [p.strip().strip('"\'') for p in m.group(1).split(",")
            if p.strip()][:12]


def annotate_regions(ink: Image.Image, part_names: list[str]) -> dict:
    """Vision pass over a generated illustration: named part bounding boxes
    (multiple boxes per name for repeated structures) + a text-presence
    verdict. Returns {"regions": {name: [[x0,y0,x1,y1] px, ...]}, "has_text":
    bool}; empty regions on any failure — callers degrade gracefully."""
    out = {"regions": {}, "has_text": False, "text_boxes": []}
    import io as _io
    buf = _io.BytesIO()
    ink_on_white = Image.new("RGB", ink.size, (255, 255, 255))
    ink_on_white.paste(ink, (0, 0), ink)
    ink_on_white.save(buf, "PNG")
    prompt = (
        "This is an unlabeled educational line diagram. Return ONLY JSON:\n"
        '{"has_text": <true if ANY letters/words/numbers appear in the '
        'image>, "text_boxes": [[ymin,xmin,ymax,xmax], ...], '
        '"regions": {"<part>": [[ymin,xmin,ymax,xmax], ...]}}\n'
        "text_boxes: a tight box around EVERY piece of text (letters, words, "
        "numbers, labels) in the image — INCLUDING small, faint, partial or "
        "lowercase caption words near or under the artwork; empty list only "
        "if the image is truly wordless.\n"
        "Boxes are normalized 0-1000. "
        + ("For each of these part names, give a box around EACH visible "
           "instance of that part (a name may have several boxes, e.g. "
           "three mitochondria): " + ", ".join(part_names)
           if part_names else 'Leave "regions" as an empty object.'))
    data = _vision_json(prompt, buf.getvalue())
    if not isinstance(data, dict):
        return out
    out["has_text"] = bool(data.get("has_text"))
    w, h = ink.size
    for b in (data.get("text_boxes") or [])[:24]:
        try:
            ymin, xmin, ymax, xmax = [float(v) for v in b[:4]]
        except (TypeError, ValueError, IndexError):
            continue
        if ymax > ymin and xmax > xmin:
            out["text_boxes"].append([xmin / 1000 * w, ymin / 1000 * h,
                                      xmax / 1000 * w, ymax / 1000 * h])
    regions: dict[str, list[list[float]]] = {}
    raw = data.get("regions")
    if isinstance(raw, dict):
        for name, boxes in raw.items():
            if not isinstance(boxes, list):
                continue
            if boxes and isinstance(boxes[0], (int, float)):
                boxes = [boxes]     # single box not nested
            clean = []
            for b in boxes[:6]:
                try:
                    ymin, xmin, ymax, xmax = [float(v) for v in b[:4]]
                except (TypeError, ValueError, IndexError):
                    continue
                if ymax <= ymin or xmax <= xmin:
                    continue
                clean.append([xmin / 1000 * w, ymin / 1000 * h,
                              xmax / 1000 * w, ymax / 1000 * h])
            if clean:
                regions[str(name).strip().lower()] = clean
    out["regions"] = regions
    missing = [n for n in part_names
               if str(n).strip().lower() not in regions]
    if missing and regions:
        # focused re-ask for JUST the unboxed parts — the multiplexed
        # N-part question reliably drops one or two (a run once lost 3 of 7,
        # suppressing their arrows). A short list gets full attention.
        data3 = _vision_json(
            "This is an unlabeled educational line diagram. Return ONLY "
            'JSON: {"regions": {"<part>": [[ymin,xmin,ymax,xmax], ...]}}. '
            "Boxes normalized 0-1000. Give a box around EACH visible "
            "instance of: " + ", ".join(missing) +
            '. Use an empty list only for a part truly not shown.',
            buf.getvalue())
        if isinstance(data3, dict) and isinstance(data3.get("regions"), dict):
            for name, boxes in data3["regions"].items():
                key2 = str(name).strip().lower()
                if key2 in regions or not isinstance(boxes, list):
                    continue
                if boxes and isinstance(boxes[0], (int, float)):
                    boxes = [boxes]
                clean = []
                for b in boxes[:6]:
                    try:
                        ymin, xmin, ymax, xmax = [float(v) for v in b[:4]]
                    except (TypeError, ValueError, IndexError):
                        continue
                    if ymax > ymin and xmax > xmin:
                        clean.append([xmin / 1000 * w, ymin / 1000 * h,
                                      xmax / 1000 * w, ymax / 1000 * h])
                if clean:
                    regions[key2] = clean
    if not out["has_text"] and not out["text_boxes"]:
        # second, single-purpose pass: the combined ask (text + N part
        # boxes) diluted attention enough that a cell covered in baked
        # gibberish labels came back has_text=false. A dedicated question
        # catches what the multiplexed one misses.
        out["text_boxes"] = scan_text(ink)
        if out["text_boxes"]:
            out["has_text"] = True
    return out


def scan_text(ink: Image.Image) -> list[list[float]]:
    """Dedicated text-only vision pass: pixel boxes around every readable
    mark. One focused question catches captions the multiplexed annotation
    call repeatedly missed."""
    import io as _io
    buf = _io.BytesIO()
    on_white = Image.new("RGB", ink.size, (255, 255, 255))
    on_white.paste(ink, (0, 0), ink)
    on_white.save(buf, "PNG")
    data = _vision_json(
        "Look ONLY for text. Return ONLY JSON "
        '{"text_boxes": [[ymin,xmin,ymax,xmax], ...]} — a tight box '
        "around EVERY letter, word, number or label anywhere in this "
        "image, however small, faint, partial or misspelled; [] only "
        "if the image is truly wordless. Boxes normalized 0-1000.",
        buf.getvalue())
    w, h = ink.size
    boxes: list[list[float]] = []
    if isinstance(data, dict):
        for b in (data.get("text_boxes") or [])[:24]:
            try:
                ymin, xmin, ymax, xmax = [float(v) for v in b[:4]]
            except (TypeError, ValueError, IndexError):
                continue
            if ymax > ymin and xmax > xmin:
                boxes.append([xmin / 1000 * w, ymin / 1000 * h,
                              xmax / 1000 * w, ymax / 1000 * h])
    return boxes


def scrub_all_text(ink: Image.Image, boxes: list[list[float]],
                   max_rounds: int = 3) -> tuple[Image.Image, list[list[float]]]:
    """Scrub-and-rescan until the image is wordless (or rounds run out).
    Vision under-reports boxes per call — a single-pass scrub once left
    'sap vacuole' and two gibberish captions standing after removing five
    other words. Returns (clean ink, boxes still found — [] on success)."""
    rounds = 0
    while boxes and rounds < max_rounds:
        ink = scrub_text(ink, boxes)
        boxes = scan_text(ink)
        rounds += 1
    return ink, boxes


# ── post-processing ──────────────────────────────────────────────────────────

def to_ink(raw: Image.Image) -> Image.Image:
    """White background -> transparency; keep dark strokes with soft edges.
    alpha = how far below near-white each pixel's luminance sits."""
    rgb = raw.convert("RGB")
    # int32, not int16: 255*299 overflows int16 and wraps negative, which read
    # EVERY pixel as ink (a pure-white image scored 100% coverage)
    arr = np.asarray(rgb).astype(np.int32)
    lum = (arr[..., 0] * 299 + arr[..., 1] * 587 + arr[..., 2] * 114) // 1000
    alpha = np.clip((215 - lum) * 2.1, 0, 255).astype(np.uint8)
    out = np.dstack([np.asarray(rgb), alpha])
    img = Image.fromarray(out, "RGBA")
    # crop to content + a small margin so placement math means the drawing
    a = np.asarray(img.getchannel("A"))
    ys, xs = np.nonzero(a > 40)
    if len(xs) == 0:
        return img
    pad = 12
    x0, x1 = max(0, xs.min() - pad), min(img.width, xs.max() + pad)
    y0, y1 = max(0, ys.min() - pad), min(img.height, ys.max() + pad)
    return img.crop((x0, y0, x1, y1))


def to_color_art(raw: Image.Image) -> Image.Image:
    """Keep the artwork's COLOUR and cut ONLY the surrounding paper.

    Any luminance threshold is wrong here: a character's light hair, pale
    skin and white shirt are as bright as the page, so a brightness cut
    renders them ghostly (measured — the founder's screenshot showed white
    hair and washed faces). The background is instead found by FLOODING
    inward from the borders across near-white pixels: enclosed light areas
    are artwork and stay fully opaque.
    """
    from collections import deque

    rgb = raw.convert("RGB")
    arr = np.asarray(rgb).astype(np.int32)
    lum = (arr[..., 0] * 299 + arr[..., 1] * 587 + arr[..., 2] * 114) // 1000
    pale = lum >= 232                      # candidate paper
    h, w = pale.shape
    bg = np.zeros_like(pale)
    q = deque()
    for x in range(w):
        for y in (0, h - 1):
            if pale[y, x] and not bg[y, x]:
                bg[y, x] = True
                q.append((y, x))
    for y in range(h):
        for x in (0, w - 1):
            if pale[y, x] and not bg[y, x]:
                bg[y, x] = True
                q.append((y, x))
    while q:
        y, x = q.popleft()
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            ny, nx = y + dy, x + dx
            if 0 <= ny < h and 0 <= nx < w and pale[ny, nx] and not bg[ny, nx]:
                bg[ny, nx] = True
                q.append((ny, nx))
    alpha = np.where(bg, 0, 255).astype(np.uint8)
    # feather the cut by one pixel so edges do not alias against the board
    a_img = Image.fromarray(alpha, "L").filter(
        __import__("PIL.ImageFilter", fromlist=["ImageFilter"]).GaussianBlur(0.6))
    img = Image.fromarray(
        np.dstack([np.asarray(rgb), np.asarray(a_img)]), "RGBA")
    a = np.asarray(img.getchannel("A"))
    ys, xs = np.nonzero(a > 40)
    if len(xs) == 0:
        return img
    pad = 12
    x0, x1 = max(0, xs.min() - pad), min(img.width, xs.max() + pad)
    y0, y1 = max(0, ys.min() - pad), min(img.height, ys.max() + pad)
    return img.crop((x0, y0, x1, y1))


def scrub_text(ink: Image.Image, text_boxes: list[list[float]],
               pad: float = 3.0) -> Image.Image:
    """Erase baked-in text deterministically: zero the alpha inside each
    vision-reported text box (plus padding). Regeneration is a coin flip —
    this run's image model wrote labels twice in a row despite an escalated
    prohibition; erasure is a guarantee. Leader lines may lose a few pixels
    where a box clips them; §'no baked text' is the harder requirement."""
    if not text_boxes:
        return ink
    out = ink.copy()
    from PIL import ImageDraw as _ID
    a = out.getchannel("A").copy()
    d = _ID.Draw(a)
    for (x0, y0, x1, y1) in text_boxes:
        d.rectangle([x0 - pad, y0 - pad, x1 + pad, y1 + pad], fill=0)
    out.putalpha(a)
    return out


def _finish(key: str, ink: Image.Image, regions: dict | None = None,
            baked_text: bool = False) -> RasterAsset:
    alpha = np.asarray(ink.getchannel("A"))
    trace = drawing_order(alpha)
    return RasterAsset(key=key, ink=ink, trace=trace,
                       stamp_r=max(4.0, ink.width / 80.0),
                       world_scale=NOMINAL_WORLD_W / ink.width,
                       regions=regions or {}, baked_text=baked_text)


# ── cache + public API ───────────────────────────────────────────────────────

_ASSET_LOCKS: dict[str, "threading.Lock"] = {}
_LOCKS_GUARD = None  # created lazily so the module stays import-light


def get_raster_asset(key: str, prompt: str, cache_dir: Path | None = None,
                     allow_generate: bool = True) -> RasterAsset | None:
    """Per-key serialized: segments render in parallel threads, and a
    baked-text regeneration once raced the readers — two segments bound the
    flagged ink mid-rewrite."""
    global _LOCKS_GUARD
    import threading
    if _LOCKS_GUARD is None:
        _LOCKS_GUARD = threading.Lock()
    with _LOCKS_GUARD:
        lock = _ASSET_LOCKS.setdefault(key, threading.Lock())
    with lock:
        return _get_raster_asset(key, prompt, cache_dir, allow_generate)


def _get_raster_asset(key: str, prompt: str, cache_dir: Path | None = None,
                      allow_generate: bool = True) -> RasterAsset | None:
    # avatars are the one COLOUR tier: they are characters, not board ink,
    # and they are revealed rather than drawn
    is_color = key.startswith("avatar_")
    cache = (cache_dir or CACHE_DIR) / key
    png, meta = cache / "asset.png", cache / "meta.json"
    names = part_names_from_prompt(prompt)
    cached_fallback: RasterAsset | None = None   # baked-text cache, still usable
    if png.exists():
        try:
            ink = Image.open(png).convert("RGBA")
            md = {}
            try:
                md = json.loads(meta.read_text(encoding="utf-8"))
            except Exception:
                pass
            fresh_baked = False
            # backfill when annotation never ran, ran without part names, or
            # ran for a DIFFERENT name set (a prompt can learn its layer-group
            # tail later — the compiler appends one when it merges per-part
            # handles into the root). annotated_for pins the set so a part
            # vision genuinely cannot find is not re-asked every load.
            if names and (md.get("annotated_for") is None
                          or sorted(md.get("annotated_for") or [])
                          != sorted(names)):
                # lazy backfill: assets cached before region annotation
                ann = annotate_regions(ink, names)
                md["annotated_for"] = list(names)
                md["regions"], md["baked_text"] = ann["regions"], ann["has_text"]
                fresh_baked = bool(ann["has_text"])
                if ann.get("text_boxes"):
                    logger.warning("cached asset %r has baked text — scrubbing "
                                   "%d box(es)", key, len(ann["text_boxes"]))
                    ink, left = scrub_all_text(ink, ann["text_boxes"])
                    try:
                        ink.save(png)
                    except OSError:
                        pass
                    md["baked_text"], fresh_baked = bool(left), bool(left)
                try:
                    meta.write_text(json.dumps(md, indent=2), encoding="utf-8")
                except OSError:
                    pass
            if not (fresh_baked and allow_generate):
                return _finish(key, ink, md.get("regions"),
                               bool(md.get("baked_text")))
            # a pre-annotation cache with baked-in labels: fall through to the
            # generation path ONCE (it retries with the escalated prohibition
            # and re-caches). Only on the discovery run — a persistent flag in
            # meta means the retry already failed, and regenerating every run
            # would just burn image credits on the same outcome. The cached
            # ink stays as the fallback: a flagged asset beats no asset.
            logger.warning("cached asset %r has baked text — regenerating", key)
            cached_fallback = _finish(key, ink, md.get("regions"), True)
        except Exception:
            logger.exception("corrupt cached asset %s; regenerating", key)
    if not allow_generate:
        return None

    def generate(extra: str = "") -> Image.Image | None:
        # the layer-groups tail addresses the VISION annotator, never the
        # image model — left in, it reads as 'write these names' and the
        # model bakes exactly those labels into the art (measured: a cell
        # covered in 'membi'/'chloropsapts'/'mito!' gibberish)
        import re as _re
        gen_prompt = _re.sub(r"\s*name the layer groups exactly:[^.]*\.?",
                             "", prompt, flags=_re.I)
        suffix = _COLOR_SUFFIX if is_color else _STYLE_SUFFIX
        raw_bytes = _vertex_call(gen_prompt + suffix + extra) or \
            _aistudio_call(gen_prompt + suffix + extra)
        if raw_bytes is None:
            return None
        import io
        try:
            src = Image.open(io.BytesIO(raw_bytes))
            candidate = to_color_art(src) if is_color else to_ink(src)
        except Exception:
            logger.exception("un-decodable image for %r", key)
            return None
        # sanity: line art is mostly white space. A photo, a gray render, or
        # a solid fill turns almost entirely to "ink" — reject it. A COLOUR
        # character is legitimately dense, so its ceiling is far higher.
        a = np.asarray(candidate.getchannel("A"))
        coverage = float((a > 128).mean())
        hi = 0.92 if is_color else 0.45
        if not (0.005 <= coverage <= hi):
            logger.warning("image for %r rejected: ink coverage %.0f%%", key,
                           coverage * 100)
            return None
        return candidate

    ink = generate()
    if ink is None:
        if cached_fallback is not None:
            logger.warning("regeneration of %r failed — keeping the "
                           "baked-text cache, flagged for validation", key)
            return cached_fallback
        logger.warning("no image credentials/output for %r — vector fallback", key)
        return None
    ann = annotate_regions(ink, names)
    if ann.get("text_boxes"):
        # baked labels duplicate and contradict the engine's own labels —
        # scrub-and-RESCAN until wordless (vision under-reports per call: a
        # single-pass scrub once removed five words and left three standing)
        logger.warning("asset %r has baked text — scrubbing (%d box(es), "
                       "round 1)", key, len(ann["text_boxes"]))
        ink, left = scrub_all_text(ink, ann["text_boxes"])
        ann["has_text"] = bool(left)
        if left:
            logger.warning("asset %r still shows text after scrubbing — "
                           "flagged for validation", key)
    elif ann["has_text"]:
        # text seen but no boxes reported: ONE regeneration attempt with the
        # prohibition escalated, then a scrub attempt on the retry
        logger.warning("asset %r contains baked text; regenerating once", key)
        retry = generate(" CRITICAL: the image must contain ZERO letters, "
                         "words, numbers or labels of any kind.")
        if retry is not None:
            ann2 = annotate_regions(retry, names)
            if ann2["has_text"] and ann2.get("text_boxes"):
                ink, left = scrub_all_text(retry, ann2["text_boxes"])
                ann2["has_text"] = bool(left)
                ann = ann2
            elif not ann2["has_text"]:
                ink, ann = retry, ann2
            else:
                logger.warning("retry for %r still has text — keeping first, "
                               "flagged for validation", key)
    try:  # cache persistence is best-effort — never fail a good asset over IO
        cache.mkdir(parents=True, exist_ok=True)
        ink.save(png)
        meta.write_text(json.dumps({"key": key, "prompt": prompt,
                                    "model": IMAGE_MODEL,
                                    "provenance": "generated",
                                    "regions": ann["regions"],
                                    "annotated_for": list(names),
                                    "baked_text": ann["has_text"]}, indent=2),
                        encoding="utf-8")
    except OSError:
        logger.exception("could not cache asset %r (continuing uncached)", key)
    return _finish(key, ink, ann["regions"], ann["has_text"])


def load_hand(key: str = "hand_pen", cache_dir: Path | None = None,
              allow_generate: bool = True):
    """(hand RGBA, tip (x,y) in image px) for PenSprite, or None."""
    cache = (cache_dir or CACHE_DIR) / key
    png, meta = cache / "asset.png", cache / "meta.json"
    if not png.exists() and allow_generate:
        prompt = ("A single right hand holding a black marker pen, photographed from "
                  "above at a slight angle, fingers gripping the pen naturally, pen tip "
                  "pointing toward the lower left, isolated cut-out on a pure white "
                  "background, realistic, no shadows outside the hand, no text.")
        raw = _vertex_call(prompt) or _aistudio_call(prompt)
        if raw is not None:
            import io
            try:
                ink = to_ink(Image.open(io.BytesIO(raw)))
                cache.mkdir(parents=True, exist_ok=True)
                ink.save(png)
                a = np.asarray(ink.getchannel("A"))
                ys, xs = np.nonzero(a > 128)
                # the nib is the opaque pixel closest to the bottom-left corner
                tip_i = int(np.argmin(xs.astype(np.int64) + (ink.height - ys)))
                meta.write_text(json.dumps({"tip": [int(xs[tip_i]), int(ys[tip_i])],
                                            "model": IMAGE_MODEL}), encoding="utf-8")
            except Exception:
                logger.exception("hand asset post-process failed")
    if not png.exists():
        return None
    try:
        img = Image.open(png).convert("RGBA")
        tip = json.loads(meta.read_text(encoding="utf-8")).get("tip", [0, img.height])
        return img, (float(tip[0]), float(tip[1]))
    except Exception:
        return None


def make_resolver(prompts: dict[str, str], prefer_ai: bool = True,
                  cache_dir: Path | None = None, allow_generate: bool = True,
                  prefer_svg: bool | None = None):
    """Asset resolver for SceneRenderer implementing the §20 fallback ladder:
    AI svg (true layered vectors) -> AI raster -> authored vector -> None."""
    from .vector_assets import vector_asset

    # SVG art is behind a flag until its visual quality matches the raster
    # tier (its drawing MECHANICS are already better: true strokes, layers)
    if prefer_svg is None:
        prefer_svg = os.getenv("SCENE_SVG_ASSETS", "").strip() == "1"

    def resolve(key: str):
        if prefer_ai and key in prompts:
            if prefer_svg:
                from .svg_assets import get_svg_asset
                sa = get_svg_asset(key, prompts[key], cache_dir, allow_generate)
                if sa is not None:
                    return ("vector", sa)  # renders exactly like authored vectors
            ra = get_raster_asset(key, prompts[key], cache_dir, allow_generate)
            if ra is not None and ra.trace:
                return ("raster", ra)
        va = vector_asset(key)
        if va is not None:
            return ("vector", va)
        return None

    return resolve
