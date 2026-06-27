"""Agent 5: Slide Builder — constructs PPTX slides and exports PNG images.

Each segment gets a 1280x720 whiteboard-style slide with:
- White (#FAFAFA) background
- Centred image from Agent 4
- Segment title text at the top

Since python-pptx cannot natively export to PNG, the PNG export
uses Pillow to composite the image onto a white canvas.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from pptx import Presentation
from pptx.util import Inches, Pt, Emu

logger = logging.getLogger(__name__)

_WIDTH = 1280
_HEIGHT = 720
_BG_COLOR = "#FAFAFA"
_TITLE_COLOR = "#2C3E50"

# PPTX slide dimensions (widescreen 16:9)
_SLIDE_WIDTH = Emu(12192000)   # 1280 * 9525
_SLIDE_HEIGHT = Emu(6858000)   # 720 * 9525


def build_slide_pptx(
    image_path: str | Path | None,
    title_text: str,
    output_pptx_path: str | Path,
) -> Path:
    """Create a single-slide PPTX with the image and title.

    Returns the output Path.
    """
    output_pptx_path = Path(output_pptx_path)
    output_pptx_path.parent.mkdir(parents=True, exist_ok=True)

    prs = Presentation()
    prs.slide_width = _SLIDE_WIDTH
    prs.slide_height = _SLIDE_HEIGHT

    # Use blank layout
    slide_layout = prs.slide_layouts[6]  # Blank
    slide = prs.slides.add_slide(slide_layout)

    # Add background rectangle (white)
    from pptx.util import Pt as PtUtil
    from pptx.dml.color import RGBColor

    bg = slide.background
    fill = bg.fill
    fill.solid()
    fill.fore_color.rgb = RGBColor(0xFA, 0xFA, 0xFA)

    # Add title text box at the top
    from pptx.util import Inches
    txBox = slide.shapes.add_textbox(
        Emu(640000),   # left margin
        Emu(100000),   # top margin
        Emu(10900000), # width
        Emu(500000),   # height
    )
    tf = txBox.text_frame
    p = tf.paragraphs[0]
    p.text = title_text[:60]
    p.font.size = Pt(20)
    p.font.bold = True
    p.font.color.rgb = RGBColor(0x2C, 0x3E, 0x50)

    # Add image centred below title
    if image_path and Path(image_path).exists():
        img_path = Path(image_path)
        # Calculate centred position (leave room for title)
        img_left = Emu(640000)      # ~0.67 inches from left
        img_top = Emu(800000)       # below title
        img_width = Emu(10900000)   # ~11.4 inches
        img_height = Emu(5700000)   # ~6.0 inches
        slide.shapes.add_picture(
            str(img_path), img_left, img_top, img_width, img_height,
        )

    prs.save(str(output_pptx_path))
    logger.info("PPTX slide saved: %s", output_pptx_path.name)
    return output_pptx_path


def export_slide_png(
    image_path: str | Path | None,
    title_text: str,
    output_png_path: str | Path,
) -> Path:
    """Create a 1280x720 PNG slide image using Pillow.

    This is the image used by the SpeedPaint video engine.
    """
    output_png_path = Path(output_png_path)
    output_png_path.parent.mkdir(parents=True, exist_ok=True)

    canvas = Image.new("RGB", (_WIDTH, _HEIGHT), _BG_COLOR)
    draw = ImageDraw.Draw(canvas)

    # Title text
    try:
        title_font = ImageFont.truetype("arial.ttf", 24)
    except OSError:
        title_font = ImageFont.load_default()

    draw.text(
        (_WIDTH // 2, 30),
        title_text[:60],
        fill=_TITLE_COLOR,
        font=title_font,
        anchor="mt",
    )

    # Composite the generated image
    if image_path and Path(image_path).exists():
        try:
            img = Image.open(str(image_path))
            # Resize to fit within the slide area (below title)
            max_w = _WIDTH - 80  # 40px padding each side
            max_h = _HEIGHT - 100  # room for title + bottom padding
            img.thumbnail((max_w, max_h), Image.LANCZOS)
            # Centre the image
            paste_x = (_WIDTH - img.width) // 2
            paste_y = 70 + (_HEIGHT - 100 - img.height) // 2
            canvas.paste(img, (paste_x, paste_y))
        except Exception as exc:
            logger.warning("Failed to composite image: %s", exc)

    canvas.save(str(output_png_path), "PNG")
    logger.info("Slide PNG exported: %s", output_png_path.name)
    return output_png_path


def create_blank_slide_png(
    title_text: str,
    output_png_path: str | Path,
) -> Path:
    """Create a blank whiteboard slide (no image)."""
    return export_slide_png(None, title_text, output_png_path)
