"""A DeckSpec as an editable .pptx, in the design language of the reference
decks.

THE DESIGN SYSTEM (what the Gemini reference decks do, made into tokens):
  * a dark cover and closer in the palette's deepest colour; content slides
    on a near-white ground;
  * every content slide has the same chrome: a small letter-spaced kicker in
    the accent colour, a large title, an optional one-line subtitle;
  * content sits in rounded CARDS on a soft tint with a hairline border;
    semantic tones (warm / good / bad / info / plum) colour cards, chips and
    callouts, never the text body;
  * one hero per slide: a formula at 40pt, a photograph cropped to fill its
    frame, an exact diagram, a labelled figure;
  * whitespace by construction: the grid has gutters and the column stack is
    centred in its cell, so a short slide is calm, not top-heavy.

EVERY WORD IS NATIVE TEXT. Titles, card text, formula text, step lines,
table cells, diagram labels, chip values, callouts and notes are PowerPoint
text a teacher can retype. The only rasters are photographs, illustrations
and approved lesson figures — and a figure's labels are still native text
with native leader lines. Circles, semicircles and quarter circles are drawn
as native shapes at exact proportions.

THE RENDERER MEASURES, THE SPEC DOES NOT. A column whose stack does not fit
its cell is redrawn at 90%, 80%, 70% of its type sizes, never below the
floors (16pt for anything the class reads as content, 14pt for card bodies
and step reasons, 11pt for kickers and credits); a stack that still does not
fit is drawn and REPORTED, and QA sends that slide back to the Director.

Every text box and picture is recorded in the slide's `boxes` so QA can
check geometry from the same numbers the renderer used.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from shared.lesson_model import display_part

from .. import metrics as mx
from ..deck_strings import T
from . import spec as S

logger = logging.getLogger(__name__)

IN = 914400
W_IN, H_IN = 13.333, 7.5
MARGIN = 0.62
GUTTER = 0.30
STACK_GAP = 0.20
FONT = "Calibri"

# ── colour ────────────────────────────────────────────────────────────

INK = (15, 23, 42)
GRAPHITE = (71, 85, 105)
FAINT = (148, 163, 184)
LINE = (226, 232, 240)
WHITE = (255, 255, 255)
LIGHT_BG = (248, 250, 252)

PALETTES = {
    "indigo": {"accent": (79, 70, 229), "dark": (30, 27, 75), "mist": (238, 242, 255), "dark2": (49, 46, 129)},
    "teal": {"accent": (13, 148, 136), "dark": (11, 18, 32), "mist": (240, 253, 250), "dark2": (17, 37, 54)},
    "forest": {"accent": (21, 128, 61), "dark": (15, 31, 23), "mist": (240, 253, 244), "dark2": (22, 56, 38)},
    "ocean": {"accent": (3, 105, 161), "dark": (11, 27, 43), "mist": (240, 249, 255), "dark2": (16, 48, 74)},
    "plum": {"accent": (124, 58, 237), "dark": (30, 16, 53), "mist": (245, 243, 255), "dark2": (55, 33, 92)},
    "sunset": {"accent": (234, 88, 12), "dark": (28, 25, 23), "mist": (255, 247, 237), "dark2": (68, 44, 30)},
}
TONES = {
    "warm": ((234, 88, 12), (255, 247, 237), (254, 215, 170)),
    "good": ((22, 163, 74), (240, 253, 244), (187, 247, 208)),
    "bad": ((220, 38, 38), (254, 242, 242), (254, 202, 202)),
    "info": ((37, 99, 235), (239, 246, 255), (191, 219, 254)),
    "plum": ((124, 58, 237), (245, 243, 255), (221, 214, 254)),
    "neutral": ((71, 85, 105), (248, 250, 252), (226, 232, 240)),
}


def _blend(a, b, t):
    return tuple(int(round(x + (y - x) * t)) for x, y in zip(a, b))


@dataclass
class Theme:
    accent: tuple
    dark: tuple
    dark2: tuple
    mist: tuple
    rtl: bool = False
    logo: Optional[str] = None
    templated: bool = False

    def tone(self, name: str) -> tuple[tuple, tuple, tuple]:
        """(strong, tint, border) for a semantic tone."""
        if name in TONES:
            return TONES[name]
        return self.accent, self.mist, _blend(self.accent, WHITE, 0.72)

    @classmethod
    def make(cls, palette: str, branding: Optional[dict] = None, rtl: bool = False) -> "Theme":
        p = PALETTES.get(palette, PALETTES["indigo"])
        th = cls(p["accent"], p["dark"], p["dark2"], p["mist"], rtl=rtl)
        b = branding or {}
        rgb = b.get("accent_rgb")
        if isinstance(rgb, (list, tuple)) and len(rgb) == 3:
            # A school's colour is the accent; its tint and dark are derived
            # so every surface stays legible whatever the colour is.
            acc = tuple(int(max(0, min(255, v))) for v in rgb)
            th.accent = acc
            th.mist = _blend(acc, WHITE, 0.92)
            th.dark = _blend(acc, (8, 10, 20), 0.78)
            th.dark2 = _blend(acc, (8, 10, 20), 0.6)
        if b.get("logo_path") and Path(str(b["logo_path"])).exists():
            th.logo = str(b["logo_path"])
        return th


def _rgb(t):
    from pptx.dml.color import RGBColor
    return RGBColor(*t)


# ── recorded geometry ─────────────────────────────────────────────────

@dataclass
class Box:
    kind: str           # text | picture | shape
    x: float
    y: float
    w: float
    h: float
    text: str = ""
    pt: float = 0.0
    role: str = ""
    fg: tuple = INK
    bg: tuple = WHITE
    px: tuple = (0, 0)   # a picture's source pixels
    bold: bool = False

    @property
    def rect(self):
        return (self.x, self.y, self.x + self.w, self.y + self.h)


@dataclass
class SlideReport:
    id: str
    index: int
    boxes: list[Box] = field(default_factory=list)
    faults: list[str] = field(default_factory=list)
    scale: float = 1.0
    words: int = 0
    background: tuple = WHITE
    degraded: list = field(default_factory=list)   # what fit_or_degrade moved to the notes


# ── primitive drawing ─────────────────────────────────────────────────

class Canvas:
    """One slide plus the record of everything drawn on it."""

    def __init__(self, slide, theme: Theme, report: SlideReport, bg: tuple):
        self.s, self.th, self.rep, self.bg = slide, theme, report, bg

    def rect(self, x, y, w, h, fill=None, line=None, radius=0.10, weight=0.75, shape="rounded"):
        from pptx.enum.shapes import MSO_SHAPE
        from pptx.util import Pt
        kind = {"rounded": MSO_SHAPE.ROUNDED_RECTANGLE, "rect": MSO_SHAPE.RECTANGLE,
                "oval": MSO_SHAPE.OVAL}[shape]
        sh = self.s.shapes.add_shape(kind, int(x * IN), int(y * IN), int(w * IN), int(h * IN))
        if shape == "rounded":
            try:
                sh.adjustments[0] = max(0.02, min(0.5, radius / max(0.1, min(w, h))))
            except Exception:  # noqa: BLE001
                pass
        if fill is None:
            sh.fill.background()
        else:
            sh.fill.solid()
            sh.fill.fore_color.rgb = _rgb(fill)
        if line is None:
            sh.line.fill.background()
        else:
            sh.line.color.rgb = _rgb(line)
            sh.line.width = Pt(weight)
        sh.shadow.inherit = False
        sh.text_frame.text = ""
        self.rep.boxes.append(Box("shape", x, y, w, h, role="container", bg=fill or self.bg))
        return sh

    def text(self, x, y, w, h, runs, pt, color=INK, bold=False, align="l", anchor="t",
             role="body", spacing=0, bg=None, line_spacing=None):
        """`runs`: a string, or [(text, {bold, color, italic})...]."""
        from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
        from pptx.util import Pt
        if isinstance(runs, str):
            runs = [(runs, {})]
        tb = self.s.shapes.add_textbox(int(x * IN), int(y * IN), int(w * IN), int(h * IN))
        tf = tb.text_frame
        tf.word_wrap = True
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
        tf.vertical_anchor = {"t": MSO_ANCHOR.TOP, "m": MSO_ANCHOR.MIDDLE, "b": MSO_ANCHOR.BOTTOM}[anchor]
        p = tf.paragraphs[0]
        if self.th.rtl and align == "l":
            align = "r"
        p.alignment = {"l": PP_ALIGN.LEFT, "c": PP_ALIGN.CENTER, "r": PP_ALIGN.RIGHT}[align]
        if line_spacing:
            p.line_spacing = line_spacing
        for t, fmt in runs:
            if not t:
                continue
            r = p.add_run()
            r.text = t
            r.font.size = Pt(pt)
            r.font.name = FONT
            r.font.bold = bool(fmt.get("bold", bold))
            r.font.italic = bool(fmt.get("italic", False))
            r.font.color.rgb = _rgb(fmt.get("color", color))
            if spacing:
                r.font._element.set("spc", str(int(spacing)))  # noqa: SLF001 — rPr letter spacing
        self.rep.boxes.append(Box("text", x, y, w, h, "".join(t for t, _ in runs), pt, role, color,
                                  bg or self.bg, bold=bool(bold)))
        return tb

    def line(self, x1, y1, x2, y2, color, weight=1.5, dash=False, arrow=False):
        from pptx.enum.dml import MSO_LINE_DASH_STYLE
        from pptx.enum.shapes import MSO_CONNECTOR
        from pptx.oxml.ns import qn
        from pptx.util import Pt
        c = self.s.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, int(x1 * IN), int(y1 * IN),
                                        int(x2 * IN), int(y2 * IN))
        c.line.color.rgb = _rgb(color)
        c.line.width = Pt(weight)
        if dash:
            c.line.dash_style = MSO_LINE_DASH_STYLE.DASH
        if arrow:
            ln = c.line._get_or_add_ln()  # noqa: SLF001
            ln.append(ln.makeelement(qn("a:tailEnd"), {"type": "triangle", "w": "med", "len": "med"}))
        return c

    def picture(self, png, x, y, w, h, px_w, px_h, mode="contain", role="picture"):
        """`cover` fills the frame and crops the overflow (photographs);
        `contain` fits inside it (diagrams, figures — nothing may be cut)."""
        if not (px_w and px_h):
            from PIL import Image
            with Image.open(png) as im:
                px_w, px_h = im.size
        ar = px_w / px_h
        if mode == "cover":
            pic = self.s.shapes.add_picture(str(png), int(x * IN), int(y * IN), int(w * IN), int(h * IN))
            frame_ar = w / h
            if ar > frame_ar:
                cut = (1 - frame_ar / ar) / 2
                pic.crop_left = pic.crop_right = cut
            else:
                cut = (1 - ar / frame_ar) / 2
                pic.crop_top = pic.crop_bottom = cut
            self.rep.boxes.append(Box("picture", x, y, w, h, role=role, px=(px_w, px_h)))
            return (x, y, w, h)
        if w / h > ar:
            pw, ph = h * ar, h
        else:
            pw, ph = w, w / ar
        px_, py_ = x + (w - pw) / 2, y + (h - ph) / 2
        self.s.shapes.add_picture(str(png), int(px_ * IN), int(py_ * IN), int(pw * IN), int(ph * IN))
        self.rep.boxes.append(Box("picture", px_, py_, pw, ph, role=role, px=(px_w, px_h)))
        return (px_, py_, pw, ph)


# Words wider than their box, counted while a column is MEASURED: PowerPoint
# would break such a word mid-way ("Diamete / r", run 5 re-render), so a
# measurement that met one does not fit, whatever its total height says.
_UNFIT = [0]


# Mathematical operators (∠ ≠ ≤ ≥ ∑ √ ∞ …) are not in Calibri; PowerPoint
# draws them in a fallback maths font whose line is taller and wider, and a
# card title above an "∠AOD + ∠DOB" line overlapped it (Types of Angles,
# matrix round 4). Text holding one is measured with that allowance.
BOLD_WIDTH = 1.15         # bold runs this much wider than the regular-weight em table
_MATH_SYMBOLS = re.compile("[∀-⋿⟀-⟯⦀-⧿]")


def _wrapped_lines(text: str, w: float, pt: float) -> int:
    """Lines `text` takes in a `w`-inch box at `pt`, wrapping WORD BY WORD
    as PowerPoint does. `metrics.text_height_in` divides total width by a
    line capacity floored at 4 em, which is right for the storyboard's wide
    columns and wrong in a narrow card: "Line AB" in a 2.5-em flow card was
    one line by that estimate and two on the slide, and sat on the text
    below it (Types of Angles, matrix round 4)."""
    cap = max(0.5, w) * 72 / pt                 # ems one line holds
    space = mx.text_width_em(" ")
    lines, used = 1, 0.0
    for word in (text or "").split():
        ww = mx.text_width_em(word)
        if used and used + space + ww > cap:
            lines += 1
            used = ww
        else:
            used += (space if used else 0.0) + ww
        while used > cap:                       # a word longer than a line breaks (and is flagged)
            lines += 1
            used -= cap
    return lines


def _h(text: str, w: float, pt: float) -> float:
    w = max(0.5, w)
    longest = max((mx.text_width_em(word) for word in (text or "").split()), default=0.0)
    if longest * pt / 72 * 1.06 > w:
        _UNFIT[0] += 1
    if _MATH_SYMBOLS.search(text or ""):
        return _wrapped_lines(text, w / 1.12, pt) * pt * mx.LINE / 72 * 1.3
    return _wrapped_lines(text, w, pt) * pt * mx.LINE / 72


def caps(text: str) -> str:
    """Small-caps styling for labels — but never for mathematics: "r = d ÷ 2"
    uppercased reads as a different formula (R and D are not r and d)."""
    t = text or ""
    # a lone lower-case letter is a variable ("r", "d"); written without a
    # word-boundary escape, which a shell heredoc turned into a backspace here
    return t if re.search(r"[=÷×²³π√]|(?<![A-Za-z])[a-z](?![A-Za-z])", t) else t.upper()


_NUMRUN = re.compile(r"(\d+(?:\.\d+)?\s?(?:cm²|cm|m²|mm|km|m)?|π\s?=\s?[\d./]+)")


def highlighted(text: str, color) -> list:
    """The KNOWN VALUES of a question in bold accent — what the reference
    decks do to a problem statement, and what a teacher underlines."""
    out, last = [], 0
    for m in _NUMRUN.finditer(text):
        out.append((text[last:m.start()], {}))
        out.append((m.group(0), {"bold": True, "color": color}))
        last = m.end()
    out.append((text[last:], {}))
    return out


# ── elements: each returns (natural height, draw(canvas, x, y, w, h)) ─

FLEX = {"image", "figure", "maths_diagram"}
FLEX_MIN = 1.7


ARABIC_SCRIPT_LANGS = ("ar", "ms-arab", "ur", "fa")


def text_for_box(lab: dict, labels: dict) -> str:
    return labels.get(lab["part"]) or display_part(lab["part"])


@dataclass
class Env:
    theme: Theme
    pictures: dict
    figures: dict
    dark: bool
    scale: float = 1.0
    faults: list = field(default_factory=list)
    language: str = "en"
    notes: list = field(default_factory=list)       # lines the renderer adds to the slide's notes

    def script_mismatch(self, text: str) -> bool:
        """A Latin-script word on an Arabic-script deck."""
        if self.language not in ARABIC_SCRIPT_LANGS:
            return False
        return bool(re.search("[A-Za-z]", text or "")) and not re.search("[؀-ۿ]", text or "")

    def pt(self, base: float, floor: float) -> float:
        return max(floor, round(base * self.scale * 2) / 2)

    @property
    def fg(self):
        return WHITE if self.dark else INK

    @property
    def fg2(self):
        return (203, 213, 225) if self.dark else GRAPHITE

    def card(self, tone: str):
        strong, tint, border = self.theme.tone(tone)
        if self.dark:
            # on a dark ground the tone's strong colour is lifted towards
            # white, or an indigo number on an indigo card reads at 1.8:1
            return _blend(strong, WHITE, 0.55), self.theme.dark2, _blend(self.theme.dark2, WHITE, 0.18)
        return strong, tint, border


def lay(e: dict, w: float, env: Env) -> tuple[float, Callable]:
    t = e["type"]
    fn = globals().get(f"_lay_{t}")
    if fn is None:
        return 0.0, lambda c, x, y, ww, hh: None
    return fn(e, w, env)


def _lay_statement(e, w, env):
    pt = env.pt(28, 20)
    # measured at +12%: bold display type is wider than the em table's
    # regular weight, and "respiration." wrapped onto the bullets below it
    # (run 4, science slide 8)
    h = _h(e["text"], w, pt * 1.12)
    return h, lambda c, x, y, ww, hh: c.text(x, y, ww, h, e["text"], pt, env.fg, bold=True, role="statement")


def _lay_paragraph(e, w, env):
    pt = env.pt(19, 16)
    h = _h(e["text"], w, pt)
    return h, lambda c, x, y, ww, hh: c.text(x, y, ww, h, e["text"], pt, env.fg2, role="paragraph")


def _lay_bullets(e, w, env):
    pt = env.pt(19, 16)
    items = [it.get("body") or it.get("title") or "" for it in e["items"]]
    hs = [_h(t, w - 0.38, pt) for t in items]
    gap = 0.16
    total = sum(hs) + gap * (len(hs) - 1)

    def draw(c, x, y, ww, hh):
        yy = y
        for t, h_ in zip(items, hs):
            d = 0.11
            c.rect(x + 0.04, yy + pt * 0.62 / 72 - d / 2, d, d, fill=env.theme.accent, shape="oval")
            c.text(x + 0.38, yy, ww - 0.38, h_, t, pt, env.fg, role="bullet")
            yy += h_ + gap
    return total, draw


def _card_parts(it, cw, env, numbered, n):
    pad = 0.24
    inner = cw - 2 * pad
    badge_pt, title_pt, body_pt = env.pt(11, 10), env.pt(20, 16), env.pt(16, 14)
    parts = []
    if numbered:
        parts.append(("num", f"{n:02d}", env.pt(26, 20), 0.5))
    if it.get("badge"):
        parts.append(("badge", caps(it["badge"]), badge_pt, _h(it["badge"], inner, badge_pt)))
    # Titles and expressions are BOLD, and bold glyphs run ~15% wider than
    # the em table's regular weight: "Line AB" wrapped to two lines in a
    # narrow flow card the estimate gave one, and sat on the line below it
    # (Types of Angles, matrix round 4). Measured in a 15% narrower box.
    if it.get("title"):
        parts.append(("title", it["title"], title_pt, _h(it["title"], inner / BOLD_WIDTH, title_pt)))
    if it.get("expr"):
        parts.append(("expr", it["expr"], env.pt(22, 18), _h(it["expr"], inner / BOLD_WIDTH, env.pt(22, 18))))
    if it.get("body"):
        parts.append(("body", it["body"], body_pt, _h(it["body"], inner, body_pt)))
    return pad, inner, parts


def _lay_cards(e, w, env):
    items = e["items"]
    numbered = e.get("numbered")
    if e.get("direction") == "column":
        laid = [_card_parts(it, w, env, numbered, i + 1) for i, it in enumerate(items)]
        hs = [2 * p + sum(h + 0.06 for *_, h in parts) - 0.06 for p, _, parts in laid]
        gap = 0.16
        total = sum(hs) + gap * (len(hs) - 1)

        def draw(c, x, y, ww, hh):
            yy = y + max(0, (hh - total) / 2) * 0
            for it, (pad, inner, parts), h_ in zip(items, laid, hs):
                _draw_card(c, env, it, x, yy, ww, h_, pad, parts, bar=True)
                yy += h_ + gap
        return total, draw
    n = len(items)
    gap = 0.22
    # "grid": two cards a row — four definitions too long for a row of four
    # and too tall for a stack of four (run 5, anatomy of a circle)
    per_row = 2 if (e.get("direction") == "grid" and n > 2) else n
    n_rows = -(-n // per_row)
    cw = (w - gap * (per_row - 1)) / per_row
    pic_h = min(1.9, cw * 0.62) if any(it.get("asset") in env.pictures for it in items) else 0.0
    laid = [_card_parts(it, cw, env, numbered, i + 1) for i, it in enumerate(items)]
    content = max(sum(h + 0.06 for *_, h in parts) - 0.06 for _, _, parts in laid)
    card_h = content + 2 * laid[0][0] + (pic_h + 0.16 if pic_h else 0)
    total = card_h * n_rows + gap * (n_rows - 1)

    def draw(c, x, y, ww, hh):
        for i, (it, (pad, inner, parts)) in enumerate(zip(items, laid)):
            xx = x + (i % per_row) * (cw + gap)
            yy = y + (i // per_row) * (card_h + gap)
            _draw_card(c, env, it, xx, yy, cw, card_h, pad, parts, pic_h=pic_h)
    return total, draw


def _draw_card(c, env, it, x, y, w, h, pad, parts, bar=False, pic_h=0.0):
    strong, tint, border = env.card(it.get("tone") or "accent")
    c.rect(x, y, w, h, fill=WHITE if not env.dark and not bar else tint, line=border, radius=0.14)
    if bar:
        c.rect(x, y, 0.07, h, fill=strong, shape="rect")
    yy = y + pad
    pic = env.pictures.get(it.get("asset") or "")
    if pic_h and pic is not None:
        c.picture(pic.png, x + pad, yy, w - 2 * pad, pic_h, pic.w, pic.h, mode="cover", role="card_picture")
        yy += pic_h + 0.16
    inner_x = x + pad + (0.06 if bar else 0)
    inner_w = w - 2 * pad - (0.06 if bar else 0)
    for kind, text, pt, ph in parts:
        if kind == "num":
            c.text(inner_x, yy, inner_w, ph, text, pt, strong, bold=True, role="card_number", bg=tint)
        elif kind == "badge":
            c.text(inner_x, yy, inner_w, ph, text, pt, strong, bold=True, spacing=120, role="badge", bg=tint)
        elif kind == "title":
            c.text(inner_x, yy, inner_w, ph, text, pt, env.fg, bold=True, role="card_title", bg=tint)
        elif kind == "expr":
            c.text(inner_x, yy, inner_w, ph, text, pt, strong, bold=True, role="card_expr", bg=tint)
        else:
            c.text(inner_x, yy, inner_w, ph, text, pt, env.fg2, role="card_body", bg=tint)
        yy += ph + 0.06


def _lay_formula(e, w, env):
    pt = env.pt(44, 30)
    cap_pt = env.pt(17, 14)
    eh = _h(e["expr"], w - 0.6, pt) + 0.08
    ch = _h(e.get("caption", ""), w - 0.6, cap_pt) if e.get("caption") else 0
    total = 0.36 + eh + (ch + 0.12 if ch else 0) + 0.36
    strong, tint, border = env.card(e.get("tone", "accent"))

    def draw(c, x, y, ww, hh):
        hh2 = max(total, min(hh, total + 0.6))
        c.rect(x, y, ww, hh2, fill=tint, line=border, radius=0.16)
        yy = y + (hh2 - (eh + (ch + 0.12 if ch else 0))) / 2
        c.text(x + 0.3, yy, ww - 0.6, eh, e["expr"], pt, strong, bold=True, align="c", role="formula", bg=tint)
        if ch:
            c.text(x + 0.3, yy + eh + 0.12, ww - 0.6, ch, e["caption"], cap_pt, env.fg2, align="c",
                   role="caption", bg=tint)
    return total, draw


def _lay_chips(e, w, env):
    items = e["items"][:4]
    n = len(items)
    gap = 0.18
    cw = (w - gap * (n - 1)) / n
    lab_pt, val_pt = env.pt(11, 10), env.pt(20, 16)
    # a label is drawn UPPERCASE and letter-spaced: measured that way, or a
    # two-line label sits on its value ("IRRATIONAL APPROXIMATION", run 2)
    def lab_h(t):
        return _h(caps(t or "") + " " * (len(t or "") // 6), cw - 0.36, lab_pt * 1.15)
    hs = [lab_h(it.get("label", "")) + 0.06 + _h(it.get("value") or it.get("body", ""),
                                                 cw - 0.36, val_pt) for it in items]
    total = max(hs) + 0.36

    def draw(c, x, y, ww, hh):
        for i, it in enumerate(items):
            strong, tint, border = env.card(it.get("tone") or "accent")
            xx = x + i * (cw + gap)
            c.rect(xx, y, cw, total, fill=tint, line=border, radius=0.12)
            lh = lab_h(it.get("label", ""))
            c.text(xx + 0.18, y + 0.18, cw - 0.36, lh, caps(it.get("label") or ""), lab_pt, strong,
                   bold=True, spacing=100, role="chip_label", bg=tint)
            c.text(xx + 0.18, y + 0.18 + lh + 0.06, cw - 0.36, total - lh - 0.4,
                   it.get("value") or it.get("body", ""), val_pt, env.fg, bold=True, role="chip_value", bg=tint)
    return total, draw


def _lay_steps(e, w, env):
    rows = e.get("resolved") or []
    q_pt, line_pt, why_pt = env.pt(19, 16), env.pt(21, 16), env.pt(15, 13)
    problem = e.get("problem", "")
    show_problem = e.get("from_step", 1) == 1 or True
    ph = _h(problem, w - 0.6, q_pt) + 0.36 if (problem and show_problem) else 0
    num_w = 0.46
    # the reason sits right after the longest line, not at a fixed fraction:
    # a fixed 60% column left "formula" floating an inch from "A = πr²"
    longest = max((mx.text_width_em(r["line"]) * line_pt / 72 for r in rows), default=2.0) + 0.45
    line_w = min((w - num_w - 0.2) * 0.66, max(2.4, longest))
    why_w = (w - num_w - 0.2) - line_w - 0.2
    rh = [max(_h(r["line"], line_w, line_pt), _h(r["why"], why_w, why_pt) if r.get("why") else 0, 0.42)
          for r in rows]
    ans = e.get("answer_text") or ""
    ans_short = _answer_short(ans)
    ah = (_h(ans_short, w - 2.2, env.pt(19, 16)) + 0.34) if ans_short else 0
    gap = 0.14
    total = ph + (0.22 if ph else 0) + sum(rh) + gap * max(0, len(rh) - 1) + (0.22 + ah if ah else 0)

    def draw(c, x, y, ww, hh):
        yy = y
        strong, tint, border = env.card("accent")
        if ph:
            c.rect(x, yy, ww, ph, fill=tint, line=border, radius=0.12)
            c.text(x + 0.3, yy + 0.18, ww - 0.6, ph - 0.36, highlighted(problem, strong), q_pt, env.fg,
                   role="problem", bg=tint)
            yy += ph + 0.22
        for r, h_ in zip(rows, rh):
            d = 0.40
            c.rect(x, yy + 0.02, d, d, fill=strong, shape="oval")
            c.text(x, yy + 0.02, d, d, str(r["n"]), env.pt(14, 12), WHITE, bold=True, align="c", anchor="m",
                   role="step_number", bg=strong)
            c.text(x + num_w + 0.2, yy, line_w, h_, r["line"], line_pt, env.fg2 if r.get("plain") else env.fg,
                   bold=not r.get("plain"), role="step_line")
            if r.get("why"):
                c.text(x + num_w + 0.2 + line_w + 0.2, yy + 0.03, why_w, h_, r["why"], why_pt, env.fg2,
                       role="step_reason")
            yy += h_ + gap
        if ah:
            yy += 0.22 - gap
            good = TONES["good"][0]
            c.rect(x, yy, ww, ah, fill=good, radius=0.12)
            c.text(x + 0.3, yy, 1.6, ah, caps(T(env.language, "answer")), env.pt(12, 11), WHITE, bold=True, anchor="m", spacing=120,
                   role="answer_label", bg=good)
            c.text(x + 1.9, yy, ww - 2.2, ah, ans_short, env.pt(19, 16), WHITE, bold=True, anchor="m",
                   role="answer", bg=good)
    return total, draw


def _answer_short(ans: str) -> str:
    """A prose 'Step 7: State the final answers…' sentence trimmed to what an
    answer bar says."""
    a = " ".join((ans or "").split())
    a = re.sub(r"^state (the|your) final answers?[^.]*\.\s*", "", a, flags=re.I)
    # never cut: "The tertiary consume" ended an answer bar mid-word (food
    # chains matrix run); the bar is measured to fit what it says
    return a


def _lay_answer(e, w, env):
    text = e.get("text") or e.get("expr") or ""
    pt = env.pt(20, 16)
    h = _h(text, w - 2.2, pt) + 0.36
    strong = env.card(e.get("tone") if e.get("tone") != "accent" else "good")[0]

    def draw(c, x, y, ww, hh):
        c.rect(x, y, ww, h, fill=strong, radius=0.12)
        c.text(x + 0.3, y, 1.6, h, caps(e.get("title") or T(env.language, "result")), env.pt(12, 11), WHITE, bold=True,
               anchor="m", spacing=120, role="answer_label", bg=strong)
        c.text(x + 1.9, y, ww - 2.2, h, text, pt, WHITE, bold=True, anchor="m", role="answer", bg=strong)
    return h, draw


_CALLOUT = {"tip": ("tip", "info"), "rule": ("key_rule", "warm"), "warning": ("watch_out", "bad"),
            "think": ("think", "plum"), "note": ("note", "neutral")}


def _lay_callout(e, w, env):
    key, tone = _CALLOUT.get(e.get("kind", "note"), ("note", "neutral"))
    label = caps(T(env.language, key))
    pt = env.pt(17, 14)
    lab_w = 1.25
    h = max(0.56, _h(e["text"], w - lab_w - 0.6, pt) + 0.3)
    strong, tint, border = env.card(tone)

    def draw(c, x, y, ww, hh):
        c.rect(x, y, ww, h, fill=tint, line=border, radius=0.10)
        c.rect(x, y, 0.08, h, fill=strong, shape="rect")
        c.text(x + 0.3, y, lab_w, h, label, env.pt(11, 10), strong, bold=True, anchor="m", spacing=120,
               role="callout_label", bg=tint)
        c.text(x + 0.3 + lab_w, y, ww - lab_w - 0.6, h, e["text"], pt, env.fg, bold=True, anchor="m",
               role="callout", bg=tint)
    return h, draw


def _lay_image(e, w, env):
    pic = env.pictures.get(e["asset"])
    if pic is None:
        env.faults.append(f"image {e['asset']} unavailable; element omitted")
        return 0.0, lambda c, x, y, ww, hh: None
    cap_pt = 12
    ch = _h(e.get("caption", ""), w, cap_pt) + 0.08 if e.get("caption") else 0
    photo = pic.need in ("photo", "micrograph") or pic.origin == "licensed"

    def draw(c, x, y, ww, hh):
        ph = max(0.8, hh - ch)
        if photo:
            c.rect(x - 0.0, y, ww, ph, fill=LINE, radius=0.14)
            c.picture(pic.png, x, y, ww, ph, pic.w, pic.h, mode="cover", role="photo")
        else:
            strong, tint, border = env.card("neutral")
            c.rect(x, y, ww, ph, fill=WHITE if not env.dark else env.theme.dark2, line=border, radius=0.14)
            c.picture(pic.png, x + 0.15, y + 0.15, ww - 0.3, ph - 0.3, pic.w, pic.h, mode="contain",
                      role="illustration")
        if ch:
            c.text(x, y + ph + 0.08, ww, ch - 0.08, e["caption"], cap_pt, env.fg2,
                   role="caption")
    return FLEX_MIN + ch, draw


def _lay_figure(e, w, env):
    fig = env.figures.get(e["figure_key"])
    if not fig or not fig.get("png") or not Path(fig["png"]).exists():
        env.faults.append(f"figure {e['figure_key']} has no artwork; element omitted")
        return 0.0, lambda c, x, y, ww, hh: None
    cap_pt = 12
    ch = _h(e.get("caption", ""), w, cap_pt) + 0.08 if e.get("caption") else 0

    def draw(c, x, y, ww, hh):
        from .. import annotated_figure as af
        fh = max(1.0, hh - ch)
        c.rect(x, y, ww, fh, fill=WHITE, line=LINE, radius=0.14)
        parts = [p for p in (e.get("parts") or []) if p]
        labellable = fig.get("labellable") and parts
        if labellable:
            label_pt = env.pt(16, 14)
            # The annotator hangs its label gutters OUTSIDE the picture's
            # frame, so the frame handed to it is the cell minus a gutter on
            # each side — handed the whole cell, labels ran off the slide
            # ("cell memb", the RTL Jawi deck, 2026-10-24).
            gut = max(1.3, min(2.3, ww * 0.26))
            frame = (x + gut + 0.3, y + 0.15, max(1.0, ww - 2 * gut - 0.6), fh - 0.3)
            labels = e.get("labels") or {}
            keyed: list[str] = []

            def text_for(g):
                part = g["part"]
                t = labels.get(part) or labels.get(part.lower())
                if t:
                    return t
                if env.script_mismatch(part):
                    # an English part name on an Arabic-script slide: a number
                    # on the picture, the name in the notes — never English
                    # in the middle of a Jawi lesson
                    keyed.append(part)
                    return str(len(keyed))
                return display_part(part)

            geo = af.figure_geometry(parts, fig["regions"], fig["w"], fig["h"])
            before = len(c.s.shapes)
            rep = af.add_annotated_figure(c.s, fig["png"], geo, fig["w"], fig["h"],
                                          frame=tuple(v * IN for v in frame), label_pt=label_pt,
                                          label_text=text_for, gutter=gut * IN)
            # A figure label is aligned TOWARDS its leader on either side of
            # the picture; the RTL pass must not right-align the right-hand
            # ones away from their lines (Jawi Forces, matrix round 2).
            for sh in list(c.s.shapes)[before:]:
                if getattr(sh, "has_text_frame", False):
                    sh.name = "keep-align label"
            env.faults.extend(af.validate(rep, label_pt=label_pt))
            px_, py_, pw_, ph_ = (v / IN for v in rep["picture"])
            c.rep.boxes.append(Box("picture", px_, py_, pw_, ph_, role="figure",
                                   px=(int(fig["w"]), int(fig["h"]))))
            for lab in rep["labels"]:
                lx, ly, lw, lh = (v / IN for v in lab["label"])
                c.rep.boxes.append(Box("text", lx, ly, lw, lh, text_for_box(lab, labels), label_pt,
                                       "figure_label", INK, WHITE))
            if keyed:
                env.notes.append(T(env.language, "figure_key") + " " + "; ".join(f"{i} = {display_part(p)}"
                                                            for i, p in enumerate(keyed, 1)))
        else:
            c.picture(fig["png"], x + 0.15, y + 0.15, ww - 0.3, fh - 0.3, int(fig["w"] or 0),
                      int(fig["h"] or 0), role="figure")
        if ch:
            c.text(x, y + fh + 0.08, ww, ch - 0.08, e["caption"], cap_pt, env.fg2, role="caption")
    return FLEX_MIN + 0.6 + ch, draw


def _lay_maths_diagram(e, w, env):
    return FLEX_MIN, lambda c, x, y, ww, hh: draw_circle_diagram(c, env, e, x, y, ww, hh)


def draw_circle_diagram(c: Canvas, env: Env, e: dict, x, y, w, h):
    """A circle, semicircle or quarter circle at EXACT proportions, as native
    shapes: the outline, centre, radius, diameter, chord and tangent are
    lines a teacher can move, and every label is a text box. No image model
    is asked to draw geometry whose relationships matter."""
    from pptx.util import Pt
    shape = e.get("shape", "circle")
    show = set(e.get("show") or [])
    labels = e.get("labels") or {}
    strong = env.theme.accent
    warm = TONES["warm"][0]
    good = TONES["good"][0]
    fill = _blend(strong, WHITE, 0.88)
    lab_pt = env.pt(16, 14)
    # always a WHITE panel, on a dark cover too: the outline and label
    # colours are chosen for white, and on the dark cover they read at 2.9:1
    c.rect(x, y, w, h, fill=WHITE, line=LINE, radius=0.14)
    fg = INK
    pad = 0.45
    # radius in inches so the whole figure (and its labels) fits the frame
    if shape == "circle":
        r = min((w - 2 * pad - 1.2) / 2, (h - 2 * pad) / 2)
        cx, cy = x + w / 2, y + h / 2
    elif shape == "semicircle":
        r = min((w - 2 * pad - 1.0) / 2, (h - 2 * pad - 0.5))
        cx, cy = x + w / 2, y + h / 2 + r / 2
    else:
        r = min(w - 2 * pad - 1.2, h - 2 * pad - 0.4)
        cx, cy = x + (w - r) / 2, y + (h + r) / 2
    r = max(0.6, r)
    outline = warm if e.get("highlight") == "perimeter" else strong
    area_fill = _blend(good, WHITE, 0.75) if e.get("highlight") == "area" else fill

    if shape == "circle":
        o = c.rect(cx - r, cy - r, 2 * r, 2 * r, fill=area_fill, line=outline, weight=3.0, shape="oval")
        o.line.width = Pt(3.0)
    else:
        a0, a1 = (math.pi, 2 * math.pi) if shape == "semicircle" else (1.5 * math.pi, 2 * math.pi)
        pts = [(cx + r * math.cos(a0 + (a1 - a0) * i / 72), cy + r * math.sin(a0 + (a1 - a0) * i / 72))
               for i in range(73)]
        pts.append((cx, cy))
        fb = c.s.shapes.build_freeform(int(pts[0][0] * IN), int(pts[0][1] * IN), scale=1.0)
        fb.add_line_segments([(int(px * IN), int(py * IN)) for px, py in pts[1:]], close=True)
        sh = fb.convert_to_shape()
        sh.fill.solid()
        sh.fill.fore_color.rgb = _rgb(area_fill)
        sh.line.color.rgb = _rgb(outline)
        sh.line.width = Pt(3.0)
        sh.shadow.inherit = False
        c.rep.boxes.append(Box("shape", cx - r, cy - r, 2 * r, 2 * r, role="diagram"))

    def label(part, default, lx, ly, align="l", color=None):
        text = labels.get(part, default)
        if not text:
            return
        tw = max(0.6, mx.text_width_em(text) * lab_pt / 72 + 0.1)
        bx = lx if align == "l" else (lx - tw if align == "r" else lx - tw / 2)
        c.text(bx, ly - lab_pt / 72 * 0.65, tw, lab_pt / 72 * 1.3, text, lab_pt, color or fg, bold=True,
               align=align, role="diagram_label", bg=WHITE)

    dot = 0.10
    if "centre" in show:
        c.rect(cx - dot / 2, cy - dot / 2, dot, dot, fill=fg, shape="oval")
        label("centre", "O", cx - 0.12, cy + 0.22, align="r")
    if shape == "circle":
        if "diameter" in show:
            c.line(cx - r, cy, cx + r, cy, GRAPHITE, 1.75, dash=True)
            label("diameter", "d", cx - r * 0.55, cy - 0.24, align="c")
        if "radius" in show:
            ang = -math.radians(38)
            ex, ey = cx + r * math.cos(ang), cy + r * math.sin(ang)
            c.line(cx, cy, ex, ey, warm, 2.5)
            label("radius", "r", ex + 0.12, ey - 0.18, color=warm)
        if "chord" in show:
            a1, a2 = math.radians(115), math.radians(30)
            c.line(cx + r * math.cos(a1), cy + r * math.sin(a1), cx + r * math.cos(a2), cy + r * math.sin(a2),
                   good, 2.25)
            label("chord", "chord", cx + r * 0.35, cy + r * 0.86, align="c", color=good)
        if "tangent" in show:
            c.line(cx - r * 0.7, cy - r, cx + r * 0.7, cy - r, TONES["bad"][0], 2.25)
            c.rect(cx - dot / 2, cy - r - dot / 2, dot, dot, fill=TONES["bad"][0], shape="oval")
            label("tangent", "tangent", cx + r * 0.72, cy - r, color=TONES["bad"][0])
        if "circumference" in show:
            label("circumference", "circumference", cx + r * 0.78, cy + r * 0.78, color=outline)
    else:
        if "diameter" in show and shape == "semicircle":
            label("diameter", "d", cx, cy + 0.26, align="c")
        if "radius" in show:
            if shape == "semicircle":
                c.line(cx, cy, cx + r, cy, warm, 2.5)
                label("radius", "r", cx + r / 2, cy + 0.26, align="c", color=warm)
            else:
                label("radius", "r", cx + r / 2, cy + 0.26, align="c", color=warm)
                label("radius_v", "", 0, 0)
        if "arc" in show or "circumference" in show:
            ang = -math.pi / 2 - (0.6 if shape == "semicircle" else -0.75)
            label("arc", "arc", cx + (r + 0.15) * math.cos(ang), cy + (r + 0.15) * math.sin(ang) - 0.1,
                  align="r" if shape == "semicircle" else "l", color=outline)
    if "area" in show:
        ax, ay = (cx, cy + r * 0.45) if shape == "circle" else (cx + (r * 0.35 if shape == "quarter" else 0),
                                                                cy - r * 0.38)
        label("area", "A", ax, ay, align="c", color=good)


def _lay_table(e, w, env):
    header, rows = e["header"], e["rows"][:6]
    pt = env.pt(16, 14)
    cols = mx.table_col_widths_in(len(header), w, 0.30)
    hs = [mx.table_row_height_in(header, cols, pt)] + [mx.table_row_height_in(r, cols, pt) for r in rows]
    total = sum(hs)

    def draw(c, x, y, ww, hh):
        from pptx.util import Pt
        shp = c.s.shapes.add_table(len(rows) + 1, len(header), int(x * IN), int(y * IN), int(ww * IN),
                                   int(total * IN))
        tbl = shp.table
        for i, cw in enumerate(cols):
            tbl.columns[i].width = int(cw * IN)
        for i, h_ in enumerate(hs):
            tbl.rows[i].height = int(h_ * IN)
        for r_i, row in enumerate([header] + rows):
            for c_i in range(len(header)):
                cell = tbl.cell(r_i, c_i)
                cell.text = ""
                run = cell.text_frame.paragraphs[0].add_run()
                run.text = str(row[c_i]) if c_i < len(row) else ""
                run.font.size = Pt(pt)
                run.font.name = FONT
                run.font.bold = r_i == 0 or c_i == 0
                run.font.color.rgb = _rgb(WHITE if r_i == 0 else INK)
                cell.fill.solid()
                cell.fill.fore_color.rgb = _rgb(env.theme.accent if r_i == 0 else
                                                (WHITE if r_i % 2 else LIGHT_BG))
        c.rep.boxes.append(Box("text", x, y, ww, total, " ".join(" ".join(r) for r in [header] + rows), pt,
                               "table"))
    return total, draw


def _lay_flow(e, w, env):
    items = e["items"]
    n = len(items)
    gap = 0.5
    cw = (w - gap * (n - 1)) / n
    pic_h = min(1.5, cw * 0.7) if any(it.get("asset") in env.pictures for it in items) else 0.0
    laid = [_card_parts(it, cw, env, False, i + 1) for i, it in enumerate(items)]
    content = max(sum(h + 0.06 for *_, h in parts) - 0.06 for _, _, parts in laid)
    total = content + 0.48 + (pic_h + 0.16 if pic_h else 0)

    def draw(c, x, y, ww, hh):
        order = list(range(n))[::-1] if env.theme.rtl else list(range(n))
        for slot, i in enumerate(order):
            xx = x + slot * (cw + gap)
            pad, inner, parts = laid[i]
            _draw_card(c, env, items[i], xx, y, cw, total, pad, parts, pic_h=pic_h)
        for slot in range(n - 1):
            ax = x + slot * (cw + gap) + cw + 0.08
            c.line(ax, y + total / 2, ax + gap - 0.16, y + total / 2, env.theme.accent, 2.25, arrow=True)
    return total, draw


def _lay_questions(e, w, env):
    items = e["items"]
    n = len(items)
    gap = 0.22
    cols = n if n <= 3 else 2
    rows_n = math.ceil(n / cols)
    cw = (w - gap * (cols - 1)) / cols
    pt = env.pt(19, 16)
    hs = [_h(it.get("q") or it.get("body", ""), cw - 0.5, pt) + 0.9 for it in items]
    rh = max(hs)
    total = rows_n * rh + gap * (rows_n - 1)

    def draw(c, x, y, ww, hh):
        for i, it in enumerate(items):
            xx = x + (i % cols) * (cw + gap)
            yy = y + (i // cols) * (rh + gap)
            strong, tint, border = env.card(it.get("tone") or "accent")
            c.rect(xx, yy, cw, rh, fill=tint, line=border, radius=0.14)
            c.text(xx + 0.25, yy + 0.22, cw - 0.5, 0.3, f"{i + 1:02d}", env.pt(13, 11), strong, bold=True,
                   role="question_number", bg=tint)
            c.text(xx + 0.25, yy + 0.58, cw - 0.5, rh - 0.8, it.get("q") or it.get("body", ""), pt, env.fg,
                   bold=True, role="question", bg=tint)
    return total, draw


def _lay_compare(e, w, env):
    gap = 0.3
    cw = (w - gap) / 2
    pt = env.pt(17, 14)
    sides = [e["left"], e["right"]]
    pic_h = min(1.8, cw * 0.5) if any(s.get("asset") in env.pictures for s in sides) else 0.0
    hs = []
    for s in sides:
        hs.append(0.3 + _h(s["title"], cw - 0.5, env.pt(20, 16)) + 0.18 +
                  sum(_h(t, cw - 0.85, pt) + 0.12 for t in s["items"]) + 0.2 + (pic_h + 0.16 if pic_h else 0))
    total = max(hs)

    def draw(c, x, y, ww, hh):
        order = [1, 0] if env.theme.rtl else [0, 1]
        for slot, i in enumerate(order):
            s = sides[i]
            xx = x + slot * (cw + gap)
            strong, tint, border = env.card(s.get("tone") or ("info" if i == 0 else "good"))
            c.rect(xx, y, cw, total, fill=tint, line=border, radius=0.14)
            c.rect(xx, y, cw, 0.09, fill=strong, shape="rect")
            yy = y + 0.3
            pic = env.pictures.get(s.get("asset") or "")
            if pic_h and pic is not None:
                c.picture(pic.png, xx + 0.25, yy, cw - 0.5, pic_h, pic.w, pic.h,
                          mode="cover" if pic.need == "photo" else "contain", role="compare_picture")
                yy += pic_h + 0.16
            th_ = _h(s["title"], cw - 0.5, env.pt(20, 16))
            c.text(xx + 0.25, yy, cw - 0.5, th_, s["title"], env.pt(20, 16), strong, bold=True,
                   role="compare_title", bg=tint)
            yy += th_ + 0.18
            for t in s["items"]:
                ih = _h(t, cw - 0.85, pt)
                c.rect(xx + 0.3, yy + pt / 72 * 0.45, 0.1, 0.1, fill=strong, shape="oval")
                c.text(xx + 0.55, yy, cw - 0.85, ih, t, pt, env.fg, role="compare_item", bg=tint)
                yy += ih + 0.12
    return total, draw


# ── slide assembly ────────────────────────────────────────────────────

# Type scales tried, largest first. ABOVE 1.0 as well as below: a slide with
# two short cards in a five-inch cell was a strip of 16pt text in a field of
# white (the first rendered stub); the class reads bigger type more easily
# and the space was there. Capped at 1.3 so a card never turns into a poster.
SCALES = (1.3, 1.18, 1.08, 1.0, 0.92, 0.84, 0.76, 0.68)


def _measure(col: S.Column, w: float, env: Env):
    """(laid, need, flex count). `need` is infinite when any word met during
    the measurement is wider than its box — that layout does not fit."""
    _UNFIT[0] = 0
    laid = [(e, *lay(e, w, env)) for e in col.elements]
    laid = [(e, hh, d) for e, hh, d in laid if hh > 0]
    fixed = sum(hh for e, hh, _ in laid if e["type"] not in FLEX)
    flex_n = sum(1 for e, _, _ in laid if e["type"] in FLEX)
    # a flexible element needs at least its OWN natural height (a figure
    # carries a caption under it); budgeting FLEX_MIN for it let a labelled
    # cell run 0.6in off the bottom edge (run 4, science slides 14-15)
    flex = sum(max(hh, FLEX_MIN) for e, hh, _ in laid if e["type"] in FLEX)
    need = fixed + flex + STACK_GAP * max(0, len(laid) - 1)
    if _UNFIT[0]:
        need = float("inf")
    return laid, need, flex_n


def best_scale(col: S.Column, w: float, h: float, env: Env) -> float:
    """The largest type scale at which the column fits its cell (the
    smallest scale when nothing fits — the overflow is then reported)."""
    for scale in SCALES:
        env.scale = scale
        _, need, _ = _measure(col, w, env)
        if need <= h + 0.02:
            env.scale = 1.0
            return scale
    env.scale = 1.0
    return SCALES[-1]


def prune_missing(sl: S.SlideSpec, pictures: dict, figures: dict) -> list[str]:
    """Take out what has no picture BEFORE layout, so the rest of the slide
    takes the room: an image element whose asset could not be had, a figure
    with no artwork, a card's or compare side's missing picture. Drawn
    after the fact it left an empty cell and a fault on every slide of the
    no-images matrix case. Returns what was taken out."""
    gone = []

    def has_pic(aid):
        return bool(aid) and aid in pictures

    for r in sl.rows:
        for c in r.cols:
            kept = []
            for e in c.elements:
                if e["type"] == "image" and not has_pic(e.get("asset")):
                    gone.append(f"image {e.get('asset')}")
                    continue
                if e["type"] == "figure" and not (figures.get(e.get("figure_key")) or {}).get("png"):
                    gone.append(f"figure {e.get('figure_key')}")
                    continue
                for it in e.get("items") or []:
                    if it.get("asset") and not has_pic(it["asset"]):
                        it.pop("asset")
                for k in ("left", "right"):
                    if isinstance(e.get(k), dict) and e[k].get("asset") and not has_pic(e[k]["asset"]):
                        e[k]["asset"] = ""
                kept.append(e)
            c.elements = kept
        r.cols = [c for c in r.cols if c.elements]
    sl.rows = [r for r in sl.rows if r.cols]
    return gone


def _col_widths(r: S.Row) -> list[float]:
    cw_sum = sum(cc.weight for cc in r.cols)
    cg = GUTTER * (len(r.cols) - 1)
    return [(W_IN - 2 * MARGIN - cg) * c.weight / cw_sum for c in r.cols]


def row_heights(rows: list[S.Row], avail: float, env: Env) -> list[float]:
    """Row heights for the slide. The model's weights first; but a row whose
    content cannot fit its weighted share even at the smallest type size,
    while another row has room to spare, gets its MEASURED minimum and the
    rest share what is left by weight. Most "column needs 1.68in of 1.43in"
    faults on the matrix were a cramped row beside a roomy one."""
    wsum = sum(r.weight for r in rows) or 1.0
    by_weight = [avail * r.weight / wsum for r in rows]
    need = []
    for r in rows:
        env.scale = SCALES[-1]
        n = max((_measure(c, w, env)[1] for c, w in zip(r.cols, _col_widths(r))), default=0.0)
        env.scale = 1.0
        need.append(n)
    if all(n <= h + 0.02 for n, h in zip(need, by_weight)) or any(n == float("inf") for n in need) \
            or sum(need) > avail:
        return by_weight                   # fits as designed, or cannot be helped by moving room
    spare = avail - sum(need)
    return [n + spare * r.weight / wsum for n, r in zip(need, rows)]


def fit_or_degrade(col: S.Column, w: float, h: float, env: Env, spill: list[str]) -> None:
    """The renderer's last resort when a column does not fit its cell at the
    smallest type size and the Director's correction rounds did not fix it
    (run 5: four stacked definition cards beside a diagram, 1.9in too tall).
    Each remedy is tried on a COPY and the first that fits is kept: a row of
    cards is shorter, but four narrow cards can be too narrow for "Circumference"
    — so a remedy that does not fit is never applied.
      1. stacked cards become a row;
      2. card bodies move to the speaker notes, titles stay (stacked, then row);
      3. bullets past the third move to the notes.
    Nothing is ever drawn past the cell; what moved is listed in `spill`,
    which the caller appends to the slide's notes."""
    import copy

    def fits(c: S.Column) -> bool:
        env.scale = SCALES[-1]
        ok = _measure(c, w, env)[1] <= h + 0.02
        env.scale = 1.0
        return ok

    if fits(col):
        return

    def as_row(c):
        for e in c.elements:
            if e["type"] == "cards" and len(e.get("items") or []) > 1:
                e["direction"] = "row"
        return []

    def bodies_out(c):
        moved = []
        for e in c.elements:
            if e["type"] == "cards":
                for it in e.get("items") or []:
                    if it.get("body") and it.get("title"):
                        moved.append(f"{it['title']}: {it.pop('body')}")
        return moved

    def bullets_cut(c):
        moved = []
        for e in c.elements:
            if e["type"] == "bullets" and len(e.get("items") or []) > 3:
                moved += [it.get("body", "") for it in e["items"][3:]]
                e["items"] = e["items"][:3]
        return moved

    def as_column(c):
        for e in c.elements:
            if e["type"] == "cards":
                e["direction"] = "column"
        return []

    def as_grid(c):
        for e in c.elements:
            if e["type"] == "cards" and len(e.get("items") or []) > 2:
                e["direction"] = "grid"
        return []

    remedies = [(as_row,), (as_grid,), (as_column,), (as_grid, bodies_out), (bodies_out,),
                (as_column, bodies_out), (as_row, bodies_out), (bodies_out, bullets_cut),
                (as_column, bodies_out, bullets_cut)]
    for steps in remedies:
        trial = copy.deepcopy(col)
        moved: list[str] = []
        for step in steps:
            moved += step(trial)
        if fits(trial):
            col.elements[:] = trial.elements
            spill.extend(moved)
            return
    # Last: secondary elements at the END of the column leave for the notes,
    # one at a time, until it fits — never the column's first (main) element.
    SECONDARY = ("callout", "paragraph", "chips", "bullets", "statement")
    trial = copy.deepcopy(col)
    moved = []
    while len(trial.elements) > 1 and trial.elements[-1]["type"] in SECONDARY:
        e = trial.elements.pop()
        text = e.get("text") or "; ".join(it.get("body") or it.get("value") or "" for it in e.get("items") or [])
        if text:
            moved.append(text)
        if fits(trial):
            col.elements[:] = trial.elements
            spill.extend(moved)
            return


def _layout_column(col: S.Column, x, y, w, h, env: Env, canvas: Canvas, rep: SlideReport,
                   scale: float = 1.0):
    """Stack a column's elements in its cell at the row's shared type scale:
    centre the stack vertically, give flexible elements the leftover."""
    env.scale = scale
    laid, need, flex_n = _measure(col, w, env)
    if not laid:
        env.scale = 1.0
        return
    if need > h + 0.02:
        rep.faults.append("a word is wider than its box at the smallest type size" if need == float("inf")
                          else f"column content needs {need:.2f}in of {h:.2f}in at the smallest type size")
        need = min(need, h)
    rep.scale = min(rep.scale, env.scale)
    spare = max(0.0, h - need)
    flex_extra = spare / flex_n if flex_n else 0.0
    total = need + (spare if flex_n else 0)
    yy = y + (h - total) / 2 if not flex_n else y
    for e, hh, draw in laid:
        eh = (max(hh, FLEX_MIN) + flex_extra) if e["type"] in FLEX else hh
        draw(canvas, x, yy, w, eh)
        yy += eh + STACK_GAP
    env.scale = 1.0


def _chrome(c: Canvas, sl: S.SlideSpec, env: Env, centered=False) -> float:
    y = 0.48
    align = "c" if centered else "l"
    if sl.kicker:
        c.text(MARGIN, y, W_IN - 2 * MARGIN, 0.26, sl.kicker.upper(), 11,
               env.theme.accent if not env.dark else _blend(env.theme.accent, WHITE, 0.45),
               bold=True, spacing=160, align=align, role="kicker")
        y += 0.34
    tpt = 32 if mx.text_width_em(sl.title) < 34 else 27
    th = _h(sl.title, W_IN - 2 * MARGIN, tpt)
    c.text(MARGIN, y, W_IN - 2 * MARGIN, th, sl.title, tpt, env.fg, bold=True, align=align, role="title")
    y += th + 0.08
    if sl.subtitle:
        sh = _h(sl.subtitle, W_IN - 2 * MARGIN, 17)
        c.text(MARGIN, y, W_IN - 2 * MARGIN, sh, sl.subtitle, 17, env.fg2, align=align, role="subtitle")
        y += sh
    return y + 0.30


def _background(slide, color):
    f = slide.background.fill
    f.solid()
    f.fore_color.rgb = _rgb(color)


def _cover(prs, sl: S.SlideSpec, deck: S.DeckSpec, env: Env, rep: SlideReport):
    s = prs.slides.add_slide(prs.slide_layouts[6] if len(prs.slide_layouts) > 6 else prs.slide_layouts[-1])
    _background(s, env.theme.dark)
    rep.background = env.theme.dark
    c = Canvas(s, env.theme, rep, env.theme.dark)
    els = list(sl.elements())
    def usable(e):
        return e["type"] in ("image", "maths_diagram") and (e["type"] != "image" or e["asset"] in env.pictures)
    hero = next((e for e in els if usable(e)), None)
    if hero is None:
        # A bare cover (run 1) is the one slide the reference decks never
        # leave plain. Borrow the deck's own first exact diagram or picture —
        # nothing is drawn or fetched for it.
        hero = next((e for s2 in deck.slides[1:] for e in s2.elements() if usable(e)), None)
    chips = next((e for e in els if e["type"] == "chips"), None)
    text_w = 7.2 if hero else W_IN - 2 * MARGIN - 0.6
    x0 = 0.85
    if sl.kicker:
        c.text(x0, 0.75, text_w, 0.3, sl.kicker.upper(), 12, _blend(env.theme.accent, WHITE, 0.5), bold=True,
               spacing=180, role="kicker")
    title = sl.title or deck.title
    tpt = 46 if mx.text_width_em(title) < 30 else 38
    th = _h(title, text_w, tpt)
    ty = 2.0
    c.text(x0, ty, text_w, th, title, tpt, WHITE, bold=True, role="title")
    sub = sl.subtitle or deck.subtitle
    if sub:
        c.text(x0, ty + th + 0.25, text_w, _h(sub, text_w, 19), sub, 19, (203, 213, 225), role="subtitle")
    # a hairline above the chips, as the reference covers do
    c.rect(x0, 5.55, W_IN - 2 * x0, 0.015, fill=_blend(env.theme.dark, WHITE, 0.18), shape="rect")
    if chips:
        n = len(chips["items"])
        cw = min(2.9, (W_IN - 2 * x0 - 0.3 * (n - 1)) / n)
        for i, it in enumerate(chips["items"][:4]):
            xx = x0 + i * (cw + 0.3)
            c.text(xx, 5.8, cw, 0.25, caps(it.get("label") or ""), 11,
                   _blend(env.theme.accent, WHITE, 0.45), bold=True, spacing=120, role="chip_label")
            c.text(xx, 6.08, cw, 0.42, it.get("value") or it.get("body", ""), 20, WHITE, bold=True,
                   role="chip_value")
    if hero is not None:
        hx, hy, hw, hh = 8.45, 1.1, 4.1, 4.1
        if hero["type"] == "image":
            pic = env.pictures[hero["asset"]]
            c.rect(hx, hy, hw, hh, fill=env.theme.dark2, radius=0.3)
            c.picture(pic.png, hx, hy, hw, hh, pic.w, pic.h,
                      mode="cover" if pic.need in ("photo", "micrograph") else "contain", role="cover_picture")
        else:
            env2 = Env(env.theme, env.pictures, env.figures, dark=True)
            lay(hero, hw, env2)[1](c, hx, hy, hw, hh)
    if env.theme.logo:
        s.shapes.add_picture(env.theme.logo, int(11.85 * IN), int(6.7 * IN), height=int(0.5 * IN))
    return s


def build(deck: S.DeckSpec, pictures: dict, figures: dict, out_path: Path, *,
          branding: Optional[dict] = None, direction: str = "ltr",
          credits: Optional[list[str]] = None, language: str = "en") -> tuple[Path, list[SlideReport]]:
    from ..deck_render import _base
    from ..deck_render import _ACCENT as _legacy_accent

    theme = Theme.make(deck.palette, branding, rtl=direction == "rtl")
    prs = _base(branding)
    theme.templated = bool(_legacy_accent.get("templated"))
    _legacy_accent["templated"] = False
    reports: list[SlideReport] = []
    for sl in deck.slides:
        prune_missing(sl, pictures, figures)
    for i, sl in enumerate(deck.slides, 1):
        rep = SlideReport(sl.id, i)
        dark = sl.background == "dark"
        env = Env(theme, pictures, figures, dark=dark, language=(language or "en").lower())
        try:
            if i == 1 and dark:
                s = _cover(prs, sl, deck, env, rep)
            else:
                s = prs.slides.add_slide(prs.slide_layouts[6] if len(prs.slide_layouts) > 6
                                         else prs.slide_layouts[-1])
                bg = {"dark": theme.dark, "tinted": theme.mist, "white": WHITE}.get(sl.background, LIGHT_BG)
                if not (theme.templated and bg in (WHITE, LIGHT_BG)):
                    _background(s, bg)
                rep.background = bg
                c = Canvas(s, theme, rep, bg)
                top = _chrome(c, sl, env, centered=dark)
                bottom = 6.95
                if sl.footer:
                    fh, fdraw = lay(sl.footer, W_IN - 2 * MARGIN, env)
                    fdraw(c, MARGIN, bottom - fh, W_IN - 2 * MARGIN, fh)
                    bottom -= fh + 0.25
                rows = sl.rows
                if rows:
                    avail = bottom - top
                    gaps = GUTTER * (len(rows) - 1)
                    heights = row_heights(rows, avail - gaps, env)
                    yy = top
                    for r, rh in zip(rows, heights):
                        cw_sum = sum(cc.weight for cc in r.cols)
                        cg = GUTTER * (len(r.cols) - 1)
                        cols = list(r.cols)[::-1] if theme.rtl else list(r.cols)
                        widths = [(W_IN - 2 * MARGIN - cg) * col.weight / cw_sum for col in cols]
                        spill: list[str] = []
                        for col, cw in zip(cols, widths):
                            fit_or_degrade(col, cw, rh, env, spill)
                        if spill:
                            sl.notes = (sl.notes + "\n\n" if sl.notes else "") + \
                                T(language, "in_full") + "\n" + "\n".join(f"- {x}" for x in spill)
                            rep.degraded.extend(spill)
                        # ONE scale per row: two columns side by side at
                        # different type sizes read as two different decks
                        row_scale = min(best_scale(col, cw, rh, env) for col, cw in zip(cols, widths))
                        xx = MARGIN
                        for col, cw in zip(cols, widths):
                            _layout_column(col, xx, yy, cw, rh, env, c, rep, row_scale)
                            xx += cw + GUTTER
                        yy += rh + GUTTER
                if theme.logo and not dark:
                    s.shapes.add_picture(theme.logo, int(12.05 * IN), int(6.95 * IN), height=int(0.4 * IN))
            rep.faults.extend(env.faults)
            notes = sl.notes or ""
            if env.notes:
                notes = "\n\n".join(x for x in [notes] + env.notes if x)
            qs = [it for e in sl.elements() if e["type"] == "questions" for it in e.get("items") or []]
            if qs and any(it.get("answer") for it in qs):
                notes += "\n\n" + T(language, "answers") + " " + "; ".join(f"{n}. {it.get('answer') or '—'}"
                                                     for n, it in enumerate(qs, 1))
            if notes.strip():
                s.notes_slide.notes_text_frame.text = notes.strip()
        except Exception as exc:  # noqa: BLE001 — one slide must not lose the deck
            logger.exception("director render: slide %s failed", sl.id)
            rep.faults.append(f"render FAILED: {type(exc).__name__}: {exc}")
        reports.append(rep)
    if credits:
        rep = SlideReport("credits", len(reports) + 1)
        s = prs.slides.add_slide(prs.slide_layouts[6] if len(prs.slide_layouts) > 6 else prs.slide_layouts[-1])
        _background(s, WHITE)
        c = Canvas(s, theme, rep, WHITE)
        c.text(MARGIN, 0.55, 8, 0.5, T(language, "image_credits"), 24, INK, bold=True, role="title")
        y = 1.35
        for line in credits[:14]:
            h_ = _h(line, W_IN - 2 * MARGIN, 11)
            c.text(MARGIN, y, W_IN - 2 * MARGIN, h_, line, 11, GRAPHITE, role="credit")
            y += h_ + 0.1
        reports.append(rep)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(out_path))
    if direction == "rtl":
        from ..slide_builder import _mirror_deck_rtl
        _mirror_deck_rtl(out_path)
    return out_path, reports
