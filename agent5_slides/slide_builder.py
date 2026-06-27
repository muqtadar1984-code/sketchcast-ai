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


def export_slide_png(
    heading: str,
    points: list[str],
    output_png_path: str | Path,
    footer_text: str = "",
    context_title: str = "",
    fallback_text: str = "",
) -> Path:
    """Render a 1280x720 lesson slide: heading + chapter bullet points."""
    output_png_path = Path(output_png_path)
    output_png_path.parent.mkdir(parents=True, exist_ok=True)

    canvas = Image.new("RGB", (_WIDTH, _HEIGHT), _BG)
    draw = ImageDraw.Draw(canvas)
    max_w = _WIDTH - 2 * _MARGIN_X

    # Context line (small, e.g. episode title)
    y = 52
    if context_title:
        cf = _font(bold=False, size=20)
        draw.text((_MARGIN_X, y), context_title.strip()[:80], fill=_MUTED, font=cf)
        y += 34

    # Heading
    head = (heading or context_title or "SketchCast AI").strip()[:80]
    hf = _font(bold=True, size=40)
    for ln in _wrap(draw, head, hf, max_w)[:2]:
        draw.text((_MARGIN_X, y), ln, fill=_GREEN, font=hf)
        y += 52
    draw.line([(_MARGIN_X, y + 6), (_WIDTH - _MARGIN_X, y + 6)], fill=_RULE, width=2)
    y += 36

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
            draw.ellipse([(_MARGIN_X, y + size // 2 - 3), (_MARGIN_X + 8, y + size // 2 + 5)], fill=_GREEN)
            for j, ln in enumerate(wlines):
                draw.text((_MARGIN_X + 28, y), ln, fill=_INK, font=bf)
                y += line_h
            y += 14
    else:
        # Fallback: no bullets — show the narration text so the slide isn't bare
        bf = _font(bold=False, size=30)
        for ln in _wrap(draw, " ".join((fallback_text or "").split()), bf, max_w)[:9]:
            draw.text((_MARGIN_X, y), ln, fill=_INK, font=bf)
            y += int(30 * 1.45)

    if footer_text:
        ff = _font(bold=False, size=18)
        fw = draw.textlength(footer_text, font=ff)
        draw.text((_WIDTH - _MARGIN_X - fw, _HEIGHT - 50), footer_text, fill=_MUTED, font=ff)

    canvas.save(str(output_png_path), "PNG")
    logger.info("Slide PNG: %s (%d points)", output_png_path.name, len(pts))
    return output_png_path


def build_episode_deck(slides: list[dict], output_pptx_path: str | Path, episode_title: str = "") -> Path:
    """Build one editable PPTX deck: heading + bullets per slide, narration in notes.

    ``slides`` is a list of {"heading", "points", "narration"} dicts.
    """
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.util import Emu, Pt

    output_pptx_path = Path(output_pptx_path)
    output_pptx_path.parent.mkdir(parents=True, exist_ok=True)

    prs = Presentation()
    prs.slide_width = Emu(12192000)
    prs.slide_height = Emu(6858000)

    for s in slides:
        slide = prs.slides.add_slide(prs.slide_layouts[6])  # blank
        bg = slide.background.fill
        bg.solid()
        bg.fore_color.rgb = RGBColor(*_BG)

        box = slide.shapes.add_textbox(Emu(700000), Emu(600000), Emu(10800000), Emu(5200000))
        tf = box.text_frame
        tf.word_wrap = True
        head = tf.paragraphs[0]
        head.text = (s.get("heading") or episode_title or "SketchCast AI")[:90]
        head.font.size = Pt(30)
        head.font.bold = True
        head.font.color.rgb = RGBColor(*_GREEN)

        for pt in s.get("points") or []:
            para = tf.add_paragraph()
            para.text = f"•  {pt}"
            para.font.size = Pt(20)
            para.font.color.rgb = RGBColor(*_INK)

        # Socratic narration → speaker notes
        slide.notes_slide.notes_text_frame.text = s.get("narration") or ""

    prs.save(str(output_pptx_path))
    logger.info("Episode deck saved: %s (%d slides)", output_pptx_path.name, len(slides))
    return output_pptx_path
