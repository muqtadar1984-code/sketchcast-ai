"""Agent 4: SVG Path Utilities — converts sketch elements to animatable SVG paths.

Each converter returns a dict:
    {
        "d": str,              # SVG path `d` attribute
        "total_length": float, # path length in px (for stroke-dasharray/dashoffset)
    }

The generate_dual_layer_svg() method produces ghost + ink HTML for the Scribe player.
"""

import math
from typing import List


class SVGPathConverter:
    """Static methods to convert shape parameters to SVG path data."""

    @staticmethod
    def line_to_path(x1: float, y1: float, x2: float, y2: float) -> dict:
        """Convert a line to a path and calculate its exact length."""
        d = f"M {x1} {y1} L {x2} {y2}"
        length = math.hypot(x2 - x1, y2 - y1)
        return {"d": d, "total_length": round(length, 2)}

    @staticmethod
    def rect_to_path(x: float, y: float, w: float, h: float, rx: float = 0) -> dict:
        """Convert a rectangle to a continuous drawing path."""
        if rx > 0:
            rx = min(rx, w / 2, h / 2)
            d = (
                f"M {x + rx} {y} "
                f"H {x + w - rx} A {rx} {rx} 0 0 1 {x + w} {y + rx} "
                f"V {y + h - rx} A {rx} {rx} 0 0 1 {x + w - rx} {y + h} "
                f"H {x + rx} A {rx} {rx} 0 0 1 {x} {y + h - rx} "
                f"V {y + rx} A {rx} {rx} 0 0 1 {x + rx} {y} Z"
            )
            # Approximate: straight edges + quarter-circle arcs
            straight = 2 * (w - 2 * rx) + 2 * (h - 2 * rx)
            arcs = 2 * math.pi * rx  # four quarter circles = full circle
            length = straight + arcs
        else:
            d = f"M {x} {y} H {x + w} V {y + h} H {x} Z"
            length = 2 * (w + h)
        return {"d": d, "total_length": round(length, 2)}

    @staticmethod
    def circle_to_path(cx: float, cy: float, r: float) -> dict:
        """Convert a circle into two arcs so it can be drawn smoothly."""
        d = (
            f"M {cx - r} {cy} "
            f"A {r} {r} 0 1 1 {cx + r} {cy} "
            f"A {r} {r} 0 1 1 {cx - r} {cy}"
        )
        length = 2 * math.pi * r
        return {"d": d, "total_length": round(length, 2)}

    @staticmethod
    def ellipse_to_path(cx: float, cy: float, rx: float, ry: float) -> dict:
        """Convert an ellipse to a path using two arcs."""
        d = (
            f"M {cx - rx} {cy} "
            f"A {rx} {ry} 0 1 1 {cx + rx} {cy} "
            f"A {rx} {ry} 0 1 1 {cx - rx} {cy}"
        )
        # Ramanujan approximation for ellipse perimeter
        h = ((rx - ry) ** 2) / ((rx + ry) ** 2) if (rx + ry) > 0 else 0
        length = math.pi * (rx + ry) * (1 + 3 * h / (10 + math.sqrt(4 - 3 * h)))
        return {"d": d, "total_length": round(length, 2)}

    @staticmethod
    def arrow_to_paths(
        x1: float, y1: float, x2: float, y2: float, arrow_size: float = 10
    ) -> List[dict]:
        """Convert an arrow to a shaft path + arrowhead triangle path.

        Returns a list of 2 path dicts: [shaft_line, arrowhead].
        """
        dx, dy = x2 - x1, y2 - y1
        length = math.hypot(dx, dy)
        if length < 1:
            return [{"d": f"M {x1} {y1} L {x2} {y2}", "total_length": 0}]

        nx, ny = dx / length, dy / length
        px, py = -ny, nx

        shaft_x2 = x2 - nx * arrow_size
        shaft_y2 = y2 - ny * arrow_size
        half_w = arrow_size / 2.5

        # Shaft line
        shaft_d = f"M {x1} {y1} L {shaft_x2:.1f} {shaft_y2:.1f}"
        shaft_len = math.hypot(shaft_x2 - x1, shaft_y2 - y1)

        # Arrowhead as a closed triangle path
        bl_x = shaft_x2 + px * half_w
        bl_y = shaft_y2 + py * half_w
        br_x = shaft_x2 - px * half_w
        br_y = shaft_y2 - py * half_w
        head_d = f"M {x2:.1f} {y2:.1f} L {bl_x:.1f} {bl_y:.1f} L {br_x:.1f} {br_y:.1f} Z"
        # Approximate arrowhead perimeter
        head_len = arrow_size * 2 + half_w * 2

        return [
            {"d": shaft_d, "total_length": round(shaft_len, 2)},
            {"d": head_d, "total_length": round(head_len, 2)},
        ]

    @staticmethod
    def polygon_to_path(points: List[tuple]) -> dict:
        """Convert a list of (x, y) tuples to a closed polygon path."""
        if not points:
            return {"d": "", "total_length": 0}
        parts = [f"M {points[0][0]} {points[0][1]}"]
        total = 0.0
        for i in range(1, len(points)):
            parts.append(f"L {points[i][0]} {points[i][1]}")
            total += math.hypot(
                points[i][0] - points[i - 1][0],
                points[i][1] - points[i - 1][1],
            )
        parts.append("Z")
        # Close: last point back to first
        total += math.hypot(
            points[-1][0] - points[0][0],
            points[-1][1] - points[0][1],
        )
        return {"d": " ".join(parts), "total_length": round(total, 2)}

    @staticmethod
    def generate_dual_layer_svg(
        path_data: dict,
        stroke_color: str = "#000000",
        stroke_width: float = 2,
    ) -> str:
        """Generate the SVG markup for the Ghost and Ink layers.

        Ghost layer: visible immediately, faint dashed outline showing where drawing goes.
        Ink layer: starts fully hidden (dashoffset = length), animated to reveal by the player.
        """
        d = path_data["d"]
        total_length = path_data["total_length"]
        path_id = path_data.get("path_id", "p0")
        color = path_data.get("stroke_color", stroke_color)
        sw = path_data.get("stroke_width", stroke_width)
        fill = path_data.get("fill", "none")

        if not d:
            return ""

        ghost = (
            f'<path d="{d}" stroke="{color}" stroke-width="{sw}" fill="{fill}" '
            f'stroke-opacity="0.1" stroke-dasharray="5,5" '
            f'stroke-linecap="round" stroke-linejoin="round" class="ghost-layer"/>'
        )

        ink = (
            f'<path id="{path_id}" d="{d}" stroke="{color}" stroke-width="{sw}" '
            f'fill="{fill}" stroke-opacity="1.0" '
            f'stroke-dasharray="{total_length}" stroke-dashoffset="{total_length}" '
            f'stroke-linecap="round" stroke-linejoin="round" '
            f'data-length="{total_length}" class="ink-layer"/>'
        )

        return f'<g id="g-{path_id}">\n  {ghost}\n  {ink}\n</g>'
