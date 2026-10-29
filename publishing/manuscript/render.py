"""Render a drafted chapter (§9): semantic HTML → PDF through headless
Chromium (Edge on this machine), plus the Gate D review packet.

Labels are never baked into a picture (Biology acceptance, §15.4). A figure
is its approved raster with the labels drawn on top as vector text in two
columns RESERVED outside the picture, each joined by a leader line to the
centre of its detected region (``visual_assets.vision.regions``). A label
whose region the vision pass did not find is not placed; it is listed under
the figure as missing, so nothing points at nothing.

The design here is a provisional sample style; the design system is Gate C.
"""
from __future__ import annotations

import base64
import html
import json
import re
import subprocess
from pathlib import Path

from publishing.snapshot import client, load_env

BUCKET = "visual-assets"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
IMG_W, COL_W, LINE_H = 330, 140, 23


def browser() -> list[str]:
    """The headless browser that prints the pages, as the start of its command line: PUBLISH_BROWSER when set, else
    Edge on Windows (this machine), else Chromium on Linux (the books runner image). Chromium in a container runs as an
    unprivileged user without the kernel sandbox it would need, so it gets --no-sandbox there."""
    import os
    import shutil
    named = os.getenv("PUBLISH_BROWSER")
    if named:
        return [named] + (["--no-sandbox"] if os.name != "nt" else [])
    if os.name == "nt" and Path(EDGE).exists():
        return [EDGE]
    for name in ("chromium", "chromium-browser", "google-chrome", "msedge"):
        found = shutil.which(name)
        if found:
            return [found] + (["--no-sandbox"] if os.name != "nt" else [])
    raise RuntimeError("no headless browser found (set PUBLISH_BROWSER)")


def assets_dir() -> Path:
    """Where the book's own pictures live: PUBLISH_ASSETS (the books runner pulls them there), else the publishing
    folder's book-assets/ as on this machine."""
    import os
    from publishing.report import OUT
    return Path(os.getenv("PUBLISH_ASSETS") or OUT / "book-assets")


def local_asset(path: str) -> Path:
    """A stored picture path as it exists here. Dossiers record the path of the machine that drafted them
    ('..\\study-guide-sprint1\\book-assets\\x.png', 'C:\\Users\\…\\book-assets\\x.png'); elsewhere the same file is
    found by its name in assets_dir()."""
    p = Path(path)
    if p.exists():
        return p
    return assets_dir() / re.split(r"[\\/]", path)[-1]


def md(text: str) -> str:
    t = html.escape(str(text or ""))
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<em>\1</em>", t)
    return t


def _norm(s: str) -> str:
    return re.sub(r"[\s_]+", " ", str(s or "").strip().lower())


def point_for(name: str, points: dict):
    """The verified point for a part a question names: exact, or a point whose name ends the part's name
    ('sap vacuole' → 'vacuole'), so a fuller name in the answer still finds its structure."""
    want = _norm(name)
    pts = {_norm(k): v for k, v in (points or {}).items()}
    if want in pts:
        return pts[want]
    tails = [k for k in pts if want.endswith(" " + k)]
    return pts[max(tails, key=len)] if tails else None


def _download(http, base: str, path: str) -> bytes:
    r = http.get(f"{base}/storage/v1/object/{BUCKET}/{path}")   # absolute: the client's base is /rest/v1
    r.raise_for_status()
    return r.content


def matte(png: bytes, tol: int = 24) -> bytes | None:
    """A generated illustration on a plain light background → the same picture
    with a TRANSPARENT background (founder 2026-10-25: no white rectangles on
    tinted paper; never a global blend mode that shifts colours). The
    background colour is sampled at the corners; only background CONNECTED to
    the border is removed (a light area inside the subject stays), and the edge
    is feathered. Returns None when the matte is not trustworthy (too little or
    too much removed), so the caller keeps the original. Photos never come here."""
    import io
    from PIL import Image, ImageChops, ImageDraw, ImageFilter
    im = Image.open(io.BytesIO(png)).convert("RGB")
    w, h = im.size
    k = max(4, w // 64)
    corners = [im.crop(b).resize((1, 1), Image.Resampling.BOX).getpixel((0, 0))
               for b in ((0, 0, k, k), (w - k, 0, w, k), (0, h - k, k, h), (w - k, h - k, w, h))]
    bg = tuple(sorted(c[i] for c in corners)[len(corners) // 2] for i in range(3))
    if min(bg) < 200:
        return None                                     # not a light plain background
    import numpy as np
    a = np.asarray(im).astype(np.float32)
    bgv = np.array(bg, dtype=np.float32)
    dist = np.abs(a - bgv).max(axis=2)
    mx, mn = a.max(axis=2), a.min(axis=2)
    sat = (mx - mn) / np.maximum(mx, 1)
    # the background family: close to the paper colour, or a soft shadow /
    # gradient of it (light and nearly grey)
    fam = (dist < tol) | ((sat < 0.10) & (dist < 70) & (mx > 150))
    near = Image.fromarray((fam * 255).astype(np.uint8)).copy()   # writable: fromarray shares a read-only buffer
    seeds = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1), (w // 2, 0), (w // 2, h - 1), (0, h // 2), (w - 1, h // 2)]
    for s in seeds:
        if near.getpixel(s) == 255:
            ImageDraw.floodfill(near, s, 128, thresh=0)
    region = np.asarray(near) == 128                     # background CONNECTED to the border only
    share = float(region.mean())
    if not 0.12 < share < 0.95:
        return None
    # difference matte: inside that region a pixel is as opaque as it is
    # different from the paper, so a shadow becomes a real soft shadow and the
    # picture looks unchanged on a paper of the same tone
    alpha = np.ones((h, w), dtype=np.float32)
    # a studio gradient within a few levels of the paper is background, not shadow
    alpha[region] = np.clip((dist[region] - 16.0) / 54.0, 0, 1) ** 0.9
    alpha = np.asarray(Image.fromarray((alpha * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.8))) / 255.0
    safe = np.maximum(alpha, 1e-3)[..., None]
    rgb = np.clip((a - (1 - alpha[..., None]) * bgv) / safe, 0, 255)    # un-premultiply: colours stay true
    out = np.dstack([rgb, alpha * 255]).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(out, "RGBA").save(buf, "PNG", optimize=True)
    return buf.getvalue()


def asset_bytes(http, base: str, asset: dict) -> bytes:
    """A library asset from storage, or a book asset from the local store."""
    if asset.get("local_path"):
        return local_asset(asset["local_path"]).read_bytes()
    return _download(http, base, asset["storage_path"])


def figure_svg(png: bytes, vision: dict, labels: list[str], anchors: dict | None = None) -> tuple[str, list[str]]:
    """The picture with its labels as vector overlays. Returns (svg, unplaced).

    With `anchors` (verified points ON each structure, from the book's annotation data) every leader ends on its
    structure; a label without a verified point is not printed (founder 2026-10-27: a wrong label is worse than
    none). Without annotation data the older region-edge placement is used."""
    if anchors is not None:
        return _figure_svg_anchored(png, labels, anchors)
    w, h = float(vision.get("w") or 0), float(vision.get("h") or 0)
    if (w <= 1 or h <= 1) and png[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = float(int.from_bytes(png[16:20], "big")), float(int.from_bytes(png[20:24], "big"))
    w, h = w or 1.0, h or 1.0
    scale = IMG_W / w
    ih = h * scale
    regions = {_norm(k): v for k, v in (vision.get("regions") or {}).items()}
    # Each label points at the NEAR EDGE of its structure, at the label's own height (founder 2026-10-26:
    # leaders aimed at box centres crossed, and an enclosing structure's centre — a wall, a membrane — is
    # the middle of something else). Labels on a side are ordered by height, so their near-horizontal
    # leaders cannot cross.
    sides = {"L": [], "R": []}
    unplaced = []
    for lab in labels:
        boxes = [[v * scale for v in b] for b in regions.get(_norm(lab)) or [] if len(b) == 4]
        if not boxes:
            unplaced.append(lab)
            continue
        # the side whose edge the structure comes closest to; the box nearest that edge
        lbox = min(boxes, key=lambda b: b[0])
        rbox = max(boxes, key=lambda b: b[2])
        side = "L" if lbox[0] <= IMG_W - rbox[2] else "R"
        sides[side].append((lab, lbox if side == "L" else rbox))
    if not sides["L"] and not sides["R"]:
        # nothing to label: no reserved columns, the picture alone
        b64 = base64.b64encode(png).decode()
        return (f'<svg viewBox="0 0 {IMG_W} {ih:.0f}" role="img"><image href="data:image/png;base64,{b64}" '
                f'x="0" y="0" width="{IMG_W}" height="{ih:.1f}"/></svg>'), unplaced
    col = max([COL_W] + [len(lab) * 8.4 + 18 for items in sides.values() for lab, _ in items])
    total_w = IMG_W + 2 * col
    height = ih
    parts = []
    for side, items in sides.items():
        items.sort(key=lambda t: (t[1][1] + t[1][3]) / 2)
        y = -LINE_H
        for lab, (x0, y0, x1, y1) in items:
            want = (y0 + y1) / 2
            y = max(y + LINE_H, want)
            ly = min(y, ih + LINE_H * len(items))
            height = max(height, ly + 8)
            inset_x = min(6.0, (x1 - x0) * 0.08)
            inset_y = min(4.0, (y1 - y0) * 0.2)
            ay = min(max(ly - 4, y0 + inset_y), y1 - inset_y)         # level with the label where the box allows
            ax = (x0 + inset_x) if side == "L" else (x1 - inset_x)    # just inside the structure's near edge
            if side == "L":
                tx, anchor, lx = col - 8, "end", col - 4
            else:
                tx, anchor, lx = col + IMG_W + 8, "start", col + IMG_W + 4
            px = col + ax
            parts.append(f'<line x1="{lx:.1f}" y1="{ly - 4:.1f}" x2="{px:.1f}" y2="{ay:.1f}" class="leader"/>'
                         f'<circle cx="{px:.1f}" cy="{ay:.1f}" r="2.2" class="dot"/>'
                         f'<text x="{tx:.1f}" y="{ly:.1f}" text-anchor="{anchor}" class="lab">{html.escape(lab)}</text>')
    b64 = base64.b64encode(png).decode()
    svg = (f'<svg viewBox="0 0 {total_w} {height:.0f}" role="img">'
           f'<image href="data:image/png;base64,{b64}" x="{col}" y="0" width="{IMG_W}" height="{ih:.1f}"/>'
           + "".join(parts) + "</svg>")
    return svg, unplaced


def _png_size(png: bytes) -> tuple[float, float]:
    from PIL import Image
    import io
    w, h = Image.open(io.BytesIO(png)).size
    return float(w), float(h)


def _strip_svg_anchored(png: bytes, labels: list[str], anchors: dict) -> tuple[str, list[str]]:
    """A wide strip of panels (founder 2026-10-26, Fig. 9.4): a side column's leader would cross the other panels
    to reach its own, so each label sits above or below its point with a short vertical leader instead. A label
    goes to the nearer edge, or to the other edge when it would touch its neighbour; the strip takes the full width
    the side columns would have used."""
    w, h = _png_size(png)
    width = IMG_W + 2 * COL_W
    scale = width / w
    ih = h * scale
    rows: dict[str, list] = {"T": [], "B": []}
    unplaced = []
    pts = []
    for lab in labels:
        pt = anchors.get(lab) or anchors.get(_norm(lab))
        if pt:
            pts.append((lab, pt[0] * scale, pt[1] * scale))
        else:
            unplaced.append(lab)
    for lab, x, y in sorted(pts, key=lambda t: t[1]):
        half = len(lab) * 3.9 + 4
        cx = min(max(x, half), width - half)
        pref = "T" if y < ih / 2 else "B"
        other = "B" if pref == "T" else "T"
        fits = lambda r: not rows[r] or rows[r][-1][3] + 6 <= cx - half    # noqa: E731
        row = pref if fits(pref) else other if fits(other) else pref
        last = rows[row][-1] if rows[row] else None
        if last is not None and last[3] + 6 > cx - half:
            cx = last[3] + 6 + half
        rows[row].append((lab, x, y, cx + half, cx))
    top = LINE_H + 6 if rows["T"] else 0
    height = top + ih + (LINE_H + 6 if rows["B"] else 0)
    parts = []
    for row, items in rows.items():
        for lab, x, y, _, cx in items:
            ty = top - 10 if row == "T" else top + ih + LINE_H - 4
            ly = top - 6 if row == "T" else top + ih + 4
            parts.append(f'<line x1="{cx:.1f}" y1="{ly:.1f}" x2="{x:.1f}" y2="{top + y:.1f}" class="leader"/>'
                         f'<circle cx="{x:.1f}" cy="{top + y:.1f}" r="2.4" class="dot"/>'
                         f'<text x="{cx:.1f}" y="{ty:.1f}" text-anchor="middle" class="lab">{html.escape(lab)}</text>')
    b64 = base64.b64encode(png).decode()
    svg = (f'<svg viewBox="0 0 {width} {height:.0f}" role="img">'
           f'<image href="data:image/png;base64,{b64}" x="0" y="{top}" width="{width}" height="{ih:.1f}"/>'
           + "".join(parts) + "</svg>")
    return svg, unplaced


def _figure_svg_anchored(png: bytes, labels: list[str], anchors: dict) -> tuple[str, list[str]]:
    w, h = _png_size(png)
    if w >= 3 * h:
        return _strip_svg_anchored(png, labels, anchors)
    scale = IMG_W / w
    ih = h * scale
    sides = {"L": [], "R": []}
    unplaced = []
    for lab in labels:
        pt = anchors.get(lab) or anchors.get(_norm(lab))
        if not pt:
            unplaced.append(lab)
            continue
        x, y = pt[0] * scale, pt[1] * scale
        sides["L" if x < IMG_W / 2 else "R"].append((lab, x, y))
    b64 = base64.b64encode(png).decode()
    if not sides["L"] and not sides["R"]:
        return (f'<svg viewBox="0 0 {IMG_W} {ih:.0f}" role="img"><image href="data:image/png;base64,{b64}" '
                f'x="0" y="0" width="{IMG_W}" height="{ih:.1f}"/></svg>'), unplaced
    col = max([COL_W] + [len(lab) * 8.4 + 18 for items in sides.values() for lab, _, _ in items])
    total_w, height, parts = IMG_W + 2 * col, ih, []
    for side, items in sides.items():
        items.sort(key=lambda t: t[2])                # ordered by height: leaders cannot cross
        y = -LINE_H
        for lab, ax, ay in items:
            y = max(y + LINE_H, ay + 4)
            ly = min(y, ih + LINE_H * len(items))
            height = max(height, ly + 8)
            if side == "L":
                tx, anchor, lx = col - 8, "end", col - 4
            else:
                tx, anchor, lx = col + IMG_W + 8, "start", col + IMG_W + 4
            px = col + ax
            parts.append(f'<line x1="{lx:.1f}" y1="{ly - 4:.1f}" x2="{px:.1f}" y2="{ay:.1f}" class="leader"/>'
                         f'<circle cx="{px:.1f}" cy="{ay:.1f}" r="2.4" class="dot"/>'
                         f'<text x="{tx:.1f}" y="{ly:.1f}" text-anchor="{anchor}" class="lab">{html.escape(lab)}</text>')
    svg = (f'<svg viewBox="0 0 {total_w} {height:.0f}" role="img">'
           f'<image href="data:image/png;base64,{b64}" x="{col}" y="0" width="{IMG_W}" height="{ih:.1f}"/>'
           + "".join(parts) + "</svg>")
    return svg, unplaced


def _marked_tile(f: dict, markers: dict, alt: str) -> str:
    """A question picture with numbered markers (1, 2, 3 …) on verified points, for questions that name 'part 1'."""
    png = f["png"]
    w, h = _png_size(png)
    pts = f.get("markers_at") or {}
    r = max(w, h) * 0.028
    dots = []
    for num, name in sorted(markers.items(), key=lambda kv: str(kv[0])):
        pt = point_for(name, pts)
        if not pt:
            continue
        x, y = pt
        dots.append(f'<circle cx="{x:.0f}" cy="{y:.0f}" r="{r:.0f}" fill="#fff" stroke="#1d4a38" stroke-width="{r * 0.16:.1f}"/>'
                    f'<text x="{x:.0f}" y="{y + r * 0.36:.0f}" text-anchor="middle" font-family="Segoe UI, Arial" '
                    f'font-weight="700" font-size="{r * 1.1:.0f}" fill="#1d4a38">{html.escape(str(num))}</text>')
    b64 = base64.b64encode(png).decode()
    return (f'<svg viewBox="0 0 {w:.0f} {h:.0f}" role="img" aria-label="{alt}">'
            f'<image href="data:image/png;base64,{b64}" x="0" y="0" width="{w:.0f}" height="{h:.0f}"/>{"".join(dots)}</svg>')


CSS = """
figure.place { margin: 2pt 0 8pt; break-inside: avoid; background: #fff; }
.box, .watch, .ex { display: flow-root; }   /* a box sits BESIDE a floated figure, never under it */
figure.place svg { width: 100%; height: auto; display: block; }
figure.beside-right { float: right; margin-left: 12pt; clear: right; }
figure.beside-left { float: left; margin-right: 12pt; clear: left; }
figure.beside-right > svg, figure.beside-left > svg { max-height: 72mm; width: auto; max-width: 100%; margin: 0 auto; }  /* a side picture stays a side picture: it must fit beside its text */
figure.w-third { width: 34%; } figure.w-half { width: 46%; } figure.w-two-thirds { width: 62%; }
figure.full, figure.centre { width: 100%; text-align: center; }
figure.centre svg { max-width: 62%; margin: 0 auto; }
figure.labelled svg { max-width: 72%; max-height: 105mm; }
figure.full svg { max-height: 95mm; width: auto; max-width: 85%; margin: 0 auto; }
h2 { clear: both; }
.group svg text.gl { font: 600 12px 'Segoe UI', Arial, sans-serif; fill: var(--ink); }
.group svg text.gs { font: 10.5px 'Segoe UI', Arial, sans-serif; fill: #5b6475; }
.group svg .rel { stroke: var(--brand); stroke-width: 1.6; fill: none; }
.group svg .relh { fill: var(--brand); }
.group svg .zoom { stroke: var(--brand); stroke-width: 2; fill: none; }
table.match { border-collapse: collapse; margin: 4pt 0 0 6pt; width: 96%; }
table.match td { padding: 2pt 8pt 2pt 0; vertical-align: top; }
table.match td.n { width: 1.6em; font-weight: 700; } table.match td.l { width: 30%; }
.sortbox { display: flex; gap: 10pt; margin: 4pt 0; } .sortbox .cat { flex: 1; border: 1pt dashed var(--line);
  border-radius: 5pt; padding: 4pt 7pt; min-height: 46pt; font-weight: 600; }
.sortitems { margin: 4pt 0; font-size: 10.5pt; }
.photos.tiles figure svg, .photos.tiles figure img { height: 150px; width: 100%; object-fit: contain; }  /* whole photo, never cropped */
.photos .ctx { display: block; font-size: 9pt; color: var(--ink); margin-top: 2pt; }
.think-answers { font-size: 10pt; } .think-answers li { margin-bottom: 4pt; }
.credits a { color: inherit; }
body { font-family: var(--bfont); color: var(--ink); font-size: 11pt; line-height: 1.5; margin: 0; }
.chapter-head { position: relative; background: var(--soft); border-radius: 10pt; padding: 16pt 18pt 12pt; margin-bottom: 14pt; overflow: hidden; border-bottom: 4px solid var(--brand); }
.chapter-head svg.motif { position: absolute; inset: 0; width: 100%; height: 100%; }
.chapter-head > * { position: relative; }
.chapter-head .num { color: var(--brand); font-weight: 700; letter-spacing: .08em; text-transform: uppercase; font-size: 10pt; }
h1 { font-family: var(--hfont); color: var(--brand-dark); font-size: 25pt; margin: 2pt 0 6pt; line-height: 1.15; }
.meta { color: var(--muted); font-size: 9.5pt; }
.opening { font-size: 12pt; font-style: italic; margin: 10pt 0 14pt; }
h2 { font-family: var(--hfont); font-size: 15pt; color: var(--brand); border-bottom: 1.5pt solid var(--line); padding-bottom: 2pt; margin: 18pt 0 6pt; break-after: avoid; }
h3 { font-size: 12.5pt; margin: 14pt 0 4pt; break-after: avoid; }
.box { border-radius: 6pt; padding: 7pt 10pt; margin: 8pt 0; break-inside: avoid; }
.key_term { background: var(--soft); border-left: 4pt solid var(--brand); }
.key_idea { background: var(--soft); font-weight: 600; }
.misconception { background: var(--warn); border-left: 4pt solid var(--warn-edge); }
.misconception .wrong { text-decoration: line-through; color: #9a3412; }
.think { border: 1.5pt dashed var(--think); }
.worked { border: 1pt solid var(--line); }
figure { margin: 10pt 0; text-align: center; break-inside: avoid; }
figure svg { max-width: 100%; height: auto; }
.leader { stroke: #334155; stroke-width: .8; } .dot { fill: #334155; } .lab { font-size: 15px; font-family: 'Segoe UI', Arial, sans-serif; fill: #111827; }
figcaption { font-size: 9.5pt; color: var(--muted); margin-top: 3pt; }
.missing-labels { font-size: 8.5pt; color: #b45309; }
.watch { background: var(--soft); border-left: 4pt solid var(--accent); padding: 7pt 10pt; border-radius: 6pt; font-size: 10pt; margin-top: 12pt; }
.ex { margin: 6pt 0 8pt; break-inside: avoid; }
.key_idea { border-left: 4pt solid var(--accent); }
.ex .tag { font-size: 8.5pt; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; }
.ex ol.opts { list-style: upper-alpha; margin: 3pt 0 0 18pt; padding: 0; }
.answers { break-before: page; }
.answers .ex { font-size: 10pt; }
.marks { color: var(--muted); font-size: 9pt; }
.gap { background: #fde2e2; padding: 6pt 9pt; border-radius: 6pt; font-size: 9.5pt; margin: 8pt 0; }
.extension { border: 1pt solid var(--ext-edge); background: var(--ext); font-size: 10pt; }
.tag { font-size: 8.5pt; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; }
.credit { display: block; font-size: 8pt; color: #8a94a6; margin-top: 2pt; }
.match { display: flex; gap: 24pt; margin: 4pt 0 0 6pt; }
.match ol.ma { list-style: decimal; } .match ol.mb { list-style: upper-alpha; }
.photos { display: flex; gap: 10pt; justify-content: center; margin: 6pt 0; break-inside: avoid; }
.photos figure { margin: 0; width: 31%; } .photos img { width: 100%; border-radius: 4pt; }
.photos figure.marked { width: 62%; } .photos.tiles figure.marked svg { height: auto; max-height: 300px; }
.photos .pl { font-weight: 700; }
.credits { font-size: 8.5pt; color: var(--muted); margin-top: 14pt; border-top: 1pt solid var(--line); padding-top: 6pt; }
"""


def _credit(f: dict) -> str:
    c = f.get("credit")
    if not c:
        return ""
    who = f" {html.escape(c['author'])}," if c.get("author") else ""
    return f'<span class="credit">Photo:{who} {html.escape(c["licence"])}, {html.escape(str(c.get("provider")))}</span>'


def _scramble(xid: str, n: int) -> list[int]:
    """A fixed, reproducible shuffle of the right-hand column (never the
    identity, so no pair prints beside its own answer)."""
    import hashlib
    base = sorted(range(n), key=lambda i: hashlib.sha1(f"{xid}:{i}".encode()).hexdigest())
    for k in range(n):
        order = base[k:] + base[:k]
        if all(order[i] != i for i in range(n)):
            return order
    return list(range(1, n)) + [0] if n > 1 else list(range(n))


def _match(x: dict) -> tuple[str, str]:
    """(question html, answer key) for a match task: column B scrambled."""
    pairs = x.get("pairs") or []
    order = _scramble(x.get("xid", ""), len(pairs))
    letters = "ABCDEFGH"
    rows = "".join(f'<tr><td class="n">{i + 1}.</td><td class="l">{md(pairs[i]["left"])}</td>'
                   f'<td class="n">{letters[k]}.</td><td>{md(pairs[order[k]]["right"])}</td></tr>'
                   for i, k in zip(range(len(pairs)), range(len(pairs))))
    key = ", ".join(f"{i + 1}–{letters[order.index(i)]}" for i in range(len(pairs)))
    return (f'<table class="match">{rows}</table>', key)


def _place(b: dict) -> str:
    """CSS classes from the author's layout intent (D5). The intent comes from
    the content; the renderer only enforces sane widths."""
    lay = b.get("layout") or {}
    if isinstance(lay, str):               # a bare placement ("beside-right") is a layout too
        lay = {"placement": lay}
    synonyms = {"full-width": "full", "fullwidth": "full", "wide": "full", "center": "centre",
                "right": "beside-right", "left": "beside-left", "beside": "beside-right"}
    lay = {**lay, "placement": synonyms.get(str(lay.get("placement") or "").lower(), lay.get("placement"))}
    p = lay.get("placement") if lay.get("placement") in ("beside-right", "beside-left", "full", "centre") else "centre"
    w = {"third": "w-third", "half": "w-half", "two-thirds": "w-two-thirds"}.get(lay.get("width"), "w-half")
    return f"place {p}" + (f" {w}" if p.startswith("beside") else "")


def _group(b: dict, figs: dict) -> str:
    """Several related pictures composed into ONE visual (D5): sources on the
    top row, the things they relate to beneath them, joined by teal arrows; a
    'zoom' target sits in a magnifier circle. Labels are vector text, outside
    the pictures and editable."""
    items = [i for i in b.get("items") or [] if figs.get(i.get("ref"))]
    if len(items) < 2:
        return ""
    rel = [r for r in b.get("relations") or [] if 0 <= r.get("from", -1) < len(items) and 0 <= r.get("to", -1) < len(items)]
    targets = {r["to"] for r in rel}
    top = [i for i in range(len(items)) if i not in targets]
    below = {t: [r["to"] for r in rel if r["from"] == t] for t in top}
    cols = max(1, len(top))
    cw, ih, gap, circ = 200, 150, 70, 120
    two_rows = bool(rel)
    H = 30 + ih + 50 + ((gap + circ + 56) if two_rows else 0)
    W = cols * cw
    out = [f'<svg viewBox="0 0 {W} {H}" role="img">',
           '<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
           'orient="auto-start-reverse"><path class="relh" d="M0,0 L10,5 L0,10 z"/></marker></defs>']

    def pic(i, cx, cy, w, h, clip=None):
        f = figs[items[i]["ref"]]
        cid = f"c{abs(hash((items[i]['ref'], cx, cy))) % 10**8}"
        clip_el = (f'<clipPath id="{cid}"><circle cx="{cx}" cy="{cy}" r="{clip}"/></clipPath>' if clip else "")
        if f.get("kind") == "photo" and not clip:
            # a print-style mount for a photograph: white border, hairline edge
            clip_el += (f'<rect x="{cx - w / 2 - 5:.0f}" y="{cy - h / 2 - 5:.0f}" width="{w + 10}" height="{h + 10}" '
                        f'fill="#fff" stroke="#cfd6cc" stroke-width="0.8" rx="2"/>')
        return (clip_el + f'<image href="{f["img"]}" x="{cx - w / 2:.0f}" y="{cy - h / 2:.0f}" width="{w}" height="{h}" '
                f'preserveAspectRatio="xMidYMid meet"' + (f' clip-path="url(#{cid})"' if clip else "") + "/>")

    def lines(text, cls, x, y, width_chars):
        """Centred label text wrapped INSIDE its own column: labels can never
        run into the next column (ch 1: 'onion bCheek cells')."""
        words, rows, cur = str(text or "").split(), [], ""
        for w_ in words:
            if cur and len(cur) + 1 + len(w_) > width_chars:
                rows.append(cur)
                cur = w_
            else:
                cur = f"{cur} {w_}".strip()
        if cur:
            rows.append(cur)
        return "".join(f'<text class="{cls}" x="{x}" y="{y + 14 * k}" text-anchor="middle">{html.escape(r_)}</text>'
                       for k, r_ in enumerate(rows[:3]))

    label_chars = max(10, int(cw / 7.2))
    for c, i in enumerate(top):
        cx, cy = c * cw + cw / 2, 30 + ih / 2
        out.append(pic(i, cx, cy, cw - 30, ih))
        out.append(lines(items[i].get("label"), "gl", cx, 30 + ih + 20, label_chars))
        for k, t in enumerate(below.get(i, [])[:1]):
            ty = 30 + ih + 34 + gap + circ / 2
            r = next(x for x in rel if x["from"] == i and x["to"] == t)
            zoom = r.get("kind") == "zoom"
            out.append(f'<path class="rel" d="M{cx},{30 + ih + 30} L{cx},{ty - circ / 2 - 4}" marker-end="url(#ah)"/>')
            if r.get("label"):
                out.append(f'<rect x="{cx - cw / 2 + 12}" y="{30 + ih + 34 + gap / 2 - 13}" width="{cw - 24}" '
                           f'height="{18 if len(r["label"]) <= label_chars + 6 else 32}" style="fill:var(--paper,#fff)"/>')
                out.append(lines(r["label"], "gs", cx, 30 + ih + 34 + gap / 2, label_chars + 6))
            out.append(pic(t, cx, ty, circ, circ, clip=circ / 2 - 2 if zoom else None))
            if zoom:
                out.append(f'<circle class="zoom" cx="{cx}" cy="{ty}" r="{circ / 2}"/>')
            out.append(lines(items[t].get("label"), "gl", cx, ty + circ / 2 + 20, label_chars))
    out.append("</svg>")
    credits = "".join(_credit(figs[i["ref"]]) for i in items)
    # a group without relations is a row of pictures: it takes the width its
    # pictures need (≈30% each), never stretched to the full measure (ch 1:
    # two photos blown up to full width pushed a page break and left half a page empty)
    cap = "" if rel else f' style="max-width:{min(100, 32 * len(items))}%;margin-left:auto;margin-right:auto"'
    num = f'<span class="fignum">{b["_fig"]}</span> ' if b.get("_fig") else ""
    photos = all(figs[i["ref"]].get("kind") == "photo" for i in items)
    return (f'<figure class="{_place(b)} group{" photo" if photos else ""}"{cap}>{"".join(out)}<figcaption>{num}'
            f'{md(b.get("caption"))}{credits}</figcaption></figure>')


def _block(b: dict, figs: dict) -> str:
    t = b.get("type")
    if t == "paragraph":
        return f"<p>{md(b.get('text'))}</p>"
    if t == "key_term":
        return f'<div class="box key_term"><strong>{md(b.get("term"))}</strong> — {md(b.get("text"))}</div>'
    if t == "key_idea":
        return f'<div class="box key_idea">{md(b.get("text"))}</div>'
    if t == "misconception":
        return (f'<div class="box misconception"><div class="tag">Common mistake</div>'
                f'<div class="wrong">{md(b.get("wrong"))}</div><div>{md(b.get("right"))}</div></div>')
    if t == "worked_example":
        steps = "".join(f"<li>{md(s)}</li>" for s in b.get("steps") or [])
        return (f'<div class="box worked"><strong>Worked example.</strong> {md(b.get("problem"))}<ol>{steps}</ol>'
                f'<strong>Answer:</strong> {md(b.get("answer"))}</div>')
    if t == "think":
        return f'<div class="box think"><strong>Think about it.</strong> {md(b.get("text"))}</div>'
    if t == "figure_group":
        return _group(b, figs)
    if t == "list":
        return "<ul>" + "".join(f"<li>{md(i)}</li>" for i in b.get("items") or []) + "</ul>"
    if t == "table":
        head = "".join(f"<th>{md(h)}</th>" for h in b.get("headers") or b.get("columns") or b.get("header") or [])
        rows = "".join("<tr>" + "".join(f"<td>{md(c)}</td>" for c in r) + "</tr>" for r in b.get("rows") or [])
        cap = f"<figcaption>{md(b.get('caption'))}</figcaption>" if b.get("caption") else ""
        return f'<figure class="tbl"><table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>{cap}</figure>'
    if t == "extension":
        return (f'<div class="box extension"><div class="tag">Extension — beyond the curriculum objective, '
                f'not assessed</div>{md(b.get("text"))}</div>')
    if t == "figure":
        f = figs.get(b.get("ref"))
        if not f:
            return f'<div class="gap">Figure {html.escape(str(b.get("ref")))} is missing.</div>'
        miss = (f'<div class="missing-labels">Labels with no detected region (not placed): '
                f'{html.escape(", ".join(f["unplaced"]))}</div>' if f["unplaced"] else "")
        num = f'<span class="fignum">{b["_fig"]}</span> ' if b.get("_fig") else ""
        # a labelled diagram is never squeezed beside the text: its labels must print at a readable size
        cls = "place centre labelled" if 'class="lab"' in f["svg"] else _place(b)
        return (f'<figure class="{cls} {f.get("kind", "illustration")}">{f["svg"]}<figcaption>{num}'
                f'{md(b.get("caption") or f["caption"])}{_credit(f)}</figcaption>{miss}</figure>')
    return f"<p>{md(json.dumps(b))}</p>"


def _figures(doc: dict, dossier: dict, env_file: Path, annotations: dict | None = None) -> dict:
    """Every fig:/asset: id the draft places, as a labelled SVG. A pinned
    article figure carries its reviewed labels. A library asset's part names
    are internal ("prokaryotic cell region"), so it is labelled only with the
    parts the surrounding text actually talks about."""
    want, context = set(), {}
    for s in doc["sections"]:
        prose = " ".join(str(v) for b in s.get("blocks") or [] for k, v in b.items()
                         if k not in ("type", "source_refs", "ref")).lower()
        for b in s.get("blocks") or []:
            refs = ([b.get("ref")] if b.get("type") == "figure" else
                    [i.get("ref") for i in b.get("items") or []] if b.get("type") == "figure_group" else [])
            for r in refs:
                want.add(r)
                context[r] = context.get(r, "") + " " + prose
    for x in doc["exercises"]:
        for r in [x.get("figure_ref")] + list(x.get("figure_refs") or []):
            if r:
                want.add(r)
                context[r] = context.get(r, "") + " " + str(x.get("stem")).lower()
    src = {}
    for t in dossier["topics"]:
        for f in t["figures"]:
            src[f["id"]] = (f["_asset"], [l["label"] for l in f["_labels"]], f["caption"], None)
    for a in dossier["library_assets"]:
        talk = context.get(a["id"], "")
        if a.get("identity") and a.get("credit"):
            a["credit"] = {**a["credit"], "title": a["identity"].get("source_title")}
        # the asset's description is a generation brief ("…Name the layer groups
        # exactly: …"); only its first sentence describes the picture
        first = re.split(r"(?<=[.!?])\s", re.sub(r"^(SOURCE|UNVERIFIED identity):\s*", "",
                                                  (a["description"] or "").strip()), maxsplit=1)[0]
        parts = [] if a.get("kind") == "photo" else [p for p in a["parts"] if _norm(p) and _norm(p) in talk]
        src[a["id"]] = (a["_asset"], parts, first, a.get("credit"))
    kinds = {a["id"]: ("photo" if a.get("kind") == "photo" else "illustration") for a in dossier["library_assets"]}
    url, key = load_env(env_file)
    out = {}
    with client(url, key) as http:
        for ref in want:
            if ref not in src or not src[ref][0]:
                continue
            asset, labels, caption, credit = src[ref]
            png = asset_bytes(http, url, asset)
            kind = kinds.get(ref, "illustration")
            # illustrations are matted onto the paper; a photograph is never altered
            vision = asset.get("vision") or {"w": 1, "h": 1}
            if kind == "illustration":
                shown, box = _trim(_feather(matte(png) or png), with_box=True)
                vision = _crop_vision(vision, png, box)      # label regions move with the crop
            else:
                shown = png
            labels = dedupe_labels(labels)
            ann = (annotations or {}).get(ref) or {}
            svg, unplaced = figure_svg(shown, vision, labels, anchors=ann.get("anchors"))
            out[ref] = {"svg": svg, "unplaced": unplaced, "caption": caption, "credit": credit, "kind": kind,
                        "img": "data:image/png;base64," + base64.b64encode(shown).decode(),
                        "png": shown, "labels": labels, "markers_at": ann.get("anchors") or {}}
    return out


def dedupe_labels(labels: list[str]) -> list[str]:
    """One label per structure: 'green chloroplast' and 'chloroplast' name the same thing — keep the plain name."""
    out = []
    for lab in labels:
        n = _norm(lab)
        if any(_norm(o) != n and _norm(o) in n.split() + [n] or (_norm(o) != n and n.endswith(" " + _norm(o)))
               for o in labels):
            continue
        if n not in [_norm(o) for o in out]:
            out.append(lab)
    return out


def annotations_for(slug: str) -> dict:
    """The book's verified label anchors (publishing/tools/label_anchors.py): ref → {"anchors": {label: [x, y]},
    "size": [w, h]}, in the pixels of the picture as printed (after matting and cropping)."""
    from publishing.report import OUT
    f = OUT / "manuscripts" / slug / "annotations.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


def hero_image(slug: str, chapter: int) -> str | None:
    """The chapter opener's artwork: the local book asset `book:hero_chNN`
    (made through the same publisher-grade style system), matted. None when the
    book has none — the opener then stands on typography alone. Without the local
    database (the books runner) the same file is found by its name, hero_chNN.png."""
    path = None
    try:
        from publishing import localdb
        conn = localdb.connect()
        row = conn.execute("select path from pub.book_assets where key=%s", (f"book:hero_ch{chapter:02d}",)).fetchone()
        path = local_asset(row["path"]) if row else None
    except Exception:  # noqa: BLE001 — the opener must render without a database
        path = assets_dir() / f"hero_ch{chapter:02d}.png"
    if not path or not path.exists():
        return None
    png = path.read_bytes()
    return "data:image/png;base64," + base64.b64encode(_vignette(matte(png) or png)).decode()


def _crop_vision(vision: dict, png: bytes, box: tuple | None) -> dict:
    """Shift detected label regions into a cropped picture's coordinates."""
    if not box or not vision.get("regions"):
        return vision
    from PIL import Image
    import io
    pw, ph = Image.open(io.BytesIO(png)).size
    vw, vh = float(vision.get("w") or pw), float(vision.get("h") or ph)
    fx, fy = vw / pw, vh / ph                       # picture pixels -> the regions' own units
    x0, y0, x1, y1 = box[0] * fx, box[1] * fy, box[2] * fx, box[3] * fy
    regions = {k: [[b[0] - x0, b[1] - y0, b[2] - x0, b[3] - y0] for b in v if len(b) == 4]
               for k, v in vision["regions"].items()}
    return {**vision, "w": x1 - x0, "h": y1 - y0, "regions": regions}


def _trim(png: bytes, pad: float = 0.03, with_box: bool = False):
    """Crop a matted illustration to what is drawn (plus a little air): empty
    transparent canvas around a subject is page space the layout cannot use."""
    import io
    import numpy as np
    from PIL import Image
    im = Image.open(io.BytesIO(png))
    if im.mode != "RGBA":
        return (png, None) if with_box else png
    a = np.asarray(im)[..., 3]
    ys, xs = np.where(a > 12)
    if not len(xs):
        return (png, None) if with_box else png
    h, w = a.shape
    px, py = int(w * pad), int(h * pad)
    box = (max(0, xs.min() - px), max(0, ys.min() - py), min(w, xs.max() + px + 1), min(h, ys.max() + py + 1))
    if (box[2] - box[0]) * (box[3] - box[1]) > 0.92 * w * h:
        return (png, None) if with_box else png
    out = io.BytesIO()
    im.crop(box).save(out, "PNG")
    return (out.getvalue(), box) if with_box else out.getvalue()


def _feather(png: bytes, margin: float = 0.07) -> bytes:
    """An illustration whose edge is still opaque after matting (a soft floor
    shadow, a studio gradient) fades out over its outer margin instead of
    showing a pale rectangle on the paper. A cleanly matted picture is untouched."""
    import io
    import numpy as np
    from PIL import Image
    im = Image.open(io.BytesIO(png)).convert("RGBA")
    a = np.asarray(im).copy()
    h, w = a.shape[:2]
    edge = np.concatenate([a[0, :, 3], a[-1, :, 3], a[:, 0, 3], a[:, -1, 3]])
    if edge.mean() < 40:
        return png
    y, x = np.ogrid[:h, :w]
    d = np.minimum(np.minimum(x, w - 1 - x) / (w * margin), np.minimum(y, h - 1 - y) / (h * margin))
    a[..., 3] = (a[..., 3] * np.clip(d, 0, 1)).astype(np.uint8)
    out = io.BytesIO()
    Image.fromarray(a).save(out, "PNG")
    return out.getvalue()


def _vignette(png: bytes, start: float = 0.62) -> bytes:
    """Fade a picture's edges to transparent in its own alpha, so the hero
    melts into the opener band. Baked into the pixels: a CSS mask would print
    as a 72 dpi raster."""
    import io
    import numpy as np
    from PIL import Image
    im = Image.open(io.BytesIO(png)).convert("RGBA")
    a = np.asarray(im).copy()
    h, w = a.shape[:2]
    y, x = np.ogrid[:h, :w]
    r = np.sqrt(((x - w / 2) / (w / 2)) ** 2 + ((y - h / 2) / (h / 2)) ** 2)
    fade = np.clip((1.0 - r) / (1.0 - start), 0, 1)
    a[..., 3] = (a[..., 3] * fade).astype(np.uint8)
    out = io.BytesIO()
    Image.fromarray(a).save(out, "PNG")
    return out.getvalue()


TIER_ORDER = ("guided", "independent", "challenge")


def question_order(doc: dict) -> list[dict]:
    """The questions in their printed sequence: by tier (Getting started, On your own, Challenge), then in the
    order the chapter lists them within a tier."""
    xs = doc.get("exercises") or []
    rank = {t: i for i, t in enumerate(TIER_ORDER)}
    return sorted(xs, key=lambda x: (rank.get(x.get("tier"), len(TIER_ORDER)), xs.index(x)))


def question_numbers(doc: dict) -> dict[str, str]:
    """Stable internal id (x7) → the number printed (X3). Numbers are given AFTER ordering, so the practice pages,
    the answers, the coverage table and the reports all use one sequence (founder 2026-10-26, Revision 3)."""
    return {x["xid"]: f"X{i}" for i, x in enumerate(question_order(doc), 1)}


def renumber_text(text, numbers: dict[str, str]):
    """Rewrite cross-references ('see X7') inside a question's text to the printed numbers."""
    if not isinstance(text, str) or not numbers:
        return text
    return re.sub(r"\bX(\d+)\b", lambda m: numbers.get(f"x{m.group(1)}", m.group(0)), text)


PICTURE_WORD = r"(?:photograph|photo|picture|drawing|diagram|image|figure|sample)s?"


DESCRIBES_PICTURE = re.compile(r"\b(drawing|diagram|illustration|depict|depicts|shown as|placed (left|side)|"
                               r"zooms?|not a photograph|composition|in (the )?(left|middle|right) part)\b", re.I)


def _photo_row(refs: list[str], figs: dict, context: dict | None = None, stem: str = "",
               markers: dict | None = None) -> str:
    """Uniform tiles, each with the letter the QUESTION uses for it, and — only when it is short — what the
    question says about it. A long description is the picture's alt text, never printed under it (founder
    2026-10-26: internal descriptions were printed as captions)."""
    context = context or {}
    shown = [r for r in refs if figs.get(r)]
    keys = sorted(k for k in context if isinstance(k, str) and len(k) == 1 and k.isalpha())
    named = sorted(set(re.findall(PICTURE_WORD + r"\s+([A-H])\b", stem or "", flags=re.I)))
    if len(keys) == len(shown) and keys:
        letters = keys                                  # the question's own letters
    elif len(named) == len(shown) and named:
        letters = named
    else:
        letters = list("ABCDEFGH"[:len(shown)])
    tiles = []
    for letter, r in zip(letters, shown):
        f = figs[r]
        ctx = context.get(letter) or context.get(r)
        # a specimen note (where the sample came from) is information the question needs, so it is printed; a
        # description of what a drawing depicts gives the answer away and stays alt text (founder 2026-10-27)
        short = ctx if ctx and len(str(ctx).split()) <= 30 and not DESCRIBES_PICTURE.search(str(ctx)) else ""
        alt = html.escape(str(ctx or ""), quote=True)
        pic = (_marked_tile(f, markers, alt) if markers and f.get("png") else f'<img src="{f["img"]}" alt="{alt}">')
        marked = ' class="marked"' if markers and f.get("png") else ""   # numbers must be readable at print size
        tiles.append(f'<figure{marked}>{pic}<figcaption><span class="pl">{letter}</span>'
                     + (f'<span class="ctx">{md(short)}</span>' if short else "") + f'{_credit(f)}</figcaption></figure>')
    return f'<div class="photos tiles">{"".join(tiles)}</div>' if tiles else ""


LICENCE_URL = {"cc0": "https://creativecommons.org/publicdomain/zero/1.0/",
               "public domain": "https://creativecommons.org/publicdomain/mark/1.0/"}


def licence_url(lic: str) -> str:
    m = re.match(r"cc (by(?:-sa)?)\s*(\d\.\d)?", (lic or "").lower())
    if m:
        return f"https://creativecommons.org/licenses/{m.group(1)}/{m.group(2) or '4.0'}/"
    return LICENCE_URL.get((lic or "").lower(), "")


def credit_line(c: dict) -> str:
    """Title, Author, Source, Licence (+ link), and the change we made (TASL)."""
    title = f'“{html.escape(c["title"])}”' if c.get("title") else "Untitled"
    who = html.escape(c.get("author") or "unknown author")
    src = f'<a href="{html.escape(c.get("url") or "")}">{html.escape(str(c.get("provider") or "source"))}</a>'
    lic = html.escape(c.get("licence") or "")
    lu = licence_url(c.get("licence") or "")
    lic = f'<a href="{lu}">{lic}</a>' if lu else lic
    addresses = "; ".join(x for x in (f"source: {html.escape(c['url'])}" if c.get("url") else "",
                                       f"licence: {lu}" if lu else "") if x)
    return f"{title} by {who}, {src}, {lic}. Resized for print." + (f" <span class='addr'>({addresses})</span>" if addresses else "")


def _sort(x: dict) -> tuple[str, str]:
    import hashlib
    items = sorted(x.get("items") or [], key=lambda i: hashlib.sha1(f"{x.get('xid')}:{i.get('text')}".encode()).hexdigest())
    cats = "".join(f'<div class="cat">{md(c)}</div>' for c in x.get("categories") or [])
    q = (f'<div class="sortitems">{" · ".join(md(i.get("text")) for i in items)}</div>'
         f'<div class="sortbox">{cats}</div>')
    key = "; ".join(f'{md(c)}: ' + ", ".join(md(i["text"]) for i in x.get("items") or [] if i.get("category") == c)
                    for c in x.get("categories") or [])
    return q, key


BOOK_CSS = """
/* ── editorial system: every colour and face comes from the theme's tokens ── */
html { print-color-adjust: exact; -webkit-print-color-adjust: exact; }
body { color: var(--ink); font-family: var(--bfont); }
h1, h2, h3, .op-numeral, .op-title { font-family: var(--hfont); }
p { margin: 0 0 6.5pt; orphans: 3; widows: 3; }
strong { color: var(--brand-dark); }

/* chapter opener — its own page */
.opener { break-after: page; position: relative; min-height: 238mm; }
.op-head { display: grid; grid-template-columns: 1fr 46%; gap: 4mm; align-items: center; height: 96mm; }
.op-kicker { font: 600 8pt var(--bfont); letter-spacing: .22em; text-transform: uppercase; color: var(--brand); }
.op-numeral { display: block; width: 42mm; height: 27mm; margin: 8mm 0 6mm; overflow: visible; }
.op-title { font-size: 30pt; line-height: 1.18; font-variant-ligatures: none; color: var(--brand-dark); margin: 0 0 3mm; font-weight: 600; }
.op-meta { font-size: 8.6pt; color: var(--muted); letter-spacing: .02em; }
.op-meta .chip { display: inline-block; border: .8pt solid var(--line); border-radius: 10pt; padding: 0 6pt; margin: 0 4pt 3pt 0;
  background: rgba(255,255,255,.55); }
.op-hero { width: 100%; height: auto; display: block; }
.op-intro { font: italic 12.2pt/1.55 var(--hfont); color: var(--ink); margin: 14mm 0 7mm; padding-left: 4mm;
  border-left: 2.2pt solid var(--accent); }
.op-cards { display: grid; grid-template-columns: 1fr 1fr; gap: 5mm; margin-bottom: 5mm; }
.op-card { background: rgba(255,255,255,.6); border: .8pt solid var(--line); border-radius: 5pt; padding: 4mm 5mm; font-size: 9.6pt; }
.op-card h4 { margin: 0 0 2mm; font: 600 8pt var(--bfont); letter-spacing: .16em; text-transform: uppercase; color: var(--brand); }
.op-card ol { margin: 0; padding: 0; list-style: none; } .op-card li { margin: 0 0 1.4mm; display: grid; grid-template-columns: 9mm 1fr; }
.op-card li .sn { display: block; color: var(--brand); font-weight: 600; }
.op-card .code { font-weight: 700; color: var(--brand-dark); margin-right: 3pt; }
.op-words { margin-top: 5mm; }
.opener.dense .op-head { height: 88mm; } .opener.dense .op-intro { margin: 14mm 0 4mm; font-size: 11pt; }
.opener.dense .op-card { font-size: 8.4pt; padding: 3mm 4mm; } .opener.dense .op-card li { margin-bottom: .6mm; }
.opener.dense .op-card p { margin: 0 0 1.6mm; } .opener.dense .op-cards { margin-bottom: 3mm; }
.opener.dense .watch { font-size: 8.4pt; padding: 2mm 4mm; } .opener.dense .op-words span { font-size: 8.4pt; }
.opener.dense .op-title { font-size: 22pt; }
.opener.dense .op-cards { grid-template-columns: 1fr; gap: 3mm; }
.opener.dense .op-card { columns: 2; column-gap: 7mm; } .opener.dense .op-card h4 { column-span: all; }
.opener.dense .op-card p, .opener.dense .op-card li { break-inside: avoid; } .op-words h4 { margin: 0 0 2mm; font: 600 8pt var(--bfont); letter-spacing: .16em; text-transform: uppercase; color: var(--brand); }
.op-words span { display: inline-block; margin: 0 2mm 2mm 0; padding: .6mm 3mm; border-radius: 10pt; background: var(--soft); color: var(--brand-dark); font-size: 9.4pt; }
a { color: var(--brand); text-decoration-color: var(--line); }

/* sections */
h2 { font-size: 17pt; font-weight: 600; color: var(--brand-dark); margin: 9mm 0 3mm; padding: 0; border: none;
  break-after: avoid; display: flex; align-items: baseline; gap: 3mm; clear: both; }
h2 .sn { font: 600 10pt var(--bfont); color: #fff; background: var(--brand); border-radius: 3pt; padding: 1pt 5pt; position: relative; top: -2pt; }
h2:after { content: ""; flex: 1; border-bottom: .6pt solid var(--line); margin-left: 2mm; position: relative; top: -3pt; }
h3 { font: 600 9pt var(--bfont); letter-spacing: .18em; text-transform: uppercase; color: var(--brand); margin: 6mm 0 2mm; }

/* callouts */
.box { border-radius: 4pt; padding: 3mm 4.5mm; margin: 3.5mm 0; break-inside: avoid; }
.key_term { background: var(--soft); border-left: 2.6pt solid var(--brand); }
.key_term strong { font-variant: small-caps; letter-spacing: .03em; font-size: 1.05em; }
.key_idea { background: rgba(255,255,255,.55); border: .8pt solid var(--line); border-left: 2.6pt solid var(--accent); font-weight: 600; }
.key_idea:before { content: "Key idea"; display: block; font: 600 7.4pt var(--bfont); letter-spacing: .18em; text-transform: uppercase; color: var(--accent); margin-bottom: 1mm; }
.misconception { background: var(--warn); border-left: 2.6pt solid var(--warn-edge); }
.misconception .tag { color: var(--warn-edge); font-weight: 600; letter-spacing: .16em; }
.misconception .wrong { text-decoration: line-through; text-decoration-color: var(--warn-edge); color: #7a5536; }
.think { border: 1pt dashed var(--think); background: rgba(255,255,255,.35); padding-left: 12mm; position: relative; }
.think:before { content: "?"; position: absolute; left: 3.5mm; top: 3mm; width: 6mm; height: 6mm; border-radius: 50%;
  background: var(--think); color: #fff; font: 700 9pt/6mm var(--hfont); text-align: center; }
.think strong:first-child { color: var(--think); }
.extension { background: var(--ext); border: .8pt solid var(--ext-edge); font-size: 9.6pt; }
.extension .tag { color: #5d7280; }
.watch { background: rgba(255,255,255,.6); border: .8pt solid var(--line); border-left: 2.6pt solid var(--think); border-radius: 4pt;
  padding: 2.5mm 4.5mm; font-size: 9.4pt; }

/* figures */
figure.place { background: transparent; }
figcaption { font: 8.6pt/1.4 var(--bfont); color: var(--muted); text-align: left; margin-top: 2mm; }
figcaption .fignum { font-weight: 700; color: var(--brand-dark); letter-spacing: .03em; margin-right: 2pt; }
figure.photo > svg { background: #fff; padding: 1.4mm; border: .6pt solid #cfd6cc; border-radius: 2pt;
  box-shadow: 0 .8pt 2.6pt rgba(40,50,40,.18); box-sizing: border-box; }
figure.group.photo > svg { background: transparent; padding: 0; border: none; box-shadow: none; }
.credit { font-size: 7.2pt; color: #8b958e; }

/* reviewer-only notes (e.g. labels the figure engine could not place) belong in the Gate D packet, never on the page */
.missing-labels, .gap { display: none; }

/* comparison tables: header in the brand wash, first column as row labels */
figure.tbl { margin: 4mm 0; break-inside: avoid; }
figure.tbl table { width: 100%; border-collapse: collapse; font-size: 9.4pt; line-height: 1.4; }
figure.tbl th { text-align: left !important; background: var(--soft); color: var(--brand-dark); font-weight: 600; text-align: left; padding: 2mm 3mm;
  border-bottom: 1pt solid var(--brand); }
figure.tbl td { text-align: left; padding: 1.8mm 3mm; border-bottom: .5pt solid var(--line); vertical-align: top; }
figure.tbl td:first-child { font-weight: 600; color: var(--brand-dark); width: 28%; }

/* practice — its own pages: calm, numbered, room to write */
.practice { break-before: page; }
.practice-head { border-bottom: 1.2pt solid var(--brand); margin-bottom: 4mm; padding-bottom: 2mm; }
.practice-head h2 { margin: 0; } .practice-head h2:after { display: none; }
.practice-head p { margin: 1mm 0 0; color: var(--muted); font-size: 9.4pt; }
.tier { display: flex; align-items: center; gap: 2.5mm; margin: 6mm 0 2.5mm; font: 600 9pt var(--bfont);
  letter-spacing: .14em; text-transform: uppercase; color: var(--brand); }
.tier .dots { letter-spacing: 1pt; color: var(--accent); }
.ex { margin: 0 0 5mm; break-inside: avoid; }
.ex .q { display: grid; grid-template-columns: 9mm 1fr auto; gap: 0 1mm; }
.ex .qn { font: 700 10pt var(--hfont); color: var(--brand); }
.ex .marks { color: var(--muted); font-size: 8.4pt; white-space: nowrap; padding-left: 3mm; }
.ex ol.opts { columns: 2; column-gap: 8mm; list-style: upper-alpha; margin: 1.5mm 0 0 15mm; padding: 0; }
.ex ol.opts li { break-inside: avoid; margin-bottom: 1mm; }
.ex .body { margin-left: 10mm; }
.lines { margin: 2mm 0 0 10mm; }
.lines i { display: block; height: calc(var(--pitch) - .6pt); border-bottom: .6pt solid #c3ccc0; }
.blank { display: inline-block; min-width: 32mm; border-bottom: .8pt solid #9fab9f; }

/* answers — same identity, compact, two columns */
.answers { break-before: page; }
.answers .cols { columns: 2; column-gap: 8mm; column-rule: .5pt solid var(--line); font-size: 9pt; line-height: 1.45; }
.answers .ans { break-inside: avoid; margin: 0 0 3.2mm; }
.answers .ans .an { font-weight: 700; color: var(--brand); }
.answers .ans .key { font-weight: 600; color: var(--brand-dark); }
.answers .ans { color: #33403a; }
.answers .ans ul { margin: 1mm 0 0; padding-left: 4mm; } .answers .ans li { margin: 0; }
.answers h3 { column-span: all; }
.credits { column-span: all; font-size: 7.6pt; color: var(--muted); margin-top: 6mm; border-top: .6pt solid var(--line); padding-top: 2mm; }
.credits ul { margin: 1mm 0 0; padding-left: 4mm; }
"""


def _pretty_curriculum(code: str) -> str:
    """'cambridge_ls_science_0893' → 'Cambridge Lower Secondary Science (0893)'."""
    words = str(code or "").split("_")
    names = {"cambridge": "Cambridge", "ls": "Lower Secondary", "cbse": "CBSE", "igcse": "IGCSE"}
    num = words[-1] if words and words[-1].isdigit() else ""
    body = " ".join(names.get(w, w.capitalize()) for w in (words[:-1] if num else words))
    return f"{body} ({num})" if num else body


KEY_WORDS_MAX = 18


def _key_words(doc: dict) -> str:
    """The chapter's vocabulary for the opener, in reading order: its key-term
    boxes and the words its paragraphs set in bold. Navigation only — nothing
    is written here that the chapter does not already say."""
    import re
    seen, words = set(), []
    for s in doc["sections"]:
        for b in s.get("blocks") or []:
            if b.get("type") == "key_term" and b.get("term"):
                found = [b["term"]]
            elif b.get("type") == "paragraph":
                found = re.findall(r"\*\*([^*]{2,40})\*\*", b.get("text") or "")
            else:
                found = []
            for w in found:
                # emphasis is not vocabulary: adverbs ('typically') stay in the text only
                if w.lower().endswith("ly") or " " not in w and any(w.lower() in x.lower() for x in words):
                    continue
                if w.lower().rstrip("s") not in seen:
                    seen.add(w.lower().rstrip("s"))
                    words.append(w)
    if not words:
        return ""
    # the opener is one page: the chapter's key-term boxes come first, so a long list keeps the defined terms
    terms = [b["term"] for sec in doc["sections"] for b in sec.get("blocks") or [] if b.get("type") == "key_term"]
    words = ([w for w in words if w in terms] + [w for w in words if w not in terms])[:KEY_WORDS_MAX]
    return '<div class="op-words"><h4>Key words</h4>' + "".join(f"<span>{html.escape(w)}</span>" for w in words) + "</div>"


def _lines(x: dict, pitch: float) -> str:
    """Writing space sized to the marks (practice pages keep room to work)."""
    if x.get("item_type") not in ("short_answer", "long_answer"):
        return ""
    marks = int(x.get("marks") or 1)
    n = max(3, marks * 2 + (1 if x.get("item_type") == "long_answer" else 0))
    return f'<div class="lines" style="--pitch:{pitch}mm">' + "<i></i>" * n + "</div>"


def chapter_html(doc: dict, figs: dict, edition: str = "digital", first_page: int = 1) -> str:
    from publishing.manuscript.theme import css_vars, for_work, page, page_css
    ch, w = doc["chapter"], doc["work"]
    theme = for_work(w["slug"])
    pg = page(theme)
    n = ch["number"]
    subject = str(w["title"]).split(":")[0].strip()
    # numbering: sections n.1 …, figures n.1 … in reading order
    fig_no = 0
    for s in doc["sections"]:
        for b in s.get("blocks") or []:
            if b.get("type") in ("figure", "figure_group"):
                fig_no += 1
                b["_fig"] = f"Figure {n}.{fig_no}"
    body = []
    # ── opener ──────────────────────────────────────────────────────────────
    hero = hero_image(w["slug"], n) if pg["opener"].get("hero") else None
    objectives = "".join(f'<p><span class="code">{html.escape(o["code"])}</span>{md(o["text"])}</p>'
                         for o in ch.get("objectives") or [])
    if doc.get("tws"):                         # Thinking and Working Scientifically, mapped independently (Rev 2)
        objectives += ('<p class="tws"><span class="code">Thinking and Working Scientifically:</span> '
                       + ", ".join(html.escape(c) for c in doc["tws"]) + "</p>")
    if len(ch.get("objectives") or []) > 4:   # a review chapter revisits the book's objectives: show their codes
        objectives = '<p class="codes">' + " ".join(f'<span class="code">{html.escape(o["code"])}</span>'
                                                     for o in ch["objectives"]) + "</p>"
    contents = "".join(f'<li><span class="sn">{n}.{i}</span>{md(s["heading"])}</li>'
                       for i, s in enumerate(doc["sections"], 1))
    video = ""
    if doc.get("videos"):
        links = " · ".join(f'<a href="https://www.youtube.com/watch?v={v.split(":", 1)[1]}">'
                           f'youtube.com/watch?v={v.split(":", 1)[1]}</a>' for v in doc["videos"])
        video = f'<div class="watch"><strong>Watch the lesson</strong> on the SketchCast channel: {links}</div>'
    stage = sorted({o["code"][0] for o in ch.get("objectives") or [] if o["code"][:1].isdigit()})
    # an opener with several objectives or a long contents list is set denser, so it stays one page
    dense = len(ch.get("objectives") or []) > 1 or len(doc["sections"]) > 6 or len(doc.get("videos") or []) > 1
    chips = [f'<span class="chip">{html.escape(_pretty_curriculum(w.get("curriculum")))}</span>']
    if stage:
        chips.append(f'<span class="chip">Stage {", ".join(stage)}</span>')
    chips.append(f'<span class="chip">Ages {html.escape(str(w.get("age_band")).replace("-", "–"))}</span>')
    body.append(
        f'<section class="opener{" dense" if dense else ""}"><div class="op-head"><div>'
        f'<div class="op-kicker">{html.escape(subject)} · Chapter {n}</div>'
        f'<svg class="op-numeral" viewBox="0 0 120 77"><text x="0" y="70" font-family="{html.escape(theme["heading_font"])}" '
        f'font-size="92" font-weight="700" fill="none" stroke="{theme["brand"]}" stroke-width="1.8">{n:02d}</text></svg>'
        f'<h1 class="op-title">{md(ch["title"])}</h1>'
        f'<div class="op-meta">{"".join(chips)}</div></div>'
        + (f'<div><img class="op-hero" src="{hero}" alt=""></div>' if hero else "<div></div>") + "</div>"
        f'<p class="op-intro">{md(doc["plan"].get("opening"))}</p>'
        '<div class="op-cards">'
        + (f'<div class="op-card"><h4>Learning objective{"s" if len(ch.get("objectives") or []) > 1 else ""}</h4>{objectives}</div>' if pg["opener"].get("objectives") else "")
        + (f'<div class="op-card"><h4>In this chapter</h4><ol>{contents}</ol></div>' if pg["opener"].get("contents") else "")
        + f'</div>{video}{"" if dense else _key_words(doc)}</section>')
    # ── teaching sections ───────────────────────────────────────────────────
    thinks = []
    for i, s in enumerate(doc["sections"], 1):
        body.append(f'<h2><span class="sn">{n}.{i}</span>{md(s["heading"])}</h2>')
        for b in s.get("blocks") or []:
            if b.get("type") == "think" and b.get("answer"):
                thinks.append(b)
            body.append(_block(b, figs))
    # ── practice ────────────────────────────────────────────────────────────
    pitch = pg["practice"].get("line_pitch_mm", 8.5)
    body.append('<section class="practice"><div class="practice-head"><h2>Practice</h2>'
                '<p>Work through the questions in order. The answers and worked solutions are at the end of the chapter.</p></div>')
    tiers = (("guided", "Getting started", "●○○"), ("independent", "On your own", "●●○"), ("challenge", "Challenge", "●●●"))
    numbers = question_numbers(doc)
    ordered = question_order(doc)
    for tier, name, dots in tiers:
        xs = [x for x in ordered if x.get("tier") == tier]
        if not xs:
            continue
        body.append(f'<div class="tier"><span class="dots">{dots}</span>{name}</div>')
        for x in xs:
            if x.get("item_type") == "match" and x.get("pairs"):
                extra, _ = _match(x)
            elif x.get("item_type") == "sort" and x.get("items"):
                extra, _ = _sort(x)
            else:
                extra = ("<ol class='opts'>" + "".join(f"<li>{md(o)}</li>" for o in x["options"]) + "</ol>"
                         if x.get("options") else "")
            refs = ([x["figure_ref"]] if x.get("figure_ref") else []) + list(x.get("figure_refs") or [])
            pics = _photo_row(refs, figs, x.get("figure_context"), x.get("stem") or "", x.get("markers"))
            stem = md(renumber_text(x["stem"], numbers)).replace("____", '<span class="blank"></span>')
            lines = _lines(x, pitch) if pg["practice"].get("answer_lines") else ""
            body.append(f'<div class="ex"><div class="q"><span class="qn">{numbers[x["xid"]]}</span><span>{stem}</span>'
                        f'<span class="marks">[{x.get("marks")}]</span></div><div class="body">{extra}{pics}</div>{lines}</div>')
    body.append("</section>")
    # ── answers ─────────────────────────────────────────────────────────────
    body.append('<section class="answers"><h2>Answers and worked solutions</h2><div class="cols">')
    if thinks:
        body.append("<h3>Think about it</h3>" + "".join(
            f'<div class="ans"><em>{md(b.get("text"))}</em><br>{md(b.get("answer"))}</div>' for b in thinks))
        body.append("<h3>Practice</h3>")
    for x in question_order(doc):              # answers in the same printed sequence as the questions
        if x.get("item_type") == "match" and x.get("pairs"):
            answer = _match(x)[1]
        elif x.get("item_type") == "sort" and x.get("items"):
            answer = _sort(x)[1]
        else:
            answer = md(renumber_text(x["answer"], numbers))
        scheme = "".join(f'<li>{md(renumber_text(p.get("point"), numbers))} <span class="marks">({p.get("marks")})</span></li>'
                         for p in x.get("marking_scheme") or [])
        body.append(f'<div class="ans"><span class="an">{numbers[x["xid"]]}</span> <span class="key">{answer}</span><br>'
                    f'{md(renumber_text(x.get("worked_solution"), numbers))}<ul>{scheme}</ul></div>')
    credits = sorted({credit_line(c) for f in figs.values() if (c := f.get("credit"))})
    if credits:
        body.append('<div class="credits"><strong>Picture credits.</strong> Generated illustrations are original '
                    'SketchCast artwork. Photographs:<ul>' + "".join(f"<li>{c}</li>" for c in credits) + "</ul></div>")
    body.append("</div></section>")
    css = css_vars(theme) + CSS + BOOK_CSS + page_css(theme, n, ch["title"], w["title"], edition)
    css += DENSITY_CSS.get(doc.get("_density") or "", "")
    if first_page != 1:                     # inside a whole book the folios run on from the chapter before
        css += f"html {{ counter-reset: page {first_page - 1}; }}"
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>{html.escape(ch['title'])}</title>"
            f"<style>{css}</style></head><body>{''.join(body)}</body></html>")


def review_html(doc: dict) -> str:
    """The Gate D packet: what the checks and the independent reviewer found."""
    rows = []
    for f in doc["checks"]:
        rows.append(f"<tr><td>{f['verdict']}</td><td>check:{f['check']}</td><td>{html.escape(f['unit'])}</td>"
                    f"<td>{md(f['detail'])}</td></tr>")
    for f in doc["review"].get("findings") or []:
        rows.append(f"<tr><td>{f.get('severity')}</td><td>review:{f.get('kind')}</td><td>{html.escape(str(f.get('unit')))}"
                    f"</td><td><em>{md(f.get('quote'))}</em><br>{md(f.get('detail'))}</td></tr>")
    keys = {x["xid"]: x["answer"] for x in doc["exercises"]}
    blind = "".join(f"<tr><td>{b['xid']}</td><td>{md(keys.get(b['xid']))}</td><td>{md(b.get('answer'))}</td></tr>"
                    for b in doc["review"].get("blind_answers") or [])
    gaps = "".join(f"<li>{md(g.get('blueprint_section'))}: {md(g.get('missing'))}</li>"
                   for g in doc["plan"].get("source_gaps") or [])
    vgaps = "".join(f"<li>{md(g.get('need'))}: {md(g.get('why'))}</li>" for g in doc["plan"].get("visual_gaps") or [])
    cost = sum(c["usd"] or 0 for c in doc["costs"])
    calls = "".join(f"<tr><td>{c['stage']}</td><td>{c['unit']}</td><td>{c['provider']}/{c['model']}</td><td>{c['in']}</td>"
                    f"<td>{c['out']}</td><td>${(c['usd'] or 0):.3f}</td><td>{c['secs']}s</td></tr>" for c in doc["costs"])
    g = doc["review"].get("gates") or {}
    verdict = ("READY for Gate D" if doc["review"].get("ready_for_gate_d") else "NOT READY — see the failures below")
    cov = "".join(f"<tr><td>{html.escape(str(c.get('objective')))}</td><td>{md(c.get('component'))}</td>"
                  f"<td>{html.escape(', '.join(c.get('taught_in') or []))}</td><td>{c.get('verdict')}</td></tr>"
                  for c in g.get("coverage") or [])
    ass = "".join(f"<tr><td>{a.get('xid')}</td><td>{a.get('verdict')}</td><td>{html.escape(', '.join(a.get('failed') or []))}"
                  f"</td><td>{md(a.get('fix'))}</td></tr>" for a in g.get("assessment") or [])
    vis = g.get("visual") or {}
    vrows = "".join(
        f"<tr><td>{html.escape(str(v.get('unit')))}</td><td>{html.escape(str(v.get('ref'))[:22])}</td>"
        f"<td>{md(v.get('shows'))}</td><td>{'✓' if v.get('identity_consistent') else '✗'} identity · "
        f"{'✓' if v.get('text_consistent') else '✗'} text · {'✓' if v.get('suitable') else '✗'} suitable · "
        f"{'✓' if v.get('labels_ok') else '✗'} labels</td><td>{v.get('confidence')}</td></tr>"
        for v in vis.get("figures") or [])
    xrows = "".join(
        f"<tr><td>{html.escape(str(v.get('xid')))}</td><td>{'✓' if v.get('answer_inferable') else '✗'} inferable · "
        f"{'✓' if v.get('scheme_matches_pictures') else '✗'} scheme</td><td>"
        + "; ".join(f"{p.get('label')}: {html.escape(str(p.get('shows')))}" for p in v.get("per_picture") or [])
        + f"</td><td>{v.get('confidence')}</td></tr>" for v in vis.get("exercises") or [])
    hr = "".join(f"<li>{html.escape(str(h.get('unit')))} {html.escape(str(h.get('ref') or ''))}: {md(h.get('why'))}</li>"
                 for h in doc["review"].get("human_review") or [])
    gates = (f"<h2>The three independent gates</h2><p><strong>Content: {str(g.get('coverage_gate', '?')).upper()}"
             f"</strong> · <strong>Visual: {str(g.get('visual_gate', '?')).upper()}</strong> · "
             f"<strong>Assessment: {str(g.get('assessment_gate', '?')).upper()}</strong> · <strong>{verdict}</strong></p>"
             f"<h3>Needs a person (the gates could not judge with confidence)</h3><ul>{hr or '<li>none</li>'}</ul>"
             f"<h3>Content: objective coverage</h3><table><tr><th>objective</th><th>component</th><th>taught in</th>"
             f"<th>verdict</th></tr>{cov}</table>"
             f"<h3>Visual: every placed picture, as printed</h3><table><tr><th>unit</th><th>picture</th><th>what it "
             f"shows</th><th>checks</th><th>conf.</th></tr>{vrows}</table>"
             f"<h3>Visual: every question picture against its key</h3><table><tr><th>ex</th><th>checks</th>"
             f"<th>what each picture shows</th><th>conf.</th></tr>{xrows}</table>"
             f"<h3>Assessment validity</h3><table><tr><th>exercise</th><th>verdict</th><th>failed</th>"
             f"<th>fix</th></tr>{ass}</table>")
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>Gate D review — ch {doc['chapter']['number']}</title>"
            "<style>body{font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#1d2433}table{border-collapse:collapse;"
            "width:100%;font-size:13px;margin:8px 0 20px}td,th{border:1px solid #d5dbe3;padding:5px 7px;vertical-align:top}"
            "h1{font-size:22px}h2{font-size:17px;margin-top:22px}</style></head><body>"
            f"<h1>Gate D review packet — Chapter {doc['chapter']['number']}: {html.escape(doc['chapter']['title'])}</h1>"
            f"<p><strong>Reviewer's overall view:</strong> {md(doc['review'].get('overall'))}</p>{gates}"
            f"<h2>Source gaps the author refused to fill</h2><ul>{gaps or '<li>none</li>'}</ul>"
            f"<h2>Visual gaps</h2><ul>{vgaps or '<li>none</li>'}</ul>"
            "<h2>Findings (deterministic checks + independent reviewer)</h2>"
            "<table><tr><th>severity</th><th>check</th><th>unit</th><th>detail</th></tr>" + "".join(rows) + "</table>"
            "<h2>Blind re-solve (reviewer answered without the key)</h2><table><tr><th>ex</th><th>author's key</th>"
            f"<th>reviewer's blind answer</th></tr>{blind}</table>"
            f"<h2>Cost and time</h2><p>Total ${cost:.2f}</p><table><tr><th>card</th><th>unit</th><th>model</th><th>in</th>"
            f"<th>out</th><th>usd</th><th>time</th></tr>{calls}</table></body></html>")


def _print(page: Path, pdf: Path) -> Path:
    """Print to a NEW file, prove it was written, then put it in place. Edge
    exits 0 even when it cannot write (ch 1, 2026-10-25: the PDF was open in a
    viewer, so the old file silently survived and was measured as the new
    one). A locked target gets a timestamped sibling instead, never a stale file."""
    import os
    import time
    import tempfile
    pdf = pdf.resolve()                    # Edge resolves a relative output path from its own directory
    tmp = pdf.with_name(f"{pdf.stem}.printing-{os.getpid()}-{int(time.time() * 1000)}.pdf")
    # each print gets its own browser profile: parallel renders sharing the default
    # profile are refused by Edge, which still exits 0 (book run, 2026-10-25)
    for attempt in (1, 2, 3):
        with tempfile.TemporaryDirectory(prefix="edge-print-") as profile:
            try:
                subprocess.run([*browser(), "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                                f"--user-data-dir={profile}", f"--print-to-pdf={tmp}", page.resolve().as_uri()],
                               check=False, timeout=180, capture_output=True)   # success = the file exists (below)
            except subprocess.TimeoutExpired:
                pass                        # a hung print is a failed attempt like any other (killed by run())
        if tmp.exists() and tmp.stat().st_size >= 1000:
            break
        time.sleep(3 * attempt)
    else:
        raise RuntimeError(f"the browser did not write {tmp.name}")
    try:
        os.replace(tmp, pdf)
        return pdf
    except PermissionError:
        alt = pdf.with_name(f"{pdf.stem}-{time.strftime('%H%M%S')}.pdf")
        os.replace(tmp, alt)
        return alt


def _underlay(pdf: Path, slug: str, chapter: int, edition: str) -> None:
    """Put the theme's page art (paper, tab, opener band, head rule) beneath
    every page: page 1 is the opener, then versos on even and rectos on odd
    folios. Vector in, vector out; the text layer is untouched."""
    import fitz
    from publishing.manuscript.theme import backgrounds, for_work
    art = {k: fitz.open(stream=fitz.open("svg", stream=v).convert_to_pdf(), filetype="pdf")
           for k, v in backgrounds(for_work(slug), chapter, edition).items()}
    doc = fitz.open(pdf)
    for i, page in enumerate(doc, 1):
        page.show_pdf_page(page.rect, art["first" if i == 1 else "verso" if i % 2 == 0 else "recto"], 0, overlay=False)
    tmp = pdf.with_name(pdf.stem + ".underlay.pdf")
    doc.save(tmp, garbage=3, deflate=True)
    doc.close()
    import os
    os.replace(tmp, pdf)


# Spacing variants the layout search may try (founder 2026-10-29: a chapter's last lines spilled onto their own page
# before the forced breaks of Practice and Answers). Spacing only — the text, its order and the type size never change.
DENSITY_CSS = {
    "snug": "body{line-height:1.44} .box{margin:2.8mm 0} h2{margin:7mm 0 2.6mm} h3{margin:4.8mm 0 1.8mm} .ex{margin:0 0 4mm}",
    "tight": "body{line-height:1.38} .box{margin:2.2mm 0;padding:2.6mm 4mm} h2{margin:5.5mm 0 2.2mm} "
             "h3{margin:4mm 0 1.5mm} .ex{margin:0 0 3.2mm}",
}


def _layout_score(diag: dict) -> float:
    """Lower is better: hard defects dominate; then dead space on pages other
    than the last beyond a generous allowance (whitespace is a signal, so only
    the excess counts); then page count, lightly."""
    pages = diag["pages"][:-1] if len(diag["pages"]) > 1 else []
    return 100 * len(diag["hard"]) + sum(max(0.0, p["unused_share"] - 0.30) for p in pages) + 0.05 * diag["page_count"]


def _alternatives(doc: dict) -> list[tuple[str, dict]]:
    """Layout candidates (D6): the authored layout, and each full/centre picture
    ROW (a group with no relation arrows) set beside the text at half width.
    Placement only — content and the author's other choices never change."""
    import copy
    out = [("as authored", doc)]
    for density in DENSITY_CSS:
        out.append((f"as authored, {density} spacing", {**doc, "_density": density}))
    for si, s in enumerate(doc["sections"]):
        for bi, b in enumerate(s.get("blocks") or []):
            lay = b.get("layout") or {}
            if b.get("type") == "figure_group" and not b.get("relations") and \
                    lay.get("placement", "centre") in ("full", "centre"):
                alt = copy.deepcopy(doc)
                blocks = alt["sections"][si]["blocks"]
                blk = blocks.pop(bi)
                blk["layout"] = {**lay, "placement": "beside-right", "width": "half",
                                 "why": (lay.get("why") or "") + " [renderer: set beside the text to avoid a page gap]"}
                # a beside figure precedes the text it sits beside: move it before the paragraph that introduces it
                blocks.insert(max(0, bi - 1), blk)
                out.append((f"{s['sid']} picture row beside the text", alt))
        for bi, b in enumerate(s.get("blocks") or []):
            lay = b.get("layout") if isinstance(b.get("layout"), dict) else {}
            # a beside picture that cannot fit beside its text may fit at a third of the width
            if b.get("type") == "figure" and str(lay.get("placement", "")).startswith("beside") and \
                    lay.get("width", "half") != "third":
                alt = copy.deepcopy(doc)
                alt["sections"][si]["blocks"][bi]["layout"] = {**lay, "width": "third"}
                out.append((f"{s['sid']} {b.get('ref')} beside at a third", alt))
                # a beside picture that does not fit where it stands is pushed to the next page while its
                # text flows on without it: let it start one or two paragraphs earlier, still beside its text
                for up in (1, 2):
                    if bi - up >= 0 and all(x.get("type") in ("paragraph", "list")
                                            for x in s["blocks"][bi - up:bi]):
                        alt = copy.deepcopy(doc)
                        blocks = alt["sections"][si]["blocks"]
                        blocks.insert(bi - up, blocks.pop(bi))
                        out.append((f"{s['sid']} {b.get('ref')} {up} paragraph(s) earlier", alt))
    return out[:10]


def render(doc: dict, dossier: dict, env_file: Path, out_dir: Path) -> dict:
    from publishing.manuscript.layout_check import diagnose, report_html
    n = doc["chapter"]["number"]
    figs = _figures(doc, dossier, env_file, annotations_for(doc["work"]["slug"]))
    page = out_dir / f"ch{n:02d}.html"
    pdf = out_dir / f"ch{n:02d}.pdf"
    tried = []
    for name, cand in _alternatives(doc):
        page.write_text(chapter_html(cand, figs), encoding="utf-8")
        d = diagnose(str(_print(page, pdf)))
        tried.append((_layout_score(d), name, cand))
    best = min(tried, key=lambda t: t[0])
    page.write_text(chapter_html(best[2], figs), encoding="utf-8")
    pdf = _print(page, pdf)
    _underlay(pdf, doc["work"]["slug"], n, "digital")
    # the print edition: same pages, trim + bleed, tabs running into the bleed
    print_page = out_dir / f"ch{n:02d}-print.html"
    print_page.write_text(chapter_html(best[2], figs, edition="print"), encoding="utf-8")
    print_pdf = _print(print_page, out_dir / f"ch{n:02d}-print.pdf")
    _underlay(print_pdf, doc["work"]["slug"], n, "print")
    doc["layout_choice"] = {"chosen": best[1], "candidates": [(name, round(score, 3)) for score, name, _ in tried]}
    packet = out_dir / f"ch{n:02d}-gate-d-review.html"
    packet.write_text(review_html(doc), encoding="utf-8")
    diag = diagnose(str(pdf))
    diag["layout_choice"] = doc["layout_choice"]
    packet.write_text(review_html(doc).replace("</body></html>", report_html(diag) + "</body></html>"),
                      encoding="utf-8")
    (out_dir / f"ch{n:02d}-diagnostics.json").write_text(json.dumps(diag, indent=1), encoding="utf-8")
    return {"html": str(page), "pdf": str(pdf), "print_pdf": str(print_pdf), "review": str(packet), "hard_defects": len(diag["hard"]),
            "pages": diag["page_count"]}
