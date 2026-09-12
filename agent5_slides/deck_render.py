"""A storyboard becomes a .pptx in which every word is a PowerPoint object.

The editability rule is absolute here and it is worth stating as a rule rather
than an aspiration: **the only raster this module ever places is a figure's
artwork.** Headings, body prose, lists, tables, labels, questions, answers,
callouts and notes are all real shapes. If a future slide kind cannot be built
that way it does not get built.

Layout is deliberately plain. The founder's complaint was that the deck says
nothing a teacher could not have typed faster themselves; the fix for that is
the CONTENT the storyboard now carries — objectives, misconceptions, worked
examples, labelled diagrams, a glossary — not decoration around the old
bullets. Art direction can come later and will not disturb any of this.
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import annotated_figure as af
from . import deck_storyboard as sb
from . import metrics as mx
from shared.lesson_model import part_boxes as _part_boxes
from .theme import FAINT, GRAPHITE, INK, LINE, MIST, TEAL_DK, TEAL_MIST, WHITE

logger = logging.getLogger(__name__)

# The accent, settable per build: a school that uploaded a template has a
# colour, and every teal dot, band and table header on the deck takes it.
_ACCENT = {"rgb": TEAL_DK, "mist": TEAL_MIST, "templated": False, "logo": None, "rtl": False}


def _acc():
    return _rgb(_ACCENT["rgb"])


def _acc_mist():
    return _rgb(_ACCENT["mist"])


def set_branding(branding: dict | None) -> None:
    b = branding or {}
    rgb = b.get("accent_rgb")
    if isinstance(rgb, (list, tuple)) and len(rgb) == 3:
        r, g, bl = (int(max(0, min(255, v))) for v in rgb)
        _ACCENT["rgb"] = (r, g, bl)
        # The tint is the accent blended 85% towards white — legible under
        # 20pt bold ink whatever the school's colour is.
        _ACCENT["mist"] = tuple(int(255 - (255 - c) * 0.15) for c in (r, g, bl))
    else:
        _ACCENT["rgb"], _ACCENT["mist"] = TEAL_DK, TEAL_MIST
    _ACCENT["logo"] = b.get("logo_path") if b.get("logo_path") and Path(str(b.get("logo_path"))).exists() else None
    _ACCENT["templated"] = False

IN = af.EMU_IN
# Geometry lives in `metrics`, in inches, because the STORYBOARD paginates
# against the same numbers. Redefining any of them here reopens the gap that
# put nine bullets on a slide with room for six.
MARGIN = mx.MARGIN_IN * IN
BODY_TOP = mx.BODY_TOP_IN * IN
BODY_BOTTOM = mx.BODY_BOTTOM_IN * IN
CONTENT_W = mx.CONTENT_W_IN * IN


def _rgb(t):
    from pptx.dml.color import RGBColor
    return RGBColor(*t)


def _slide(prs, bg=WHITE):
    layouts = prs.slide_layouts
    s = prs.slides.add_slide(layouts[6] if len(layouts) > 6 else layouts[-1])
    # On a school template the content slides keep the template's own
    # background — that is what the school uploaded it for. Our dark title
    # and closing slides are ours either way.
    if not (_ACCENT["templated"] and bg == WHITE):
        f = s.background.fill
        f.solid()
        f.fore_color.rgb = _rgb(bg)
    if bg == WHITE and _ACCENT["logo"]:
        try:
            s.shapes.add_picture(str(_ACCENT["logo"]), int(11.95 * IN), int(6.85 * IN), height=int(0.45 * IN))
        except Exception as exc:  # noqa: BLE001 — a logo is decoration
            logger.warning("logo not placed: %s", exc)
    return s


def _chrome(s, sl):
    """Kicker + heading + the rule under them. Every content slide has this."""
    y = 0.42 * IN
    if sl.kicker:
        af._textbox(s, MARGIN, y, CONTENT_W, 0.26 * IN,
                    sl.kicker.upper(), 11, _acc(), bold=True)
        y += 0.30 * IN
    head = sl.heading
    af._textbox(s, MARGIN, y, CONTENT_W, 0.55 * IN,
                head[:120], 26 if mx.text_width_em(head) < 30 else 21, _rgb(INK), bold=True)


def _height(text: str, w: float, pt: float) -> float:
    """EMU wrapper over the shared inch metric — never a second estimate."""
    return mx.text_height_in(text, w / IN, pt) * IN


def _para(s, x, y, w, text, pt=mx.BODY_PT, color=GRAPHITE):
    h = _height(text, w, pt)
    af._textbox(s, x, y, w, h, text, pt, _rgb(color))
    return y + h + mx.PARA_GAP_IN * IN


def _bullets(s, x, y, w, items, pt=mx.LIST_PT):
    """A real list stays a list. The complaint was that EVERYTHING was bullets,
    not that bullets are wrong — a list in the article's own prose is one."""
    from pptx.enum.shapes import MSO_SHAPE
    for it in items:
        h = _height(it, w - mx.BULLET_INDENT_IN * IN, pt)
        d = 0.075 * IN
        o = s.shapes.add_shape(MSO_SHAPE.OVAL, int(x + 0.05 * IN),
                               int(y + pt * 0.42 * (IN / 72)), int(d), int(d))
        o.fill.solid()
        o.fill.fore_color.rgb = _acc()
        o.line.fill.background()
        o.shadow.inherit = False
        af._textbox(s, x + mx.BULLET_INDENT_IN * IN, y,
                    w - mx.BULLET_INDENT_IN * IN, h, it, pt, _rgb(INK))
        y += h + mx.LIST_GAP_IN * IN
    return y + mx.LIST_TAIL_IN * IN


def _table(s, x, y, w, header, rows, pt=mx.TABLE_PT, head_pt=mx.TABLE_HEAD_PT,
           first_col=0.34):
    """A NATIVE PowerPoint table.

    The single biggest editability win in a body: a table is exactly the thing
    a teacher retypes for their own class, and a picture of one is useless to
    them. python-pptx gives real cells, so there is no reason to draw it.
    """
    from pptx.util import Pt

    n = len(rows) + 1
    cols_in = mx.table_col_widths_in(len(header), w / IN, first_col)
    heights = [mx.table_row_height_in(header, cols_in, head_pt)] + \
              [mx.table_row_height_in(r, cols_in, pt) for r in rows]
    total = sum(heights) * IN
    shape = s.shapes.add_table(n, len(header), int(x), int(y), int(w), int(total))
    tbl = shape.table
    for c, cw in enumerate(cols_in):
        tbl.columns[c].width = int(cw * IN)
    # Row heights are SET to the measured value rather than left for
    # PowerPoint to grow on open, so what the overflow check measured is
    # what the file says.
    for i, h in enumerate(heights):
        tbl.rows[i].height = int(h * IN)
    for c, text in enumerate(header):
        cell = tbl.cell(0, c)
        cell.text = ""
        p = cell.text_frame.paragraphs[0]
        r = p.add_run()
        r.text = str(text)
        r.font.size = Pt(head_pt)
        r.font.bold = True
        r.font.name = "Calibri"
        r.font.color.rgb = _rgb(WHITE)
        cell.fill.solid()
        cell.fill.fore_color.rgb = _acc()
    for i, row in enumerate(rows, 1):
        for c in range(len(header)):
            cell = tbl.cell(i, c)
            cell.text = ""
            p = cell.text_frame.paragraphs[0]
            r = p.add_run()
            r.text = str(row[c]) if c < len(row) else ""
            r.font.size = Pt(pt)
            r.font.bold = (c == 0 and len(header) == 2)
            r.font.name = "Calibri"
            r.font.color.rgb = _rgb(INK)
            cell.fill.solid()
            cell.fill.fore_color.rgb = _rgb(WHITE if i % 2 else MIST)
    return y + total + mx.TABLE_GAP_IN * IN


def _blocks(s, blocks, x=MARGIN, y=BODY_TOP, w=None):
    w = CONTENT_W if w is None else w
    for b in blocks:
        if b["kind"] == "heading":
            af._textbox(s, x, y, w, 0.32 * IN, b["text"], mx.HEADING_PT,
                        _acc(), bold=True)
            y += mx.HEADING_H_IN * IN
        elif b["kind"] == "para":
            y = _para(s, x, y, w, b["text"])
        elif b["kind"] == "list":
            y = _bullets(s, x, y, w, b["items"])
        elif b["kind"] == "table":
            y = _table(s, x, y, w, b["header"], b["rows"])
    return y


# ── one function per slide kind ───────────────────────────────────────

def _title(prs, sl):
    s = _slide(prs, INK)
    af._textbox(s, 1.0 * IN, 2.35 * IN, 11.3 * IN, 1.0 * IN,
                sl.heading, 44, _rgb(WHITE), bold=True)
    if sl.subtitle:
        af._textbox(s, 1.0 * IN, 3.45 * IN, 11.3 * IN, 0.4 * IN,
                    sl.subtitle, 18, _acc())
    af._textbox(s, 1.0 * IN, 6.7 * IN, 11.3 * IN, 0.3 * IN,
                "SketchCast AI", 11, _rgb(FAINT))
    return s


def _objectives(prs, sl):
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
    from pptx.util import Pt

    s = _slide(prs)
    _chrome(s, sl)
    y = BODY_TOP + 0.15 * IN
    for i, text in enumerate(sl.items, 1):
        d = 0.42 * IN
        o = s.shapes.add_shape(MSO_SHAPE.OVAL, int(MARGIN), int(y), int(d), int(d))
        o.fill.solid()
        o.fill.fore_color.rgb = _acc_mist()
        o.line.fill.background()
        o.shadow.inherit = False
        tf = o.text_frame
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
        tf.vertical_anchor = MSO_ANCHOR.MIDDLE
        p = tf.paragraphs[0]
        p.alignment = PP_ALIGN.CENTER
        r = p.add_run()
        r.text = str(i)
        r.font.size = Pt(14)
        r.font.bold = True
        r.font.name = "Calibri"
        r.font.color.rgb = _acc()
        h = _height(text, CONTENT_W - 0.75 * IN, 17)
        af._textbox(s, MARGIN + 0.68 * IN, y + 0.04 * IN,
                    CONTENT_W - 0.75 * IN, h, text, 17, _rgb(INK))
        y += max(h, 0.5 * IN) + 0.20 * IN
    return s


def _key_idea(s, text, y=BODY_TOP):
    """The section's own claim, across the top of the body, in a tinted band.

    A slide needs something to SAY before it has things to add. The article
    already writes one precise sentence per section for the question
    generator; putting it here costs nothing and turns a page of body copy
    into a slide with a point.
    """
    from pptx.enum.shapes import MSO_SHAPE

    inner = CONTENT_W - 0.6 * IN
    th = _height(text, inner, mx.KEY_IDEA_PT)
    box = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, int(MARGIN), int(y),
                             int(CONTENT_W), int(th + mx.KEY_IDEA_PAD_IN * IN))
    box.fill.solid()
    box.fill.fore_color.rgb = _acc_mist()
    box.line.fill.background()
    box.shadow.inherit = False
    box.text_frame.text = ""
    # Anchored MIDDLE: the height estimate is deliberately generous (a label
    # that overflows its box is worse than one with room to spare), so the
    # slack has to sit evenly above and below rather than all underneath.
    from pptx.enum.text import MSO_ANCHOR
    af._textbox(s, MARGIN + 0.3 * IN, y + 0.14 * IN, inner, th + 0.14 * IN,
                text, mx.KEY_IDEA_PT, _rgb(INK), bold=True, anchor=MSO_ANCHOR.MIDDLE)
    return y + th + (mx.KEY_IDEA_PAD_IN + mx.KEY_IDEA_GAP_IN) * IN


def _section(prs, sl):
    s = _slide(prs)
    _chrome(s, sl)
    y = _key_idea(s, sl.key_idea) if sl.key_idea else BODY_TOP
    w = (sl.body_w_in or mx.CONTENT_W_IN) * IN
    if sl.illustration is not None:
        # The figure beside its own prose, UNLABELLED and small: the labelled
        # version gets a slide of its own a moment later, and repeating the
        # labels here would just be the same slide twice at two sizes.
        fw = CONTENT_W - w - 0.35 * IN
        af._fit(sl.illustration.w, sl.illustration.h, (0, 0, fw, BODY_BOTTOM - y))
        px, py, pw, ph = af._fit(sl.illustration.w, sl.illustration.h,
                                 (MARGIN + w + 0.35 * IN, y, fw, BODY_BOTTOM - y))
        s.shapes.add_picture(str(sl.illustration.png), int(px), int(py),
                             int(pw), int(ph))
    end = _blocks(s, sl.blocks, y=y, w=w)
    return s, _overflow(end, sl)


def _focus(prs, sl):
    """One part of a figure, cropped from the SAME artwork, with the article's
    own words beside it. No second image was generated for this."""
    s = _slide(prs)
    _chrome(s, sl)
    fig = sl.figure
    boxes = [b for p in sl.parts for b in _part_boxes(fig.regions, p)]
    region = (min(b[0] for b in boxes), min(b[1] for b in boxes),
              max(b[2] for b in boxes), max(b[3] for b in boxes))
    af.add_focus_view(s, fig.png, region, fig.w, fig.h,
                      frame=(MARGIN, BODY_TOP, 5.9 * IN, BODY_BOTTOM - BODY_TOP))
    end = _blocks(s, sl.blocks, x=MARGIN + 6.3 * IN, y=BODY_TOP + 0.1 * IN,
                  w=CONTENT_W - 6.3 * IN)
    if sl.subtitle:
        af._textbox(s, MARGIN, 6.72 * IN, CONTENT_W, 0.3 * IN,
                    sl.subtitle, 11, _rgb(FAINT))
    return s, _overflow(end, sl)


def _compare(prs, sl):
    """Two figures side by side, with the difference the DATA states."""
    from pptx.enum.text import PP_ALIGN

    s = _slide(prs)
    _chrome(s, sl)
    top, h = BODY_TOP + 0.30 * IN, 4.05 * IN
    for fig, left in ((sl.figure, MARGIN), (sl.figure_b, 6.90 * IN)):
        x, y, w, hh = af._fit(fig.w, fig.h, (left, top, 5.70 * IN, h))
        s.shapes.add_picture(str(fig.png), int(x), int(y), int(w), int(hh))
        af._textbox(s, left, BODY_TOP - 0.02 * IN, 5.70 * IN, 0.3 * IN,
                    (fig.caption or fig.key)[:60], mx.CAPTION_PT, _rgb(GRAPHITE),
                    bold=True, align=PP_ALIGN.CENTER)
    y = top + h + 0.18 * IN
    if sl.items:
        af._textbox(s, MARGIN, y, CONTENT_W, 0.34 * IN,
                    (sl.label or "Only the second has:") + "  " + ",  ".join(sl.items[:8]),
                    15, _acc(), bold=True)
        y += 0.36 * IN
    if sl.subtitle:
        af._textbox(s, MARGIN, y, CONTENT_W, 0.30 * IN, sl.subtitle, 11, _rgb(FAINT))
    return s, []


def _overflow(end_y: float, sl) -> list[str]:
    """Did the body run off the slide?

    The storyboard paginates against `metrics` and this draws against
    `metrics`, so in principle they cannot disagree — which is exactly why
    the check is here. A silent overflow is the one deck fault that survives
    every other guard: the .pptx opens, the shapes are valid, the text is
    editable, and the last two bullets are simply below the bottom edge where
    nobody sees them until a class does.
    """
    if end_y <= BODY_BOTTOM + 0.06 * IN:
        return []
    over = (end_y - BODY_BOTTOM) / IN
    return [f"body overflows the slide by {over:.2f}in ({sl.heading[:40]!r})"]


def _diagram(prs, sl, label_pt=mx.LABEL_PT):
    s = _slide(prs)
    _chrome(s, sl)
    fig = sl.figure
    geo = af.figure_geometry(sl.parts, fig.regions, fig.w, fig.h)
    rep = af.add_annotated_figure(
        s, fig.png, geo, fig.w, fig.h,
        frame=(3.45 * IN, BODY_TOP, 6.45 * IN, BODY_BOTTOM - BODY_TOP),
        label_pt=label_pt)
    faults = af.validate(rep, label_pt=label_pt)
    if sl.subtitle:
        af._textbox(s, MARGIN, 6.72 * IN, CONTENT_W, 0.3 * IN,
                    sl.subtitle, 11, _rgb(FAINT))
    return s, faults


def _check(prs, sl, label_pt=14.0):
    s = _slide(prs)
    _chrome(s, sl)
    fig = sl.figure
    geo = af.figure_geometry(sl.parts, fig.regions, fig.w, fig.h)
    for n, g in enumerate(geo, 1):
        g["n"] = n
    rep = af.add_annotated_figure(
        s, fig.png, geo, fig.w, fig.h,
        frame=(1.6 * IN, BODY_TOP, 5.2 * IN, BODY_BOTTOM - BODY_TOP),
        label_pt=label_pt, label_text=af.numbers_only, gutter=0.42 * IN)
    faults = af.validate(rep, label_pt=label_pt)
    y = BODY_TOP + 0.25 * IN
    for n in range(1, len(geo) + 1):
        af._textbox(s, 7.9 * IN, y, 4.8 * IN, 0.34 * IN,
                    f"{n}.  ______________________", 16, _rgb(INK))
        y += 0.58 * IN
    return s, faults


def _misconceptions(prs, sl):
    s = _slide(prs)
    _chrome(s, sl)
    header = sl.label.split("|") if sl.label else ["Learners often think", "In fact"]
    end = _table(s, MARGIN, BODY_TOP + 0.1 * IN, CONTENT_W, header,
                 [[a, b] for a, b in sl.items], first_col=0.40)
    return s, _overflow(end, sl)


def _worked(prs, sl):
    s = _slide(prs)
    _chrome(s, sl)
    y = _key_idea(s, sl.key_idea) if sl.key_idea else BODY_TOP + 0.1 * IN
    end = _blocks(s, sl.blocks, y=y)
    return s, _overflow(end, sl)


def _glossary(prs, sl):
    s = _slide(prs)
    _chrome(s, sl)
    header = sl.label.split("|") if sl.label else ["Term", "Meaning"]
    end = _table(s, MARGIN, BODY_TOP + 0.1 * IN, CONTENT_W, header,
                 [[a, b] for a, b in sl.items], first_col=0.26)
    return s, _overflow(end, sl)


def _closing(prs, sl):
    s = _slide(prs, INK)
    af._textbox(s, 1.0 * IN, 2.45 * IN, 11.3 * IN, 0.8 * IN,
                sl.heading, 34, _rgb(WHITE), bold=True)          # "Ready to teach.", localised
    af._textbox(s, 1.0 * IN, 3.35 * IN, 11.3 * IN, 0.4 * IN,
                sl.subtitle, 18, _acc())                          # the lesson title
    af._textbox(s, 1.0 * IN, 3.95 * IN, 11.3 * IN, 0.4 * IN,
                sl.label, 13, _rgb(FAINT))
    return s


def _quiz(prs, sl):
    """A comprehension check: the question as the heading, the options as
    lettered native boxes, the answer in the speaker notes only — the
    projected slide must not give it away."""
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import MSO_ANCHOR

    s = _slide(prs)
    _chrome(s, sl)
    y = BODY_TOP + 0.25 * IN
    for i, opt in enumerate(sl.items[:4]):
        letter = "ABCD"[i]
        h = max(0.62 * IN, _height(opt, CONTENT_W - 1.2 * IN, 18) + 0.3 * IN)
        box = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, int(MARGIN), int(y), int(CONTENT_W), int(h))
        box.fill.solid()
        box.fill.fore_color.rgb = _rgb(MIST)
        box.line.color.rgb = _rgb(LINE)
        box.shadow.inherit = False
        box.text_frame.text = ""
        af._textbox(s, MARGIN + 0.25 * IN, y, 0.6 * IN, h, letter, 18, _acc(), bold=True,
                    anchor=MSO_ANCHOR.MIDDLE)
        af._textbox(s, MARGIN + 0.9 * IN, y, CONTENT_W - 1.2 * IN, h, opt, 18, _rgb(INK),
                    anchor=MSO_ANCHOR.MIDDLE)
        y += h + 0.18 * IN
    return s, _overflow(y, sl)


def _arrow(s, x1, y1, x2, y2, elbow=False):
    """A connector with an arrowhead. python-pptx has no arrowhead API; the
    OOXML `a:tailEnd` on the line is what PowerPoint reads."""
    from pptx.enum.shapes import MSO_CONNECTOR
    from pptx.oxml.ns import qn
    from pptx.util import Pt

    c = s.shapes.add_connector(MSO_CONNECTOR.ELBOW if elbow else MSO_CONNECTOR.STRAIGHT,
                               int(x1), int(y1), int(x2), int(y2))
    c.line.color.rgb = _rgb(GRAPHITE)
    c.line.width = Pt(1.5)
    ln = c.line._get_or_add_ln()  # noqa: SLF001
    tail = ln.makeelement(qn("a:tailEnd"), {"type": "triangle", "w": "med", "len": "med"})
    ln.append(tail)
    return c


def _node(s, x, y, w, h, text, pt=16):
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN

    box = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, int(x), int(y), int(w), int(h))
    box.fill.solid()
    box.fill.fore_color.rgb = _acc_mist()
    box.line.color.rgb = _acc()
    box.shadow.inherit = False
    box.text_frame.text = ""
    af._textbox(s, x + 0.08 * IN, y, w - 0.16 * IN, h, text, pt, _rgb(INK), bold=True,
                align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
    return box


def _shapes(prs, sl):
    """flow / cycle / hierarchy as NATIVE shapes and connectors.

    The legacy renderer drew these with PIL into the slide PNG; the labels
    are instructional text, so here every node is a shape a teacher can
    retype and every arrow a connector they can drag.
    """
    import math

    s = _slide(prs)
    _chrome(s, sl)
    kind = (sl.visual or {}).get("kind")
    nodes = list(sl.items)[:5]
    n = len(nodes)
    top, bottom = BODY_TOP + 0.3 * IN, BODY_BOTTOM - (0.5 * IN if sl.subtitle else 0.1 * IN)
    if kind == "flow":
        gap = 0.55 * IN
        w = (CONTENT_W - gap * (n - 1)) / n
        h = min(1.5 * IN, (bottom - top))
        y = top + (bottom - top - h) / 2
        xs = [MARGIN + i * (w + gap) for i in range(n)]
        # A process reads in the direction the script does: in an Arabic or
        # Jawi deck the first step sits at the RIGHT and the arrows point left.
        if _ACCENT["rtl"]:
            xs = list(reversed(xs))
        for x, t in zip(xs, nodes):
            _node(s, x, y, w, h, t)
        for i in range(n - 1):
            if _ACCENT["rtl"]:
                _arrow(s, xs[i], y + h / 2, xs[i + 1] + w, y + h / 2)
            else:
                _arrow(s, xs[i] + w, y + h / 2, xs[i + 1], y + h / 2)
    elif kind == "cycle":
        cx, cy = MARGIN + CONTENT_W / 2, (top + bottom) / 2
        w, h = 2.4 * IN, 1.0 * IN
        r = min((bottom - top) / 2 - h / 2, 3.6 * IN)
        pts = []
        for i, t in enumerate(nodes):
            a = -math.pi / 2 + 2 * math.pi * i / n
            x, y = cx + r * math.cos(a) - w / 2, cy + r * math.sin(a) - h / 2
            _node(s, x, y, w, h, t)
            pts.append((x + w / 2, y + h / 2, a))
        for i in range(n):
            x1, y1, a1 = pts[i]
            x2, y2, a2 = pts[(i + 1) % n]
            # leave the boxes: start/end on the ring, just outside each box
            ox1, oy1 = cx + (r + 0.05 * IN) * math.cos(a1 + 0.35), cy + (r + 0.05 * IN) * math.sin(a1 + 0.35)
            ox2, oy2 = cx + (r + 0.05 * IN) * math.cos(a2 - 0.35), cy + (r + 0.05 * IN) * math.sin(a2 - 0.35)
            _arrow(s, ox1, oy1, ox2, oy2)
    else:  # hierarchy: root above, children in a row below
        root, kids = nodes[0], nodes[1:]
        rw, rh = 3.4 * IN, 1.0 * IN
        rx, ry = MARGIN + (CONTENT_W - rw) / 2, top
        _node(s, rx, ry, rw, rh, root)
        if kids:
            gap = 0.4 * IN
            kw = min(3.0 * IN, (CONTENT_W - gap * (len(kids) - 1)) / len(kids))
            total = kw * len(kids) + gap * (len(kids) - 1)
            kx0 = MARGIN + (CONTENT_W - total) / 2
            ky = ry + rh + 1.3 * IN
            for i, t in enumerate(kids):
                kx = kx0 + i * (kw + gap)
                _node(s, kx, ky, kw, rh, t)
                _arrow(s, rx + rw / 2, ry + rh, kx + kw / 2, ky, elbow=True)
    if sl.subtitle:
        af._textbox(s, MARGIN, BODY_BOTTOM - 0.3 * IN, CONTENT_W, 0.3 * IN, sl.subtitle, 13, _rgb(GRAPHITE))
    return s, []


def _icons(prs, sl):
    """Icon tiles: the glyph is a small raster (illustrative artwork is
    allowed to be), the label under it is native text."""
    import tempfile

    from PIL import Image, ImageDraw
    from pptx.enum.text import PP_ALIGN

    from .diagram_builder import draw_icon

    s = _slide(prs)
    _chrome(s, sl)
    items = list(sl.items)[:6]
    n = len(items)
    cols = n if n <= 3 else 3
    rows = 1 if n <= 3 else 2
    tw = CONTENT_W / cols
    th = (BODY_BOTTOM - BODY_TOP - (0.4 * IN if sl.subtitle else 0)) / rows
    size = int(min(1.6 * IN, th * 0.5))
    tmp = Path(tempfile.mkdtemp(prefix="deck_icons_"))
    for i, (icon, label) in enumerate(items):
        cx = MARGIN + (i % cols) * tw + tw / 2
        ty = BODY_TOP + (i // cols) * th + 0.15 * IN
        img = Image.new("RGB", (256, 256), (255, 255, 255))
        d = ImageDraw.Draw(img)
        d.ellipse([8, 8, 248, 248], fill=_ACCENT["mist"])
        draw_icon(d, icon, 128, 128, 128, accent=_ACCENT["rgb"])
        png = tmp / f"icon_{i}.png"
        img.save(str(png))
        s.shapes.add_picture(str(png), int(cx - size / 2), int(ty), int(size), int(size))
        af._textbox(s, cx - tw / 2 + 0.1 * IN, ty + size + 0.15 * IN, tw - 0.2 * IN, 0.7 * IN,
                    label, 16, _rgb(INK), bold=True, align=PP_ALIGN.CENTER)
    if sl.subtitle:
        af._textbox(s, MARGIN, BODY_BOTTOM - 0.3 * IN, CONTENT_W, 0.3 * IN, sl.subtitle, 13, _rgb(GRAPHITE))
    return s, []


# Kinds whose renderer already returns (slide, faults); the rest return a
# slide and are wrapped at the call site.
_RENDER = {
    sb.TITLE: _title, sb.OBJECTIVES: _objectives, sb.SECTION: _section,
    sb.MISCONCEPTIONS: _misconceptions, sb.WORKED: _worked,
    sb.GLOSSARY: _glossary, sb.CLOSING: _closing,
}
_CHECKED = {sb.SECTION, sb.WORKED, sb.DIAGRAM, sb.CHECK, sb.FOCUS, sb.COMPARE,
            sb.MISCONCEPTIONS, sb.GLOSSARY, sb.QUIZ, sb.SHAPES, sb.ICONS}
_RENDER[sb.QUIZ] = _quiz
_RENDER[sb.SHAPES] = _shapes
_RENDER[sb.ICONS] = _icons
_RENDER[sb.TAKEAWAYS] = _objectives
_RENDER[sb.FOCUS] = _focus
_RENDER[sb.COMPARE] = _compare


def _base(branding: dict | None):
    """The Presentation to build on: the school's template when it is a
    16:9 file, else a blank one.

    A template brings its masters, fonts and backgrounds; our shapes go on
    top. A 4:3 template cannot host this geometry — every inch here assumes
    13.333 x 7.5 — so it falls back to the blank base with the school's
    accent and logo still applied, rather than to the legacy renderer. A
    template's own slides are removed: they are the school's sample deck,
    not this lesson.
    """
    from pptx import Presentation

    tpl = (branding or {}).get("pptx_template")
    if tpl and Path(str(tpl)).exists():
        try:
            prs = Presentation(str(tpl))
            ratio = prs.slide_width / max(1, prs.slide_height)
            if abs(ratio - 16 / 9) < 0.03:
                sldIdLst = prs.slides._sldIdLst  # noqa: SLF001
                for sldId in list(sldIdLst):
                    prs.part.drop_rel(sldId.rId)
                    sldIdLst.remove(sldId)
                prs.slide_width, prs.slide_height = af.SLIDE_W, af.SLIDE_H
                _ACCENT["templated"] = True
                return prs
            logger.warning("school template is %.2f:1, not 16:9 — using the blank base with its colours",
                           ratio)
        except Exception as exc:  # noqa: BLE001 — a bad template must not cost the deck
            logger.warning("school template unusable (%s); using the blank base", exc)
    prs = Presentation()
    prs.slide_width, prs.slide_height = af.SLIDE_W, af.SLIDE_H
    return prs


def build(slides, out_path: str | Path, label_pt: float = mx.LABEL_PT,
          branding: dict | None = None, direction: str = "ltr") -> tuple[Path, list[str]]:
    """Render a storyboard. Returns the path and every geometry fault found.

    Faults are RETURNED rather than raised: the caller decides whether a deck
    with one crowded diagram is worse than no deck, and that is a judgement
    about the artifact, not about the geometry.
    """
    set_branding(branding)
    _ACCENT["rtl"] = direction == "rtl"
    prs = _base(branding)
    faults: list[str] = []
    for i, sl in enumerate(slides, 1):
        try:
            if sl.kind == sb.DIAGRAM:
                s, f = _diagram(prs, sl, label_pt)
            elif sl.kind == sb.CHECK:
                s, f = _check(prs, sl)
            elif sl.kind in _CHECKED:
                s, f = _RENDER[sl.kind](prs, sl)
            else:
                s, f = _RENDER[sl.kind](prs, sl), []
            faults += [f"slide {i} ({sl.kind}): {x}" for x in f]
            if sl.notes:
                s.notes_slide.notes_text_frame.text = sl.notes
        except Exception as exc:          # noqa: BLE001 — one slide must not lose the deck
            logger.warning("deck slide %d (%s) failed: %s", i, sl.kind, exc)
            faults.append(f"slide {i} ({sl.kind}) FAILED: {type(exc).__name__}: {exc}")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(out_path))
    return out_path, faults
