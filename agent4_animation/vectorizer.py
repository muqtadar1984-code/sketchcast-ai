"""Agent 4: Vectorizer — converts raster images into Scribe-compatible dual-layer SVGs.

Pipeline
--------
1.  Nano Banana Pro (or Gemini) generates a line-art PNG from a text prompt.
2.  ``Vectorizer.image_to_scribe_paths()`` traces the raster to SVG path data.
3.  ``Vectorizer.save_svg_from_paths()`` wraps those paths in a ghost + ink
    dual-layer SVG ready for the Scribe player.

When a real tracing backend (potrace / vtracer) is unavailable, the stub
returns a placeholder rectangle so the rest of the pipeline stays intact.
"""

from __future__ import annotations

import logging
import math
import uuid
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)

# ── SVG constants ─────────────────────────────────────────────────────
_CANVAS_W = 1280
_CANVAS_H = 720

_SVG_HEADER = (
    f'<svg xmlns="http://www.w3.org/2000/svg" '
    f'viewBox="0 0 {_CANVAS_W} {_CANVAS_H}" '
    f'width="{_CANVAS_W}" height="{_CANVAS_H}">\n'
    '<defs>\n'
    '  <filter id="hand-drawn" x="-2%" y="-2%" width="104%" height="104%">\n'
    '    <feTurbulence type="turbulence" baseFrequency="0.02" '
    'numOctaves="3" result="noise" seed="1"/>\n'
    '    <feDisplacementMap in="SourceGraphic" in2="noise" '
    'scale="1.5" xChannelSelector="R" yChannelSelector="G"/>\n'
    '  </filter>\n'
    '</defs>\n'
    f'<rect width="{_CANVAS_W}" height="{_CANVAS_H}" fill="#FAFAFA"/>\n'
)

_SVG_FOOTER = "</svg>\n"


class Vectorizer:
    """Convert raster line-art images to Scribe-compatible SVG path data.

    All public methods are **static** so the class can be used without
    instantiation (matching the call pattern in ``sketch_generator.py``).
    """

    # ── public API ────────────────────────────────────────────────────

    @staticmethod
    def image_to_scribe_paths(image_path: str) -> List[dict]:
        """Trace a raster image into a list of SVG path-data dicts.

        Each dict contains the fields expected by the Scribe player:

        * ``path_id``      – unique identifier for the path element
        * ``d``            – SVG ``<path d="...">`` attribute
        * ``total_length`` – stroke length in px (for dashoffset animation)
        * ``stroke_color`` – hex colour string
        * ``stroke_width`` – line width in px

        Production path (TODO — uncomment when tracing backend is ready)
        -----------------------------------------------------------------
        1. Load image with Pillow.
        2. Convert to greyscale → threshold to binary.
        3. Trace outlines via **potrace** (subprocess) or **vtracer** (Rust).
        4. Parse the resulting SVG to extract ``<path d=...>`` elements.
        5. Calculate ``total_length`` for each path.

        Stub behaviour
        --------------
        Returns a single placeholder rectangle path so downstream code
        (``save_svg_from_paths``, manifest builder) exercises the real
        code paths without a tracing backend installed.
        """
        img = Path(image_path)
        if not img.exists():
            logger.warning("Vectorizer: image not found at %s — returning empty paths", image_path)
            return []

        # ── attempt real vectorisation ────────────────────────────────
        try:
            return Vectorizer._trace_with_potrace(image_path)
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "Vectorizer: potrace unavailable (%s) — falling back to placeholder",
                exc,
            )

        # ── stub / placeholder ────────────────────────────────────────
        return Vectorizer._placeholder_paths()

    @staticmethod
    def save_svg_from_paths(paths: List[dict], svg_path: str) -> None:
        """Write a complete dual-layer (ghost + ink) SVG file from path data.

        The ghost layer is a faint dashed outline visible immediately.
        The ink layer starts fully hidden (``stroke-dashoffset == length``)
        and is revealed progressively by the Scribe player.
        """
        parts: list[str] = [_SVG_HEADER]

        for pd in paths:
            markup = Vectorizer._dual_layer_markup(pd)
            if markup:
                parts.append(markup)

        parts.append(_SVG_FOOTER)

        out = Path(svg_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("".join(parts), encoding="utf-8")
        logger.info("Vectorizer: saved SVG with %d paths → %s", len(paths), svg_path)

    # ── internals ─────────────────────────────────────────────────────

    @staticmethod
    def _dual_layer_markup(path_data: dict) -> str:
        """Generate ghost + ink ``<g>`` element for one path.

        Mirrors the pattern used in ``svg_utils.SVGPathConverter.generate_dual_layer_svg()``.
        """
        d = path_data.get("d", "")
        total_length = path_data.get("total_length", 0)
        path_id = path_data.get("path_id", f"p_{uuid.uuid4().hex[:6]}")
        color = path_data.get("stroke_color", "#2C3E50")
        sw = path_data.get("stroke_width", 2)

        if not d:
            return ""

        ghost = (
            f'  <path d="{d}" stroke="{color}" stroke-width="{sw}" fill="none" '
            f'stroke-opacity="0.1" stroke-dasharray="5,5" '
            f'stroke-linecap="round" stroke-linejoin="round" class="ghost-layer"/>'
        )

        ink = (
            f'  <path id="{path_id}" d="{d}" stroke="{color}" stroke-width="{sw}" '
            f'fill="none" stroke-opacity="1.0" '
            f'stroke-dasharray="{total_length}" stroke-dashoffset="{total_length}" '
            f'stroke-linecap="round" stroke-linejoin="round" '
            f'data-length="{total_length}" class="ink-layer"/>'
        )

        return f'<g id="g-{path_id}" filter="url(#hand-drawn)">\n{ghost}\n{ink}\n</g>\n'

    @staticmethod
    def _placeholder_paths() -> List[dict]:
        """Return a simple centred rectangle as placeholder path data.

        Used when no real tracing backend is available so the pipeline
        can still exercise SVG creation end-to-end.
        """
        # Centred 400×250 rounded rectangle
        x, y, w, h, r = 440, 235, 400, 250, 12
        d = (
            f"M {x + r} {y} "
            f"H {x + w - r} A {r} {r} 0 0 1 {x + w} {y + r} "
            f"V {y + h - r} A {r} {r} 0 0 1 {x + w - r} {y + h} "
            f"H {x + r} A {r} {r} 0 0 1 {x} {y + h - r} "
            f"V {y + r} A {r} {r} 0 0 1 {x + r} {y} Z"
        )
        straight = 2 * (w - 2 * r) + 2 * (h - 2 * r)
        arcs = 2 * math.pi * r
        total_length = round(straight + arcs, 2)

        return [
            {
                "path_id": "placeholder_rect",
                "d": d,
                "total_length": total_length,
                "stroke_color": "#2C3E50",
                "stroke_width": 2,
                "fill": "none",
                "draw_order": 0,
                "element_type": "path",
            }
        ]

    @staticmethod
    def _trace_with_potrace(image_path: str) -> List[dict]:
        """Trace a raster image to SVG paths using potrace (subprocess).

        Raises ``RuntimeError`` if potrace is not installed or the trace fails.

        TODO: Replace with vtracer Python bindings for better quality
              and no subprocess overhead when available.
        """
        import subprocess
        import tempfile
        import xml.etree.ElementTree as ET

        from PIL import Image

        img = Image.open(image_path).convert("L")

        # Threshold to 1-bit PBM (potrace input format)
        bw = img.point(lambda px: 0 if px < 128 else 255, "1")

        with tempfile.NamedTemporaryFile(suffix=".bmp", delete=False) as tmp_bmp:
            bw.save(tmp_bmp.name)
            bmp_path = tmp_bmp.name

        svg_tmp = bmp_path.replace(".bmp", "_traced.svg")

        try:
            subprocess.run(
                [
                    "potrace",
                    bmp_path,
                    "-s",           # SVG output
                    "--flat",       # no grouping
                    "-t", "5",      # suppress speckles < 5px
                    "-o", svg_tmp,
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("potrace binary not found on PATH") from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"potrace failed: {exc.stderr.decode()[:200]}") from exc

        # Parse the SVG and extract paths
        tree = ET.parse(svg_tmp)
        root = tree.getroot()
        ns = {"svg": "http://www.w3.org/2000/svg"}
        paths: List[dict] = []

        for idx, path_el in enumerate(root.iter("{http://www.w3.org/2000/svg}path")):
            d = path_el.get("d", "")
            if not d:
                continue
            # Estimate path length (rough heuristic — count M/L/C commands)
            cmd_count = sum(1 for ch in d if ch.isalpha())
            est_length = round(cmd_count * 15.0, 2)  # ~15px per command avg

            paths.append(
                {
                    "path_id": f"traced_{idx:03d}",
                    "d": d,
                    "total_length": est_length,
                    "stroke_color": "#2C3E50",
                    "stroke_width": 2,
                    "fill": "none",
                    "draw_order": idx,
                    "element_type": "path",
                }
            )

        # Clean up temp files
        Path(bmp_path).unlink(missing_ok=True)
        Path(svg_tmp).unlink(missing_ok=True)

        if not paths:
            logger.warning("Vectorizer: potrace produced no paths — falling back to placeholder")
            return Vectorizer._placeholder_paths()

        logger.info("Vectorizer: potrace traced %d paths from %s", len(paths), image_path)
        return paths
