"""Book design themes (founder 2026-10-25): every book gets a theme suited to
it, chosen by DATA — the brief's `theme`, else the first `by_strand` prefix of
its subject strand, else the default (config/book_themes.json). A theme is a
palette, typography, a chapter-opener motif and a palette hint for generated
images. Motifs are named by SHAPE (organic, particles, waves, grid, dots,
contours, rule), never by subject, so one renderer serves every book.
"""
from __future__ import annotations

import json
import os
import math

from publishing.schemas import ROOT

THEMES = ROOT / "config" / "book_themes.json"
BRIEFS = ROOT / "config" / "briefs.json"


def for_work(slug: str) -> dict:
    cfg = json.loads(THEMES.read_text(encoding="utf-8"))
    brief = next((b for b in json.loads(BRIEFS.read_text(encoding="utf-8")) if b.get("slug") == slug), {})
    name = brief.get("theme")
    if name not in cfg["themes"]:
        strand = str(brief.get("subject_strand") or "")
        name = next((t for p, t in sorted(cfg["by_strand"].items(), key=lambda kv: -len(kv[0]))
                     if strand.startswith(p)), cfg["default"])
    theme = {"name": name, **cfg["themes"][name]}
    # a font trial (books runner, founder D3 2026-10-28: Sitka is Windows-only, pick an open serif) overrides the
    # theme's fonts for one render without touching the theme itself
    for key, var in (("heading_font", "PUBLISH_HEADING_FONT"), ("body_font", "PUBLISH_BODY_FONT")):
        if os.getenv(var):
            theme[key] = os.environ[var]
    return theme


def css_vars(t: dict) -> str:
    return (":root{" + f"--brand:{t['brand']};--brand-dark:{t['brand_dark']};--soft:{t['brand_soft']};"
            f"--accent:{t['accent']};--ink:{t['ink']};--muted:{t['muted']};--line:{t['line']};"
            f"--warn:{t['warn']};--warn-edge:{t['warn_edge']};--think:{t['think']};"
            f"--ext:{t['extension']};--ext-edge:{t['extension_edge']};"
            f"--hfont:{t['heading_font']};--bfont:{t['body_font']};--paper:{page(t)['paper']};" + "}")


PAGE_DEFAULTS = {
    "paper": "#ffffff", "paper_deep": "#f4f4f4", "sage": "#9aa5a0",
    "trim": {"width_mm": 210, "height_mm": 297}, "bleed_mm": 3,
    "margins_mm": {"top": 20, "bottom": 22, "inner": 22, "outer": 18},
    "body_size_pt": 10.5, "body_leading": 1.5,
    "tab": None, "running_head": {"verso": "work", "recto": "chapter", "rule": "#dddddd"},
    "folio": {"position": "outer", "font": "inherit"},
    "opener": {"numeral": "plain", "hero": False, "objectives": True, "contents": True},
    "practice": {"new_page": True, "answer_lines": True, "line_pitch_mm": 8.5},
    "answers": {"new_page": True, "columns": 1},
}


KDP_BLEED_MM = 3.175     # KDP paperback: 0.125 in on the top, bottom and outside edge (the spine side is trimmed later)


def bleed(p: dict, edition: str) -> float:
    """Bleed for an edition: the theme's for print, KDP's own for the KDP interior, none on screen. Pages are
    laid out with the bleed on all four sides; the KDP build removes the spine-side strip after printing."""
    return {"print": p["bleed_mm"], "kdp": KDP_BLEED_MM}.get(edition, 0)


def page(t: dict) -> dict:
    """The theme's page treatment, every key defaulted (a theme without a
    `page` block still renders: plain white, no tabs)."""
    p = {**PAGE_DEFAULTS, **(t.get("page") or {})}
    for k in ("margins_mm", "trim", "running_head", "folio", "opener", "practice", "answers"):
        p[k] = {**PAGE_DEFAULTS[k], **((t.get("page") or {}).get(k) or {})}
    return p


def _esc(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def _bg_svg(t: dict, p: dict, side: str, chapter: int, edition: str) -> str:
    """The page background for one side: paper, the thumb-index tab on the
    OUTER edge (stepping down by chapter, into the bleed for print) and the
    running-head hairline. Drawn as the page background so nothing in the text
    flow can collide with it."""
    import base64
    b = bleed(p, edition)
    W, H = p["trim"]["width_mm"] + 2 * b, p["trim"]["height_mm"] + 2 * b
    m = p["margins_mm"]
    out = [f'<rect width="{W}" height="{H}" fill="{p["paper"]}"/>']
    tab = p.get("tab")
    if tab:
        y = b + tab["first_top_mm"] + (chapter - 1) * tab["step_mm"]
        tw = tab["width_mm"] + b
        x = 0 if side == "verso" else W - tw
        rx = 2.2
        # a tab with the inner corners rounded, the outer edge running off the page
        if side == "verso":
            path = (f"M{x},{y} H{x + tw - rx} Q{x + tw},{y} {x + tw},{y + rx} V{y + tab['height_mm'] - rx} "
                    f"Q{x + tw},{y + tab['height_mm']} {x + tw - rx},{y + tab['height_mm']} H{x} Z")
            tx = b + tab["width_mm"] / 2
        else:
            path = (f"M{x + tw},{y} H{x + rx} Q{x},{y} {x},{y + rx} V{y + tab['height_mm'] - rx} "
                    f"Q{x},{y + tab['height_mm']} {x + rx},{y + tab['height_mm']} H{x + tw} Z")
            tx = W - b - tab["width_mm"] / 2
        out.append(f'<path d="{path}" fill="{tab["fill"]}"/>')
        out.append(f'<text x="{tx}" y="{y + tab["height_mm"] / 2 + 2.2}" text-anchor="middle" '
                   f'font-family="Georgia, serif" font-weight="700" font-size="6.2" fill="{tab["text"]}">{chapter}</text>')
    inner, outer = (m["outer"], m["inner"]) if side == "verso" else (m["inner"], m["outer"])
    ry = b + m["top"] - 1.8   # under the running head, never through it
    out.append(f'<line x1="{b + inner}" y1="{ry}" x2="{W - b - outer}" y2="{ry}" '
               f'stroke="{p["running_head"]["rule"]}" stroke-width="0.25"/>')
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}mm" height="{H}mm" viewBox="0 0 {W} {H}">'
           + "".join(out) + "</svg>")
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


def page_css(t: dict, chapter: int, chapter_title: str, work_title: str, edition: str = "digital") -> str:
    """@page rules from the theme: trim (+ bleed for print), the binding gutter
    on the inside of facing pages, outer-edge tabs, running heads (verso: the
    book, recto: the chapter) and folios on the outer corners. The opening
    page carries none of them — the chapter opener is its own design."""
    p = page(t)
    b = bleed(p, edition)
    W, H = p["trim"]["width_mm"] + 2 * b, p["trim"]["height_mm"] + 2 * b
    m = p["margins_mm"]
    head = "font: 7.6pt " + t["body_font"] + "; color: " + t["muted"] + "; letter-spacing: .06em; text-transform: uppercase;"
    folio = "font: 9pt " + p["folio"]["font"] + "; color: " + t["brand_dark"] + ";"
    return (
        f"@page {{ size: {W}mm {H}mm; margin: {m['top'] + b}mm {m['outer'] + b}mm {m['bottom'] + b}mm {m['inner'] + b}mm; }}"
        f"@page :left {{ margin-left: {m['outer'] + b}mm; margin-right: {m['inner'] + b}mm;"
        f"  @top-left {{ content: \"{_esc(work_title)}\"; {head} vertical-align: bottom; padding-bottom: 3mm; }}"
        f"  @bottom-left {{ content: counter(page); {folio} }} }}"
        f"@page :right {{ margin-left: {m['inner'] + b}mm; margin-right: {m['outer'] + b}mm;"
        f"  @top-right {{ content: \"Chapter {chapter} · {_esc(chapter_title)}\"; {head} vertical-align: bottom; padding-bottom: 3mm; }}"
        f"  @bottom-right {{ content: counter(page); {folio} }} }}"
        f"@page :first {{ margin-top: {b + 12}mm;"
        f"  @top-right {{ content: none; }} @top-left {{ content: none; }} @bottom-right {{ content: none; }} }}"
        f"html, body {{ background: transparent; }}"
        f"body {{ font-size: {p['body_size_pt']}pt; line-height: {p['body_leading']}; }}")


def backgrounds(t: dict, chapter: int, edition: str = "digital") -> dict[str, bytes]:
    """The page art as SVG for the opener, versos and rectos. Edge prints only a
    flat @page colour, never an @page image (probe 2026-10-25), so the renderer
    lays these UNDER each printed page as vector art instead."""
    import base64
    p = page(t)
    dec = lambda uri: base64.b64decode(uri.split(",", 1)[1])
    return {"first": dec(_first_bg(t, p, edition, chapter)), "verso": dec(_bg_svg(t, p, "verso", chapter, edition)),
            "recto": dec(_bg_svg(t, p, "recto", chapter, edition))}


def _first_bg(t: dict, p: dict, edition: str, chapter: int = 1) -> str:
    """The opener page: paper with a deeper band across the top that holds the
    chapter identity, the motif inside it, and the tab."""
    import base64
    import re as _re
    b = bleed(p, edition)
    W, H = p["trim"]["width_mm"] + 2 * b, p["trim"]["height_mm"] + 2 * b
    band = b + 112
    motif = motif_svg(t, 600, 200)
    inner = _re.sub(r"^<svg[^>]*>|</svg>$", "", motif)
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}mm" height="{H}mm" viewBox="0 0 {W} {H}">'
           f'<rect width="{W}" height="{H}" fill="{p["paper"]}"/>'
           f'<rect width="{W}" height="{band}" fill="{p["paper_deep"]}"/>'
           f'<g transform="translate(0,0) scale({W / 600:.4f},{band / 200:.4f})" opacity="0.3">{inner}</g>'
           f'<rect y="{band}" width="{W}" height="0.8" fill="{t["brand"]}"/>'
           + (_tab_only(t, p, b, W, chapter) if p.get("tab") else "") + "</svg>")
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


def _tab_only(t: dict, p: dict, b: float, W: float, chapter: int) -> str:
    tab = p["tab"]
    y, tw = b + tab["first_top_mm"] + (chapter - 1) * tab["step_mm"], tab["width_mm"] + b
    return (f'<path d="M{W},{y} H{W - tw + 2.2} Q{W - tw},{y} {W - tw},{y + 2.2} V{y + tab["height_mm"] - 2.2} '
            f'Q{W - tw},{y + tab["height_mm"]} {W - tw + 2.2},{y + tab["height_mm"]} H{W} Z" fill="{tab["fill"]}"/>'
            f'<text x="{W - b - tab["width_mm"] / 2}" y="{y + tab["height_mm"] / 2 + 2.2}" text-anchor="middle" '
            f'font-family="Georgia, serif" font-weight="700" font-size="6.2" fill="{tab["text"]}">{chapter}</text>')


def motif_svg(t: dict, w: int = 600, h: int = 120) -> str:
    """A quiet decorative band for the chapter opener, drawn from the theme's
    motif and colours (decoration only: aria-hidden, never content)."""
    c1, c2 = t["brand"], t["accent"]
    m, out = t.get("motif"), []
    if m == "organic":
        for i in range(14):
            x, y, r = 40 + i * 42 % w, 30 + (i * 37) % 70, 10 + (i * 7) % 22
            out.append(f'<ellipse cx="{x}" cy="{y}" rx="{r * 1.3:.0f}" ry="{r}" fill="none" stroke="{c1}" '
                       f'stroke-width="2" opacity="{0.10 + (i % 3) * 0.06:.2f}"/>'
                       f'<circle cx="{x + r * 0.3:.0f}" cy="{y}" r="{max(2, r // 4)}" fill="{c2}" opacity="0.18"/>')
    elif m == "particles":
        for i in range(60):
            out.append(f'<circle cx="{(i * 53) % w}" cy="{(i * 29) % h}" r="{3 + i % 5}" fill="{c1 if i % 3 else c2}" '
                       f'opacity="0.16"/>')
    elif m == "waves":
        for k in range(5):
            pts = " ".join(f"{x},{h / 2 + math.sin(x / 40 + k) * (10 + 6 * k):.1f}" for x in range(0, w + 1, 10))
            out.append(f'<polyline points="{pts}" fill="none" stroke="{c1 if k % 2 else c2}" stroke-width="2" opacity="0.18"/>')
    elif m == "grid":
        out += [f'<line x1="{x}" y1="0" x2="{x}" y2="{h}" stroke="{c1}" opacity="0.12"/>' for x in range(0, w, 20)]
        out += [f'<line x1="0" y1="{y}" x2="{w}" y2="{y}" stroke="{c1}" opacity="0.12"/>' for y in range(0, h, 20)]
        out.append(f'<circle cx="{w - 90}" cy="60" r="44" fill="none" stroke="{c2}" stroke-width="2" opacity="0.4"/>'
                   f'<line x1="{w - 160}" y1="104" x2="{w - 20}" y2="16" stroke="{c1}" stroke-width="2" opacity="0.35"/>')
    elif m == "dots":
        out += [f'<circle cx="{x}" cy="{y}" r="2.2" fill="{c1}" opacity="0.18"/>' for x in range(10, w, 18)
                for y in range(10, h, 18)]
    elif m == "contours":
        for k in range(6):
            out.append(f'<ellipse cx="{w * 0.7:.0f}" cy="{h / 2:.0f}" rx="{40 + 30 * k}" ry="{14 + 12 * k}" fill="none" '
                       f'stroke="{c1}" stroke-width="1.5" opacity="0.18"/>')
    else:
        out.append(f'<line x1="0" y1="{h - 10}" x2="{w}" y2="{h - 10}" stroke="{c2}" stroke-width="3" opacity="0.5"/>')
    return (f'<svg class="motif" viewBox="0 0 {w} {h}" preserveAspectRatio="xMidYMid slice" aria-hidden="true">'
            + "".join(out) + "</svg>")
