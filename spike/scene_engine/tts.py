"""Narration for scenes — a thin wrapper over shared/tts.

Reuses the existing provider-agnostic registry (free Edge default, premium
gate untouched) and the repo's rule that TIMING TRUTH IS THE MEASURED MP3:
`narrate` returns the actual duration read back from the file via ffmpeg, and
0.0 on any failure — the caller then renders the scene silent (anullsrc path),
because a lesson never dies over a TTS hiccup.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

from shared.text_clean import strip_ssml
from shared.tts import synthesize

from .encode import ffmpeg_exe

logger = logging.getLogger(__name__)

_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)")


def audio_duration(path: str | Path) -> float:
    proc = subprocess.run([ffmpeg_exe(), "-i", str(path)], capture_output=True, text=True)
    m = _DUR_RE.search(proc.stderr or "")
    if not m:
        return 0.0
    h, mnt, s = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + float(s)


def narrate(text: str, out_path: str | Path, voice_id: str | None = None) -> float:
    """Synthesize narration; return MEASURED seconds (0.0 = failed => silent)."""
    out_path = Path(out_path)
    clean = strip_ssml(text).strip()
    if not clean:
        return 0.0
    try:
        synthesize(clean, out_path, voice_id=voice_id)
    except Exception:
        logger.exception("TTS failed; scene will render silent")
        return 0.0
    if not out_path.exists() or out_path.stat().st_size == 0:
        return 0.0
    return audio_duration(out_path)
