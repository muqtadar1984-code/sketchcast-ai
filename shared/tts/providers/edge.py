"""Edge TTS provider — Microsoft online voices, free, no API key."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path


def synthesize(text: str, out_path: Path, ref_voice: str,
               boundaries_out: Path | None = None) -> None:
    """Render `text` to MP3. When `boundaries_out` is given, the streaming API
    is used so Edge's WordBoundary events are captured — a JSON list of
    {"t": seconds, "w": word} written beside the audio. Word-level timestamps
    make narration-cued animation frame-accurate instead of char-midpoint
    approximate; the file is best-effort (its absence never fails TTS)."""
    import edge_tts

    clean = " ".join((text or "").split())
    if not clean:
        raise ValueError("empty text for TTS")

    async def _go() -> list[dict]:
        comm = edge_tts.Communicate(clean, ref_voice)
        words: list[dict] = []
        if boundaries_out is None:
            await comm.save(str(out_path))
            return words
        with open(out_path, "wb") as f:
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    f.write(chunk["data"])
                elif chunk["type"] == "WordBoundary":
                    # offset arrives in 100-nanosecond ticks
                    words.append({"t": chunk["offset"] / 1e7,
                                  "w": str(chunk.get("text", ""))})
        return words

    loop = asyncio.new_event_loop()  # fresh loop is safe inside worker threads
    try:
        words = loop.run_until_complete(_go())
    finally:
        loop.close()

    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError("edge-tts produced no audio")

    if boundaries_out is not None and words:
        try:
            Path(boundaries_out).write_text(
                json.dumps(words, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass  # timing degrades to char-midpoint, audio is intact
