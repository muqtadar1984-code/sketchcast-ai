"""Arabic text shaping for the PIL slide renderer.

Pillow's ImageDraw does NO cursive shaping and NO bidi: raw Arabic renders as
disconnected, isolated letterforms in the wrong visual order — garbage, not
merely mirrored. The standard fix (arabic_reshaper → presentation-form
codepoints, python-bidi → visual order) is applied HERE, once, at draw time.
The bundled DejaVu fonts include the Arabic presentation forms, so no new
font assets are needed.

Applied to EVERY drawn string, not just Arabic lessons: Islamic-Education
books quote Quran/Hadith in Arabic script inside Malay/English chapters, and
those embedded runs must join correctly too.

Graceful degradation: if the libraries are missing (partial deploy), text
passes through unshaped rather than failing the job.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_AR_RE = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")

try:  # pragma: no cover - import guard
    import arabic_reshaper
    from bidi.algorithm import get_display

    _RESHAPER = arabic_reshaper.ArabicReshaper()
    _AVAILABLE = True
except Exception:  # noqa: BLE001
    _RESHAPER = None
    get_display = None  # type: ignore[assignment]
    _AVAILABLE = False
    logger.warning("arabic_reshaper/python-bidi unavailable — Arabic text will render unshaped")


def contains_arabic(text: str) -> bool:
    return bool(_AR_RE.search(text or ""))


def display_text(text: str, rtl_base: bool = False) -> str:
    """Shape + reorder a string for PIXEL rendering (PIL only — never storage).

    ``rtl_base``: the paragraph's base direction is RTL (an Arabic lesson) —
    affects how mixed runs (numbers, Latin terms) are ordered by bidi.
    """
    if not text or not _AVAILABLE or not contains_arabic(text):
        return text
    try:
        shaped = _RESHAPER.reshape(text)
        return get_display(shaped, base_dir="R" if rtl_base else "L")
    except Exception as exc:  # noqa: BLE001
        logger.warning("text shaping failed (%r…): %s", text[:30], exc)
        return text
