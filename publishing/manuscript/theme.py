"""Book design themes (founder 2026-10-25): every book gets a theme suited to
it, chosen by DATA — the brief's `theme`, else the first `by_strand` prefix of
its subject strand, else the default (config/book_themes.json). A theme is a
palette, typography, a chapter-opener motif and a palette hint for generated
images. Motifs are named by SHAPE (organic, particles, waves, grid, dots,
contours, rule), never by subject, so one renderer serves every book.
"""
from __future__ import annotations

import json
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
    return {"name": name, **cfg["themes"][name]}


def css_vars(t: dict) -> str:
    return (":root{" + f"--brand:{t['brand']};--brand-dark:{t['brand_dark']};--soft:{t['brand_soft']};"
            f"--accent:{t['accent']};--ink:{t['ink']};--muted:{t['muted']};--line:{t['line']};"
            f"--warn:{t['warn']};--warn-edge:{t['warn_edge']};--think:{t['think']};"
            f"--ext:{t['extension']};--ext-edge:{t['extension_edge']};"
            f"--hfont:{t['heading_font']};--bfont:{t['body_font']};" + "}")


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
