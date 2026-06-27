"""Google Gemini image generation client."""

from __future__ import annotations

import io
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Style suffix appended to every prompt for consistent output
_STYLE_SUFFIX = (
    " Clean line art on white background, no shading, no color, "
    "educational illustration, simple and clear, 1280x720 aspect ratio."
)


def _get_api_key() -> str:
    """Read Google AI API key from Streamlit secrets with env var fallback."""
    try:
        import streamlit as st
        key = st.secrets.get("GOOGLE_AI_API_KEY", "")
        if key:
            return key
    except Exception:
        pass
    key = os.getenv("GOOGLE_AI_API_KEY", "")
    if not key:
        raise RuntimeError(
            "GOOGLE_AI_API_KEY not configured. "
            "Add it to Streamlit secrets or set as environment variable."
        )
    return key


def generate_image(
    prompt: str,
    output_path: str | Path,
    negative_prompt: str = "",
) -> bool:
    """Generate an image using Google Gemini and save as PNG.

    Returns True on success, False on failure (caller should use fallback).
    """
    try:
        from google import genai
        from google.genai import types

        api_key = _get_api_key()
        client = genai.Client(api_key=api_key)

        full_prompt = prompt + _STYLE_SUFFIX
        if negative_prompt:
            full_prompt += f" Avoid: {negative_prompt}"

        logger.info("Gemini image request: %.100s...", full_prompt)

        response = client.models.generate_content(
            model="gemini-2.0-flash-exp",
            contents=full_prompt,
            config=types.GenerateContentConfig(
                response_modalities=["Text", "Image"],
            ),
        )

        # Extract image from response parts
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        for part in response.candidates[0].content.parts:
            if part.inline_data is not None:
                from PIL import Image as PILImage

                image = PILImage.open(io.BytesIO(part.inline_data.data))
                # Resize to 1280x720 if needed
                if image.size != (1280, 720):
                    image = image.resize((1280, 720), PILImage.LANCZOS)
                image.save(str(output_path), "PNG")
                logger.info("Gemini image saved: %s (%d bytes)",
                            output_path.name, output_path.stat().st_size)
                return True

        logger.warning("Gemini response contained no image data")
        return False

    except Exception as exc:
        logger.warning("Gemini image generation failed: %s", exc)
        return False
