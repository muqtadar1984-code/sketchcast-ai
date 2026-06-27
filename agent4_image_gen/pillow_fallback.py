"""Pillow-based placeholder image generator.

Used when the Gemini API is unavailable or fails, so the pipeline
can still exercise the full flow end-to-end.
"""

from __future__ import annotations

import logging
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

_WIDTH = 1280
_HEIGHT = 720
_BG_COLOR = "#FAFAFA"
_BORDER_COLOR = "#CCCCCC"
_TEXT_COLOR = "#666666"
_WATERMARK_COLOR = "#EEEEEE"


def generate_placeholder(
    prompt: str,
    output_path: str | Path,
) -> bool:
    """Create a 1280x720 placeholder PNG with the prompt text centred.

    Returns True on success.
    """
    try:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        img = Image.new("RGB", (_WIDTH, _HEIGHT), _BG_COLOR)
        draw = ImageDraw.Draw(img)

        # Border
        draw.rectangle(
            [10, 10, _WIDTH - 10, _HEIGHT - 10],
            outline=_BORDER_COLOR,
            width=2,
        )

        # Watermark
        try:
            wm_font = ImageFont.truetype("arial.ttf", 80)
        except OSError:
            wm_font = ImageFont.load_default()
        draw.text(
            (_WIDTH // 2, _HEIGHT // 2 - 60),
            "PLACEHOLDER",
            fill=_WATERMARK_COLOR,
            font=wm_font,
            anchor="mm",
        )

        # Prompt text wrapped
        try:
            txt_font = ImageFont.truetype("arial.ttf", 18)
        except OSError:
            txt_font = ImageFont.load_default()

        wrapped = textwrap.fill(prompt, width=80)
        lines = wrapped.split("\n")[:8]  # max 8 lines
        y = _HEIGHT // 2 + 20
        for line in lines:
            draw.text((_WIDTH // 2, y), line, fill=_TEXT_COLOR, font=txt_font, anchor="mm")
            y += 24

        img.save(str(output_path), "PNG")
        logger.info("Pillow placeholder saved: %s", output_path.name)
        return True

    except Exception as exc:
        logger.error("Pillow fallback failed: %s", exc)
        return False
