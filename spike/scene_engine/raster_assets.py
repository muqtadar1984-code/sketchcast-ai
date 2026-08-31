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


def _finish(key: str, ink: Image.Image) -> RasterAsset:
    alpha = np.asarray(ink.getchannel("A"))
    trace = drawing_order(alpha)
    return RasterAsset(key=key, ink=ink, trace=trace,
                       stamp_r=max(4.0, ink.width / 80.0),
                       world_scale=NOMINAL_WORLD_W / ink.width)


# ── cache + public API ───────────────────────────────────────────────────────

def get_raster_asset(key: str, prompt: str, cache_dir: Path | None = None,
                     allow_generate: bool = True) -> RasterAsset | None:
    cache = (cache_dir or CACHE_DIR) / key
    png, meta = cache / "asset.png", cache / "meta.json"
    if png.exists():
        try:
            return _finish(key, Image.open(png).convert("RGBA"))
        except Exception:
            logger.exception("corrupt cached asset %s; regenerating", key)
    if not allow_generate:
        return None
    raw_bytes = _vertex_call(prompt + _STYLE_SUFFIX) or _aistudio_call(prompt + _STYLE_SUFFIX)
    if raw_bytes is None:
        logger.warning("no image credentials/output for %r — vector fallback", key)
        return None
    import io
    try:
        ink = to_ink(Image.open(io.BytesIO(raw_bytes)))
    except Exception:
        logger.exception("un-decodable image for %r", key)
        return None
    # sanity: line art is mostly white space. A photo, a gray render, or a
    # solid fill turns almost entirely to "ink" — reject it (vector fallback)
    # instead of tracing a black rectangle onto the whiteboard.
    a = np.asarray(ink.getchannel("A"))
    coverage = float((a > 128).mean())
    if not (0.005 <= coverage <= 0.45):
        logger.warning("image for %r rejected: ink coverage %.0f%% is not line "
                       "art — vector fallback", key, coverage * 100)
        return None
    try:  # cache persistence is best-effort — never fail a good asset over IO
        cache.mkdir(parents=True, exist_ok=True)
        ink.save(png)
        meta.write_text(json.dumps({"key": key, "prompt": prompt, "model": IMAGE_MODEL,
                                    "provenance": "generated"}, indent=2), encoding="utf-8")
    except OSError:
        logger.exception("could not cache asset %r (continuing uncached)", key)
    return _finish(key, ink)


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
