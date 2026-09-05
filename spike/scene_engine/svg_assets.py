"""SVG-native AI assets — generated illustrations as TRUE layered vectors.

The raster tier's weakness is structural: one flat image, revealed along a
guessed pen walk, semantic layers approximated by slicing. This tier asks the
model for SVG *markup* instead — a TEXT generation, so it runs on Vertex AND
on the free-tier AI Studio key — and parses the paths into the same
VectorAsset the authored tier uses. Result: generated art with genuine
stroke-by-stroke reveal, real named layers ("wall", "nucleus", ...) that cues
can address, and crisp rendering at any camera zoom.

Ladder position: svg -> raster -> authored vector (make_resolver). Every
failure mode — refused output, unparseable markup, degenerate geometry —
returns None and falls through; a lesson never fails because an asset did.

The path parser is deliberately minimal (M L H V C Q S T Z, absolute and
relative; arcs degrade to a line to the endpoint) because the prompt forbids
everything else and the validator rejects what slips through anyway.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from pathlib import Path

import requests

from shared.asset_keys import canonical_key

from .geometry import Point, path_length, resample, roughen
from .svg_validate import (SvgValidation, is_valid_group_id,
                           validate_svg_document)
from .vector_assets import VectorAsset, VLayer, VStroke

logger = logging.getLogger(__name__)

__all__ = [
    "CACHE_DIR", "NOMINAL_W", "SvgValidation", "extract_svg_document",
    "get_svg_asset", "is_valid_group_id", "parse_path_d", "parse_svg_asset",
    "svg_cache_dir", "svg_group_ids", "validate_svg_document",
]

SVG_MODEL = os.getenv("GEMINI_SVG_MODEL", "gemini-2.5-flash")
CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "storage" / "scene_assets"
NOMINAL_W = 800.0  # parsed assets normalize to this width, like authored ones

_SVG_RULES = """
Return ONLY SVG markup — no prose, no markdown fences, no XML declaration.
STRICT RULES:
- <svg viewBox="0 0 800 600"> exactly.
- Content is ONLY <g id="..."> groups containing ONLY <path> elements.
- Group ids name the diagram's parts (lowercase_snake_case), and groups appear
  in DRAWING ORDER: main outline first, then internal structure, then details.
  Use 4 to 12 groups, 1 to 8 paths each.
- Every path: stroke="black" (or a single dark accent color for one featured
  part), fill="none", stroke-width between 3.5 and 5.
- Path data uses ONLY M, L, H, V, C, Q, Z commands. NO arcs (A), NO
  transforms, NO text, NO rect/circle/ellipse/line elements, NO defs, NO use.
- Draw with long, smooth, confident C-curves — organic shapes need many
  control points, never polygonal approximations.
- Be RICH like a good textbook diagram: every part carries interior detail
  (internal lines, folds, small structures), not just an outline blob.
- No shading, no hatching fills, no labels (the renderer adds labels).
"""


# ── generation transport (text) ──────────────────────────────────────────────

def _gen_text(prompt: str, model: str | None = None) -> str | None:
    """Text generation via the prod Gemini stack: Vertex first (credited),
    AI Studio key fallback (text runs on the free tier). Shared by the SVG
    tier and the live director (direct.py)."""
    model = model or SVG_MODEL
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
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
            url = (f"https://{host}/v1/projects/{project}/locations/{region}"
                   f"/publishers/google/models/{model}:generateContent")
            res = requests.post(url, headers={"Authorization": f"Bearer {creds.token}"},
                                json=body, timeout=120)
            res.raise_for_status()
            return _text_from(res.json())
        except Exception as e:
            logger.warning("Vertex SVG call failed (%s); trying AI Studio", e)
    key = os.getenv("GOOGLE_AI_API_KEY", "") or os.getenv("GEMINI_API_KEY", "")
    if not key:
        return None
    try:  # text models DO run on the free tier, unlike image models
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{model}:generateContent")
        res = requests.post(url, headers={"x-goog-api-key": key}, json=body,
                            timeout=120)
        res.raise_for_status()
        return _text_from(res.json())
    except Exception as e:
        logger.warning("AI Studio SVG call failed: %s", e)
        return None


def _text_from(payload: dict) -> str | None:
    for cand in payload.get("candidates", []):
        parts = cand.get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts)
        if text.strip():
            return text
    return None


# ── minimal SVG path parser ──────────────────────────────────────────────────

_NUM = re.compile(r"[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?")
_CMD = re.compile(r"([MmLlHhVvCcQqSsTtAaZz])")


def _bezier(p0: Point, ctrl: list[Point], p1: Point, steps: int = 16) -> list[Point]:
    pts: list[Point] = []
    for i in range(1, steps + 1):
        t = i / steps
        pts_all = [p0] + ctrl + [p1]
        while len(pts_all) > 1:  # de Casteljau
            pts_all = [((1 - t) * a[0] + t * b[0], (1 - t) * a[1] + t * b[1])
                       for a, b in zip(pts_all, pts_all[1:])]
        pts.append(pts_all[0])
    return pts


def parse_path_d(d: str) -> list[list[Point]]:
    """Path data -> subpaths as polylines. Unknown/arc commands degrade to a
    straight line to their endpoint; malformed tails are dropped, not fatal."""
    tokens = [t for t in _CMD.split(d) if t.strip()]
    subpaths: list[list[Point]] = []
    cur: list[Point] = []
    pos: Point = (0.0, 0.0)
    start: Point = (0.0, 0.0)
    prev_ctrl: Point | None = None
    cmd = None
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if _CMD.fullmatch(tok):
            cmd = tok
            i += 1
            nums = []
        else:
            nums = [float(x) for x in _NUM.findall(tok)]
            i += 1
        if cmd is None or not cmd:
            continue
        rel = cmd.islower()
        C = cmd.upper()

        def pt(x: float, y: float) -> Point:
            return (pos[0] + x, pos[1] + y) if rel else (x, y)

        try:
            if C == "Z":
                if cur:
                    cur.append(start)
                pos = start
                prev_ctrl = None
                continue
            if not nums:
                continue
            j = 0
            while j < len(nums):
                if C == "M":
                    p = pt(nums[j], nums[j + 1]); j += 2
                    if cur and len(cur) > 1:
                        subpaths.append(cur)
                    cur = [p]; pos = p; start = p
                    C = "L"  # subsequent pairs are implicit lineto
                elif C == "L":
                    p = pt(nums[j], nums[j + 1]); j += 2
                    cur.append(p); pos = p
                elif C == "H":
                    p = (pos[0] + nums[j], pos[1]) if rel else (nums[j], pos[1]); j += 1
                    cur.append(p); pos = p
                elif C == "V":
                    p = (pos[0], pos[1] + nums[j]) if rel else (pos[0], nums[j]); j += 1
                    cur.append(p); pos = p
                elif C == "C":
                    c1, c2 = pt(nums[j], nums[j + 1]), pt(nums[j + 2], nums[j + 3])
                    p = pt(nums[j + 4], nums[j + 5]); j += 6
                    cur.extend(_bezier(pos, [c1, c2], p)); prev_ctrl = c2; pos = p
                elif C == "Q":
                    c1 = pt(nums[j], nums[j + 1]); p = pt(nums[j + 2], nums[j + 3]); j += 4
                    cur.extend(_bezier(pos, [c1], p)); prev_ctrl = c1; pos = p
                elif C in ("S", "T"):
                    # smooth variants: reflect the previous control point
                    refl = ((2 * pos[0] - prev_ctrl[0], 2 * pos[1] - prev_ctrl[1])
                            if prev_ctrl else pos)
                    if C == "S":
                        c2 = pt(nums[j], nums[j + 1]); p = pt(nums[j + 2], nums[j + 3]); j += 4
                        cur.extend(_bezier(pos, [refl, c2], p)); prev_ctrl = c2
                    else:
                        p = pt(nums[j], nums[j + 1]); j += 2
                        cur.extend(_bezier(pos, [refl], p)); prev_ctrl = refl
                    pos = p
                elif C == "A":  # forbidden by prompt; degrade to a line
                    p = pt(nums[j + 5], nums[j + 6]); j += 7
                    cur.append(p); pos = p
                else:
                    break
                if C not in ("C", "Q", "S", "T"):
                    prev_ctrl = None
        except IndexError:
            break  # malformed tail — keep what parsed
    if cur and len(cur) > 1:
        subpaths.append(cur)
    return subpaths


# ── SVG document -> VectorAsset ──────────────────────────────────────────────

_VIEWBOX = re.compile(r'viewBox\s*=\s*"([\d.\s+-]+)"')
_GROUP = re.compile(r"<g\b[^>]*?\bid\s*=\s*\"([^\"]+)\"[^>]*>(.*?)</g>", re.S)
_PATH = re.compile(r"<path\b[^>]*?>", re.S)
_ATTR_D = re.compile(r'\bd\s*=\s*"([^"]+)"')
_ATTR_W = re.compile(r'\bstroke-width\s*=\s*"([\d.]+)"')
_ATTR_S = re.compile(r'\bstroke\s*=\s*"([^"]+)"')
_DOC = re.compile(r"<svg\b.*?</svg>", re.S)


def extract_svg_document(text: str) -> str | None:
    """The ``<svg>…</svg>`` slice of a model reply, or None.

    A generation arrives wrapped in prose or markdown fences; the STORED asset
    is this slice and nothing else, so validation and parsing see the same
    bytes the library will serve.
    """
    m = _DOC.search(str(text or ""))
    return m.group(0) if m else None


def svg_group_ids(svg_text: str) -> list[str]:
    """The group ids of a document, EXACTLY as written, in drawing order.

    Storage is exact: this is what goes into the library row, because the ids
    are the labelling contract a lesson will address. Matching stays tolerant
    elsewhere (`vector_assets.match_layer_ids`), and validation is exact and
    unforgiving (`validate_svg_document`) — three different jobs.
    """
    doc = extract_svg_document(svg_text) or str(svg_text or "")
    return [m.group(1) for m in _GROUP.finditer(doc)]


def svg_cache_dir(cache_dir: Path | None, key: str) -> Path:
    """Where an SVG asset for `key` lives on disk.

    Keyed by CANONICAL identity, like the raster tier and like the library, so
    a hydrated download lands where the renderer will read it — the bug that
    made every *_cell library hit a paid regeneration. The ``svg_`` prefix
    keeps the markup in its own directory beside the PNG cache rather than
    sharing one meta.json between two formats.
    """
    return (cache_dir or CACHE_DIR) / f"svg_{canonical_key(key)}"


def parse_svg_asset(key: str, svg_text: str) -> VectorAsset | None:
    """SVG markup -> VectorAsset (normalized to NOMINAL_W wide, hand-roughened).
    Returns None for anything that would not read as a layered line diagram."""
    doc = extract_svg_document(svg_text)
    if doc is None:
        return None
    vb = _VIEWBOX.search(doc)
    try:
        _, _, vbw, vbh = [float(x) for x in vb.group(1).split()] if vb else (0, 0, 800, 600)
    except ValueError:
        vbw, vbh = 800.0, 600.0
    if vbw <= 0 or vbh <= 0:
        return None
    k = NOMINAL_W / vbw

    def build_strokes(block: str, li: int) -> list[VStroke]:
        strokes: list[VStroke] = []
        for si, tag in enumerate(_PATH.findall(block)):
            dm = _ATTR_D.search(tag)
            if not dm:
                continue
            wm = _ATTR_W.search(tag)
            width = min(6.0, max(1.5, float(wm.group(1)) if wm else 3.0)) * k
            sm = _ATTR_S.search(tag)
            stroke = (sm.group(1) if sm else "black").lower()
            color = "ink" if stroke in ("black", "#000", "#000000", "none") else "accent"
            for pts in parse_path_d(dm.group(1)):
                scaled = [(x * k, y * k) for x, y in pts]
                # clip runaway geometry instead of trusting the model
                if any(not (-0.3 * NOMINAL_W < x < 1.3 * NOMINAL_W and
                            -0.3 * vbh * k < y < 1.3 * vbh * k) for x, y in scaled):
                    continue
                if path_length(scaled) < 8.0 * k:
                    continue
                import zlib
                seed = zlib.crc32(f"{key}:{li}:{si}".encode()) & 0xFFFF
                sm_pts = roughen(resample(scaled, 6.0), amplitude=0.7,
                                 wobble=1.6, seed=seed)
                strokes.append(VStroke(tuple(sm_pts), width, color))
        return strokes

    layers: list[VLayer] = []
    consumed_spans: list[tuple[int, int]] = []
    for li, gm in enumerate(_GROUP.finditer(doc)):
        # An id that already satisfies the contract is kept VERBATIM. The
        # rewrite below is a runtime repair for markup that would be refused
        # at publish anyway (a stray capital, a hyphen, a space) — it must not
        # touch a valid id, because the stored id is the labelling contract
        # and a silently normalised one no longer names what the row says.
        raw = gm.group(1).strip()
        gid = raw if is_valid_group_id(raw) else (
            re.sub(r"[^a-z0-9_]+", "_", raw.lower()).strip("_") or f"layer{li}")
        strokes = build_strokes(gm.group(2), li)
        if strokes:
            layers.append(VLayer(gid, tuple(strokes)))
        consumed_spans.append(gm.span())
    # paths outside any group land in one trailing layer
    rest = doc
    for a, b in sorted(consumed_spans, reverse=True):
        rest = rest[:a] + rest[b:]
    loose = build_strokes(rest, 99)
    if loose:
        layers.append(VLayer("detail", tuple(loose)))

    asset = VectorAsset(key, NOMINAL_W, vbh * k, tuple(layers))
    total = asset.ink_length()
    if len(layers) < 2 or sum(len(l.strokes) for l in layers) < 3 or \
            not (400.0 <= total <= 90000.0):
        logger.warning("SVG for %r rejected: %d layers, ink %.0fpx", key,
                       len(layers), total)
        return None
    return asset


# ── cache + public API ───────────────────────────────────────────────────────

def get_svg_asset(key: str, prompt: str, cache_dir: Path | None = None,
                  allow_generate: bool = True) -> VectorAsset | None:
    cache = svg_cache_dir(cache_dir, key)
    svg_file, meta = cache / "asset.svg", cache / "meta.json"
    if svg_file.exists():
        asset = parse_svg_asset(key, svg_file.read_text(encoding="utf-8"))
        if asset is not None:
            return asset
        logger.warning("cached SVG for %r no longer parses; regenerating", key)
    if not allow_generate:
        return None
    text = _gen_text(f"Draw this as an educational diagram: {prompt}\n{_SVG_RULES}")
    if not text:
        return None
    asset = parse_svg_asset(key, text)
    if asset is None:
        return None
    try:  # cache best-effort
        cache.mkdir(parents=True, exist_ok=True)
        doc = extract_svg_document(text) or text
        # newline="\n" on purpose. The stored bytes are hashed for publish
        # idempotency, so letting Windows translate LF to CRLF would give the
        # same asset two content hashes and two library rows depending on
        # which machine generated it.
        svg_file.write_text(doc, encoding="utf-8", newline="\n")
        meta.write_text(json.dumps({"key": key, "prompt": prompt,
                                    "model": SVG_MODEL,
                                    # "generated", spelled the same way the
                                    # raster tier spells it: the visual-library
                                    # wrapper publishes what it generated and
                                    # must never re-publish what it hydrated,
                                    # and it decides that by reading this word.
                                    "provenance": "generated",
                                    "asset_format": "svg",
                                    # exact ids, drawing order — the row's
                                    # labelling contract
                                    "group_ids": svg_group_ids(doc),
                                    "layers": asset.layer_ids()}, indent=2),
                        encoding="utf-8")
    except OSError:
        logger.exception("could not cache SVG asset %r", key)
    return asset
