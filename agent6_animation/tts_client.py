"""ElevenLabs TTS client — generates MP3 audio from script text.

Ported verbatim from agent5_audio/tts_client.py into the new Agent 6
animation module, which now owns both audio generation and video composition.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Defaults — model is configurable, voice ID always from secrets/env
DEFAULT_MODEL = "eleven_turbo_v2"
INTER_CALL_DELAY = 0.5  # seconds between sequential API calls
MAX_RETRIES = 1


def _get_api_key() -> str:
    """Read ElevenLabs API key from Streamlit secrets with env var fallback."""
    try:
        import streamlit as st
        key = st.secrets.get("ELEVENLABS_API_KEY", "")
        if key:
            return key
    except Exception:
        pass
    key = os.getenv("ELEVENLABS_API_KEY", "")
    if not key:
        raise RuntimeError(
            "ELEVENLABS_API_KEY not configured. "
            "Add it to Streamlit secrets or set as environment variable."
        )
    return key


def _get_voice_id() -> str:
    """Read voice ID from Streamlit secrets with env var fallback."""
    try:
        import streamlit as st
        vid = st.secrets.get("ELEVENLABS_VOICE_ID", "")
        if vid:
            return vid
    except Exception:
        pass
    vid = os.getenv("ELEVENLABS_VOICE_ID", "")
    if not vid:
        raise RuntimeError(
            "ELEVENLABS_VOICE_ID not configured. "
            "Add it to Streamlit secrets or set as environment variable."
        )
    return vid


def _get_model() -> str:
    """Read model from Streamlit secrets with env var fallback."""
    try:
        import streamlit as st
        m = st.secrets.get("ELEVENLABS_MODEL", "")
        if m:
            return m
    except Exception:
        pass
    return os.getenv("ELEVENLABS_MODEL", DEFAULT_MODEL)


def synthesize_segment(
    text: str,
    output_path: str | Path,
    voice_id: str | None = None,
    model: str | None = None,
) -> Path:
    """
    Send text (with ElevenLabs <break> markup) to TTS and save as MP3.

    Returns the output Path on success, raises on failure.
    """
    from elevenlabs import ElevenLabs

    api_key = _get_api_key()
    voice_id = voice_id or _get_voice_id()
    model = model or _get_model()
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    client = ElevenLabs(api_key=api_key)

    last_error: Exception | None = None
    for attempt in range(1 + MAX_RETRIES):
        try:
            audio_iter = client.text_to_speech.convert(
                voice_id=voice_id,
                text=text,
                model_id=model,
                output_format="mp3_44100_128",
            )
            # Write chunks to file
            with open(output_path, "wb") as f:
                for chunk in audio_iter:
                    f.write(chunk)

            logger.info("TTS OK: %s (%d bytes)", output_path.name, output_path.stat().st_size)
            return output_path

        except Exception as e:
            last_error = e
            if attempt < MAX_RETRIES:
                wait = 2 ** (attempt + 1)  # exponential backoff: 2s, 4s, ...
                logger.warning("TTS retry %d after %ds: %s", attempt + 1, wait, e)
                time.sleep(wait)

    raise RuntimeError(f"TTS failed after {1 + MAX_RETRIES} attempts: {last_error}")


def generate_all_segments(
    segments: list[dict],
    output_dir: str | Path,
    voice_id: str | None = None,
    model: str | None = None,
    progress_callback=None,
) -> list[dict]:
    """
    Generate MP3 for every segment sequentially.

    Args:
        segments: list of dicts with at least {segment_id, elevenlabs_text}
        output_dir: directory for MP3 files
        voice_id: override voice ID
        model: override model
        progress_callback: fn(current_index, total, segment_id)

    Returns list of dicts: {segment_id, audio_path, pause_for_question}
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    voice_id = voice_id or _get_voice_id()
    model = model or _get_model()

    results: list[dict] = []
    total = len(segments)

    for i, seg in enumerate(segments):
        seg_id = seg.get("segment_id", f"s{i+1:03d}")
        text = seg.get("elevenlabs_text", seg.get("text", ""))

        if progress_callback:
            progress_callback(i, total, seg_id)

        mp3_path = output_dir / f"{seg_id}.mp3"
        synthesize_segment(text, mp3_path, voice_id=voice_id, model=model)

        results.append({
            "segment_id": seg_id,
            "audio_path": str(mp3_path),
            "pause_for_question": seg.get("pause_for_question", False),
        })

        # Delay between API calls (skip after last)
        if i < total - 1:
            time.sleep(INTER_CALL_DELAY)

    if progress_callback:
        progress_callback(total, total, "done")

    return results
