"""Static output of a drawing: an SVG (millimetres) and a PNG (pixels),
from the same primitives, for worksheets, test papers and answer keys.

Scale is a policy decision. An EVIDENCE figure in centimetres prints at
TRUE SIZE — 1 unit = 10 mm — because the question says "measure the
sides"; anything else is fitted into a box. The returned width/height in
millimetres is what the document sets the image to, so a 6 cm side is
6 cm on paper.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import escape

from PIL import Image, ImageDraw, ImageFont

from maths.geometry.layout import Drawing, build_drawing
from maths.geometry.model import Model
from maths.geometry.spec import FigureSpec

_FONT_DIR = Path(__file__).resolve().parents[2] / "agent5_slides" / "fonts"
_FONT = _FONT_DIR / "DejaVuSans.ttf"
_FONT_ITALIC = _FONT_DIR / "DejaVuSans.ttf"

TRUE_SCALE_MM = {"cm": 10.0, "mm": 1.0, "m": 1000.0}
FIT_BOX_MM = (80.0, 60.0)
MIN_SCALE, MAX_SCALE = 6.0, 14.0
MARGIN_MM = 3.0
STROKE_MM = 0.35
LABEL_MM = 3.6
PALETTE = {"R": "#d9453b", "Y": "#f2c94c", "G": "#4caf50", "B": "#3b6fd9", "O": "#f28c28", "P": "#9b59b6",
           "W": "#ffffff", "K": "#333333"}


@dataclass
class Rendered:
    svg: str
    png: bytes
    width_mm: float
    height_mm: float
    scale_mm: float
    true_scale: bool
    notes: list[str]


def choose_scale(m: Model, bbox, *, role: str, units: Optional[str]) -> tuple[float, bool]:
    if role == "evidence" and units in TRUE_SCALE_MM:
        return TRUE_SCALE_MM[units], True
    w = max(1e-6, bbox[2] - bbox[0])
    h = max(1e-6, bbox[3] - bbox[1])
    s = min(FIT_BOX_MM[0] / w, FIT_BOX_MM[1] / h)
    return max(MIN_SCALE, min(MAX_SCALE, s)), False


def render_figure(m: Model, spec: FigureSpec, *, role: str = "reasoning", policy: str = "instructional_metric",
                  dpi: int = 200, show_hidden: bool = False, scale_mm: Optional[float] = None,
                  note: Optional[str] = None) -> Rendered:
    """``note`` is the "Not drawn to scale" text in the document's language
    (English when None); it is set in the script's own font."""
    # the scale comes from the figure itself, so labels can be sized in
    # millimetres (a fitted figure at 6 mm/unit must not get 2.5 mm text)
    if scale_mm is None:
        scale, true_scale = choose_scale(m, m.bbox(), role=role, units=spec.units)
    else:
        scale, true_scale = scale_mm, False
    d = build_drawing(m, spec, show_hidden=show_hidden, policy=policy, label_size=LABEL_MM / scale, note=note)
    x0, y0, x1, y1 = d.bbox
    note_h = 5.0 if d.notes else 0.0
    w_mm = (x1 - x0) * scale + 2 * MARGIN_MM
    h_mm = (y1 - y0) * scale + 2 * MARGIN_MM + note_h

    def tx(p):
        return ((p[0] - x0) * scale + MARGIN_MM, (y1 - p[1]) * scale + MARGIN_MM)

    svg = _svg(d, tx, w_mm, h_mm, scale)
    png = _png(d, tx, w_mm, h_mm, scale, dpi)
    return Rendered(svg, png, w_mm, h_mm, scale, true_scale, list(d.notes))


# ── SVG ───────────────────────────────────────────────────────────────────

def _svg(d: Drawing, tx, w_mm: float, h_mm: float, scale: float) -> str:
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w_mm:.2f}mm" height="{h_mm:.2f}mm" '
           f'viewBox="0 0 {w_mm:.2f} {h_mm:.2f}" font-family="DejaVu Sans, Arial, sans-serif">']
    for f in d.fills:
        pts = " ".join(f"{x:.3f},{y:.3f}" for x, y in (tx(p) for p in f.points))
        out.append(f'<polygon points="{pts}" fill="{PALETTE.get(f.colour, "#cccccc")}" stroke="none"/>')
    for s in d.strokes:
        pts = " ".join(f"{x:.3f},{y:.3f}" for x, y in (tx(p) for p in s.points))
        width = STROKE_MM * s.width
        colour = {"hidden": "#555555", "grid": "#b8b8b8"}.get(s.role, "#111111")   # v2: the grid is background
        dash = ' stroke-dasharray="1.2,0.8"' if s.dashed else ""
        out.append(f'<polyline points="{pts}" fill="none" stroke="{colour}" stroke-width="{width:.2f}" '
                   f'stroke-linecap="round" stroke-linejoin="round"{dash}/>')
    for dot in d.dots:
        x, y = tx((dot.x, dot.y))
        out.append(f'<circle cx="{x:.3f}" cy="{y:.3f}" r="{dot.r * scale:.2f}" fill="#111111"/>')
    for t in d.texts:
        x, y = tx((t.x, t.y))
        size = t.size * scale
        anchor = {"middle": "middle", "start": "start", "end": "end"}[t.anchor]
        style = ' font-style="italic"' if t.italic else ""
        out.append(f'<text x="{x:.3f}" y="{y:.3f}" font-size="{size:.2f}" text-anchor="{anchor}" '
                   f'dominant-baseline="central"{style} fill="#111111">{escape(t.text)}</text>')
    for i, note in enumerate(d.notes):
        out.append(f'<text x="{w_mm - 2:.2f}" y="{h_mm - 1.5 - i * 3.5:.2f}" font-size="2.6" text-anchor="end" '
                   f'font-style="italic" fill="#444444">{escape(note)}</text>')
    out.append("</svg>")
    return "\n".join(out)


# ── PNG ───────────────────────────────────────────────────────────────────

def _png(d: Drawing, tx, w_mm: float, h_mm: float, scale: float, dpi: int) -> bytes:
    px = dpi / 25.4
    W, H = max(1, int(math.ceil(w_mm * px))), max(1, int(math.ceil(h_mm * px)))
    # draw at 2x and downsample for clean lines
    S = 2
    img = Image.new("RGB", (W * S, H * S), "white")
    dr = ImageDraw.Draw(img)

    def P(p):
        x, y = tx(p)
        return (x * px * S, y * px * S)

    for f in d.fills:
        dr.polygon([P(p) for p in f.points], fill=PALETTE.get(f.colour, "#cccccc"))
    for s in d.strokes:
        width = max(1, int(round(STROKE_MM * s.width * px * S)))
        colour = {"hidden": "#555555", "grid": "#b8b8b8"}.get(s.role, "#111111")   # v2: the grid is background
        pts = [P(p) for p in s.points]
        if s.dashed:
            _dashed(dr, pts, width, colour, 1.2 * px * S, 0.8 * px * S)
        else:
            dr.line(pts, fill=colour, width=width, joint="curve")
    for dot in d.dots:
        x, y = P((dot.x, dot.y))
        r = dot.r * scale * px * S
        dr.ellipse([x - r, y - r, x + r, y + r], fill="#111111")
    for t in d.texts:
        size = max(6, int(round(t.size * scale * px * S)))
        font = ImageFont.truetype(str(_FONT), size)
        x, y = P((t.x, t.y))
        anchor = {"middle": "mm", "start": "lm", "end": "rm"}[t.anchor]
        dr.text((x, y), t.text, fill="#111111", font=font, anchor=anchor)
    for i, note in enumerate(d.notes):
        # the note may be Arabic, Devanagari or Telugu: the slide builder's
        # font picker and shaper know which face and shaping each needs
        from agent5_slides.slide_builder import _font as script_font  # noqa: PLC0415
        from shared.text_shaping import display_text  # noqa: PLC0415
        font = script_font(False, max(6, int(round(2.6 * px * S))), note)
        dr.text(((w_mm - 2) * px * S, (h_mm - 1.5 - i * 3.5) * px * S), display_text(note), fill="#444444",
                font=font, anchor="rm")
    img = img.resize((W, H), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", dpi=(dpi, dpi))
    return buf.getvalue()


def _dashed(dr, pts, width, colour, on, off) -> None:
    for p, q in zip(pts, pts[1:]):
        length = math.dist(p, q)
        if length < 1e-9:
            continue
        ux, uy = (q[0] - p[0]) / length, (q[1] - p[1]) / length
        t = 0.0
        while t < length:
            t2 = min(length, t + on)
            dr.line([(p[0] + ux * t, p[1] + uy * t), (p[0] + ux * t2, p[1] + uy * t2)], fill=colour, width=width)
            t = t2 + off
