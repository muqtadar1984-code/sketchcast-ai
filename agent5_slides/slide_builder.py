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


def build_episode_deck(
    slides: list[dict],
    output_pptx_path: str | Path,
    episode_title: str = "",
    template: str | None = None,
    accent: tuple[int, int, int] | None = None,
) -> Path:
    """Build one editable PPTX deck: heading + bullets per slide, narration in notes.

    ``slides`` is a list of {"heading", "points", "narration"} dicts.
    When ``template`` (a school .pptx) is given, the deck inherits its theme,
    master, fonts and logo; ``accent`` overrides the heading colour.
    """
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.util import Emu, Pt

    output_pptx_path = Path(output_pptx_path)
    output_pptx_path.parent.mkdir(parents=True, exist_ok=True)

    prs = Presentation(template) if template else Presentation()
    if not template:
        prs.slide_width = Emu(12192000)
        prs.slide_height = Emu(6858000)
    # Prefer a blank layout; fall back gracefully for arbitrary templates.
    layout = prs.slide_layouts[6] if len(prs.slide_layouts) > 6 else prs.slide_layouts[-1]
    head_rgb = RGBColor(*(accent or _GREEN))

    for s in slides:
        slide = prs.slides.add_slide(layout)
        if not template:  # keep the cream bg only for the default style
            bg = slide.background.fill
            bg.solid()
            bg.fore_color.rgb = RGBColor(*_BG)

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
            if not template:
                para.font.color.rgb = RGBColor(*_INK)

        # Socratic narration → speaker notes
        slide.notes_slide.notes_text_frame.text = s.get("narration") or ""

    prs.save(str(output_pptx_path))
    logger.info("Episode deck saved: %s (%d slides, themed=%s)", output_pptx_path.name, len(slides), bool(template))
    return output_pptx_path
