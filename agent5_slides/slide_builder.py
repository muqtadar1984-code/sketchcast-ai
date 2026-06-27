"""Agent 5: Slide Builder — content slides (PNG for video, PPTX for download).

Each segment becomes a clean 1280x720 lesson slide in the Scholar palette:
- cream background, a green episode header, the narration text as readable body.

Rendering uses bundled DejaVu fonts (shipped in ./fonts) so it works on any
OS — the old code asked for ``arial.ttf``, which doesn't exist on Linux, so
text rendered in a tiny fallback font and slides looked blank.

The PPTX version puts a short heading on the slide and the full Socratic
narration into the speaker notes — the editable-deck deliverable.
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
    title_text: str,
    body_text: str,
    output_png_path: str | Path,
    footer_text: str = "",
) -> Path:
    """Render a 1280x720 lesson slide PNG used as the video background."""
    output_png_path = Path(output_png_path)
    output_png_path.parent.mkdir(parents=True, exist_ok=True)

    canvas = Image.new("RGB", (_WIDTH, _HEIGHT), _BG)
    draw = ImageDraw.Draw(canvas)

    # Header (episode title) + divider
    header_font = _font(bold=True, size=30)
    head = (title_text or "SketchCast AI").strip()[:70]
    draw.text((_MARGIN_X, 56), head, fill=_GREEN, font=header_font)
    draw.line([(_MARGIN_X, 104), (_WIDTH - _MARGIN_X, 104)], fill=_RULE, width=2)

    # Body (narration) — wrapped, size auto-shrinks for long text
    max_w = _WIDTH - 2 * _MARGIN_X
    body = " ".join((body_text or "").split())
    body_size = 38
    while body_size >= 22:
        body_font = _font(bold=False, size=body_size)
        lines = _wrap(draw, body, body_font, max_w)
        line_h = int(body_size * 1.42)
        block_h = line_h * len(lines)
        if block_h <= (_HEIGHT - 200) or body_size == 22:
            break
        body_size -= 2

    start_y = 104 + max(40, (_HEIGHT - 104 - 90 - block_h) // 2)
    y = start_y
    for ln in lines:
        draw.text((_MARGIN_X, y), ln, fill=_INK, font=body_font)
        y += line_h

    # Footer label
    if footer_text:
        ff = _font(bold=False, size=18)
        fw = draw.textlength(footer_text, font=ff)
        draw.text((_WIDTH - _MARGIN_X - fw, _HEIGHT - 50), footer_text, fill=_MUTED, font=ff)

    canvas.save(str(output_png_path), "PNG")
    logger.info("Slide PNG exported: %s (body %dpx, %d lines)", output_png_path.name, body_size, len(lines))
    return output_png_path


def build_slide_pptx(
    title_text: str,
    body_text: str,
    output_pptx_path: str | Path,
) -> Path:
    """Create a single-slide PPTX: heading on the slide, narration in notes."""
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.util import Emu, Pt

    output_pptx_path = Path(output_pptx_path)
    output_pptx_path.parent.mkdir(parents=True, exist_ok=True)

    prs = Presentation()
    prs.slide_width = Emu(12192000)
    prs.slide_height = Emu(6858000)
    slide = prs.slides.add_slide(prs.slide_layouts[6])  # blank

    bg = slide.background.fill
    bg.solid()
    bg.fore_color.rgb = RGBColor(*_BG)

    box = slide.shapes.add_textbox(Emu(700000), Emu(700000), Emu(10800000), Emu(4800000))
    tf = box.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = (title_text or "SketchCast AI")[:80]
    p.font.size = Pt(28)
    p.font.bold = True
    p.font.color.rgb = RGBColor(*_GREEN)

    p2 = tf.add_paragraph()
    p2.text = body_text or ""
    p2.font.size = Pt(18)
    p2.font.color.rgb = RGBColor(*_INK)

    # Socratic narration → speaker notes (the editable-deck deliverable)
    slide.notes_slide.notes_text_frame.text = body_text or ""

    prs.save(str(output_pptx_path))
    logger.info("PPTX slide saved: %s", output_pptx_path.name)
    return output_pptx_path


def create_blank_slide_png(title_text: str, output_png_path: str | Path) -> Path:
    """Back-compat shim — a slide with only a heading."""
    return export_slide_png(title_text, "", output_png_path)
