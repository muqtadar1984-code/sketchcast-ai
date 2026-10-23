"""Pin cards drawn with Pillow at 1000×1500 (2:3, Pinterest's ratio) from
artwork and text that already exist. NO image model: the same rule as
``publish.build_thumbnail`` and ``catalogue/previews.py`` — the Vertex image
pool is a total outage for a teacher while it is busy.

Three layouts (P1, decision D10): ``explainer`` (title + 3–5 key points,
with the article's first figure when it has one), ``diagram`` (one approved
figure large, with its approved part names as a legend — never a vision
guess, never a stand-in), and ``worksheet`` (the kit's worksheet preview card
re-framed to 2:3 under a "Free worksheet" band).

SAFE ZONES (guide §4): the title band is the top 12 %, the brand strip
(seal + sketchcast.app) the bottom 10 %, and the bottom-right 120 px square
carries nothing but background, because Pinterest lays its buttons over it.

TEXT IS MEASURED, NOT ESTIMATED (the maths point-wrap lesson): every block is
wrapped and measured with the font it will be drawn in, shrunk step by step
down to the 40 px floor, and when it still does not fit the card is REFUSED
(``TextDoesNotFit``) — a clipped pin is worse than no pin. Fonts and shaping
are the slide builder's (``_font`` / ``_wrap`` / ``display_text``): Noto per
script, Arabic-script text reshaped and drawn right-aligned, and Devanagari /
Telugu refused where HarfBuzz shaping is required and missing
(``ShapingUnavailable``).

``render`` returns ``(image, meta)``; ``meta`` carries what the prechecks
measure: ``dominant_color``, ``diagram_coverage`` (the diagram's drawn area
over the canvas — None when the layout has no diagram), ``image_coverage``,
``min_contrast`` (WCAG ratio of every text against its panel) and
``text_boxes`` (each block's box and the box its ink actually used).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from PIL import Image, ImageChops, ImageDraw

W, H = 1000, 1500
SIZE = (W, H)
TITLE_BAND = (0, 0, W, 180)           # 12 %
BRAND_STRIP = (0, 1350, W, H)         # 10 %
CORNER = 120                          # bottom-right square kept clear
CLEAR_BOX = (W - CORNER, H - CORNER, W, H)
MARGIN = 60
FONT_FLOOR = 40
TEMPLATES = ("explainer", "diagram", "worksheet")
RTL_LANGUAGES = ("ar", "ur", "ms-arab")
MAX_POINTS = 5
MAX_LEGEND_PARTS = 8

PAPER = (252, 251, 247)
INK = (22, 28, 36)
MUTED = (70, 78, 88)
BRAND = (24, 62, 92)
WHITE = (255, 255, 255)
# A subject colour per board (guide §4). Every one carries white text at
# better than WCAG AA (≥ 4.5:1).
SUBJECT_COLOURS = {
    "biology": (27, 94, 32), "chemistry": (74, 20, 140), "physics": (13, 71, 161),
    "algebra": (191, 54, 12), "geometry": (0, 96, 100), "mathematics": (0, 96, 100),
    "maths": (0, 96, 100), "math": (0, 96, 100),
}
DEFAULT_COLOUR = (38, 50, 56)
BRAND_TEXT = "sketchcast.app"
# THE FIXED WORDS a layout draws, per lesson language. A pin whose layout
# needs a word this table lacks in its language is REFUSED (StringMissing),
# never drawn in English on a French or Telugu pin (fail over degrade).
# Jawi (ms-arab) is the Malay transliterated, as the kits do it. The non-
# English entries want a native reader's look before P2 turns them on.
LANGUAGES = ("en", "ar", "ms", "ms-arab", "hi", "mr", "te", "ur", "fr", "es", "pt")
STRINGS = {
    "free_worksheet": {
        "en": "FREE WORKSHEET",
        "ar": "ورقة عمل مجانية",
        "ms": "LEMBARAN KERJA PERCUMA",
        "ms-arab": "لمبرن کرجا ڤرچوما",
        "hi": "मुफ़्त वर्कशीट",
        "mr": "मोफत वर्कशीट",
        "te": "ఉచిత వర్క్‌షీట్",
        "ur": "مفت ورک شیٹ",
        "fr": "FICHE D'EXERCICES GRATUITE",
        "es": "HOJA DE TRABAJO GRATIS",
        "pt": "FICHA DE EXERCÍCIOS GRÁTIS",
    },
}
TEMPLATE_STRINGS = {"explainer": (), "diagram": (), "worksheet": ("free_worksheet",)}
FREE_BAND = STRINGS["free_worksheet"]["en"]


class TextDoesNotFit(RuntimeError):
    """A text block does not fit its box even at the floor size."""


class StringMissing(TextDoesNotFit):
    """A layout's fixed words do not exist in the pin's language."""


def _lang(language: str) -> str:
    return str(language or "").strip().lower() or "en"


def missing_strings(template: str, language: str) -> list[str]:
    """The fixed words ``template`` needs that the table lacks in ``language``."""
    return [k for k in TEMPLATE_STRINGS.get(template, ()) if not STRINGS.get(k, {}).get(_lang(language))]


def string(key: str, language: str) -> str:
    text = STRINGS.get(key, {}).get(_lang(language))
    if not text:
        raise StringMissing(f"the {key!r} words have no {_lang(language)} text; the pin is not drawn in another language")
    return text


@dataclass
class Inputs:
    """What a layout draws. ``figure`` / ``preview`` are PIL images the caller
    has already read out of storage."""
    title: str
    points: list = field(default_factory=list)
    labels: list = field(default_factory=list)
    subject: str = ""
    figure: Optional[Image.Image] = None
    preview: Optional[Image.Image] = None


# ── colour ──────────────────────────────────────────────────────────────


def _lin(c: float) -> float:
    c = c / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(rgb) -> float:
    r, g, b = rgb[:3]
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def contrast(a, b) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return round((la + 0.05) / (lb + 0.05), 2)


def subject_colour(subject: str):
    return SUBJECT_COLOURS.get(str(subject or "").strip().lower(), DEFAULT_COLOUR)


def dominant_color(img: Image.Image) -> str:
    """The most common colour of a small quantised copy, as ``#rrggbb``."""
    small = img.convert("RGB").resize((50, 75))
    q = small.quantize(colors=8)
    palette = q.getpalette() or []
    counts = sorted(q.getcolors() or [], reverse=True)
    if not counts:
        return "#ffffff"
    idx = counts[0][1]
    r, g, b = palette[idx * 3: idx * 3 + 3]
    return f"#{r:02x}{g:02x}{b:02x}"


# ── text ────────────────────────────────────────────────────────────────


def _font(bold: bool, size: int, sample: str):
    from agent5_slides.slide_builder import _font as slide_font

    return slide_font(bold, size, sample)


def _shown(text: str, rtl: bool) -> str:
    from shared.text_shaping import display_text

    return display_text(text, rtl_base=rtl)


def _wrap(draw, text: str, font, max_w: int) -> list[str]:
    from agent5_slides.slide_builder import _wrap as slide_wrap

    return slide_wrap(draw, text, font, max_w)


class _Card:
    """A canvas plus the record of every text block drawn on it."""

    def __init__(self, rtl: bool, language: str = "en"):
        self.language = _lang(language)
        self.img = Image.new("RGB", SIZE, PAPER)
        self.draw = ImageDraw.Draw(self.img)
        self.rtl = rtl
        self.text_boxes: list[dict] = []
        self.contrasts: list[float] = []

    def _fits(self, blocks: list[str], box, size: int, bold: bool, max_lines: int, gap: float):
        x0, y0, x1, y1 = box
        sample = " ".join(blocks)
        font = _font(bold, size, sample)
        laid: list[list[str]] = []
        for text in blocks:
            lines = _wrap(self.draw, text, font, x1 - x0)
            if not lines or len(lines) > max_lines:
                return None
            if max(self.draw.textlength(_shown(ln, self.rtl), font=font) for ln in lines) > x1 - x0:
                return None   # one word wider than the box
            laid.append(lines)
        line_h = int(size * 1.25)
        total = sum(len(ls) for ls in laid) * line_h + int(size * gap) * (len(laid) - 1)
        if total > y1 - y0:
            return None
        return font, laid, line_h

    def fit(self, name: str, blocks: list[str], box, *, bold: bool, start: int, floor: int = FONT_FLOOR,
            max_lines: int = 2, gap: float = 0.6):
        """The largest size (start → floor, in 2 px steps) at which every
        block wraps within ``max_lines`` and the whole stack fits the box."""
        blocks = [b for b in (" ".join(str(x or "").split()) for x in blocks) if b]
        if not blocks:
            raise TextDoesNotFit(f"{name}: nothing to draw")
        for size in range(start, floor - 1, -2):
            got = self._fits(blocks, box, size, bold, max_lines, gap)
            if got:
                return (*got, size)
        raise TextDoesNotFit(f"{name}: {blocks[0][:60]!r} does not fit {box[2] - box[0]}x{box[3] - box[1]} px "
                             f"even at {floor} px")

    def text(self, name: str, blocks: list[str], box, *, fill, panel, bold: bool, start: int,
             floor: int = FONT_FLOOR, max_lines: int = 2, bullets: bool = False, valign: str = "top",
             gap: float = 0.6) -> None:
        x0, y0, x1, y1 = box
        inner = (x0 + (34 if bullets and not self.rtl else 0), y0, x1 - (34 if bullets and self.rtl else 0), y1)
        font, laid, line_h, size = self.fit(name, blocks, inner, bold=bold, start=start, floor=floor,
                                            max_lines=max_lines, gap=gap)
        total = sum(len(ls) for ls in laid) * line_h + int(size * gap) * (len(laid) - 1)
        y = y0 if valign == "top" else y0 + (y1 - y0 - total) // 2
        used = None
        for lines in laid:
            if bullets:
                r = max(5, size // 6)
                cy = y + line_h // 2
                cx = x0 + 10 if not self.rtl else x1 - 10
                self.draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fill)
            for ln in lines:
                shown = _shown(ln, self.rtl)
                tw = self.draw.textlength(shown, font=font)
                x = inner[2] - tw if self.rtl else inner[0]
                self.draw.text((x, y), shown, font=font, fill=fill)
                bb = self.draw.textbbox((x, y), shown, font=font)
                used = bb if used is None else (min(used[0], bb[0]), min(used[1], bb[1]),
                                                max(used[2], bb[2]), max(used[3], bb[3]))
                y += line_h
            y += int(size * gap)
        tol = 2
        overflow = bool(used) and (used[0] < x0 - tol or used[1] < y0 - tol or used[2] > x1 + tol
                                   or used[3] > y1 + tol)
        self.text_boxes.append({"name": name, "box": list(box), "used": list(used or box), "size": size,
                                "overflow": overflow})
        self.contrasts.append(contrast(fill, panel))


# ── pictures ────────────────────────────────────────────────────────────


def content_crop(img: Image.Image, threshold: int = 18) -> Image.Image:
    """The picture without its empty margin: the bounding box of every pixel
    that differs from the corner (background) colour."""
    rgb = img.convert("RGBA")
    flat = Image.new("RGB", rgb.size, (255, 255, 255))
    flat.paste(rgb, mask=rgb.split()[3])
    bg = Image.new("RGB", flat.size, flat.getpixel((0, 0)))
    diff = ImageChops.difference(flat, bg).convert("L").point(lambda p: 255 if p > threshold else 0)
    box = diff.getbbox()
    if not box:
        return flat
    pad = 8
    return flat.crop((max(0, box[0] - pad), max(0, box[1] - pad), min(flat.width, box[2] + pad),
                      min(flat.height, box[3] + pad)))


def paste_fit(canvas: Image.Image, img: Image.Image, slot, *, max_upscale: float = 4.0) -> tuple:
    """Scale ``img`` to fit ``slot`` (aspect kept), centre it; the box used."""
    x0, y0, x1, y1 = slot
    sw, sh = x1 - x0, y1 - y0
    scale = min(sw / img.width, sh / img.height, max_upscale)
    w, h = max(1, int(img.width * scale)), max(1, int(img.height * scale))
    pic = img.resize((w, h), Image.LANCZOS)
    px, py = x0 + (sw - w) // 2, y0 + (sh - h) // 2
    canvas.paste(pic, (px, py))
    return (px, py, px + w, py + h)


def _area(box) -> float:
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1]) / float(W * H)


# ── the frame every layout shares ───────────────────────────────────────


def _frame(card: _Card, title: str, colour, *, title_start: int = 64) -> None:
    d = card.draw
    d.rectangle(TITLE_BAND, fill=colour)
    card.text("title", [title], (MARGIN, 24, W - MARGIN, TITLE_BAND[3] - 20), fill=WHITE, panel=colour,
              bold=True, start=title_start, max_lines=2, valign="middle", gap=0)
    # the brand strip: seal + wordmark on the LEFT; the right 120 px is clear
    d.rectangle(BRAND_STRIP, fill=BRAND)
    cy = (BRAND_STRIP[1] + BRAND_STRIP[3]) // 2
    d.ellipse([MARGIN, cy - 40, MARGIN + 80, cy + 40], outline=WHITE, width=5)
    s_font = _font(True, 48, "S")
    sw = d.textlength("S", font=s_font)
    d.text((MARGIN + 40 - sw / 2, cy - 30), "S", font=s_font, fill=WHITE)
    b_font = _font(True, 44, BRAND_TEXT)
    d.text((MARGIN + 104, cy - 26), BRAND_TEXT, font=b_font, fill=WHITE)
    bb = d.textbbox((MARGIN + 104, cy - 26), BRAND_TEXT, font=b_font)
    card.text_boxes.append({"name": "brand", "box": [MARGIN, BRAND_STRIP[1], CLEAR_BOX[0], H], "used": list(bb),
                            "size": 44, "overflow": bb[2] > CLEAR_BOX[0]})
    card.contrasts.append(contrast(WHITE, BRAND))


def _explainer(card: _Card, inp: Inputs, colour) -> dict:
    points = [p for p in inp.points if str(p or "").strip()][:MAX_POINTS]
    if len(points) < 3 and not inp.figure:
        raise TextDoesNotFit(f"explainer: {len(points)} key point(s); a text-only explainer needs 3 to 5")
    _frame(card, inp.title, colour)
    image_box = None
    if inp.figure is not None:
        image_box = paste_fit(card.img, content_crop(inp.figure), (MARGIN, 210, W - MARGIN, 760))
        box = (MARGIN, 800, W - MARGIN, BRAND_STRIP[1] - 30)
        start = 44
    else:
        box = (MARGIN, 230, W - MARGIN, BRAND_STRIP[1] - 40)
        start = 64
    if points:
        card.text("points", points, box, fill=INK, panel=PAPER, bold=False, start=start, max_lines=3,
                  bullets=True, valign="middle" if inp.figure is None else "top")
    return {"image_coverage": round(_area(image_box), 3) if image_box else None, "diagram_coverage": None}


def _diagram(card: _Card, inp: Inputs, colour) -> dict:
    if inp.figure is None:
        raise TextDoesNotFit("diagram: no figure to draw — a diagram pin is never drawn without its artwork")
    labels = [str(x).strip() for x in inp.labels if str(x or "").strip()][:MAX_LEGEND_PARTS]
    if not labels:
        raise TextDoesNotFit("diagram: the figure has no approved part names")
    _frame(card, inp.title, colour)
    box = paste_fit(card.img, content_crop(inp.figure), (40, 200, W - 40, 1090))
    sep = "  ·  "
    card.text("labels", [sep.join(labels)], (MARGIN, 1110, W - MARGIN, BRAND_STRIP[1] - 24), fill=MUTED,
              panel=PAPER, bold=False, start=44, max_lines=4, valign="middle", gap=0)
    cov = round(_area(box), 3)
    return {"image_coverage": cov, "diagram_coverage": cov}


def _worksheet(card: _Card, inp: Inputs, colour) -> dict:
    if inp.preview is None:
        raise TextDoesNotFit("worksheet: no preview card to frame")
    _frame(card, inp.title, colour)
    d = card.draw
    d.rectangle([0, TITLE_BAND[3], W, TITLE_BAND[3] + 90], fill=PAPER)
    card.text("band", [string("free_worksheet", card.language)], (MARGIN, TITLE_BAND[3] + 20, W - MARGIN, TITLE_BAND[3] + 82), fill=colour,
              panel=PAPER, bold=True, start=48, max_lines=1, valign="middle", gap=0)
    slot = (MARGIN, 330, W - MARGIN, 1180)
    pic = inp.preview.convert("RGB")
    box = paste_fit(card.img, pic, slot)
    # a paper edge so the card reads as a page
    d.rectangle([box[0] - 3, box[1] - 3, box[2] + 2, box[3] + 2], outline=(200, 200, 196), width=3)
    if inp.subject and card.language == "en":    # the subject's name is English: drawn on English pins only
        card.text("subject", [inp.subject], (MARGIN, 1210, W - MARGIN, BRAND_STRIP[1] - 30), fill=MUTED,
                  panel=PAPER, bold=False, start=44, max_lines=2, valign="middle", gap=0)
    return {"image_coverage": round(_area(box), 3), "diagram_coverage": None}


_LAYOUTS = {"explainer": _explainer, "diagram": _diagram, "worksheet": _worksheet}


def render(template: str, inputs: Inputs, language: str = "en") -> tuple[Image.Image, dict]:
    """One pin card and what the prechecks measure on it."""
    if template not in _LAYOUTS:
        raise ValueError(f"unknown pin template {template!r}")
    rtl = _lang(language) in RTL_LANGUAGES
    missing = missing_strings(template, language)
    if missing:
        raise StringMissing(f"{template}: no {_lang(language)} text for {', '.join(missing)}")
    card = _Card(rtl, language)
    colour = subject_colour(inputs.subject)
    extra = _LAYOUTS[template](card, inputs, colour)
    # the bottom-right corner carries background only
    card.draw.rectangle(CLEAR_BOX, fill=BRAND)
    meta = {"template": template, "width": W, "height": H, "rtl": rtl,
            "dominant_color": dominant_color(card.img),
            "min_contrast": min(card.contrasts) if card.contrasts else None,
            "text_boxes": card.text_boxes, **extra}
    return card.img, meta


__all__ = ["STRINGS", "LANGUAGES", "TEMPLATE_STRINGS", "StringMissing", "missing_strings", "string", "W", "H", "SIZE", "TITLE_BAND", "BRAND_STRIP", "CLEAR_BOX", "FONT_FLOOR", "TEMPLATES", "RTL_LANGUAGES",
           "TextDoesNotFit", "Inputs", "render", "contrast", "luminance", "dominant_color", "content_crop",
           "paste_fit", "subject_colour"]
