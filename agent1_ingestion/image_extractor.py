"""Diagram / chart / image extraction from PDFs using PyMuPDF."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF

from agent1_ingestion.config import (
    EXTRACTED_IMAGES_DIR,
    IMAGE_CONTEXT_WORDS,
    MIN_IMAGE_HEIGHT,
    MIN_IMAGE_WIDTH,
)


@dataclass
class ExtractedImage:
    filename: str
    page_num: int  # 0-indexed
    context_label: str
    x: float
    y: float
    width: float
    height: float
    file_path: str


def _get_context(page: fitz.Page, rect: fitz.Rect, num_words: int = IMAGE_CONTEXT_WORDS) -> str:
    """
    Get surrounding text for an image by looking at text near the image rectangle.

    Grabs text from the full page and takes words around the approximate
    position of the image.
    """
    full_text = page.get_text("text")
    words = full_text.split()
    if not words:
        return ""

    # Approximate: take up to num_words from the page text as context
    # since positional matching is imprecise, take the first N and last N words
    half = num_words // 2
    before = " ".join(words[:half])
    after = " ".join(words[-half:]) if len(words) > half else ""
    context = f"{before} [...] {after}".strip()
    return context[:500]  # cap length


def extract_images(
    pdf_path: str | Path,
    book_id: str,
) -> list[ExtractedImage]:
    """
    Extract all embedded images from a PDF.

    Images smaller than MIN_IMAGE_WIDTH x MIN_IMAGE_HEIGHT are skipped
    (likely icons or decorations).
    """
    doc = fitz.open(str(pdf_path))
    output_dir = EXTRACTED_IMAGES_DIR / book_id
    output_dir.mkdir(parents=True, exist_ok=True)

    extracted: list[ExtractedImage] = []

    for page_idx in range(len(doc)):
        page = doc[page_idx]
        image_list = page.get_images(full=True)

        for img_index, img_info in enumerate(image_list):
            xref = img_info[0]

            try:
                base_image = doc.extract_image(xref)
            except Exception:
                continue

            if not base_image:
                continue

            img_bytes = base_image["image"]
            img_ext = base_image.get("ext", "png")
            width = base_image.get("width", 0)
            height = base_image.get("height", 0)

            if width < MIN_IMAGE_WIDTH or height < MIN_IMAGE_HEIGHT:
                continue

            filename = f"{book_id}_page{page_idx}_img{img_index}.{img_ext}"
            file_path = output_dir / filename

            with open(file_path, "wb") as f:
                f.write(img_bytes)

            # Try to find the image rect on the page for position info
            img_rects = page.get_image_rects(xref)
            if img_rects:
                rect = img_rects[0]
                x, y = rect.x0, rect.y0
                w, h = rect.width, rect.height
            else:
                x, y, w, h = 0, 0, float(width), float(height)

            context_label = _get_context(page, fitz.Rect(x, y, x + w, y + h))

            extracted.append(
                ExtractedImage(
                    filename=filename,
                    page_num=page_idx,
                    context_label=context_label,
                    x=x,
                    y=y,
                    width=w,
                    height=h,
                    file_path=str(file_path),
                )
            )

    doc.close()
    return extracted
