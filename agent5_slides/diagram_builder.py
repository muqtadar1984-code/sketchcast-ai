"""Agent 5: composable diagram renderer (Phase 2 of the native video engine).

Draws a small, fixed vocabulary of textbook diagrams — ``flow`` (process),
``cycle`` (loop), ``hierarchy`` (part/whole) and ``compare`` (contrast) — into a
slide's content region using the Scholar palette, and returns the diagram's parts
as ordered *reveal boxes*. The Agent 6 native renderer animates those boxes with
exactly the same mechanism it uses for text (write-on, left→right, in teaching
order), so nodes draw in first and the arrows/connectors follow — no new
animation code, perfect fidelity, deterministic and free.

``render_diagram(draw, region, visual, accent) -> list[list[Box]]`` draws onto the
caller's canvas and returns ``anim_elements`` (each element is a list of per-line
boxes). It returns ``[]`` when the spec is unusable, so the caller can fall back
to plain bullet points and never ship an empty slide.
"""

from __future__ import annotations

import math

from PIL import ImageDraw

from shared.text_shaping import display_text as _dt
from .slide_builder import _BG, _GREEN, _INK, _MUTED, _RULE, _font, _wrap

_Box = tuple[int, int, int, int]
_Region = tuple[int, int, int, int]


def _fit_label(draw, label: str, max_w: int, max_h: int, bold: bool, sizes: list[int]):
    """Pick the largest font from ``sizes`` whose wrapped label fits the box."""
    label = " ".join((label or "").split())
    for size in sizes:
        f = _font(bold, size)
        lines = _wrap(draw, label, f, max_w)
        line_h = int(size * 1.25)
        if len(lines) * line_h <= max_h and all(draw.textlength(ln, font=f) <= max_w for ln in lines):
            return f, lines, line_h, size
    f = _font(bold, sizes[-1])
    return f, _wrap(draw, label, f, max_w), int(sizes[-1] * 1.25), sizes[-1]


def _node(draw, cx: int, cy: int, w: int, h: int, label: str, *,
          accent, filled: bool = False, bold: bool = False, sizes=(24, 22, 20, 18, 16)) -> _Box:
    """Draw a rounded label box centred at (cx, cy); return its reveal box."""
    x0, y0, x1, y1 = cx - w // 2, cy - h // 2, cx + w // 2, cy + h // 2
    if filled:
        draw.rounded_rectangle([x0, y0, x1, y1], radius=14, fill=accent, outline=accent, width=3)
        txt_fill = _BG
    else:
        draw.rounded_rectangle([x0, y0, x1, y1], radius=14, outline=accent, width=3)
        txt_fill = _INK
    f, lines, line_h, _ = _fit_label(draw, label, w - 24, h - 16, bold, list(sizes))
    ty = cy - (len(lines) * line_h) // 2
    for ln in lines:
        s = _dt(ln)  # Arabic joins correctly; centred layout is direction-neutral
        tw = draw.textlength(s, font=f)
        draw.text((cx - tw / 2, ty), s, fill=txt_fill, font=f)
        ty += line_h
    return (x0, y0, x1, y1)


def _arrow(draw, p0: tuple[float, float], p1: tuple[float, float], *, accent, width: int = 3) -> _Box:
    """Draw an arrow from p0 to p1 with a small head; return its reveal box."""
    draw.line([p0, p1], fill=accent, width=width)
    ang = math.atan2(p1[1] - p0[1], p1[0] - p0[0])
    hl = 15
    left = (p1[0] - hl * math.cos(ang - 0.5), p1[1] - hl * math.sin(ang - 0.5))
    right = (p1[0] - hl * math.cos(ang + 0.5), p1[1] - hl * math.sin(ang + 0.5))
    draw.polygon([p1, left, right], fill=accent)
    x0, y0 = min(p0[0], p1[0]), min(p0[1], p1[1])
    x1, y1 = max(p0[0], p1[0]), max(p0[1], p1[1])
    return (int(x0) - hl, int(y0) - hl, int(x1) + hl, int(y1) + hl)


def _flow(draw, region: _Region, nodes: list[str], accent) -> list[list[_Box]]:
    rx0, ry0, rx1, ry1 = region
    rw, rh = rx1 - rx0, ry1 - ry0
    n = len(nodes)
    gap = 38
    bw = min(240, max(120, (rw - (n - 1) * gap) // n))
    bh = min(150, rh - 30)
    total = n * bw + (n - 1) * gap
    sx = rx0 + max(0, (rw - total) // 2)
    cy = ry0 + rh // 2
    elements: list[list[_Box]] = []
    centers: list[int] = []
    for i, label in enumerate(nodes):
        cx = sx + bw // 2 + i * (bw + gap)
        centers.append(cx)
        elements.append([_node(draw, cx, cy, bw, bh, label, accent=accent)])
        if i > 0:  # arrow into this node from the previous one
            p0 = (centers[i - 1] + bw // 2 + 3, cy)
            p1 = (cx - bw // 2 - 3, cy)
            elements.insert(len(elements) - 1, [_arrow(draw, p0, p1, accent=accent)])
    return elements


def _cycle(draw, region: _Region, nodes: list[str], accent) -> list[list[_Box]]:
    rx0, ry0, rx1, ry1 = region
    cx, cy = (rx0 + rx1) // 2, (ry0 + ry1) // 2
    radx = int((rx1 - rx0) * 0.33)
    rady = int((ry1 - ry0) * 0.36)
    n = len(nodes)
    bw, bh = 196, 78
    pts: list[tuple[int, int]] = []
    node_boxes: list[list[_Box]] = []
    for i, label in enumerate(nodes):
        ang = math.radians(-90 + i * 360 / n)
        x = int(cx + radx * math.cos(ang))
        y = int(cy + rady * math.sin(ang))
        pts.append((x, y))
        node_boxes.append([_node(draw, x, y, bw, bh, label, accent=accent, sizes=(20, 18, 16))])
    arrows: list[list[_Box]] = []
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        ux, uy = b[0] - a[0], b[1] - a[1]
        d = math.hypot(ux, uy) or 1
        ux, uy = ux / d, uy / d
        off = 64
        p0 = (a[0] + ux * off, a[1] + uy * off)
        p1 = (b[0] - ux * off, b[1] - uy * off)
        arrows.append([_arrow(draw, p0, p1, accent=accent)])
    return node_boxes + arrows


def _hierarchy(draw, region: _Region, nodes: list[str], accent) -> list[list[_Box]]:
    rx0, ry0, rx1, ry1 = region
    rw = rx1 - rx0
    cx = (rx0 + rx1) // 2
    bh = 92
    root_cy = ry0 + bh // 2 + 6
    child_cy = ry1 - bh // 2 - 8
    children = nodes[1:]
    elements: list[list[_Box]] = [[_node(draw, cx, root_cy, min(360, rw // 2), bh,
                                         nodes[0], accent=accent, filled=True, bold=True)]]
    m = len(children)
    gap = 30
    bw = min(250, max(110, (rw - (m - 1) * gap) // m))
    total = m * bw + (m - 1) * gap
    sx = rx0 + max(0, (rw - total) // 2)
    for j, label in enumerate(children):
        ccx = sx + bw // 2 + j * (bw + gap)
        elements.append([_arrow(draw, (cx, root_cy + bh // 2 + 2), (ccx, child_cy - bh // 2 - 2), accent=accent)])
        elements.append([_node(draw, ccx, child_cy, bw, bh, label, accent=accent, sizes=(22, 20, 18, 16))])
    return elements


def _compare(draw, region: _Region, groups: list[dict], accent) -> list[list[_Box]]:
    rx0, ry0, rx1, ry1 = region
    rw, rh = rx1 - rx0, ry1 - ry0
    g = (list(groups) + [{}, {}])[:2]
    col_w = rw // 2
    centers = [rx0 + col_w // 2, rx0 + col_w + col_w // 2]
    head_h = 64
    head_w = col_w - 64
    elements: list[list[_Box]] = []
    mid_x = (rx0 + rx1) // 2
    for ci, grp in enumerate(g):
        cx = centers[ci]
        head = (grp.get("heading") or f"Option {ci + 1}").strip()
        elements.append([_node(draw, cx, ry0 + head_h // 2 + 4, head_w, head_h, head,
                               accent=accent, filled=True, bold=True, sizes=(24, 22, 20, 18))])
        # Items as a left-aligned bulleted list under the header.
        items = [str(it).strip() for it in (grp.get("items") or []) if str(it).strip()][:5]
        bf = _font(False, 20)
        y = ry0 + head_h + 24
        tx = cx - head_w // 2
        for it in items:
            draw.ellipse([(tx, y + 8), (tx + 8, y + 16)], fill=accent)
            line_box: list[_Box] = []
            for k, ln in enumerate(_wrap(draw, it, bf, head_w - 30)):
                s = _dt(ln)
                draw.text((tx + 22, y), s, fill=_INK, font=bf)
                x0 = tx if k == 0 else tx + 22
                line_box.append((x0, y, tx + 22 + int(draw.textlength(s, font=bf)), y + 24))
                y += 28
            if line_box:
                elements.append(line_box)
            y += 8
        if ci == 0:  # vertical divider between the two columns
            draw.line([(mid_x, ry0 + 4), (mid_x, ry1 - 4)], fill=_RULE, width=2)
            elements.append([(mid_x - 1, ry0 + 4, mid_x + 1, ry1 - 4)])
    return elements


# --- Icons (Phase 3) ---------------------------------------------------------
# Most icons are crisp symbol glyphs from the bundled DejaVu font, drawn in the
# accent colour; a few high-value concepts with no clean glyph are hand-drawn
# from primitives. Unknown names fall back to a generic spark so a slide never
# breaks. ``_ALIASES`` maps natural words Claude is likely to use → a canonical key.

_GLYPH_ICONS = {
    "check": "✓", "cross": "✗", "star": "★", "heart": "♥", "sun": "☀",
    "cloud": "☁", "umbrella": "☂", "snow": "❄", "gear": "⚙", "scale": "⚖",
    "music": "♪", "flower": "✿", "sparkle": "✦", "peace": "☮", "atom": "⚛",
    "recycle": "♻", "warning": "⚠", "pencil": "✎", "arrow": "→", "hourglass": "⌛",
    "phone": "☎", "flag": "⚑", "scissors": "✂", "airplane": "✈", "anchor": "⚓",
    "crown": "♔", "infinity": "∞", "bolt": "⚡", "sum": "∑", "sqrt": "√",
    "question": "?", "plus": "+", "note": "♫",
}  # NB: ⌚/⌛ are absent from DejaVu (render as tofu) — clock is a primitive below.
_PRIMITIVE_ICONS = {"lightbulb", "book", "target", "globe", "search", "clock"}
_ALIASES = {
    "idea": "lightbulb", "correct": "check", "yes": "check", "tick": "check", "right": "check",
    "wrong": "cross", "no": "cross", "incorrect": "cross",
    "important": "star", "key": "star", "favourite": "star", "favorite": "star",
    "care": "heart", "health": "heart", "love": "heart",
    "energy": "sun", "light": "sun", "power": "bolt", "electric": "bolt", "electricity": "bolt",
    "weather": "cloud", "rain": "umbrella", "cold": "snow", "winter": "snow", "ice": "snow",
    "process": "gear", "machine": "gear", "system": "gear", "settings": "gear",
    "balance": "scale", "justice": "scale", "law": "scale", "fair": "scale",
    "sound": "music", "audio": "music",
    "plant": "flower", "nature": "flower", "growth": "flower", "grow": "flower",
    "science": "atom", "chemistry": "atom", "physics": "atom", "molecule": "atom",
    "reuse": "recycle", "sustainability": "recycle", "environment": "recycle",
    "caution": "warning", "danger": "warning", "alert": "warning",
    "write": "pencil", "edit": "pencil",
    "next": "arrow", "result": "arrow", "then": "arrow",
    "time": "clock", "duration": "clock",
    "call": "phone", "contact": "phone",
    "goal": "flag", "country": "flag", "destination": "flag",
    "cut": "scissors",
    "travel": "airplane", "transport": "airplane", "fly": "airplane",
    "stable": "anchor", "stability": "anchor",
    "king": "crown", "royal": "crown", "leader": "crown",
    "forever": "infinity", "endless": "infinity",
    "total": "sum", "add": "plus", "root": "sqrt", "math": "sum",
    "hourglass": "clock", "watch": "clock", "timer": "clock",
    "read": "book", "study": "book", "learn": "book",
    "aim": "target", "focus": "target", "objective": "target", "precision": "target",
    "world": "globe", "earth": "globe", "geography": "globe", "planet": "globe", "global": "globe",
    "find": "search", "observe": "search", "explore": "search", "look": "search", "magnify": "search",
}


def _lw(s: int) -> int:
    return max(3, s // 18)


def _prim_lightbulb(d, cx, cy, s, acc):
    h = s / 2
    lw = _lw(s)
    d.ellipse([cx - 0.34 * s, cy - 0.46 * s, cx + 0.34 * s, cy + 0.22 * s], outline=acc, width=lw)
    for dx in (-0.16, 0.16):  # base sides
        d.line([(cx + dx * s, cy + 0.2 * s), (cx + dx * s, cy + 0.36 * s)], fill=acc, width=lw)
    d.line([(cx - 0.16 * s, cy + 0.36 * s), (cx + 0.16 * s, cy + 0.36 * s)], fill=acc, width=lw)
    d.line([(cx - 0.1 * s, cy + 0.44 * s), (cx + 0.1 * s, cy + 0.44 * s)], fill=acc, width=lw)
    for ang in (-140, -90, -40):  # rays
        import math
        a = math.radians(ang)
        d.line([(cx + 0.46 * s * math.cos(a), cy - 0.2 * s + 0.46 * s * math.sin(a)),
                (cx + 0.6 * s * math.cos(a), cy - 0.2 * s + 0.6 * s * math.sin(a))], fill=acc, width=lw)


def _prim_book(d, cx, cy, s, acc):
    lw = _lw(s)
    top, bot = cy - 0.3 * s, cy + 0.3 * s
    d.line([(cx, top + 0.06 * s), (cx, bot)], fill=acc, width=lw)  # spine
    for sgn in (-1, 1):  # two pages
        d.line([(cx, top + 0.06 * s), (cx + sgn * 0.42 * s, top), (cx + sgn * 0.42 * s, bot - 0.04 * s),
                (cx, bot)], fill=acc, width=lw, joint="curve")


def _prim_target(d, cx, cy, s, acc):
    lw = _lw(s)
    for r in (0.44, 0.28, 0.13):
        d.ellipse([cx - r * s, cy - r * s, cx + r * s, cy + r * s], outline=acc, width=lw)
    d.ellipse([cx - 0.04 * s, cy - 0.04 * s, cx + 0.04 * s, cy + 0.04 * s], fill=acc)


def _prim_globe(d, cx, cy, s, acc):
    lw = _lw(s)
    r = 0.44 * s
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=acc, width=lw)
    d.ellipse([cx - 0.18 * s, cy - r, cx + 0.18 * s, cy + r], outline=acc, width=lw)  # meridian
    d.line([(cx - r, cy), (cx + r, cy)], fill=acc, width=lw)  # equator
    d.arc([cx - r, cy - 0.22 * s, cx + r, cy + 0.06 * s], 20, 160, fill=acc, width=lw)


def _prim_search(d, cx, cy, s, acc):
    lw = _lw(s)
    d.ellipse([cx - 0.4 * s, cy - 0.4 * s, cx + 0.08 * s, cy + 0.08 * s], outline=acc, width=lw)
    d.line([(cx + 0.04 * s, cy + 0.04 * s), (cx + 0.36 * s, cy + 0.36 * s)], fill=acc, width=lw + 2)


def _prim_clock(d, cx, cy, s, acc):
    lw = _lw(s)
    r = 0.44 * s
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=acc, width=lw)
    d.line([(cx, cy), (cx, cy - 0.3 * s)], fill=acc, width=lw)            # minute hand
    d.line([(cx, cy), (cx + 0.22 * s, cy + 0.12 * s)], fill=acc, width=lw)  # hour hand
    d.ellipse([cx - 0.05 * s, cy - 0.05 * s, cx + 0.05 * s, cy + 0.05 * s], fill=acc)


_PRIMS = {"lightbulb": _prim_lightbulb, "book": _prim_book, "target": _prim_target,
          "globe": _prim_globe, "search": _prim_search, "clock": _prim_clock}


def draw_icon(draw, name: str, cx: int, cy: int, s: int, *, accent) -> None:
    """Draw the named icon centred at (cx, cy) within an s×s box, in ``accent``."""
    n = " ".join((name or "").lower().split())
    n = _ALIASES.get(n, n)
    if n in _PRIMS:
        _PRIMS[n](draw, cx, cy, s, accent)
        return
    glyph = _GLYPH_ICONS.get(n, "✦")  # unknown → generic spark
    f = _font(False, int(s * 0.96))
    bb = draw.textbbox((0, 0), glyph, font=f)
    w, h = bb[2] - bb[0], bb[3] - bb[1]
    draw.text((cx - w / 2 - bb[0], cy - h / 2 - bb[1]), glyph, font=f, fill=accent)


def _icons(draw, region: _Region, items: list[dict], accent) -> list[list[_Box]]:
    rx0, ry0, rx1, ry1 = region
    rw, rh = rx1 - rx0, ry1 - ry0
    n = len(items)
    cols = n if n <= 4 else 3
    rows = (n + cols - 1) // cols
    tile_w = rw / cols
    tile_h = rh / rows
    icon_s = int(min(tile_w * 0.5, tile_h * 0.46, 120))
    lf = _font(False, 22)
    elements: list[list[_Box]] = []
    for idx, it in enumerate(items):
        r, c = idx // cols, idx % cols
        cx = int(rx0 + tile_w * (c + 0.5))
        icon_cy = int(ry0 + tile_h * r + tile_h * 0.36)
        draw_icon(draw, it.get("icon", ""), cx, icon_cy, icon_s, accent=accent)
        # Label under the icon, centred, up to 2 lines.
        label = " ".join((it.get("label") or "").split())
        lines = _wrap(draw, label, lf, int(tile_w - 24))[:2]
        ly = icon_cy + icon_s // 2 + 14
        maxw = icon_s
        for ln in lines:
            s = _dt(ln)
            tw = draw.textlength(s, font=lf)
            maxw = max(maxw, tw)
            draw.text((cx - tw / 2, ly), s, fill=_INK, font=lf)
            ly += 28
        half = int(max(icon_s, maxw) / 2) + 8
        elements.append([(cx - half, icon_cy - icon_s // 2 - 4, cx + half, ly)])
    return elements


def render_diagram(draw: ImageDraw.ImageDraw, region: _Region, visual: dict, *, accent=None) -> list[list[_Box]]:
    """Draw a diagram into ``region`` and return its ordered reveal elements.

    Returns ``[]`` for an unusable spec so the caller can fall back to bullets.
    """
    acc = accent or _GREEN
    if not isinstance(visual, dict):
        return []
    kind = (visual.get("kind") or "").strip()
    nodes = [str(n).strip() for n in (visual.get("nodes") or []) if str(n).strip()][:5]
    groups = visual.get("groups") or []
    items = [
        {"icon": str(it.get("icon") or "").strip(), "label": str(it.get("label") or "").strip()}
        for it in (visual.get("items") or []) if isinstance(it, dict) and str(it.get("label") or "").strip()
    ][:6]

    if kind == "icons" and len(items) >= 2:
        return _icons(draw, region, items, acc)
    if kind == "compare" and len(groups) >= 1:
        return _compare(draw, region, groups, acc)
    if kind == "hierarchy" and len(nodes) >= 2:
        return _hierarchy(draw, region, nodes, acc)
    if kind == "cycle" and len(nodes) >= 3:
        return _cycle(draw, region, nodes, acc)
    if len(nodes) >= 2:  # flow (also the fallback for malformed cycle/hierarchy)
        return _flow(draw, region, nodes, acc)
    return []


def caption_element(draw: ImageDraw.ImageDraw, region: _Region, caption: str) -> list[_Box]:
    """Draw a centred caption under the diagram; return its reveal box (or [])."""
    cap = " ".join((caption or "").split())
    if not cap:
        return []
    rx0, ry0, rx1, ry1 = region
    f = _font(False, 20)
    cap = _wrap(draw, cap, f, rx1 - rx0)[0] if cap else cap
    cap = _dt(cap)
    tw = draw.textlength(cap, font=f)
    x = (rx0 + rx1) // 2 - tw / 2
    y = ry1 + 6
    draw.text((x, y), cap, fill=_MUTED, font=f)
    return [(int(x), y, int(x + tw), y + 24)]
