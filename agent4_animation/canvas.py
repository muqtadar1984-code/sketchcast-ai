"""Agent 4: SVG Canvas — whiteboard sketch drawing primitives.

Build an SVG document by calling draw_* methods, then call to_svg()
to get the complete document string.  All coordinates are in pixels
on a default 1280 × 720 canvas.
"""

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


def _esc(text: str) -> str:
    """Escape XML special characters."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


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
        el = (
            f'<circle cx="{cx}" cy="{cy}" r="{r}" '
            f'stroke="{color}" stroke-width="{stroke_width}" fill="{fill}"/>'
        )
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
        el = (
            f'<rect x="{x}" y="{y}" width="{width}" height="{height}" '
            f'rx="{corner_radius}" ry="{corner_radius}" '
            f'stroke="{color}" stroke-width="{stroke_width}" fill="{fill}"/>'
        )
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
        el = (
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
            f'stroke="{color}" stroke-width="{stroke_width}" stroke-linecap="round"/>'
        )
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
        """Draw a line with a filled arrowhead at (x2, y2)."""
        arrow_size = max(10.0, stroke_width * 4)
        dx, dy = x2 - x1, y2 - y1
        length = math.sqrt(dx * dx + dy * dy)
        if length < 1:
            return ""
        nx, ny = dx / length, dy / length   # unit along line
        px, py = -ny, nx                    # perpendicular

        shaft_x2 = x2 - nx * arrow_size
        shaft_y2 = y2 - ny * arrow_size
        half_w = arrow_size / 2.5

        bl = (shaft_x2 + px * half_w, shaft_y2 + py * half_w)
        br = (shaft_x2 - px * half_w, shaft_y2 - py * half_w)
        pts = f"{x2:.1f},{y2:.1f} {bl[0]:.1f},{bl[1]:.1f} {br[0]:.1f},{br[1]:.1f}"

        line = (
            f'<line x1="{x1}" y1="{y1}" x2="{shaft_x2:.1f}" y2="{shaft_y2:.1f}" '
            f'stroke="{color}" stroke-width="{stroke_width}" stroke-linecap="round"/>'
        )
        head = f'<polygon points="{pts}" fill="{color}" stroke="none"/>'
        el = f"<g>{line}{head}</g>"
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
        anchor = {"left": "start", "center": "middle", "right": "end"}.get(align, "middle")
        weight = "bold" if bold else "normal"
        el = (
            f'<text x="{x}" y="{y}" font-size="{font_size}" fill="{color}" '
            f'font-weight="{weight}" text-anchor="{anchor}" '
            f'font-family="{FONT}" dominant-baseline="middle">{_esc(text)}</text>'
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
        parts = []
        cell_w, cell_h = width / max(cols, 1), height / max(rows, 1)
        for i in range(cols + 1):
            cx = x + i * cell_w
            parts.append(
                f'<line x1="{cx:.1f}" y1="{y}" x2="{cx:.1f}" y2="{y + height}" '
                f'stroke="{color}" stroke-width="1"/>'
            )
        for j in range(rows + 1):
            cy = y + j * cell_h
            parts.append(
                f'<line x1="{x}" y1="{cy:.1f}" x2="{x + width}" y2="{cy:.1f}" '
                f'stroke="{color}" stroke-width="1"/>'
            )
        el = f'<g>{"".join(parts)}</g>'
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
        """Horizontal timeline with event markers.

        events: [{"label": "...", "year": "..."}]
        """
        parts = []
        # Main line
        parts.append(
            f'<line x1="{x}" y1="{y}" x2="{x + width - 12}" y2="{y}" '
            f'stroke="{color}" stroke-width="3" stroke-linecap="round"/>'
        )
        # Arrowhead at end
        pts = (
            f"{x + width},{y} {x + width - 12},{y - 5} {x + width - 12},{y + 5}"
        )
        parts.append(f'<polygon points="{pts}" fill="{color}"/>')

        n = max(len(events), 1)
        for i, event in enumerate(events):
            ex = x + (i + 1) * width / (n + 1)
            parts.append(
                f'<line x1="{ex:.1f}" y1="{y - 8}" x2="{ex:.1f}" y2="{y + 8}" '
                f'stroke="{color}" stroke-width="2"/>'
            )
            label = _esc(event.get("label", ""))
            year = _esc(str(event.get("year", "")))
            parts.append(
                f'<text x="{ex:.1f}" y="{y - 22}" text-anchor="middle" '
                f'font-size="13" fill="{color}" font-family="{FONT}" '
                f'font-weight="bold">{label}</text>'
            )
            if year:
                parts.append(
                    f'<text x="{ex:.1f}" y="{y + 24}" text-anchor="middle" '
                    f'font-size="12" fill="{color}" font-family="{FONT}">{year}</text>'
                )
        el = f'<g>{"".join(parts)}</g>'
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
        """Simple 2-level tree: root at top, children spread below."""
        parts = []
        node_r = 30
        root_cx = x + width / 2
        root_cy = y + node_r + 10

        parts.append(
            f'<circle cx="{root_cx:.1f}" cy="{root_cy:.1f}" r="{node_r}" '
            f'fill="{BLUE}" stroke="none"/>'
        )
        parts.append(
            f'<text x="{root_cx:.1f}" y="{root_cy:.1f}" text-anchor="middle" '
            f'dominant-baseline="middle" font-size="13" fill="white" '
            f'font-family="{FONT}" font-weight="bold">{_esc(str(root_label)[:20])}</text>'
        )

        n = max(len(children), 1)
        child_y = y + height - node_r - 10
        for i, child in enumerate(children):
            cx = x + (i + 0.5) * width / n
            parts.append(
                f'<line x1="{root_cx:.1f}" y1="{root_cy + node_r:.1f}" '
                f'x2="{cx:.1f}" y2="{child_y - node_r:.1f}" '
                f'stroke="{color}" stroke-width="2"/>'
            )
            parts.append(
                f'<circle cx="{cx:.1f}" cy="{child_y:.1f}" r="{node_r}" '
                f'fill="{ORANGE}" stroke="none"/>'
            )
            label = child if isinstance(child, str) else child.get("label", str(child))
            parts.append(
                f'<text x="{cx:.1f}" y="{child_y:.1f}" text-anchor="middle" '
                f'dominant-baseline="middle" font-size="12" fill="white" '
                f'font-family="{FONT}">{_esc(str(label)[:15])}</text>'
            )

        el = f'<g>{"".join(parts)}</g>'
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
        """Radial mind map: center node + branches at equal angles."""
        parts = []
        cr = 55  # center node radius

        parts.append(
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{cr}" fill="{BLUE}" stroke="none"/>'
        )
        parts.append(
            f'<text x="{cx:.1f}" y="{cy:.1f}" text-anchor="middle" '
            f'dominant-baseline="middle" font-size="14" fill="white" '
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
            parts.append(
                f'<line x1="{sx:.1f}" y1="{sy:.1f}" x2="{bx:.1f}" y2="{by:.1f}" '
                f'stroke="{bc}" stroke-width="2.5"/>'
            )
            parts.append(
                f'<circle cx="{bx:.1f}" cy="{by:.1f}" r="{br}" '
                f'fill="{bc}" stroke="none"/>'
            )
            parts.append(
                f'<text x="{bx:.1f}" y="{by:.1f}" text-anchor="middle" '
                f'dominant-baseline="middle" font-size="12" fill="white" '
                f'font-family="{FONT}">{_esc(str(branch)[:15])}</text>'
            )

        el = f'<g>{"".join(parts)}</g>'
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
        """Layered pyramid, apex at top, base at bottom."""
        parts = []
        colors = [BLUE, ORANGE, GREEN, PURPLE, RED]
        n = max(len(levels), 1)
        level_h = height / n

        for i, label in enumerate(levels):
            ratio = (i + 1) / n
            lw = width * ratio
            lx = x + (width - lw) / 2
            ly = y + i * level_h
            bc = colors[i % len(colors)]
            parts.append(
                f'<rect x="{lx:.1f}" y="{ly:.1f}" width="{lw:.1f}" '
                f'height="{level_h:.1f}" fill="{bc}" stroke="white" stroke-width="2"/>'
            )
            ty = ly + level_h / 2
            parts.append(
                f'<text x="{x + width / 2:.1f}" y="{ty:.1f}" text-anchor="middle" '
                f'dominant-baseline="middle" font-size="13" fill="white" '
                f'font-family="{FONT}" font-weight="bold">{_esc(str(label)[:30])}</text>'
            )

        el = f'<g>{"".join(parts)}</g>'
        self._elements.append(el)
        return el

    def draw_venn(
        self,
        circles: List[dict],
        cx: float,
        cy: float,
    ) -> str:
        """Overlapping Venn diagram circles.

        circles: [{"label": "...", "color": "..."}]
        """
        parts = []
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
            parts.append(
                f'<circle cx="{cx + ox:.1f}" cy="{cy + oy:.1f}" r="{r}" '
                f'fill="{c}" fill-opacity="0.30" stroke="{c}" stroke-width="2"/>'
            )
            parts.append(
                f'<text x="{cx + ox:.1f}" y="{cy + oy:.1f}" text-anchor="middle" '
                f'dominant-baseline="middle" font-size="13" fill="{PRIMARY}" '
                f'font-family="{FONT}" font-weight="bold">{label}</text>'
            )

        el = f'<g>{"".join(parts)}</g>'
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
        """Horizontal boxes connected by arrows."""
        parts = []
        n = max(len(steps), 1)
        gap = 18
        box_w = (width - gap * (n - 1)) / n
        box_h = 52

        for i, step in enumerate(steps):
            bx = x + i * (box_w + gap)
            by = y
            parts.append(
                f'<rect x="{bx:.1f}" y="{by:.1f}" width="{box_w:.1f}" '
                f'height="{box_h}" rx="8" ry="8" fill="{BLUE}" '
                f'stroke="{color}" stroke-width="1.5"/>'
            )
            parts.append(
                f'<text x="{bx + box_w / 2:.1f}" y="{by + box_h / 2:.1f}" '
                f'text-anchor="middle" dominant-baseline="middle" '
                f'font-size="12" fill="white" font-family="{FONT}" '
                f'font-weight="bold">{_esc(str(step)[:22])}</text>'
            )
            if i < n - 1:
                ax1 = bx + box_w
                ay = by + box_h / 2
                ax2 = bx + box_w + gap
                parts.append(
                    f'<line x1="{ax1:.1f}" y1="{ay:.1f}" '
                    f'x2="{ax2 - 7:.1f}" y2="{ay:.1f}" '
                    f'stroke="{color}" stroke-width="2"/>'
                )
                pts = (
                    f"{ax2:.1f},{ay:.1f} {ax2 - 8:.1f},{ay - 4:.1f} "
                    f"{ax2 - 8:.1f},{ay + 4:.1f}"
                )
                parts.append(f'<polygon points="{pts}" fill="{color}"/>')

        el = f'<g>{"".join(parts)}</g>'
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
        """Return the complete SVG document as a string."""
        body = "\n  ".join(self._elements)
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="0 0 {self.width} {self.height}" '
            f'width="{self.width}" height="{self.height}">\n'
            f'  <rect width="{self.width}" height="{self.height}" '
            f'fill="{self.bg_color}"/>\n'
            f'  {body}\n'
            f'</svg>'
        )


def blank_svg(width: int = CANVAS_WIDTH, height: int = CANVAS_HEIGHT) -> str:
    """Return a minimal blank whiteboard SVG."""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {width} {height}" width="{width}" height="{height}">\n'
        f'  <rect width="{width}" height="{height}" fill="{BG_COLOR}"/>\n'
        f'</svg>'
    )
