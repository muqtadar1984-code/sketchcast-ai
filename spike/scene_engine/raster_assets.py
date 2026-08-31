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
        res = requests.post(url, headers={"Authorization": f"Bearer {creds.token}"},
                            json=_body(prompt), timeout=120)
        res.raise_for_status()
        return _image_from(res.json())
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
        res = requests.post(url, headers={"x-goog-api-key": key},
                            json=_body(prompt), timeout=120)
        res.raise_for_status()
        return _image_from(res.json())
    except Exception as e:
        logger.warning("AI Studio image call failed: %s", e)
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
        res = requests.post(url, headers=headers, json=body, timeout=120)
        res.raise_for_status()
        for cand in res.json().get("candidates", []):
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
    if not part_names:
        return out
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
        "numbers, labels) in the image; empty list if there is none.\n"
        "Boxes are normalized 0-1000. For each of these part names, give a "
        "box around EACH visible instance of that part (a name may have "
        "several boxes, e.g. three mitochondria): "
        + ", ".join(part_names))
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
    return out


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
            if names and (not md.get("regions")
                          or sorted(md.get("annotated_for") or [])
                          != sorted(names)):
                # lazy backfill: assets cached before region annotation
                ann = annotate_regions(ink, names)
                md["annotated_for"] = list(names)
                md["regions"], md["baked_text"] = ann["regions"], ann["has_text"]
                fresh_baked = bool(ann["has_text"])
                if fresh_baked and ann.get("text_boxes"):
                    logger.warning("cached asset %r has baked text — scrubbing "
                                   "%d box(es)", key, len(ann["text_boxes"]))
                    ink = scrub_text(ink, ann["text_boxes"])
                    try:
                        ink.save(png)
                    except OSError:
                        pass
                    md["baked_text"], fresh_baked = False, False
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
        raw_bytes = _vertex_call(prompt + _STYLE_SUFFIX + extra) or \
            _aistudio_call(prompt + _STYLE_SUFFIX + extra)
        if raw_bytes is None:
            return None
        import io
        try:
            candidate = to_ink(Image.open(io.BytesIO(raw_bytes)))
        except Exception:
            logger.exception("un-decodable image for %r", key)
            return None
        # sanity: line art is mostly white space. A photo, a gray render, or
        # a solid fill turns almost entirely to "ink" — reject it.
        a = np.asarray(candidate.getchannel("A"))
        coverage = float((a > 128).mean())
        if not (0.005 <= coverage <= 0.45):
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
    if ann["has_text"] and ann.get("text_boxes"):
        # baked labels duplicate and contradict the engine's own labels —
        # scrubbing the reported boxes is deterministic where a regeneration
        # is a coin flip (this model labelled the cell twice in a row)
        logger.warning("asset %r has baked text — scrubbing %d box(es)", key,
                       len(ann["text_boxes"]))
        ink = scrub_text(ink, ann["text_boxes"])
        ann["has_text"] = False
    elif ann["has_text"]:
        # text seen but no boxes reported: ONE regeneration attempt with the
        # prohibition escalated, then a scrub attempt on the retry
        logger.warning("asset %r contains baked text; regenerating once", key)
        retry = generate(" CRITICAL: the image must contain ZERO letters, "
                         "words, numbers or labels of any kind.")
        if retry is not None:
            ann2 = annotate_regions(retry, names)
            if ann2["has_text"] and ann2.get("text_boxes"):
                ink = scrub_text(retry, ann2["text_boxes"])
                ann2["has_text"] = False
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
