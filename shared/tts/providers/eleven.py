"""ElevenLabs provider — premium, gated + metered. Key from env only, never logged."""

from __future__ import annotations

import os
from pathlib import Path


def _api_key() -> str:
    key = os.getenv("ELEVENLABS_API_KEY", "")
    if not key:
        raise RuntimeError("ELEVENLABS_API_KEY not configured")
    return key


def synthesize(text: str, out_path: Path, ref_voice: str) -> None:
    """Write ElevenLabs MP3 for `text` (may include <break/> markup) to out_path."""
    from elevenlabs import ElevenLabs

    clean = (text or "").strip()
    if not clean:
        raise ValueError("empty text for TTS")

    client = ElevenLabs(api_key=_api_key())
    audio = client.text_to_speech.convert(
        voice_id=ref_voice,
        text=clean,
        model_id=os.getenv("ELEVENLABS_MODEL", "eleven_turbo_v2"),
        output_format="mp3_44100_128",
    )
    with open(out_path, "wb") as f:
        for chunk in audio:
            if chunk:
                f.write(chunk)

    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError("elevenlabs produced no audio")
