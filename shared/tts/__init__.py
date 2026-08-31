"""Provider-agnostic text-to-speech.

Resolve a registry voice → its provider, enforce the free/premium gate + the
local spend cap server-side, log per-call characters + estimated cost. Free Edge
is the default and is never gated; ElevenLabs is premium and only runs when
`allow_premium` is true AND under the spend cap — otherwise it falls back to the
free voice so a generation never fails or overspends.

    from shared.tts import synthesize, list_voices, DEFAULT_VOICE_ID
    synthesize(text, out, voice_id="edge-aria")                       # free ($0)
    synthesize(text, out, voice_id="el-rachel", allow_premium=True,
               ssml_text=elevenlabs_text)                             # premium (gated)
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import cost
from .registry import DEFAULT_VOICE_ID, TTSVoice, default_voice, get_voice, list_voices
from ..text_clean import strip_ssml

logger = logging.getLogger("shared.tts")

__all__ = ["synthesize", "resolve_voice", "list_voices", "get_voice", "DEFAULT_VOICE_ID", "TTSVoice"]


def resolve_voice(voice_id: str | None, allow_premium: bool = False) -> TTSVoice:
    """Registry voice for `voice_id`, with the gate enforced. An unknown id OR a
    premium id without permission both resolve to the free default — the client
    can never bypass the gate just by sending a premium voice_id."""
    v = get_voice(voice_id) or default_voice()
    if v.tier == "premium" and not allow_premium:
        logger.warning("premium voice %r requested without permission → free default", voice_id)
        return default_voice()
    return v


def synthesize(
    text: str,
    out_path: str | Path,
    voice_id: str | None = None,
    *,
    allow_premium: bool = False,
    ssml_text: str | None = None,
    stream: bool = False,
    report: dict | None = None,
    boundaries_out: str | Path | None = None,
) -> Path:
    """Synthesize `text` to an MP3 at `out_path`. Returns the path.

    `voice_id` is a registry id (default = free Edge). `allow_premium` is the
    server-resolved gate. `ssml_text` (ElevenLabs <break> markup) is used for
    ElevenLabs; Edge speaks plain `text`. `stream` is reserved for the future
    tutor path (lesson narration is non-streaming) and currently ignored.

    If `report` (a dict) is passed, it is filled with what ACTUALLY happened —
    {requested, used, provider, downgraded} — so callers can surface a silent
    premium→free downgrade (gate off / cap hit / API failure) instead of a
    teacher discovering the wrong voice only by listening. `downgraded` is True
    when a premium voice was requested but a free voice was rendered.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    requested = get_voice(voice_id)
    requested_premium = bool(requested and requested.tier == "premium")
    voice = resolve_voice(voice_id, allow_premium=allow_premium)

    say = text
    if voice.provider == "elevenlabs":
        say = ssml_text or text
        if not cost.within_cap(len((say or "").strip())):
            logger.warning("ElevenLabs spend cap reached → free voice for this call")
            voice, say = default_voice(), text

    if voice.provider == "elevenlabs":
        from .providers import eleven

        try:
            eleven.synthesize(say, out_path, voice.ref)
        except Exception as exc:  # noqa: BLE001 — never fail a generation over TTS
            logger.error("ElevenLabs failed (%s) → falling back to free Edge", exc)
            from .providers import edge

            # Edge reads SSML tags ALOUD — it only ever gets plain text. (The
            # ElevenLabs branch is never stripped: its <break>s are intended.)
            voice, say = default_voice(), strip_ssml(text)
            edge.synthesize(say, out_path, voice.ref,
                            boundaries_out=Path(boundaries_out) if boundaries_out else None)
    else:
        from .providers import edge

        say = strip_ssml(say)  # Edge reads SSML tags aloud — plain text only
        edge.synthesize(say, out_path, voice.ref,
                        boundaries_out=Path(boundaries_out) if boundaries_out else None)

    cost.record(len((say or "").strip()), voice.provider)
    downgraded = requested_premium and voice.tier != "premium"
    if downgraded:
        logger.warning(
            "VOICE DOWNGRADE: requested premium %r but rendered %r (%s) — check "
            "ELEVENLABS_ENABLED + ELEVENLABS_API_KEY on the worker, or the spend cap.",
            voice_id, voice.voice_id, voice.provider,
        )
    if report is not None:
        report.update(
            {"requested": voice_id, "used": voice.voice_id, "provider": voice.provider, "downgraded": downgraded}
        )
    logger.info("TTS ok: voice=%s provider=%s -> %s", voice.voice_id, voice.provider, out_path.name)
    return out_path
