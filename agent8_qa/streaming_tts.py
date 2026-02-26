"""ElevenLabs streaming TTS — buffers text into sentences and streams audio."""

from __future__ import annotations

import logging
import os
from typing import AsyncGenerator

logger = logging.getLogger(__name__)


def _get_el_api_key() -> str:
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
        raise RuntimeError("ELEVENLABS_API_KEY not configured.")
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
        raise RuntimeError("ELEVENLABS_VOICE_ID not configured.")
    return vid


def synthesize_answer(text: str, voice_id: str | None = None) -> bytes:
    """
    Synthesize a complete answer to audio (non-streaming, for Streamlit).

    Returns the full MP3 bytes.
    """
    from elevenlabs import ElevenLabs

    api_key = _get_el_api_key()
    voice_id = voice_id or _get_voice_id()
    client = ElevenLabs(api_key=api_key)

    audio_iter = client.text_to_speech.convert(
        voice_id=voice_id,
        text=text,
        model_id="eleven_turbo_v2",
        output_format="mp3_44100_128",
    )

    chunks = []
    for chunk in audio_iter:
        chunks.append(chunk)
    return b"".join(chunks)


async def stream_answer_audio(
    text_stream: AsyncGenerator[str, None],
    voice_id: str | None = None,
) -> AsyncGenerator[bytes, None]:
    """
    Buffer text chunks into sentences, send each sentence to ElevenLabs
    streaming TTS immediately. Audio starts playing within ~1.5s.

    Yields MP3 audio chunks.
    """
    from elevenlabs import ElevenLabs

    api_key = _get_el_api_key()
    voice_id = voice_id or _get_voice_id()
    client = ElevenLabs(api_key=api_key)

    sentence_buffer = ""

    async for text_chunk in text_stream:
        sentence_buffer += text_chunk

        # Check if we have a complete sentence
        if any(sentence_buffer.rstrip().endswith(p) for p in [".", "?", "!"]):
            # Send sentence to TTS
            sentence = sentence_buffer.strip()
            if sentence:
                try:
                    audio_iter = client.text_to_speech.convert(
                        voice_id=voice_id,
                        text=sentence,
                        model_id="eleven_turbo_v2",
                        output_format="mp3_44100_128",
                    )
                    for audio_chunk in audio_iter:
                        yield audio_chunk
                except Exception as e:
                    logger.warning("TTS streaming error: %s", e)
            sentence_buffer = ""

    # Flush remaining buffer
    if sentence_buffer.strip():
        try:
            audio_iter = client.text_to_speech.convert(
                voice_id=voice_id,
                text=sentence_buffer.strip(),
                model_id="eleven_turbo_v2",
                output_format="mp3_44100_128",
            )
            for audio_chunk in audio_iter:
                yield audio_chunk
        except Exception as e:
            logger.warning("TTS streaming flush error: %s", e)
