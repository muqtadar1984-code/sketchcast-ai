"""Spike: enhance a basic slide PNG into an illustrated slide via Nano Banana Pro
(Gemini 3 Pro Image). Image-edit call — keeps the slide text, adds illustration."""

from __future__ import annotations

import io
import os
from pathlib import Path

DEFAULT_MODEL = "gemini-3-pro-image-preview"
# Tried in order; first one that returns an image wins. Pro is best for slides;
# Flash is the cheaper Nano Banana. Both require billing/credits on the key.
CANDIDATE_MODELS = [
    "gemini-3-pro-image-preview",
    "gemini-2.5-flash-image",
]

DEFAULT_INSTRUCTION = (
    "You are designing a polished classroom lesson slide. Take this slide and "
    "redraw it as a clean, visually engaging illustrated slide for school students. "
    "Keep ALL of the existing text EXACTLY as written and clearly legible. Add a "
    "relevant, simple illustration or diagram and tasteful layout that reinforces "
    "the concept. 16:9 landscape, uncluttered, professional."
)


def _api_key(explicit: str | None = None) -> str:
    key = explicit or os.getenv("GOOGLE_AI_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("Set GOOGLE_AI_API_KEY (or GEMINI_API_KEY) for Nano Banana Pro.")
    return key


def _extract_image(resp, output_png: Path) -> bool:
    from PIL import Image as PILImage
    for cand in resp.candidates or []:
        for part in (cand.content.parts or []):
            inline = getattr(part, "inline_data", None)
            if inline is not None and inline.data:
                PILImage.open(io.BytesIO(inline.data)).convert("RGB").save(str(output_png), "PNG")
                return True
    return False


def enhance_slide(
    input_png: str | Path,
    output_png: str | Path,
    instruction: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
) -> Path:
    """Edit `input_png` → `output_png` with Nano Banana Pro (Gemini 3 Pro Image),
    falling back to the Nano Banana Flash models if Pro isn't available."""
    from google import genai
    from google.genai import types

    from PIL import Image as PILImage

    client = genai.Client(api_key=_api_key(api_key))
    src = PILImage.open(str(input_png))
    contents = [instruction or DEFAULT_INSTRUCTION, src]
    output_png = Path(output_png)
    output_png.parent.mkdir(parents=True, exist_ok=True)

    models = [model] if model else CANDIDATE_MODELS
    last_err: Exception | None = None
    for m in models:
        try:
            try:
                resp = client.models.generate_content(model=m, contents=contents)
            except Exception:
                resp = client.models.generate_content(
                    model=m, contents=contents,
                    config=types.GenerateContentConfig(response_modalities=["Text", "Image"]),
                )
            if _extract_image(resp, output_png):
                print(f"   (enhanced with {m})")
                return output_png
            last_err = RuntimeError(f"{m}: no image in response")
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            print(f"   ({m} unavailable: {str(exc)[:90]})")
    raise RuntimeError(f"Nano Banana enhancement failed for all models: {last_err}")
