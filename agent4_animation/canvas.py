"""Agent 4: SVG Canvas — whiteboard sketch drawing primitives.

Build an SVG document by calling draw_* methods, then call to_svg()
to get the complete document string.  All coordinates are in pixels
on a default 1280 × 720 canvas.

Enhancement: every generated SVG now applies SVG filter effects that
make clean geometric shapes look hand-drawn and slightly wobbly, and
includes a subtle paper-texture background.
"""

import hashlib
import math
from typing import List

CANVAS_WIDTH = 1280
CANVAS_HEIGHT = 720
BG_COLOR = "#FAFAFA"

PRIMARY = "#2C3E50"
BLUE = "#4A90D9"
ORANGE = "#E67E22"
GREEN = "#27AE60"
PURPLE = "#9B59B6"
RED = "#E74C3C"

FONT = "Arial, Helvetica, sans-serif"

_BRANCH_COLORS = [BLUE, ORANGE, GREEN, PURPLE, RED, "#1ABC9C", "#F39C12", "#16A085"]

# ── SVG filter defs (hand-drawn + paper texture) ─────────────────────

_SVG_DEFS = """<defs>
  <filter id="hand-drawn" x="-5%" y="-5%" width="110%" height="110%">
    <feTurbulence
      type="fractalNoise"
      baseFrequency="0.035"
      numOctaves="3"
      seed="2"
      result="noise"/>
    <feDisplacementMap
      in="SourceGraphic"
      in2="noise"
      scale="2.5"
      xChannelSelector="R"
      yChannelSelector="G"/>
  </filter>
  <filter id="hand-drawn-text" x="-5%" y="-5%" width="110%" height="110%">
    <feTurbulence
      type="fractalNoise"
      baseFrequency="0.025"
      numOctaves="2"
      seed="5"
      result="noise"/>
    <feDisplacementMap
      in="SourceGraphic"
      in2="noise"
      scale="1.2"
      xChannelSelector="R"
      yChannelSelector="G"/>
  </filter>
  <filter id="paper">
    <feTurbulence type="fractalNoise" baseFrequency="0.65" numOctaves="3" stitchTiles="stitch"/>
    <feColorMatrix type="saturate" values="0"/>
    <feBlend in="SourceGraphic" mode="multiply"/>
  </filter>
</defs>"""


# ── style helpers ─────────────────────────────────────────────────────

def _seeded_stroke(base: float, seed: str) -> float:
    """Return a slightly varied stroke width (range 1.8–2.4) using a deterministic seed."""
    h = int(hashlib.md5(seed.encode()).hexdigest()[:4], 16)
    delta = (h % 13) * 0.05          # 0 … 0.60 in steps of 0.05
    varied = max(base - 0.2, 1.6) + delta * 0.4
    return round(varied, 2)


def _wobbly_path(x1: float, y1: float, x2: float, y2: float, seed: str) -> str:
    """Convert a straight line to a slightly wobbly cubic bezier SVG path (max ±3 px)."""
    h1 = int(hashlib.md5((seed + "a").encode()).hexdigest()[:4], 16)
    h2 = int(hashlib.md5((seed + "b").encode()).hexdigest()[:4], 16)
    ox1 = (h1 % 7) - 3               # −3 … +3
    oy1 = ((h1 >> 4) % 7) - 3
    ox2 = (h2 % 7) - 3
    oy2 = ((h2 >> 4) % 7) - 3
    dx, dy = x2 - x1, y2 - y1
    cp1x, cp1y = x1 + dx / 3 + ox1, y1 + dy / 3 + oy1
    cp2x, cp2y = x1 + 2 * dx / 3 + ox2, y1 + 2 * dy / 3 + oy2
    return (
        f"M{x1:.1f},{y1:.1f} "
        f"C{cp1x:.1f},{cp1y:.1f} {cp2x:.1f},{cp2y:.1f} {x2:.1f},{y2:.1f}"
    )


def _paper_bg(width: int, height: int, color: str) -> str:
    """Return SVG markup for a paper-textured background."""
    return (
        f'<rect width="{width}" height="{height}" fill="{color}"/>\n'
        f'  <rect width="{width}" height="{height}" '
        f'fill="#A08060" opacity="0.04" filter="url(#paper)"/>'
    )


def _esc(text: str) -> str:
    """Escape XML special characters."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ── SVG canvas class ─────────────────────────────────────────────────

class SVGCanvas:
    """Accumulates SVG elements and renders them into a complete SVG document."""

    def __init__(
        self,
        width: int = CANVAS_WIDTH,
        height: int = CANVAS_HEIGHT,
        bg_color: str = BG_COLOR,
    ) -> None:
        self.width = width
        self.height = height
        self.bg_color = bg_color
        self._elements: List[str] = []

    # ── basic primitives ─────────────────────────────────────────────

    def draw_circle(
        self,
        cx: float,
        cy: float,
        r: float,
        color: str = PRIMARY,
        stroke_width: float = 2,
        fill: str = "none",
    ) -> str:
        """Draw a circle as a continuous path for Speed Paint animation."""
        sw = _seeded_stroke(stroke_width, f"c{cx:.0f}{cy:.0f}{r:.0f}")
        d = (
            f"M {cx - r} {cy} "
            f"A {r} {r} 0 1 1 {cx + r} {cy} "
            f"A {r} {r} 0 1 1 {cx - r} {cy}"
        )
        path_length = round(2 * math.pi * r, 2)
        ghost = (
            f'<path d="{d}" stroke="{color}" stroke-width="{sw}" fill="none" '
            f'stroke-opacity="0.1" stroke-dasharray="5,5" '
            f'stroke-linecap="round" stroke-linejoin="round" class="ghost-layer"/>'
        )
        ink = (
            f'<path d="{d}" stroke="{color}" stroke-width="{sw}" fill="none" '
            f'stroke-opacity="1.0" stroke-dasharray="{path_length}" '
            f'stroke-dashoffset="{path_length}" data-length="{path_length}" '
            f'stroke-linecap="round" stroke-linejoin="round" class="ink-layer"/>'
        )
        el = f'<g filter="url(#hand-drawn)">{ghost}\n{ink}</g>'
        self._elements.append(el)
        return el

    def draw_rectangle(
        self,
        x: float,
        y: float,
        width: float,
        height: float,
        color: str = PRIMARY,
        stroke_width: float = 2,
        corner_radius: float = 0,
        fill: str = "none",
    ) -> str:
        """Draw a rectangle as a continuous path for Speed Paint animation."""
        sw = _seeded_stroke(stroke_width, f"r{x:.0f}{y:.0f}{width:.0f}{height:.0f}")
        cr = min(corner_radius, width / 2, height / 2)
        if cr > 0:
            d = (
                f"M {x + cr} {y} "
                f"H {x + width - cr} A {cr} {cr} 0 0 1 {x + width} {y + cr} "
                f"V {y + height - cr} A {cr} {cr} 0 0 1 {x + width - cr} {y + height} "
                f"H {x + cr} A {cr} {cr} 0 0 1 {x} {y + height - cr} "
                f"V {y + cr} A {cr} {cr} 0 0 1 {x + cr} {y} Z"
            )
            straight = 2 * (width - 2 * cr) + 2 * (height - 2 * cr)
            arcs = 2 * math.pi * cr  # four quarter-arcs = full circle
            path_length = round(straight + arcs, 2)
        else:
            d = f"M {x} {y} H {x + width} V {y + height} H {x} Z"
            path_length = round(2 * (width + height), 2)
        ghost = (
            f'<path d="{d}" stroke="{color}" stroke-width="{sw}" fill="none" '
            f'stroke-opacity="0.1" stroke-dasharray="5,5" '
            f'stroke-linecap="round" stroke-linejoin="round" class="ghost-layer"/>'
        )
        ink = (
            f'<path d="{d}" stroke="{color}" stroke-width="{sw}" fill="none" '
            f'stroke-opacity="1.0" stroke-dasharray="{path_length}" '
            f'stroke-dashoffset="{path_length}" data-length="{path_length}" '
            f'stroke-linecap="round" stroke-linejoin="round" class="ink-layer"/>'
        )
        el = f'<g filter="url(#hand-drawn)">{ghost}\n{ink}</g>'
        self._elements.append(el)
        return el

    def draw_line(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        color: str = PRIMARY,
        stroke_width: float = 2,
    ) -> str:
        """Draw a slightly wobbly line as ghost+ink path pair for Speed Paint."""
        seed = f"l{x1:.0f}{y1:.0f}{x2:.0f}{y2:.0f}"
        sw = _seeded_stroke(stroke_width, seed)
        d = _wobbly_path(x1, y1, x2, y2, seed)
        path_length = round(math.hypot(x2 - x1, y2 - y1), 2)
        ghost = (
            f'<path d="{d}" stroke="{color}" stroke-width="{sw}" fill="none" '
            f'stroke-opacity="0.1" stroke-dasharray="5,5" '
            f'stroke-linecap="round" stroke-linejoin="round" class="ghost-layer"/>'
        )
        ink = (
            f'<path d="{d}" stroke="{color}" stroke-width="{sw}" fill="none" '
            f'stroke-opacity="1.0" stroke-dasharray="{path_length}" '
            f'stroke-dashoffset="{path_length}" data-length="{path_length}" '
            f'stroke-linecap="round" stroke-linejoin="round" class="ink-layer"/>'
        )
        el = f'<g filter="url(#hand-drawn)">{ghost}\n{ink}</g>'
        self._elements.append(el)
        return el

    def draw_arrow(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        color: str = PRIMARY,
        stroke_width: float = 2,
    ) -> str:
        """Draw a wobbly arrow as ghost+ink path pairs for Speed Paint."""
        arrow_size = max(10.0, stroke_width * 4)
        dx, dy = x2 - x1, y2 - y1
        length = math.sqrt(dx * dx + dy * dy)
        if length < 1:
            return ""
        nx, ny = dx / length, dy / length
        px, py = -ny, nx

        shaft_x2 = x2 - nx * arrow_size
        shaft_y2 = y2 - ny * arrow_size
        half_w = arrow_size / 2.5

        bl = (shaft_x2 + px * half_w, shaft_y2 + py * half_w)
        br = (shaft_x2 - px * half_w, shaft_y2 - py * half_w)

        seed = f"a{x1:.0f}{y1:.0f}{x2:.0f}{y2:.0f}"
        sw = _seeded_stroke(stroke_width, seed)

        # Shaft: wobbly bezier path with ghost+ink
        d_shaft = _wobbly_path(x1, y1, shaft_x2, shaft_y2, seed)
        shaft_len = round(math.hypot(shaft_x2 - x1, shaft_y2 - y1), 2)
        shaft_ghost = (
            f'<path d="{d_shaft}" stroke="{color}" stroke-width="{sw}" fill="none" '
            f'stroke-opacity="0.1" stroke-dasharray="5,5" '
            f'stroke-linecap="round" class="ghost-layer"/>'
        )
        shaft_ink = (
            f'<path d="{d_shaft}" stroke="{color}" stroke-width="{sw}" fill="none" '
            f'stroke-opacity="1.0" stroke-dasharray="{shaft_len}" '
            f'stroke-dashoffset="{shaft_len}" data-length="{shaft_len}" '
            f'stroke-linecap="round" class="ink-layer"/>'
        )

        # Arrowhead: outline-only triangle path with ghost+ink
        d_head = (
            f"M {x2:.1f} {y2:.1f} L {bl[0]:.1f} {bl[1]:.1f} "
            f"L {br[0]:.1f} {br[1]:.1f} Z"
        )
        head_len = round(
            math.hypot(x2 - bl[0], y2 - bl[1])
            + math.hypot(bl[0] - br[0], bl[1] - br[1])
            + math.hypot(br[0] - x2, br[1] - y2),
            2,
        )
        head_ghost = (
            f'<path d="{d_head}" stroke="{color}" stroke-width="{sw}" fill="none" '
            f'stroke-opacity="0.1" stroke-dasharray="5,5" '
            f'stroke-linecap="round" stroke-linejoin="round" class="ghost-layer"/>'
        )
        head_ink = (
            f'<path d="{d_head}" stroke="{color}" stroke-width="{sw}" fill="none" '
            f'stroke-opacity="1.0" stroke-dasharray="{head_len}" '
            f'stroke-dashoffset="{head_len}" data-length="{head_len}" '
            f'stroke-linecap="round" stroke-linejoin="round" class="ink-layer"/>'
        )

        el = (
            f'<g filter="url(#hand-drawn)">'
            f'{shaft_ghost}\n{shaft_ink}\n{head_ghost}\n{head_ink}'
            f'</g>'
        )
        self._elements.append(el)
        return el

    def draw_text(
        self,
        x: float,
        y: float,
        text: str,
        font_size: int = 16,
        color: str = PRIMARY,
        bold: bool = False,
        align: str = "center",
    ) -> str:
        """Draw text with a lighter hand-drawn-text filter for readability."""
        anchor = {"left": "start", "center": "middle", "right": "end"}.get(align, "middle")
        weight = "bold" if bold else "normal"
        el = (
            f'<text x="{x}" y="{y}" font-size="{font_size}" fill="{color}" '
            f'font-weight="{weight}" text-anchor="{anchor}" '
            f'font-family="{FONT}" dominant-baseline="middle" '
            f'filter="url(#hand-drawn-text)">{_esc(text)}</text>'
        )
        self._elements.append(el)
        return el

    # ── compound shapes ──────────────────────────────────────────────

    def draw_grid(
        self,
        cols: int,
        rows: int,
        x: float,
        y: float,
        width: float,
        height: float,
        color: str = "#CCCCCC",
    ) -> str:
        """Draw a grid as ghost+ink path pairs for Speed Paint."""
        parts: list[str] = []
        cell_w, cell_h = width / max(cols, 1), height / max(rows, 1)
        for i in range(cols + 1):
            lx = x + i * cell_w
            d = f"M{lx:.1f},{y:.1f} L{lx:.1f},{y + height:.1f}"
            pl = round(height, 2)
            parts.append(
                f'<path d="{d}" stroke="{color}" stroke-width="1" fill="none" '
                f'stroke-opacity="0.1" stroke-dasharray="5,5" '
                f'stroke-linecap="round" class="ghost-layer"/>'
            )
            parts.append(
                f'<path d="{d}" stroke="{color}" stroke-width="1" fill="none" '
                f'stroke-opacity="1.0" stroke-dasharray="{pl}" '
                f'stroke-dashoffset="{pl}" data-length="{pl}" '
                f'stroke-linecap="round" class="ink-layer"/>'
            )
        for j in range(rows + 1):
            ly = y + j * cell_h
            d = f"M{x:.1f},{ly:.1f} L{x + width:.1f},{ly:.1f}"
            pl = round(width, 2)
            parts.append(
                f'<path d="{d}" stroke="{color}" stroke-width="1" fill="none" '
                f'stroke-opacity="0.1" stroke-dasharray="5,5" '
                f'stroke-linecap="round" class="ghost-layer"/>'
            )
            parts.append(
                f'<path d="{d}" stroke="{color}" stroke-width="1" fill="none" '
                f'stroke-opacity="1.0" stroke-dasharray="{pl}" '
                f'stroke-dashoffset="{pl}" data-length="{pl}" '
                f'stroke-linecap="round" class="ink-layer"/>'
            )
        el = f'<g filter="url(#hand-drawn)">{"".join(parts)}</g>'
        self._elements.append(el)
        return el

    def draw_timeline(
        self,
        x: float,
        y: float,
        width: float,
        events: List[dict],
        color: str = PRIMARY,
    ) -> str:
        """Horizontal timeline as ghost+ink paths for Speed Paint.

        events: [{"label": "...", "year": "..."}]
        """
        parts: list[str] = []

        # Main horizontal axis line
        d_axis = f"M{x:.1f},{y:.1f} L{x + width - 12:.1f},{y:.1f}"
        axis_len = round(width - 12, 2)
        parts.append(
            f'<path d="{d_axis}" stroke="{color}" stroke-width="3" fill="none" '
            f'stroke-opacity="0.1" stroke-dasharray="5,5" '
            f'stroke-linecap="round" class="ghost-layer"/>'
        )
        parts.append(
            f'<path d="{d_axis}" stroke="{color}" stroke-width="3" fill="none" '
            f'stroke-opacity="1.0" stroke-dasharray="{axis_len}" '
            f'stroke-dashoffset="{axis_len}" data-length="{axis_len}" '
            f'stroke-linecap="round" class="ink-layer"/>'
        )

        # Arrowhead at end — outline-only triangle path
        ax = x + width
        d_head = (
            f"M {ax:.1f} {y:.1f} L {ax - 12:.1f} {y - 5:.1f} "
            f"L {ax - 12:.1f} {y + 5:.1f} Z"
        )
        head_len = round(12 + 10 + math.hypot(12, 5), 2)
        parts.append(
            f'<path d="{d_head}" stroke="{color}" stroke-width="1.5" fill="none" '
            f'stroke-opacity="0.1" stroke-dasharray="5,5" '
            f'stroke-linecap="round" stroke-linejoin="round" class="ghost-layer"/>'
        )
        parts.append(
            f'<path d="{d_head}" stroke="{color}" stroke-width="1.5" fill="none" '
            f'stroke-opacity="1.0" stroke-dasharray="{head_len}" '
            f'stroke-dashoffset="{head_len}" data-length="{head_len}" '
            f'stroke-linecap="round" stroke-linejoin="round" class="ink-layer"/>'
        )

        n = max(len(events), 1)
        for i, event in enumerate(events):
            ex = x + (i + 1) * width / (n + 1)
            # Tick mark
            d_tick = f"M{ex:.1f},{y - 8:.1f} L{ex:.1f},{y + 8:.1f}"
            tick_len = 16.0
            parts.append(
                f'<path d="{d_tick}" stroke="{color}" stroke-width="2" fill="none" '
                f'stroke-opacity="0.1" stroke-dasharray="5,5" '
                f'stroke-linecap="round" class="ghost-layer"/>'
            )
            parts.append(
                f'<path d="{d_tick}" stroke="{color}" stroke-width="2" fill="none" '
                f'stroke-opacity="1.0" stroke-dasharray="{tick_len}" '
                f'stroke-dashoffset="{tick_len}" data-length="{tick_len}" '
                f'stroke-linecap="round" class="ink-layer"/>'
            )
            label = _esc(event.get("label", ""))
            year = _esc(str(event.get("year", "")))
            parts.append(
                f'<text x="{ex:.1f}" y="{y - 22}" text-anchor="middle" '
                f'font-size="13" fill="{PRIMARY}" font-family="{FONT}" '
                f'font-weight="bold">{label}</text>'
            )
            if year:
                parts.append(
                    f'<text x="{ex:.1f}" y="{y + 24}" text-anchor="middle" '
                    f'font-size="12" fill="{PRIMARY}" font-family="{FONT}">{year}</text>'
                )
        el = f'<g filter="url(#hand-drawn)">{"".join(parts)}</g>'
        self._elements.append(el)
        return el

    def draw_tree(
        self,
        root_label: str,
        children: List,
        x: float,
        y: float,
        width: float,
        height: float,
        color: str = PRIMARY,
    ) -> str:
        """Simple 2-level tree as ghost+ink paths for Speed Paint."""
        parts: list[str] = []
        node_r = 30
        root_cx = x + width / 2
        root_cy = y + node_r + 10

        # Root circle — outline-only path
        d_root = (
            f"M {root_cx - node_r} {root_cy} "
            f"A {node_r} {node_r} 0 1 1 {root_cx + node_r} {root_cy} "
            f"A {node_r} {node_r} 0 1 1 {root_cx - node_r} {root_cy}"
        )
        root_len = round(2 * math.pi * node_r, 2)
        parts.append(
            f'<path d="{d_root}" stroke="{BLUE}" stroke-width="2" fill="none" '
            f'stroke-opacity="0.1" stroke-dasharray="5,5" '
            f'stroke-linecap="round" class="ghost-layer"/>'
        )
        parts.append(
            f'<path d="{d_root}" stroke="{BLUE}" stroke-width="2" fill="none" '
            f'stroke-opacity="1.0" stroke-dasharray="{root_len}" '
            f'stroke-dashoffset="{root_len}" data-length="{root_len}" '
            f'stroke-linecap="round" class="ink-layer"/>'
        )
        parts.append(
            f'<text x="{root_cx:.1f}" y="{root_cy:.1f}" text-anchor="middle" '
            f'dominant-baseline="middle" font-size="13" fill="{PRIMARY}" '
            f'font-family="{FONT}" font-weight="bold">{_esc(str(root_label)[:20])}</text>'
        )

        n = max(len(children), 1)
        child_y = y + height - node_r - 10
        for i, child in enumerate(children):
            cx = x + (i + 0.5) * width / n
            # Connector line — ghost+ink path
            lx1, ly1 = root_cx, root_cy + node_r
            lx2, ly2 = cx, child_y - node_r
            d_line = f"M{lx1:.1f},{ly1:.1f} L{lx2:.1f},{ly2:.1f}"
            line_len = round(math.hypot(lx2 - lx1, ly2 - ly1), 2)
            parts.append(
                f'<path d="{d_line}" stroke="{color}" stroke-width="2" fill="none" '
                f'stroke-opacity="0.1" stroke-dasharray="5,5" '
                f'stroke-linecap="round" class="ghost-layer"/>'
            )
            parts.append(
                f'<path d="{d_line}" stroke="{color}" stroke-width="2" fill="none" '
                f'stroke-opacity="1.0" stroke-dasharray="{line_len}" '
                f'stroke-dashoffset="{line_len}" data-length="{line_len}" '
                f'stroke-linecap="round" class="ink-layer"/>'
            )
            # Child circle — outline-only path
            d_child = (
                f"M {cx - node_r} {child_y} "
                f"A {node_r} {node_r} 0 1 1 {cx + node_r} {child_y} "
                f"A {node_r} {node_r} 0 1 1 {cx - node_r} {child_y}"
            )
            child_len = round(2 * math.pi * node_r, 2)
            parts.append(
                f'<path d="{d_child}" stroke="{ORANGE}" stroke-width="2" fill="none" '
                f'stroke-opacity="0.1" stroke-dasharray="5,5" '
                f'stroke-linecap="round" class="ghost-layer"/>'
            )
            parts.append(
                f'<path d="{d_child}" stroke="{ORANGE}" stroke-width="2" fill="none" '
                f'stroke-opacity="1.0" stroke-dasharray="{child_len}" '
                f'stroke-dashoffset="{child_len}" data-length="{child_len}" '
                f'stroke-linecap="round" class="ink-layer"/>'
            )
            label = child if isinstance(child, str) else child.get("label", str(child))
            parts.append(
                f'<text x="{cx:.1f}" y="{child_y:.1f}" text-anchor="middle" '
                f'dominant-baseline="middle" font-size="12" fill="{PRIMARY}" '
                f'font-family="{FONT}">{_esc(str(label)[:15])}</text>'
            )

        el = f'<g filter="url(#hand-drawn)">{"".join(parts)}</g>'
        self._elements.append(el)
        return el

    def draw_mind_map(
        self,
        center_label: str,
        branches: List[str],
        cx: float,
        cy: float,
        radius: float,
        color: str = PRIMARY,
    ) -> str:
        """Radial mind map as ghost+ink paths for Speed Paint."""
        parts: list[str] = []
        cr = 55

        # Center circle — outline-only path
        d_center = (
            f"M {cx - cr} {cy} "
            f"A {cr} {cr} 0 1 1 {cx + cr} {cy} "
            f"A {cr} {cr} 0 1 1 {cx - cr} {cy}"
        )
        center_len = round(2 * math.pi * cr, 2)
        parts.append(
            f'<path d="{d_center}" stroke="{BLUE}" stroke-width="2.5" fill="none" '
            f'stroke-opacity="0.1" stroke-dasharray="5,5" '
            f'stroke-linecap="round" class="ghost-layer"/>'
        )
        parts.append(
            f'<path d="{d_center}" stroke="{BLUE}" stroke-width="2.5" fill="none" '
            f'stroke-opacity="1.0" stroke-dasharray="{center_len}" '
            f'stroke-dashoffset="{center_len}" data-length="{center_len}" '
            f'stroke-linecap="round" class="ink-layer"/>'
        )
        parts.append(
            f'<text x="{cx:.1f}" y="{cy:.1f}" text-anchor="middle" '
            f'dominant-baseline="middle" font-size="14" fill="{PRIMARY}" '
            f'font-family="{FONT}" font-weight="bold">{_esc(str(center_label)[:20])}</text>'
        )

        n = max(len(branches), 1)
        for i, branch in enumerate(branches):
            angle = (2 * math.pi * i / n) - math.pi / 2
            bx = cx + radius * math.cos(angle)
            by = cy + radius * math.sin(angle)
            bc = _BRANCH_COLORS[i % len(_BRANCH_COLORS)]
            br = 38

            sx = cx + cr * math.cos(angle)
            sy = cy + cr * math.sin(angle)

            # Connector line — ghost+ink
            d_conn = f"M{sx:.1f},{sy:.1f} L{bx:.1f},{by:.1f}"
            conn_len = round(math.hypot(bx - sx, by - sy), 2)
            parts.append(
                f'<path d="{d_conn}" stroke="{bc}" stroke-width="2.5" fill="none" '
                f'stroke-opacity="0.1" stroke-dasharray="5,5" '
                f'stroke-linecap="round" class="ghost-layer"/>'
            )
            parts.append(
                f'<path d="{d_conn}" stroke="{bc}" stroke-width="2.5" fill="none" '
                f'stroke-opacity="1.0" stroke-dasharray="{conn_len}" '
                f'stroke-dashoffset="{conn_len}" data-length="{conn_len}" '
                f'stroke-linecap="round" class="ink-layer"/>'
            )

            # Branch circle — outline-only path
            d_branch = (
                f"M {bx - br} {by} "
                f"A {br} {br} 0 1 1 {bx + br} {by} "
                f"A {br} {br} 0 1 1 {bx - br} {by}"
            )
            branch_len = round(2 * math.pi * br, 2)
            parts.append(
                f'<path d="{d_branch}" stroke="{bc}" stroke-width="2" fill="none" '
                f'stroke-opacity="0.1" stroke-dasharray="5,5" '
                f'stroke-linecap="round" class="ghost-layer"/>'
            )
            parts.append(
                f'<path d="{d_branch}" stroke="{bc}" stroke-width="2" fill="none" '
                f'stroke-opacity="1.0" stroke-dasharray="{branch_len}" '
                f'stroke-dashoffset="{branch_len}" data-length="{branch_len}" '
                f'stroke-linecap="round" class="ink-layer"/>'
            )
            parts.append(
                f'<text x="{bx:.1f}" y="{by:.1f}" text-anchor="middle" '
                f'dominant-baseline="middle" font-size="12" fill="{PRIMARY}" '
                f'font-family="{FONT}">{_esc(str(branch)[:15])}</text>'
            )

        el = f'<g filter="url(#hand-drawn)">{"".join(parts)}</g>'
        self._elements.append(el)
        return el

    def draw_pyramid(
        self,
        levels: List[str],
        x: float,
        y: float,
        width: float,
        height: float,
    ) -> str:
        """Layered pyramid as ghost+ink paths for Speed Paint."""
        parts: list[str] = []
        colors = [BLUE, ORANGE, GREEN, PURPLE, RED]
        n = max(len(levels), 1)
        level_h = height / n

        for i, label in enumerate(levels):
            ratio = (i + 1) / n
            lw = width * ratio
            lx = x + (width - lw) / 2
            ly = y + i * level_h
            bc = colors[i % len(colors)]
            # Rectangle as outline-only path
            d_rect = f"M {lx:.1f} {ly:.1f} H {lx + lw:.1f} V {ly + level_h:.1f} H {lx:.1f} Z"
            rect_len = round(2 * (lw + level_h), 2)
            parts.append(
                f'<path d="{d_rect}" stroke="{bc}" stroke-width="2" fill="none" '
                f'stroke-opacity="0.1" stroke-dasharray="5,5" '
                f'stroke-linecap="round" stroke-linejoin="round" class="ghost-layer"/>'
            )
            parts.append(
                f'<path d="{d_rect}" stroke="{bc}" stroke-width="2" fill="none" '
                f'stroke-opacity="1.0" stroke-dasharray="{rect_len}" '
                f'stroke-dashoffset="{rect_len}" data-length="{rect_len}" '
                f'stroke-linecap="round" stroke-linejoin="round" class="ink-layer"/>'
            )
            ty = ly + level_h / 2
            parts.append(
                f'<text x="{x + width / 2:.1f}" y="{ty:.1f}" text-anchor="middle" '
                f'dominant-baseline="middle" font-size="13" fill="{PRIMARY}" '
                f'font-family="{FONT}" font-weight="bold">{_esc(str(label)[:30])}</text>'
            )

        el = f'<g filter="url(#hand-drawn)">{"".join(parts)}</g>'
        self._elements.append(el)
        return el

    def draw_venn(
        self,
        circles: List[dict],
        cx: float,
        cy: float,
    ) -> str:
        """Overlapping Venn diagram as ghost+ink paths for Speed Paint.

        circles: [{"label": "...", "color": "..."}]
        """
        parts: list[str] = []
        r = 110
        offsets = {
            1: [(0, 0)],
            2: [(-65, 0), (65, 0)],
            3: [(-75, 35), (75, 35), (0, -55)],
        }
        n = min(len(circles), 3)
        offs = offsets.get(n, offsets[3])

        for i, circle in enumerate(circles[:3]):
            ox, oy = offs[i] if i < len(offs) else (0, 0)
            c = circle.get("color", _BRANCH_COLORS[i % len(_BRANCH_COLORS)])
            label = _esc(str(circle.get("label", ""))[:15])
            vcx, vcy = cx + ox, cy + oy
            d_venn = (
                f"M {vcx - r} {vcy} "
                f"A {r} {r} 0 1 1 {vcx + r} {vcy} "
                f"A {r} {r} 0 1 1 {vcx - r} {vcy}"
            )
            venn_len = round(2 * math.pi * r, 2)
            parts.append(
                f'<path d="{d_venn}" stroke="{c}" stroke-width="2" fill="none" '
                f'stroke-opacity="0.1" stroke-dasharray="5,5" '
                f'stroke-linecap="round" class="ghost-layer"/>'
            )
            parts.append(
                f'<path d="{d_venn}" stroke="{c}" stroke-width="2" fill="none" '
                f'stroke-opacity="1.0" stroke-dasharray="{venn_len}" '
                f'stroke-dashoffset="{venn_len}" data-length="{venn_len}" '
                f'stroke-linecap="round" class="ink-layer"/>'
            )
            parts.append(
                f'<text x="{vcx:.1f}" y="{vcy:.1f}" text-anchor="middle" '
                f'dominant-baseline="middle" font-size="13" fill="{PRIMARY}" '
                f'font-family="{FONT}" font-weight="bold">{label}</text>'
            )

        el = f'<g filter="url(#hand-drawn)">{"".join(parts)}</g>'
        self._elements.append(el)
        return el

    def draw_process_flow(
        self,
        steps: List[str],
        x: float,
        y: float,
        width: float,
        color: str = PRIMARY,
    ) -> str:
        """Horizontal boxes connected by arrows as ghost+ink paths for Speed Paint."""
        parts: list[str] = []
        n = max(len(steps), 1)
        gap = 18
        box_w = (width - gap * (n - 1)) / n
        box_h = 52
        cr = 8  # corner radius

        for i, step in enumerate(steps):
            bx = x + i * (box_w + gap)
            by = y
            # Rounded rectangle as outline-only path
            d_box = (
                f"M {bx + cr:.1f} {by:.1f} "
                f"H {bx + box_w - cr:.1f} A {cr} {cr} 0 0 1 {bx + box_w:.1f} {by + cr:.1f} "
                f"V {by + box_h - cr:.1f} A {cr} {cr} 0 0 1 {bx + box_w - cr:.1f} {by + box_h:.1f} "
                f"H {bx + cr:.1f} A {cr} {cr} 0 0 1 {bx:.1f} {by + box_h - cr:.1f} "
                f"V {by + cr:.1f} A {cr} {cr} 0 0 1 {bx + cr:.1f} {by:.1f} Z"
            )
            straight = 2 * (box_w - 2 * cr) + 2 * (box_h - 2 * cr)
            arcs = 2 * math.pi * cr
            box_len = round(straight + arcs, 2)
            parts.append(
                f'<path d="{d_box}" stroke="{BLUE}" stroke-width="2" fill="none" '
                f'stroke-opacity="0.1" stroke-dasharray="5,5" '
                f'stroke-linecap="round" stroke-linejoin="round" class="ghost-layer"/>'
            )
            parts.append(
                f'<path d="{d_box}" stroke="{BLUE}" stroke-width="2" fill="none" '
                f'stroke-opacity="1.0" stroke-dasharray="{box_len}" '
                f'stroke-dashoffset="{box_len}" data-length="{box_len}" '
                f'stroke-linecap="round" stroke-linejoin="round" class="ink-layer"/>'
            )
            parts.append(
                f'<text x="{bx + box_w / 2:.1f}" y="{by + box_h / 2:.1f}" '
                f'text-anchor="middle" dominant-baseline="middle" '
                f'font-size="12" fill="{PRIMARY}" font-family="{FONT}" '
                f'font-weight="bold">{_esc(str(step)[:22])}</text>'
            )
            if i < n - 1:
                ax1 = bx + box_w
                ay = by + box_h / 2
                ax2 = bx + box_w + gap
                # Connector line — ghost+ink
                d_conn = f"M{ax1:.1f},{ay:.1f} L{ax2 - 7:.1f},{ay:.1f}"
                conn_len = round(gap - 7, 2)
                parts.append(
                    f'<path d="{d_conn}" stroke="{color}" stroke-width="2" fill="none" '
                    f'stroke-opacity="0.1" stroke-dasharray="5,5" '
                    f'stroke-linecap="round" class="ghost-layer"/>'
                )
                parts.append(
                    f'<path d="{d_conn}" stroke="{color}" stroke-width="2" fill="none" '
                    f'stroke-opacity="1.0" stroke-dasharray="{conn_len}" '
                    f'stroke-dashoffset="{conn_len}" data-length="{conn_len}" '
                    f'stroke-linecap="round" class="ink-layer"/>'
                )
                # Arrowhead — outline-only triangle path
                d_head = (
                    f"M {ax2:.1f} {ay:.1f} L {ax2 - 8:.1f} {ay - 4:.1f} "
                    f"L {ax2 - 8:.1f} {ay + 4:.1f} Z"
                )
                head_len = round(8 + 8 + math.hypot(8, 4), 2)
                parts.append(
                    f'<path d="{d_head}" stroke="{color}" stroke-width="1.5" fill="none" '
                    f'stroke-opacity="0.1" stroke-dasharray="5,5" '
                    f'stroke-linecap="round" stroke-linejoin="round" class="ghost-layer"/>'
                )
                parts.append(
                    f'<path d="{d_head}" stroke="{color}" stroke-width="1.5" fill="none" '
                    f'stroke-opacity="1.0" stroke-dasharray="{head_len}" '
                    f'stroke-dashoffset="{head_len}" data-length="{head_len}" '
                    f'stroke-linecap="round" stroke-linejoin="round" class="ink-layer"/>'
                )

        el = f'<g filter="url(#hand-drawn)">{"".join(parts)}</g>'
        self._elements.append(el)
        return el

    # ── utility ──────────────────────────────────────────────────────

    def add_raw(self, svg_fragment: str) -> None:
        """Append a raw SVG string fragment (e.g. a <g> open/close tag)."""
        self._elements.append(svg_fragment)

    def clear(self) -> None:
        """Remove all accumulated elements."""
        self._elements.clear()

    def to_svg(self) -> str:
        """Return the complete SVG document with hand-drawn filters and paper texture."""
        body = "\n  ".join(self._elements)
        bg = _paper_bg(self.width, self.height, self.bg_color)
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="0 0 {self.width} {self.height}" '
            f'width="{self.width}" height="{self.height}">\n'
            f'  {_SVG_DEFS}\n'
            f'  {bg}\n'
            f'  {body}\n'
            f'</svg>'
        )


def blank_svg(width: int = CANVAS_WIDTH, height: int = CANVAS_HEIGHT) -> str:
    """Return a blank whiteboard SVG with paper texture (no sketch elements)."""
    bg = _paper_bg(width, height, BG_COLOR)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {width} {height}" width="{width}" height="{height}">\n'
        f'  {_SVG_DEFS}\n'
        f'  {bg}\n'
        f'</svg>'
    )
