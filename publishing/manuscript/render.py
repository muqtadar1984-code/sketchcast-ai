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
IMG_W, COL_W, LINE_H = 330, 140, 19


def md(text: str) -> str:
    t = html.escape(str(text or ""))
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<em>\1</em>", t)
    return t


def _norm(s: str) -> str:
    return re.sub(r"[\s_]+", " ", str(s or "").strip().lower())


def _download(http, base: str, path: str) -> bytes:
    r = http.get(f"{base}/storage/v1/object/{BUCKET}/{path}")   # absolute: the client's base is /rest/v1
    r.raise_for_status()
    return r.content


def asset_bytes(http, base: str, asset: dict) -> bytes:
    """A library asset from storage, or a book asset from the local store."""
    if asset.get("local_path"):
        return Path(asset["local_path"]).read_bytes()
    return _download(http, base, asset["storage_path"])


def figure_svg(png: bytes, vision: dict, labels: list[str]) -> tuple[str, list[str]]:
    """The picture with its labels as vector overlays. Returns (svg, unplaced)."""
    w, h = float(vision.get("w") or 0), float(vision.get("h") or 0)
    if (w <= 1 or h <= 1) and png[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = float(int.from_bytes(png[16:20], "big")), float(int.from_bytes(png[20:24], "big"))
    w, h = w or 1.0, h or 1.0
    scale = IMG_W / w
    ih = h * scale
    regions = {_norm(k): v for k, v in (vision.get("regions") or {}).items()}
    sides = {"L": [], "R": []}
    unplaced = []
    for lab in labels:
        boxes = regions.get(_norm(lab))
        if not boxes:
            unplaced.append(lab)
            continue
        centres = [((b[0] + b[2]) / 2 * scale, (b[1] + b[3]) / 2 * scale) for b in boxes]
        left = min(centres, key=lambda c: c[0])
        right = max(centres, key=lambda c: c[0])
        side = "L" if left[0] < IMG_W - right[0] else "R"
        sides[side].append((lab, left if side == "L" else right))
    if not sides["L"] and not sides["R"]:
        # nothing to label: no reserved columns, the picture alone
        b64 = base64.b64encode(png).decode()
        return (f'<svg viewBox="0 0 {IMG_W} {ih:.0f}" role="img"><image href="data:image/png;base64,{b64}" '
                f'x="0" y="0" width="{IMG_W}" height="{ih:.1f}"/></svg>'), unplaced
    total_w = IMG_W + 2 * COL_W
    height = ih
    parts = []
    for side, items in sides.items():
        items.sort(key=lambda t: t[1][1])
        y = 0.0
        for lab, (cx, cy) in items:
            y = max(y + LINE_H, cy)
            ly = min(y, ih + LINE_H * len(items))
            height = max(height, ly + 8)
            if side == "L":
                tx, anchor, lx = COL_W - 8, "end", COL_W - 4
            else:
                tx, anchor, lx = COL_W + IMG_W + 8, "start", COL_W + IMG_W + 4
            px = COL_W + cx
            parts.append(f'<line x1="{lx:.1f}" y1="{ly - 4:.1f}" x2="{px:.1f}" y2="{cy:.1f}" class="leader"/>'
                         f'<circle cx="{px:.1f}" cy="{cy:.1f}" r="2.2" class="dot"/>'
                         f'<text x="{tx:.1f}" y="{ly:.1f}" text-anchor="{anchor}" class="lab">{html.escape(lab)}</text>')
    b64 = base64.b64encode(png).decode()
    svg = (f'<svg viewBox="0 0 {total_w} {height:.0f}" role="img">'
           f'<image href="data:image/png;base64,{b64}" x="{COL_W}" y="0" width="{IMG_W}" height="{ih:.1f}"/>'
           + "".join(parts) + "</svg>")
    return svg, unplaced


CSS = """
@page { size: A4; margin: 18mm 17mm 20mm;
  @top-right { font: 8pt 'Segoe UI', Arial, sans-serif; color: #8a94a6; }
  @bottom-center { content: counter(page); font: 9pt 'Segoe UI', Arial, sans-serif; color: #5b6475; } }
figure.place { margin: 2pt 0 8pt; break-inside: avoid; background: #fff; }
.box, .watch, .ex { display: flow-root; }   /* a box sits BESIDE a floated figure, never under it */
figure.place svg { width: 100%; height: auto; display: block; }
figure.beside-right { float: right; margin-left: 12pt; clear: right; }
figure.beside-left { float: left; margin-right: 12pt; clear: left; }
figure.w-third { width: 34%; } figure.w-half { width: 46%; } figure.w-two-thirds { width: 62%; }
figure.full, figure.centre { width: 100%; text-align: center; }
figure.centre svg { max-width: 62%; margin: 0 auto; }
h2 { clear: both; }
.group svg text.gl { font: 600 12px 'Segoe UI', Arial, sans-serif; fill: #1d2433; }
.group svg text.gs { font: 10.5px 'Segoe UI', Arial, sans-serif; fill: #5b6475; }
.group svg .rel { stroke: #0f766e; stroke-width: 1.6; fill: none; }
.group svg .relh { fill: #0f766e; }
.group svg .zoom { stroke: #0f766e; stroke-width: 2; fill: none; }
table.match { border-collapse: collapse; margin: 4pt 0 0 6pt; width: 96%; }
table.match td { padding: 2pt 8pt 2pt 0; vertical-align: top; }
table.match td.n { width: 1.6em; font-weight: 700; } table.match td.l { width: 30%; }
.sortbox { display: flex; gap: 10pt; margin: 4pt 0; } .sortbox .cat { flex: 1; border: 1pt dashed var(--line);
  border-radius: 5pt; padding: 4pt 7pt; min-height: 46pt; font-weight: 600; }
.sortitems { margin: 4pt 0; font-size: 10.5pt; }
.photos.tiles figure img { height: 150px; width: 100%; object-fit: contain; }  /* whole photo, never cropped */
.photos .ctx { display: block; font-size: 9pt; color: var(--ink); margin-top: 2pt; }
.think-answers { font-size: 10pt; } .think-answers li { margin-bottom: 4pt; }
.credits a { color: inherit; }
:root { --ink:#1d2433; --muted:#5b6475; --brand:#0f766e; --soft:#e7f4f2; --warn:#fff4e5; --line:#c9d3cf; }
body { font-family: 'Segoe UI', 'Noto Sans', Arial, sans-serif; color: var(--ink); font-size: 11pt; line-height: 1.5; margin: 0; }
.chapter-head { border-bottom: 3px solid var(--brand); margin-bottom: 14pt; padding-bottom: 8pt; }
.chapter-head .num { color: var(--brand); font-weight: 700; letter-spacing: .08em; text-transform: uppercase; font-size: 10pt; }
h1 { font-size: 24pt; margin: 2pt 0 6pt; line-height: 1.15; }
.meta { color: var(--muted); font-size: 9.5pt; }
.opening { font-size: 12pt; font-style: italic; margin: 10pt 0 14pt; }
h2 { font-size: 15pt; color: var(--brand); margin: 18pt 0 6pt; break-after: avoid; }
h3 { font-size: 12.5pt; margin: 14pt 0 4pt; break-after: avoid; }
.box { border-radius: 6pt; padding: 7pt 10pt; margin: 8pt 0; break-inside: avoid; }
.key_term { background: var(--soft); border-left: 4pt solid var(--brand); }
.key_idea { background: var(--soft); font-weight: 600; }
.misconception { background: var(--warn); border-left: 4pt solid #d97706; }
.misconception .wrong { text-decoration: line-through; color: #9a3412; }
.think { border: 1.5pt dashed var(--brand); }
.worked { border: 1pt solid var(--line); }
figure { margin: 10pt 0; text-align: center; break-inside: avoid; }
figure svg { max-width: 100%; height: auto; }
.leader { stroke: #334155; stroke-width: .8; } .dot { fill: #334155; } .lab { font-size: 10.5px; font-family: 'Segoe UI', Arial, sans-serif; fill: #111827; }
figcaption { font-size: 9.5pt; color: var(--muted); margin-top: 3pt; }
.missing-labels { font-size: 8.5pt; color: #b45309; }
.watch { background: #f1f5f9; padding: 7pt 10pt; border-radius: 6pt; font-size: 10pt; margin-top: 12pt; }
.ex { margin: 6pt 0 8pt; break-inside: avoid; }
.ex .tag { font-size: 8.5pt; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; }
.ex ol.opts { list-style: upper-alpha; margin: 3pt 0 0 18pt; padding: 0; }
.answers { break-before: page; }
.answers .ex { font-size: 10pt; }
.marks { color: var(--muted); font-size: 9pt; }
.gap { background: #fde2e2; padding: 6pt 9pt; border-radius: 6pt; font-size: 9.5pt; margin: 8pt 0; }
.extension { border: 1pt solid #a5b4fc; background: #eef2ff; font-size: 10pt; }
.tag { font-size: 8.5pt; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; }
.credit { display: block; font-size: 8pt; color: #8a94a6; margin-top: 2pt; }
.match { display: flex; gap: 24pt; margin: 4pt 0 0 6pt; }
.match ol.ma { list-style: decimal; } .match ol.mb { list-style: upper-alpha; }
.photos { display: flex; gap: 10pt; justify-content: center; margin: 6pt 0; break-inside: avoid; }
.photos figure { margin: 0; width: 31%; } .photos img { width: 100%; border-radius: 4pt; }
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
                           f'height="{18 if len(r["label"]) <= label_chars + 6 else 32}" fill="#fff"/>')
                out.append(lines(r["label"], "gs", cx, 30 + ih + 34 + gap / 2, label_chars + 6))
            out.append(pic(t, cx, ty, circ, circ, clip=circ / 2 - 2 if zoom else None))
            if zoom:
                out.append(f'<circle class="zoom" cx="{cx}" cy="{ty}" r="{circ / 2}"/>')
            out.append(lines(items[t].get("label"), "gl", cx, ty + circ / 2 + 20, label_chars))
    out.append("</svg>")
    credits = "".join(_credit(figs[i["ref"]]) for i in items)
    return (f'<figure class="{_place(b)} group">{"".join(out)}<figcaption>{md(b.get("caption"))}{credits}'
            f'</figcaption></figure>')


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
    if t == "extension":
        return (f'<div class="box extension"><div class="tag">Extension — beyond the curriculum objective, '
                f'not assessed</div>{md(b.get("text"))}</div>')
    if t == "figure":
        f = figs.get(b.get("ref"))
        if not f:
            return f'<div class="gap">Figure {html.escape(str(b.get("ref")))} is missing.</div>'
        miss = (f'<div class="missing-labels">Labels with no detected region (not placed): '
                f'{html.escape(", ".join(f["unplaced"]))}</div>' if f["unplaced"] else "")
        return (f'<figure class="{_place(b)}">{f["svg"]}<figcaption>{md(b.get("caption") or f["caption"])}'
                f'{_credit(f)}</figcaption>{miss}</figure>')
    return f"<p>{md(json.dumps(b))}</p>"


def _figures(doc: dict, dossier: dict, env_file: Path) -> dict:
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
    url, key = load_env(env_file)
    out = {}
    with client(url, key) as http:
        for ref in want:
            if ref not in src or not src[ref][0]:
                continue
            asset, labels, caption, credit = src[ref]
            png = asset_bytes(http, url, asset)
            svg, unplaced = figure_svg(png, asset.get("vision") or {"w": 1, "h": 1}, labels)
            out[ref] = {"svg": svg, "unplaced": unplaced, "caption": caption, "credit": credit,
                        "img": "data:image/png;base64," + base64.b64encode(png).decode()}
    return out


def _photo_row(refs: list[str], figs: dict, context: dict | None = None) -> str:
    """Uniform tiles, lettered, each with what the question says about it."""
    tiles = []
    for i, r in enumerate(refs):
        f = figs.get(r)
        if f:
            letter = "ABCDEFGH"[i]
            ctx = (context or {}).get(letter)
            tiles.append(f'<figure><img src="{f["img"]}" alt=""><figcaption><span class="pl">{letter}</span>'
                         + (f'<span class="ctx">{md(ctx)}</span>' if ctx else "") + f'{_credit(f)}</figcaption></figure>')
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
    return f"{title} by {who}, {src}, {lic}. Resized for print."


def _sort(x: dict) -> tuple[str, str]:
    import hashlib
    items = sorted(x.get("items") or [], key=lambda i: hashlib.sha1(f"{x.get('xid')}:{i.get('text')}".encode()).hexdigest())
    cats = "".join(f'<div class="cat">{md(c)}</div>' for c in x.get("categories") or [])
    q = (f'<div class="sortitems">{" · ".join(md(i.get("text")) for i in items)}</div>'
         f'<div class="sortbox">{cats}</div>')
    key = "; ".join(f'{md(c)}: ' + ", ".join(md(i["text"]) for i in x.get("items") or [] if i.get("category") == c)
                    for c in x.get("categories") or [])
    return q, key


def chapter_html(doc: dict, figs: dict) -> str:
    ch, w = doc["chapter"], doc["work"]
    running = f'Chapter {ch["number"]} · {ch["title"]}'.replace('"', "'")
    body = [f'<div class="chapter-head"><div class="num">Chapter {ch["number"]}</div><h1>{md(ch["title"])}</h1>'
            f'<div class="meta">{md(w["title"])} · ages {md(w["age_band"])} · Cambridge Lower Secondary '
            f'{", ".join(ch["node_codes"])}</div></div>', f'<p class="opening">{md(doc["plan"].get("opening"))}</p>']
    if doc.get("videos"):
        links = " · ".join(f'<a href="https://www.youtube.com/watch?v={v.split(":", 1)[1]}">'
                           f'youtube.com/watch?v={v.split(":", 1)[1]}</a>' for v in doc["videos"])
        body.append(f'<div class="watch"><strong>Watch the SketchCast lesson on this topic:</strong> {links}</div>')
    thinks = []
    for s in doc["sections"]:
        body.append(f"<h2>{md(s['heading'])}</h2>")
        for b in s.get("blocks") or []:
            if b.get("type") == "think" and b.get("answer"):
                thinks.append((s["heading"], b))
            body.append(_block(b, figs))
    body.append("<h2>Practice</h2>")
    for tier, name in (("guided", "Getting started"), ("independent", "On your own"), ("challenge", "Challenge")):
        xs = [x for x in doc["exercises"] if x.get("tier") == tier]
        if not xs:
            continue
        body.append(f"<h3>{name}</h3>")
        for x in xs:
            if x.get("item_type") == "match" and x.get("pairs"):
                opts, _ = _match(x)
            elif x.get("item_type") == "sort" and x.get("items"):
                opts, _ = _sort(x)
            else:
                opts = ("<ol class='opts'>" + "".join(f"<li>{md(o)}</li>" for o in x["options"]) + "</ol>"
                        if x.get("options") else "")
            # a question's pictures print as tiles with what the question says about
            # them, never with a caption (a caption can give the answer away)
            refs = ([x["figure_ref"]] if x.get("figure_ref") else []) + list(x.get("figure_refs") or [])
            fig = _photo_row(refs, figs, x.get("figure_context"))
            body.append(f'<div class="ex"><strong>{x["xid"].upper()}.</strong> {md(x["stem"])} '
                        f'<span class="marks">[{x.get("marks")}]</span>{opts}{fig}</div>')
    body.append('<div class="answers"><h2>Answers and worked solutions</h2>')
    if thinks:
        body.append('<h3>Think about it</h3><ol class="think-answers">' +
                    "".join(f'<li><em>{md(b.get("text"))}</em><br>{md(b.get("answer"))}</li>' for _, b in thinks) +
                    "</ol><h3>Practice</h3>")
    for x in doc["exercises"]:
        if x.get("item_type") == "match" and x.get("pairs"):
            answer = _match(x)[1]
        elif x.get("item_type") == "sort" and x.get("items"):
            answer = _sort(x)[1]
        else:
            answer = md(x["answer"])
        scheme = "".join(f'<li>{md(p.get("point"))} <span class="marks">({p.get("marks")})</span></li>'
                         for p in x.get("marking_scheme") or [])
        body.append(f'<div class="ex"><strong>{x["xid"].upper()}.</strong> <strong>{answer}</strong><br>'
                    f'{md(x.get("worked_solution"))}<div class="marks">Mark scheme:</div><ul>{scheme}</ul></div>')
    body.append("</div>")
    credits = sorted({credit_line(c) for f in figs.values() if (c := f.get("credit"))})
    if credits:
        body.append('<div class="credits"><strong>Picture credits.</strong><ul>' +
                    "".join(f"<li>{c}</li>" for c in credits) + "</ul></div>")
    page_style = f'@page {{ @top-right {{ content: "{html.escape(running)}"; }} }} @page :first {{ @top-right {{ content: none; }} }}'
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>{html.escape(ch['title'])}</title>"
            f"<style>{CSS}{page_style}</style></head><body>{''.join(body)}</body></html>")


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


def render(doc: dict, dossier: dict, env_file: Path, out_dir: Path) -> dict:
    n = doc["chapter"]["number"]
    figs = _figures(doc, dossier, env_file)
    page = out_dir / f"ch{n:02d}.html"
    page.write_text(chapter_html(doc, figs), encoding="utf-8")
    packet = out_dir / f"ch{n:02d}-gate-d-review.html"
    packet.write_text(review_html(doc), encoding="utf-8")
    pdf = out_dir / f"ch{n:02d}.pdf"
    subprocess.run([EDGE, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                    f"--print-to-pdf={pdf}", page.resolve().as_uri()], check=True, timeout=120,
                   capture_output=True)
    from publishing.manuscript.layout_check import diagnose, report_html
    diag = diagnose(str(pdf))
    packet.write_text(review_html(doc).replace("</body></html>", report_html(diag) + "</body></html>"),
                      encoding="utf-8")
    (out_dir / f"ch{n:02d}-diagnostics.json").write_text(json.dumps(diag, indent=1), encoding="utf-8")
    return {"html": str(page), "pdf": str(pdf), "review": str(packet), "hard_defects": len(diag["hard"]),
            "pages": diag["page_count"]}
