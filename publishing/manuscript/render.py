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
    svg = (f'<svg viewBox="0 0 {total_w} {height:.0f}" width="{total_w}" role="img">'
           f'<image href="data:image/png;base64,{b64}" x="{COL_W}" y="0" width="{IMG_W}" height="{ih:.1f}"/>'
           + "".join(parts) + "</svg>")
    return svg, unplaced


CSS = """
@page { size: A4; margin: 18mm 17mm 20mm; }
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
    left = "".join(f"<li>{md(p['left'])}</li>" for p in pairs)
    right = "".join(f"<li>{md(pairs[j]['right'])}</li>" for j in order)
    key = ", ".join(f"{i + 1}–{letters[order.index(i)]}" for i in range(len(pairs)))
    return (f'<div class="match"><ol class="ma">{left}</ol><ol class="mb">{right}</ol></div>', key)


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
        return (f'<figure>{f["svg"]}<figcaption>{md(b.get("caption") or f["caption"])}{_credit(f)}</figcaption>'
                f'{miss}</figure>')
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
            if b.get("type") == "figure":
                want.add(b.get("ref"))
                context[b.get("ref")] = context.get(b.get("ref"), "") + " " + prose
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
        # the asset's description is a generation brief ("…Name the layer groups
        # exactly: …"); only its first sentence describes the picture
        first = re.split(r"(?<=[.!?])\s", (a["description"] or "").strip(), maxsplit=1)[0]
        parts = [] if a.get("kind") == "photo" else [p for p in a["parts"] if _norm(p) and _norm(p) in talk]
        src[a["id"]] = (a["_asset"], parts, first, a.get("credit"))
    url, key = load_env(env_file)
    out = {}
    with client(url, key) as http:
        for ref in want:
            if ref not in src or not src[ref][0]:
                continue
            asset, labels, caption, credit = src[ref]
            png = _download(http, url, asset["storage_path"])
            svg, unplaced = figure_svg(png, asset.get("vision") or {"w": 1, "h": 1}, labels)
            out[ref] = {"svg": svg, "unplaced": unplaced, "caption": caption, "credit": credit,
                        "img": "data:image/png;base64," + base64.b64encode(png).decode()}
    return out


def _photo_row(refs: list[str], figs: dict) -> str:
    tiles = []
    for i, r in enumerate(refs):
        f = figs.get(r)
        if f:
            tiles.append(f'<figure><img src="{f["img"]}" alt=""><figcaption><span class="pl">{"ABCDEFGH"[i]}</span>'
                         f'{_credit(f)}</figcaption></figure>')
    return f'<div class="photos">{"".join(tiles)}</div>' if tiles else ""


def chapter_html(doc: dict, figs: dict) -> str:
    ch, w = doc["chapter"], doc["work"]
    body = [f'<div class="chapter-head"><div class="num">Chapter {ch["number"]}</div><h1>{md(ch["title"])}</h1>'
            f'<div class="meta">{md(w["title"])} · ages {md(w["age_band"])} · Cambridge Lower Secondary '
            f'{", ".join(ch["node_codes"])}</div></div>', f'<p class="opening">{md(doc["plan"].get("opening"))}</p>']
    for s in doc["sections"]:
        body.append(f"<h2>{md(s['heading'])}</h2>")
        body += [_block(b, figs) for b in s.get("blocks") or []]
    if doc.get("videos"):
        links = " · ".join(f'<a href="https://www.youtube.com/watch?v={v.split(":", 1)[1]}">'
                           f'youtu.be/{v.split(":", 1)[1]}</a>' for v in doc["videos"])
        body.append(f'<div class="watch"><strong>Watch the lesson:</strong> {links}</div>')
    body.append("<h2>Practice</h2>")
    for tier, name in (("guided", "Getting started"), ("independent", "On your own"), ("challenge", "Challenge")):
        xs = [x for x in doc["exercises"] if x.get("tier") == tier]
        if not xs:
            continue
        body.append(f"<h3>{name}</h3>")
        for n, x in enumerate(xs, 1):
            if x.get("item_type") == "match" and x.get("pairs"):
                opts, _ = _match(x)
            else:
                opts = ("<ol class='opts'>" + "".join(f"<li>{md(o)}</li>" for o in x["options"]) + "</ol>"
                        if x.get("options") else "")
            fig = _block({"type": "figure", "ref": x["figure_ref"]}, figs) if x.get("figure_ref") else ""
            fig += _photo_row(x.get("figure_refs") or [], figs)
            body.append(f'<div class="ex"><strong>{x["xid"].upper()}.</strong> {md(x["stem"])} '
                        f'<span class="marks">[{x.get("marks")}]</span>{opts}{fig}</div>')
    body.append('<div class="answers"><h2>Answers and worked solutions</h2>')
    for x in doc["exercises"]:
        answer = _match(x)[1] if x.get("item_type") == "match" and x.get("pairs") else md(x["answer"])
        scheme = "".join(f'<li>{md(p.get("point"))} <span class="marks">({p.get("marks")})</span></li>'
                         for p in x.get("marking_scheme") or [])
        body.append(f'<div class="ex"><strong>{x["xid"].upper()}.</strong> <strong>{answer}</strong><br>'
                    f'{md(x.get("worked_solution"))}<div class="marks">Mark scheme:</div><ul>{scheme}</ul></div>')
    body.append("</div>")
    credits = sorted({f'{c["author"] or "Unknown"}, {c["licence"]}, {c.get("provider")} — {c.get("url")}'
                      for f in figs.values() if (c := f.get("credit"))})
    if credits:
        body.append('<div class="credits"><strong>Picture credits.</strong> ' +
                    "; ".join(html.escape(c) for c in credits) + "</div>")
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>{html.escape(ch['title'])}</title>"
            f"<style>{CSS}</style></head><body>{''.join(body)}</body></html>")


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
    gates = (f"<h2>The two compulsory gates</h2><p><strong>Content coverage: {g.get('coverage_gate', '?').upper()}"
             f"</strong> · <strong>Assessment validity: {g.get('assessment_gate', '?').upper()}</strong> · "
             f"<strong>{verdict}</strong></p><table><tr><th>objective</th><th>component</th><th>taught in</th>"
             f"<th>verdict</th></tr>{cov}</table><table><tr><th>exercise</th><th>verdict</th><th>failed</th>"
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
    return {"html": str(page), "pdf": str(pdf), "review": str(packet)}
