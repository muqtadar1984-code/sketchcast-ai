"""Post-render page DIAGNOSTICS (founder 2026-10-25, D6).

Measured on the PDF itself. Only objective defects are hard failures: text
drawn over a picture, an element clipped by the page edge, a raster picture
below its legibility floor at its printed size. Whitespace and page count are
SIGNALS for a person and the page-level visual review — deliberate space
(exercise room, a chapter end, a full-width diagram) is legitimate. Vector
graphics have no dpi and are never measured for it.
"""
from __future__ import annotations

FLOOR_DPI = 150      # below this a raster picture is visibly soft at its printed size: a hard defect
PRINT_DPI = 300      # the print target: below it is a warning, never a failure on its own


def diagnose(pdf_path: str) -> dict:
    import fitz

    doc = fitz.open(pdf_path)
    pages, hard, warn = [], [], []
    for i, page in enumerate(doc, 1):
        W, H = page.rect.width, page.rect.height
        text_boxes = [b[:4] for b in page.get_text("blocks") if b[4].strip()]
        imgs = []
        for info in page.get_image_info(xrefs=True):
            x0, y0, x1, y1 = info["bbox"]
            wpt = max(1e-6, x1 - x0)
            dpi = info["width"] / (wpt / 72.0)
            imgs.append({"bbox": [round(v) for v in (x0, y0, x1, y1)], "px": [info["width"], info["height"]],
                         "dpi": round(dpi)})
            if dpi < FLOOR_DPI:
                hard.append({"page": i, "check": "raster below legibility floor", "detail": f"{round(dpi)} dpi"})
            elif dpi < PRINT_DPI:
                warn.append({"page": i, "check": "raster below print target", "detail": f"{round(dpi)} dpi"})
            if x0 < -1 or y0 < -1 or x1 > W + 1 or y1 > H + 1:
                hard.append({"page": i, "check": "picture clipped by the page edge", "detail": str(info["bbox"])})
        # text on text: two separate text runs drawn over each other (e.g. labels
        # from neighbouring columns colliding) — measured on spans, a hard defect
        spans = [s for b in page.get_text("dict")["blocks"] for l in b.get("lines", []) for s in l.get("spans", [])
                 if s.get("text", "").strip()]
        for a_i in range(len(spans)):
            for b_i in range(a_i + 1, len(spans)):
                a, b = spans[a_i]["bbox"], spans[b_i]["bbox"]
                ox = min(a[2], b[2]) - max(a[0], b[0])
                oy = min(a[3], b[3]) - max(a[1], b[1])
                same = spans[a_i]["text"] == spans[b_i]["text"] and max(abs(u - v) for u, v in zip(a, b)) < 2
                if ox > 2 and oy > 3 and not same:   # a stroke drawn over its own fill is one glyph run
                    hard.append({"page": i, "check": "text overlaps text",
                                 "detail": f"{spans[a_i]['text'][:30]!r} / {spans[b_i]['text'][:30]!r}"})
        for tb in text_boxes:
            for im in imgs:
                bx = im["bbox"]
                ox = min(tb[2], bx[2]) - max(tb[0], bx[0])
                oy = min(tb[3], bx[3]) - max(tb[1], bx[1])
                if ox > 4 and oy > 4:
                    hard.append({"page": i, "check": "text overlaps a picture", "detail": f"text {tuple(round(v) for v in tb)}"})
        # unused area: share of the page's content area with no ink (a signal)
        pix = page.get_pixmap(dpi=36, colorspace=fitz.csGRAY)
        rows = [pix.samples[r * pix.stride:r * pix.stride + pix.width] for r in range(pix.height)]
        top, bottom = int(pix.height * 0.07), int(pix.height * 0.93)
        # 'no ink' is measured against the page's own paper (ivory, tinted bands), not white
        from collections import Counter
        paper = Counter(v for r in rows[top:bottom] for v in r).most_common(1)[0][0]
        blank = sum(1 for r in rows[top:bottom] if min(r) >= paper - 10) / max(1, bottom - top)
        pages.append({"page": i, "unused_share": round(blank, 2), "images": imgs, "text_blocks": len(text_boxes)})
    return {"pages": pages, "hard": hard, "warnings": warn, "page_count": len(doc)}


def report_html(diag: dict) -> str:
    rows = "".join(f"<tr><td>{p['page']}</td><td>{int(p['unused_share'] * 100)}%</td>"
                   f"<td>{', '.join(str(im['dpi']) + ' dpi' for im in p['images']) or '—'}</td></tr>"
                   for p in diag["pages"])
    hard = "".join(f"<li>p{h['page']}: {h['check']} — {h['detail']}</li>" for h in diag["hard"]) or "<li>none</li>"
    warn = "".join(f"<li>p{h['page']}: {h['check']} — {h['detail']}</li>" for h in diag["warnings"]) or "<li>none</li>"
    return (f"<h2>Page diagnostics (signals, not rules)</h2><p>{diag['page_count']} pages. Hard defects:</p><ul>{hard}</ul>"
            f"<p>Warnings:</p><ul>{warn}</ul><table><tr><th>page</th><th>unused area (signal)</th>"
            f"<th>raster pictures at printed size</th></tr>{rows}</table>")
