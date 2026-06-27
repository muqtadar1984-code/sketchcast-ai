"""Claude streaming answer generation for Q&A interjections."""

from __future__ import annotations

import logging
import os
from typing import AsyncGenerator

logger = logging.getLogger(__name__)


def _get_api_key() -> str:
    """Read Anthropic API key from Streamlit secrets with env var fallback."""
    try:
        import streamlit as st
        key = st.secrets.get("ANTHROPIC_API_KEY", "")
        if key:
            return key
    except Exception:
        pass
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not configured.")
    return key


def generate_answer_stream(
    question: str,
    segment_context: dict,
    chapter_analysis: dict,
    narrator_persona: str = "Socratic",
) -> str:
    """
    Generate a concise Socratic answer using Claude (non-streaming for Streamlit).

    Returns the full answer text.
    """
    from anthropic import Anthropic

    api_key = _get_api_key()
    client = Anthropic(api_key=api_key)

    chapter_title = chapter_analysis.get("chapter_title", "this chapter")
    segment_text = segment_context.get("text", "")

    system_prompt = (
        f"You are the {narrator_persona} narrator of a podcast episode about "
        f"'{chapter_title}'. A student has paused to ask a question.\n\n"
        f"Current episode context: {segment_text}\n\n"
        f"Answer in 2-4 sentences in your narrator voice. Be warm, encouraging, and Socratic. "
        f"End by connecting back to what the episode was just discussing. "
        f"Do not use markdown. Speak naturally as if talking to the student directly."
    )

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        system=system_prompt,
        messages=[{"role": "user", "content": question}],
    )

    answer = response.content[0].text
    tokens_used = getattr(response.usage, "input_tokens", 0) + getattr(response.usage, "output_tokens", 0)

    logger.info("Generated answer (%d tokens): '%s...'", tokens_used, answer[:80])
    return answer, tokens_used


async def generate_answer_stream_async(
    question: str,
    segment_context: dict,
    chapter_analysis: dict,
    narrator_persona: str = "Socratic",
) -> AsyncGenerator[str, None]:
    """
    Generate a streaming Socratic answer using Claude.

    Yields text chunks as they arrive. For Agent 8 FastAPI endpoint.
    """
    from anthropic import AsyncAnthropic

    api_key = _get_api_key()
    client = AsyncAnthropic(api_key=api_key)

    chapter_title = chapter_analysis.get("chapter_title", "this chapter")
    segment_text = segment_context.get("text", "")

    system_prompt = (
        f"You are the {narrator_persona} narrator of a podcast episode about "
        f"'{chapter_title}'. A student has paused to ask a question.\n\n"
        f"Current episode context: {segment_text}\n\n"
        f"Answer in 2-4 sentences in your narrator voice. Be warm, encouraging, and Socratic. "
        f"End by connecting back to what the episode was just discussing. "
        f"Do not use markdown. Speak naturally as if talking to the student directly."
    )

    async with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=200,
        system=system_prompt,
        messages=[{"role": "user", "content": question}],
    ) as stream:
        async for text in stream.text_stream:
            yield text
