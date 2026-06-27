"""Agent 6: Text-to-speech via Microsoft Edge TTS (free, no API key).

Edge TTS streams from Microsoft's online voices over the network — no key,
no per-character cost — which is the right fit for the freemium tier. The
voice is configurable via the SKETCHCAST_TTS_VOICE env var.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# A clear, neutral default. Override with SKETCHCAST_TTS_VOICE (e.g.
# "en-IN-NeerjaNeural", "en-GB-SoniaNeural", "en-US-GuyNeural").
_DEFAULT_VOICE = "en-US-AriaNeural"


def default_voice() -> str:
    return os.getenv("SKETCHCAST_TTS_VOICE", _DEFAULT_VOICE)


def _run(coro):
    """Run a coroutine on a fresh event loop (safe inside Streamlit threads)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def synthesize(text: str, out_path: str | Path, voice: str | None = None) -> Path:
    """Synthesize ``text`` to an MP3 at ``out_path``. Returns the path.

    Raises if synthesis fails (caller decides whether to fall back to silence).
    """
    import edge_tts

    voice = voice or default_voice()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    clean = " ".join((text or "").split())
    if not clean:
        raise ValueError("empty text for TTS")

    async def _go():
        communicate = edge_tts.Communicate(clean, voice)
        await communicate.save(str(out_path))

    _run(_go())
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError("edge-tts produced no audio")
    logger.info("TTS synthesized: %s (%d bytes, voice=%s)", out_path.name, out_path.stat().st_size, voice)
    return out_path
