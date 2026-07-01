"""Edge TTS provider — Microsoft online voices, free, no API key."""

from __future__ import annotations

import asyncio
from pathlib import Path


def synthesize(text: str, out_path: Path, ref_voice: str) -> None:
    import edge_tts

    clean = " ".join((text or "").split())
    if not clean:
        raise ValueError("empty text for TTS")

    async def _go():
        await edge_tts.Communicate(clean, ref_voice).save(str(out_path))

    loop = asyncio.new_event_loop()  # fresh loop is safe inside worker threads
    try:
        loop.run_until_complete(_go())
    finally:
        loop.close()

    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError("edge-tts produced no audio")
