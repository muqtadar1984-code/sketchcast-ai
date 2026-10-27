"""The KDP paperback edition (founder 2026-10-27, plan W1). KDP prints the interior with bleed on the top, bottom
and OUTSIDE edge only, takes the cover as a separate wrap (back + spine + front), and has no API, so the founder
uploads by hand from the sheet this writes.

  interior   pages are laid out like print with KDP's bleed on all four sides, then the spine-side strip is cut
             off: the left strip of a right-hand page, the right strip of a left-hand page (page 1 is right-hand)
  cover      the book's own cover page as the front panel, a spine with the title, and a back panel with the
             description; the barcode area stays clear
  sheet      every KDP field in the order KDP asks for it, with the measurements, and what still blocks upload

Book-specific text comes from the book's kdp.json; nothing subject-specific lives here.
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

MM = 72 / 25.4
BARCODE_IN = (2.0, 1.2)        # KDP prints its barcode in a white box this size at the bottom of the back cover
SPINE_TEXT_MIN_PAGES = 79      # KDP: no spine text below this page count
FRONT_BAND_MM = 92             # the front cover's dark band (book.py), mirrored on the back


def cut_spine_bleed(src: Path, dest: Path, bleed_mm: float, trim_mm: tuple[float, float] = (210, 297)) -> dict:
    """Remove the spine-side bleed from every page and declare trim and bleed boxes. Every page comes out at
    exactly KDP's size (trim + bleed on three sides): the browser rounds printed page sizes by about 0.1 mm, and
    KDP checks the size, so each page is fitted to the exact size (a stretch of well under 0.1 %). Text and
    vectors are copied, not rasterised."""
    import fitz
    b = bleed_mm * MM
    w, h = (trim_mm[0] + bleed_mm) * MM, (trim_mm[1] + 2 * bleed_mm) * MM
    doc, out = fitz.open(src), fitz.open()
    for i, pg in enumerate(doc):
        W, H = pg.rect.width, pg.rect.height
        right_hand = i % 2 == 0
        clip = fitz.Rect(b, 0, W, H) if right_hand else fitz.Rect(0, 0, W - b, H)
        new = out.new_page(width=w, height=h)
        new.show_pdf_page(new.rect, doc, i, clip=clip, keep_proportion=False)
        m = new.mediabox          # the trim sits inside the page's own box (no float drift past its edge)
        new.set_trimbox(fitz.Rect(m.x0, m.y0 + b, m.x1 - b, m.y1 - b) if right_hand
                        else fitz.Rect(m.x0 + b, m.y0 + b, m.x1, m.y1 - b))
    out.set_toc(doc.get_toc())
    out.set_metadata(doc.metadata)
    out.save(dest, garbage=3, deflate=True)
    first = out[0]
    return {"pages": out.page_count, "page_mm": [round(first.rect.width / MM, 2), round(first.rect.height / MM, 2)],
            "trim_mm": [round(first.trimbox.width / MM, 2), round(first.trimbox.height / MM, 2)]}


def _plain(desc_html: str) -> list[tuple[str, str]]:
    """The description as (kind, text) runs for the back cover: paragraphs and bullet items."""
    runs = []
    for m in re.finditer(r"<(p|li)>(.*?)</\1>", desc_html or "", re.S):
        runs.append((m.group(1), re.sub(r"<[^>]+>", "", m.group(2)).strip()))
    return runs


def cover_wrap(front_cover: Path, dest: Path, pages: int, kdp: dict, theme: dict, page: dict, bleed_mm: float) -> dict:
    """Back + spine + front on one sheet at the trim size, with KDP's bleed all round. The back panel mirrors the
    front's design (paper above, the dark band below); text is set as HTML so every character (dashes, bullets)
    has a glyph."""
    import fitz
    tw, th = page["trim"]["width_mm"], page["trim"]["height_mm"]
    spine = pages * float(kdp["print"]["paper_in_per_page"]) * 25.4
    W, H = 2 * bleed_mm + 2 * tw + spine, 2 * bleed_mm + th
    hexrgb = lambda h: tuple(int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))
    dark, deep = hexrgb(theme["brand_dark"]), hexrgb(page["paper_deep"])
    e = html.escape
    doc = fitz.open()
    pg = doc.new_page(width=W * MM, height=H * MM)
    back = fitz.Rect(0, 0, (bleed_mm + tw) * MM, H * MM)
    spine_r = fitz.Rect(back.x1, 0, back.x1 + spine * MM, H * MM)
    band_h = (FRONT_BAND_MM + bleed_mm) * MM
    pg.draw_rect(back, color=None, fill=deep)
    pg.draw_rect(fitz.Rect(back.x0, back.y1 - band_h, back.x1, back.y1), color=None, fill=dark)
    # front panel: the book's own cover page, whose spine-side bleed runs under the spine
    src = fitz.open(front_cover)
    cw, ch = src[0].rect.width, src[0].rect.height
    pg.show_pdf_page(fitz.Rect(spine_r.x1 - bleed_mm * MM, 0, spine_r.x1 - bleed_mm * MM + cw, ch), src, 0)
    pg.draw_rect(spine_r, color=None, fill=dark)
    css = (f"* {{ font-family: sans-serif; color: {theme['ink'] if 'ink' in theme else '#1f2933'}; }} "
           f"h1 {{ font-family: serif; font-size: 21pt; color: {theme['brand_dark']}; margin: 0 0 10pt; }} "
           "p, li { font-size: 10.5pt; line-height: 1.4; margin: 0 0 7pt; } ul { margin: 0 0 8pt; padding-left: 14pt; } "
           ".light, .light * { color: " + page["paper"] + "; } .brand { font-family: serif; font-weight: bold; font-size: 14pt; }")
    if pages >= SPINE_TEXT_MIN_PAGES:
        clear = 0.0625 * 72                       # KDP: keep spine text this far from each fold
        box = fitz.Rect(spine_r.x0 + clear, (bleed_mm + 14) * MM, spine_r.x1 - clear, (H - bleed_mm - 14) * MM)
        spine_html = (f"<p class='light' style='text-align:center;font-size:{min(11.0, box.width * 0.55):.1f}pt;"
                      f"font-weight:bold;margin:0'>{e(kdp['title'])} &#160;&#183;&#160; {e(kdp['author'])}</p>")
        pg.insert_htmlbox(box, spine_html, css=css, rotate=270, scale_low=1)
    # back panel: title and description above the band, inside a 15 mm safe margin from the trim
    m = 15
    x0, x1 = (bleed_mm + m) * MM, (bleed_mm + tw - m) * MM
    text = fitz.Rect(x0, (bleed_mm + m + 4) * MM, x1, back.y1 - band_h - 8 * MM)
    spare, _ = pg.insert_htmlbox(text, f"<h1>{e(kdp['title'])}</h1>{kdp.get('description_html') or ''}",
                                 css=css, scale_low=1)
    if spare < 0:
        raise ValueError("the back-cover description does not fit above the band: shorten it in kdp.json")
    bw, bh = BARCODE_IN[0] * 72, BARCODE_IN[1] * 72
    barcode = fitz.Rect(back.x1 - (bleed_mm + m) * MM - bw, (H - bleed_mm - m) * MM - bh,
                        back.x1 - (bleed_mm + m) * MM, (H - bleed_mm - m) * MM)
    pg.insert_htmlbox(fitz.Rect(x0, barcode.y0, barcode.x0 - 12, barcode.y1),
                      f"<p class='light brand'>{e(kdp['author'])}</p>"
                      + (f"<p class='light'>{e(kdp['portal_url'])}</p>" if kdp.get("portal_url") else ""), css=css)
    doc.set_metadata({"title": f"{kdp['title']} - cover", "author": kdp["author"]})
    doc.save(dest, garbage=3, deflate=True)
    return {"cover_mm": [round(W, 2), round(H, 2)], "spine_mm": round(spine, 2), "spine_in": round(spine / 25.4, 4),
            "barcode_box_in": list(BARCODE_IN)}


def blockers(kdp: dict) -> list[str]:
    """What still stops this edition from being uploaded (the build calls it a PROOF until the list is empty)."""
    out = []
    for key, why in (("isbn", "no ISBN yet (P4)"), ("edition_code", "no book code yet (P1, from the SK access work)"),
                     ("price_usd", "no list price yet (P3)"),
                     ("portal_url", "no portal address yet (from the portal work, W4)")):
        if not kdp.get(key):
            out.append(why)
    return out


def sheet(kdp: dict, facts: dict, dest: Path) -> Path:
    """kdp-sheet.html: one row per KDP field, in KDP's order, with a copy button."""
    e = html.escape
    p = kdp["print"]
    rows = [
        ("Language", kdp["language"]), ("Book title", kdp["title"]), ("Subtitle", kdp["subtitle"]),
        ("Series", kdp.get("series") or ""), ("Edition number", "1"), ("Author", kdp["author"]),
        ("Contributors", kdp.get("contributors") or ""), ("Description (HTML)", kdp["description_html"]),
        ("Publishing rights", "I own the copyright and I hold the necessary publishing rights."),
        ("Keywords", "\n".join(kdp["keywords"])), ("Categories", "\n".join(kdp["categories"])),
        ("Reading age", kdp["reading_age"]), ("ISBN", kdp.get("isbn") or "(missing)"),
        ("Imprint", kdp.get("publisher") or ""),
        ("AI-generated content: text", kdp["ai_disclosure"]["text"]),
        ("AI-generated content: images", kdp["ai_disclosure"]["images"]),
        ("AI-generated content: translation", kdp["ai_disclosure"]["translation"]),
        ("Ink and paper", f"{p['ink']}, {p['paper']} paper"), ("Trim size", p["trim"]),
        ("Bleed", "Bleed (PDF only)" if p["bleed"] else "No bleed"), ("Cover finish", p["cover_finish"]),
        ("Manuscript", facts["interior_file"]), ("Cover", facts["cover_file"]),
        ("List price (USD)", str(kdp.get("price_usd") or "(set from KDP's printing-cost calculator)")),
    ]
    trs = "".join(f"<tr><th>{e(k)}</th><td><pre>{e(v)}</pre></td><td><button onclick=\"navigator.clipboard."
                  f"writeText(this.parentNode.previousSibling.innerText)\">Copy</button></td></tr>" for k, v in rows)
    block = facts["blockers"]
    status = ("<p class=ok>Ready to upload.</p>" if not block else
              "<p class=no><b>PROOF, not for upload:</b> " + e("; ".join(block)) + "</p>")
    meas = "".join(f"<li>{e(k)}: {e(json.dumps(v))}</li>" for k, v in facts["measurements"].items())
    dest.write_text(
        "<!doctype html><meta charset=utf-8><title>KDP sheet</title><style>body{font:14px Segoe UI,Arial;margin:24px}"
        "table{border-collapse:collapse;width:100%}th{text-align:left;width:22%;vertical-align:top;padding:6px}"
        "td{padding:6px;border-bottom:1px solid #eee}pre{white-space:pre-wrap;margin:0;font:13px Consolas,monospace}"
        ".ok{color:#1d6b3a}.no{color:#a12d1d}</style>"
        f"<h1>KDP sheet — {e(kdp['title'])}</h1>{status}<h2>Measurements</h2><ul>{meas}</ul>"
        "<h2>Fields, in KDP's order</h2><table>" + trs + "</table>"
        "<h2>Checklist</h2><ol><li>Upload the manuscript and cover; KDP previewer passes with no errors</li>"
        "<li>Order a proof copy and check it on paper: gutter, colours, labels, the book code page</li>"
        "<li>Publish; record the ASIN</li><li>Check the live product page</li></ol>", encoding="utf-8")
    return dest
