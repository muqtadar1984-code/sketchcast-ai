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
    s = prs.slides.add_slide(prs.slide_layouts[6])
    f = s.background.fill
    f.solid()
    f.fore_color.rgb = _rgb(bg)
    return s


def _chrome(s, sl):
    """Kicker + heading + the rule under them. Every content slide has this."""
    y = 0.42 * IN
    if sl.kicker:
        af._textbox(s, MARGIN, y, CONTENT_W, 0.26 * IN,
                    sl.kicker.upper(), 11, _rgb(TEAL_DK), bold=True)
        y += 0.30 * IN
    head = sl.heading + ("  (continued)" if sl.continued else "")
    af._textbox(s, MARGIN, y, CONTENT_W, 0.55 * IN,
                head[:120], 26 if len(head) < 60 else 21, _rgb(INK), bold=True)


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
        o.fill.fore_color.rgb = _rgb(TEAL_DK)
        o.line.fill.background()
        o.shadow.inherit = False
        af._textbox(s, x + mx.BULLET_INDENT_IN * IN, y,
                    w - mx.BULLET_INDENT_IN * IN, h, it, pt, _rgb(INK))
        y += h + mx.LIST_GAP_IN * IN
    return y + mx.LIST_TAIL_IN * IN


def _table(s, x, y, w, header, rows, pt=mx.TABLE_PT, head_pt=12, first_col=0.34):
    """A NATIVE PowerPoint table.

    The single biggest editability win in a body: a table is exactly the thing
    a teacher retypes for their own class, and a picture of one is useless to
    them. python-pptx gives real cells, so there is no reason to draw it.
    """
    from pptx.util import Pt

    n = len(rows) + 1
    rh = mx.TABLE_ROW_IN * IN
    shape = s.shapes.add_table(n, len(header), int(x), int(y), int(w), int(rh * n))
    tbl = shape.table
    if len(header) == 2:
        tbl.columns[0].width = int(w * first_col)
        tbl.columns[1].width = int(w - w * first_col)
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
        cell.fill.fore_color.rgb = _rgb(TEAL_DK)
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
    return y + rh * n + mx.TABLE_GAP_IN * IN


def _blocks(s, blocks, x=MARGIN, y=BODY_TOP, w=None):
    w = CONTENT_W if w is None else w
    for b in blocks:
        if b["kind"] == "heading":
            af._textbox(s, x, y, w, 0.32 * IN, b["text"], mx.HEADING_PT,
                        _rgb(TEAL_DK), bold=True)
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
                    sl.subtitle, 18, _rgb(TEAL_DK))
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
        o.fill.fore_color.rgb = _rgb(TEAL_MIST)
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
        r.font.color.rgb = _rgb(TEAL_DK)
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
    box.fill.fore_color.rgb = _rgb(TEAL_MIST)
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
                    (fig.caption or fig.key)[:60], 12, _rgb(GRAPHITE),
                    bold=True, align=PP_ALIGN.CENTER)
    y = top + h + 0.18 * IN
    if sl.items:
        af._textbox(s, MARGIN, y, CONTENT_W, 0.34 * IN,
                    "Only the second has:  " + ",  ".join(sl.items[:8]),
                    15, _rgb(TEAL_DK), bold=True)
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


def _diagram(prs, sl, label_pt=12.0):
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
    _table(s, MARGIN, BODY_TOP + 0.1 * IN, CONTENT_W,
           ["Learners often think", "In fact"],
           [[a, b] for a, b in sl.items], pt=13, first_col=0.40)
    return s


def _worked(prs, sl):
    s = _slide(prs)
    _chrome(s, sl)
    y = _key_idea(s, sl.key_idea) if sl.key_idea else BODY_TOP + 0.1 * IN
    end = _blocks(s, sl.blocks, y=y)
    return s, _overflow(end, sl)


def _glossary(prs, sl):
    s = _slide(prs)
    _chrome(s, sl)
    _table(s, MARGIN, BODY_TOP + 0.1 * IN, CONTENT_W, ["Term", "Meaning"],
           [[a, b] for a, b in sl.items], pt=12, first_col=0.26)
    return s


def _closing(prs, sl):
    s = _slide(prs, INK)
    af._textbox(s, 1.0 * IN, 2.45 * IN, 11.3 * IN, 0.8 * IN,
                "Ready to teach.", 34, _rgb(WHITE), bold=True)
    af._textbox(s, 1.0 * IN, 3.35 * IN, 11.3 * IN, 0.4 * IN,
                sl.heading, 18, _rgb(TEAL_DK))
    af._textbox(s, 1.0 * IN, 3.95 * IN, 11.3 * IN, 0.4 * IN,
                sl.subtitle, 13, _rgb(FAINT))
    return s


# Kinds whose renderer already returns (slide, faults); the rest return a
# slide and are wrapped at the call site.
_RENDER = {
    sb.TITLE: _title, sb.OBJECTIVES: _objectives, sb.SECTION: _section,
    sb.MISCONCEPTIONS: _misconceptions, sb.WORKED: _worked,
    sb.GLOSSARY: _glossary, sb.CLOSING: _closing,
}
_CHECKED = {sb.SECTION, sb.WORKED, sb.DIAGRAM, sb.CHECK, sb.FOCUS, sb.COMPARE}
_RENDER[sb.FOCUS] = _focus
_RENDER[sb.COMPARE] = _compare


def build(slides, out_path: str | Path, label_pt: float = 12.0) -> tuple[Path, list[str]]:
    """Render a storyboard. Returns the path and every geometry fault found.

    Faults are RETURNED rather than raised: the caller decides whether a deck
    with one crowded diagram is worse than no deck, and that is a judgement
    about the artifact, not about the geometry.
    """
    from pptx import Presentation

    prs = Presentation()
    prs.slide_width, prs.slide_height = af.SLIDE_W, af.SLIDE_H
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
