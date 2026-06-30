"""Agent 5: Slide Builder — content slides (PNG for video, PPTX for download).

Each segment becomes a clean 1280x720 lesson slide in the Scholar palette
showing the TEXTBOOK CHAPTER CONTENT (a heading + key bullet points) — not the
narration. The spoken Socratic narration is what plays as audio and what goes
into the PPTX speaker notes.

Rendering uses bundled DejaVu fonts (shipped in ./fonts) so it works on any OS.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

_WIDTH = 1280
_HEIGHT = 720

# Scholar palette
_BG = (251, 246, 236)        # cream  #FBF6EC
_INK = (44, 42, 38)          # near-black #2C2A26
_GREEN = (46, 107, 78)       # forest green #2E6B4E
_MUTED = (111, 106, 95)      # #6F6A5F
_RULE = (224, 217, 200)      # divider

_FONT_DIR = Path(__file__).resolve().parent / "fonts"
_MARGIN_X = 90


def _font(bold: bool, size: int) -> ImageFont.FreeTypeFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    return ImageFont.truetype(str(_FONT_DIR / name), size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    """Greedy word-wrap to a pixel width."""
    words = text.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=font) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


# An element is an ordered list of per-line bounding boxes (x0, y0, x1, y1) to
# reveal left→right; a slide's animation is an ordered list of such elements.
_Box = tuple[int, int, int, int]


def compose_slide(
    heading: str,
    points: list[str],
    footer_text: str = "",
    context_title: str = "",
    fallback_text: str = "",
    accent: tuple[int, int, int] | None = None,
    logo_path: str | None = None,
    visual: dict | None = None,
) -> tuple[Image.Image, list[list[_Box]], list[_Box]]:
    """Render the canonical 1280x720 lesson slide and report its object layout.

    Returns ``(image, anim_elements, static_boxes)``:

    * ``image`` is the fully-drawn slide (what :func:`export_slide_png` saves and
      what the Agent 6 native renderer animates toward — pixel-identical).
    * ``anim_elements`` is the content (context line, heading, divider, bullets)
      in *teaching order*; each element is a list of per-line boxes so the
      renderer can write it on left→right.
    * ``static_boxes`` are regions (logo, footer) shown from the first frame.

    ``accent`` (school brand colour) overrides the heading colour; ``logo_path``
    stamps a logo top-right. Both fall back to the default Scholar style.
    """
    acc = accent or _GREEN
    canvas = Image.new("RGB", (_WIDTH, _HEIGHT), _BG)
    draw = ImageDraw.Draw(canvas)
    max_w = _WIDTH - 2 * _MARGIN_X
    anim: list[list[_Box]] = []
    static: list[_Box] = []

    # Optional school logo, top-right (present from the first frame).
    if logo_path:
        try:
            logo = Image.open(logo_path).convert("RGBA")
            lh = 56
            lw = max(1, int(logo.width * lh / max(1, logo.height)))
            logo = logo.resize((lw, lh))
            lx, ly = _WIDTH - _MARGIN_X - lw, 36
            canvas.paste(logo, (lx, ly), logo)
            static.append((lx, ly, lx + lw, ly + lh))
        except Exception:  # noqa: BLE001
            pass

    # Context line (small, e.g. episode title)
    y = 52
    if context_title:
        cf = _font(bold=False, size=20)
        txt = context_title.strip()[:80]
        draw.text((_MARGIN_X, y), txt, fill=_MUTED, font=cf)
        anim.append([(_MARGIN_X, y, _MARGIN_X + int(draw.textlength(txt, font=cf)), y + 26)])
        y += 34

    # Heading
    head = (heading or context_title or "SketchCast AI").strip()[:80]
    hf = _font(bold=True, size=40)
    hbox: list[_Box] = []
    for ln in _wrap(draw, head, hf, max_w)[:2]:
        draw.text((_MARGIN_X, y), ln, fill=acc, font=hf)
        hbox.append((_MARGIN_X, y, _MARGIN_X + int(draw.textlength(ln, font=hf)), y + 50))
        y += 52
    if hbox:
        anim.append(hbox)

    draw.line([(_MARGIN_X, y + 6), (_WIDTH - _MARGIN_X, y + 6)], fill=_RULE, width=2)
    anim.append([(_MARGIN_X, y + 3, _WIDTH - _MARGIN_X, y + 10)])  # divider grows L→R
    y += 36

    # A composable diagram (Phase 2) claims the content area when present; it
    # falls back to plain bullets if the spec is unusable, so slides are never bare.
    diagram_elems: list[list[_Box]] = []
    if visual:
        from .diagram_builder import caption_element, render_diagram

        region = (_MARGIN_X, y + 4, _WIDTH - _MARGIN_X, _HEIGHT - 96)
        diagram_elems = render_diagram(draw, region, visual, accent=acc)
        if diagram_elems:
            anim.extend(diagram_elems)
            cap = caption_element(draw, region, visual.get("caption", "") if isinstance(visual, dict) else "")
            if cap:
                anim.append(cap)

    if not diagram_elems:
        pts = [p for p in (points or []) if p.strip()]
        if pts:
            # Bullet points (chapter content)
            size = 32
            while size >= 20:
                bf = _font(bold=False, size=size)
                line_h = int(size * 1.5)
                wrapped: list[list[str]] = [_wrap(draw, p, bf, max_w - 40) for p in pts]
                total_h = sum(len(w) * line_h + 14 for w in wrapped)
                if total_h <= (_HEIGHT - y - 70) or size == 20:
                    break
                size -= 2
            for wlines in wrapped:
                ebox: list[_Box] = []
                draw.ellipse([(_MARGIN_X, y + size // 2 - 3), (_MARGIN_X + 8, y + size // 2 + 5)], fill=_GREEN)
                for j, ln in enumerate(wlines):
                    draw.text((_MARGIN_X + 28, y), ln, fill=_INK, font=bf)
                    x0 = _MARGIN_X if j == 0 else _MARGIN_X + 28  # first line includes the bullet
                    ebox.append((x0, y, _MARGIN_X + 28 + int(draw.textlength(ln, font=bf)), y + size))
                    y += line_h
                if ebox:
                    anim.append(ebox)
                y += 14
        else:
            # Fallback: no bullets — show the narration text so the slide isn't bare
            bf = _font(bold=False, size=30)
            fb: list[_Box] = []
            for ln in _wrap(draw, " ".join((fallback_text or "").split()), bf, max_w)[:9]:
                draw.text((_MARGIN_X, y), ln, fill=_INK, font=bf)
                fb.append((_MARGIN_X, y, _MARGIN_X + int(draw.textlength(ln, font=bf)), y + 30))
                y += int(30 * 1.45)
            if fb:
                anim.append(fb)

    if footer_text:
        ff = _font(bold=False, size=18)
        fw = int(draw.textlength(footer_text, font=ff))
        fx = _WIDTH - _MARGIN_X - fw
        draw.text((fx, _HEIGHT - 50), footer_text, fill=_MUTED, font=ff)
        static.append((fx, _HEIGHT - 50, _WIDTH - _MARGIN_X, _HEIGHT - 50 + 22))

    return canvas, anim, static


def export_slide_png(
    heading: str,
    points: list[str],
    output_png_path: str | Path,
    footer_text: str = "",
    context_title: str = "",
    fallback_text: str = "",
    accent: tuple[int, int, int] | None = None,
    logo_path: str | None = None,
    visual: dict | None = None,
) -> Path:
    """Render a 1280x720 lesson slide PNG: heading + chapter bullets or a diagram.

    Thin wrapper over :func:`compose_slide` (the single source of slide layout,
    shared with the Agent 6 native video renderer) that just saves the image.
    """
    output_png_path = Path(output_png_path)
    output_png_path.parent.mkdir(parents=True, exist_ok=True)

    canvas, _anim, _static = compose_slide(
        heading=heading,
        points=points,
        footer_text=footer_text,
        context_title=context_title,
        fallback_text=fallback_text,
        accent=accent,
        logo_path=logo_path,
        visual=visual,
    )
    canvas.save(str(output_png_path), "PNG")
    logger.info("Slide PNG: %s (%d points)", output_png_path.name, len([p for p in (points or []) if p.strip()]))
    return output_png_path


# "Live Ink" deck palette (matches the web app): one dominant ink, white content,
# a single teal accent carried by the repeated icon-in-circle motif.
_LI_INK = (0x14, 0x18, 0x1F)
_LI_WHITE = (0xFF, 0xFF, 0xFF)
_LI_TEAL = (0x1F, 0xB8, 0xA6)
_LI_TEAL_DK = (0x0C, 0x81, 0x75)
_LI_GRAPHITE = (0x5B, 0x64, 0x70)
_LI_MIST = (0xF5, 0xF6, 0xF3)
_LI_DIM = (0x9A, 0xA1, 0xAA)


def build_episode_deck(
    slides: list[dict],
    output_pptx_path: str | Path,
    episode_title: str = "",
    template: str | None = None,
    accent: tuple[int, int, int] | None = None,
) -> Path:
    """Build one editable PPTX deck: a designed lesson deck, narration in notes.

    ``slides`` is a list of {"heading", "points", "narration"} dicts. Without a
    ``template`` it builds the designed "Live Ink" deck (dark title/closing,
    light content, icon-in-circle motif, alternating layouts). When a school
    ``template`` (.pptx) is given the deck inherits that theme instead; ``accent``
    overrides the brand colour in both.
    """
    output_pptx_path = Path(output_pptx_path)
    output_pptx_path.parent.mkdir(parents=True, exist_ok=True)
    if template:
        return _build_branded_deck(slides, output_pptx_path, episode_title, template, accent)
    return _build_designed_deck(slides, output_pptx_path, episode_title, accent)


def _build_branded_deck(slides, output_pptx_path, episode_title, template, accent):
    """School-template path — inherit the uploaded theme (unchanged behaviour)."""
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.util import Emu, Pt

    prs = Presentation(template)
    layout = prs.slide_layouts[6] if len(prs.slide_layouts) > 6 else prs.slide_layouts[-1]
    head_rgb = RGBColor(*(accent or _GREEN))
    for s in slides:
        slide = prs.slides.add_slide(layout)
        box = slide.shapes.add_textbox(Emu(700000), Emu(900000), Emu(10800000), Emu(5000000))
        tf = box.text_frame
        tf.word_wrap = True
        head = tf.paragraphs[0]
        head.text = (s.get("heading") or episode_title or "SketchCast AI")[:90]
        head.font.size = Pt(30)
        head.font.bold = True
        head.font.color.rgb = head_rgb
        for pt in s.get("points") or []:
            para = tf.add_paragraph()
            para.text = f"•  {pt}"
            para.font.size = Pt(20)
        slide.notes_slide.notes_text_frame.text = s.get("narration") or ""
    prs.save(str(output_pptx_path))
    logger.info("Episode deck (branded) saved: %s (%d content slides)", output_pptx_path.name, len(slides))
    return output_pptx_path


def _build_designed_deck(slides, output_pptx_path, episode_title, accent):
    """Default path — a designed "Live Ink" deck built deterministically."""
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
    from pptx.util import Emu, Pt

    IN = 914400
    INK, WHITE = RGBColor(*_LI_INK), RGBColor(*_LI_WHITE)
    ACC, ACC_DK = RGBColor(*(accent or _LI_TEAL)), RGBColor(*_LI_TEAL_DK)
    GRA, MIST, DIM = RGBColor(*_LI_GRAPHITE), RGBColor(*_LI_MIST), RGBColor(*_LI_DIM)
    BODY = "Calibri"

    prs = Presentation()
    prs.slide_width = Emu(12192000)
    prs.slide_height = Emu(6858000)
    blank = prs.slide_layouts[6] if len(prs.slide_layouts) > 6 else prs.slide_layouts[-1]

    def new_slide(bg):
        s = prs.slides.add_slide(blank)
        f = s.background.fill
        f.solid()
        f.fore_color.rgb = bg
        return s

    def textbox(s, l, t, w, h, anchor=MSO_ANCHOR.TOP):
        tf = s.shapes.add_textbox(int(l), int(t), int(w), int(h)).text_frame
        tf.word_wrap = True
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
        tf.vertical_anchor = anchor
        return tf

    def para(tf, text, size, color, bold=False, first=False, space=6):
        p = tf.paragraphs[0] if first else tf.add_paragraph()
        p.alignment = PP_ALIGN.LEFT
        p.space_before = Pt(0)
        p.space_after = Pt(space)
        r = p.add_run()
        r.text = text
        r.font.size = Pt(size)
        r.font.bold = bold
        r.font.name = BODY
        r.font.color.rgb = color
        return p

    def circle(s, cx, cy, d, fill, label="", label_size=15):
        o = s.shapes.add_shape(MSO_SHAPE.OVAL, int(cx - d / 2), int(cy - d / 2), int(d), int(d))
        o.fill.solid()
        o.fill.fore_color.rgb = fill
        o.line.fill.background()
        o.shadow.inherit = False
        if label:
            tf = o.text_frame
            tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
            tf.vertical_anchor = MSO_ANCHOR.MIDDLE
            p = tf.paragraphs[0]
            p.alignment = PP_ALIGN.CENTER
            r = p.add_run()
            r.text = label
            r.font.size = Pt(label_size)
            r.font.bold = True
            r.font.name = BODY
            r.font.color.rgb = WHITE
        return o

    def panel(s, l, t, w, h, fill):
        sh = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, int(l), int(t), int(w), int(h))
        sh.fill.solid()
        sh.fill.fore_color.rgb = fill
        sh.line.fill.background()
        sh.shadow.inherit = False
        return sh

    def heading_block(s, n, heading):
        circle(s, 0.95 * IN, 1.02 * IN, 0.52 * IN, ACC, str(n))
        tf = textbox(s, 1.45 * IN, 0.7 * IN, 11.1 * IN, 0.95 * IN, anchor=MSO_ANCHOR.MIDDLE)
        para(tf, (heading or episode_title or "Lesson")[:90], 27, INK, bold=True, first=True, space=0)

    def bullet_rows(s, points, left, top, width, size=16, step=0.66):
        y = top
        for pt in points:
            circle(s, left + 0.09 * IN, y + 0.13 * IN, 0.17 * IN, ACC)
            tf = textbox(s, left + 0.36 * IN, y - 0.03 * IN, width - 0.36 * IN, step * IN)
            para(tf, str(pt), size, INK, first=True, space=0)
            y += step * IN

    # Title (dark)
    s = new_slide(INK)
    circle(s, 1.0 * IN, 0.95 * IN, 0.62 * IN, ACC, "S", label_size=20)
    tf = textbox(s, 1.0 * IN, 2.45 * IN, 11.3 * IN, 2.3 * IN)
    para(tf, episode_title or "SketchCast Lesson", 40, WHITE, bold=True, first=True, space=8)
    para(tf, "A narrated, Socratic lesson", 18, ACC, space=0)
    para(textbox(s, 1.0 * IN, 6.7 * IN, 11.3 * IN, 0.5 * IN), "SketchCast AI", 12, DIM, first=True, space=0)

    # Content (light), alternating layouts
    for i, sd in enumerate(slides, 1):
        heading = (sd.get("heading") or "").strip()
        points = [str(p).strip() for p in (sd.get("points") or []) if str(p).strip()][:5]
        s = new_slide(WHITE)
        heading_block(s, i, heading)
        if not points:
            para(textbox(s, 1.45 * IN, 2.2 * IN, 10.8 * IN, 3.0 * IN),
                 "Pause here — discuss this together before moving on.", 16, GRA, first=True, space=0)
        elif i % 2 == 1:
            bullet_rows(s, points, 0.95 * IN, 2.15 * IN, 11.0 * IN)
        else:
            panel(s, 0.95 * IN, 2.1 * IN, 5.2 * IN, 2.85 * IN, MIST)
            tf = textbox(s, 1.28 * IN, 2.32 * IN, 4.55 * IN, 2.4 * IN, anchor=MSO_ANCHOR.MIDDLE)
            para(tf, "KEY IDEA", 11, ACC_DK, bold=True, first=True, space=8)
            para(tf, points[0], 19, INK, bold=True, space=0)
            if len(points) > 1:
                bullet_rows(s, points[1:], 6.5 * IN, 2.25 * IN, 5.65 * IN, size=15, step=0.62)
        s.notes_slide.notes_text_frame.text = sd.get("narration") or ""

    # Closing (dark)
    s = new_slide(INK)
    circle(s, 1.0 * IN, 0.95 * IN, 0.62 * IN, ACC)
    tf = textbox(s, 1.0 * IN, 2.5 * IN, 11.3 * IN, 2.4 * IN)
    para(tf, "Ready to teach.", 34, WHITE, bold=True, first=True, space=8)
    para(tf, episode_title or "", 18, ACC, space=12)
    para(tf, "Every slide's speaker notes carry the Socratic questions.", 14, DIM, space=0)
    para(textbox(s, 1.0 * IN, 6.7 * IN, 11.3 * IN, 0.5 * IN), "SketchCast AI", 12, DIM, first=True, space=0)

    prs.save(str(output_pptx_path))
    logger.info("Episode deck (designed) saved: %s (%d content slides)", output_pptx_path.name, len(slides))
    return output_pptx_path
