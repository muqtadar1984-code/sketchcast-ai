"""Voice registry — the single source of selectable narration voices.

Adding a new voice (Edge or ElevenLabs) = adding one entry here. No code
changes anywhere else. The web voice-picker and the worker both read this list,
so they can never drift.

Gating rule (enforced server-side in ``shared.tts.synthesize``): entries with
``tier='premium'`` (all ElevenLabs voices) are selectable ONLY when the paid/
enabled flag is on. The free tier sees and gets only ``tier='free'`` voices, and
the free Edge voice is always the default.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class TTSVoice:
    voice_id: str          # our STABLE id (stored in generation params) — never the raw provider id
    label: str             # human label for the picker
    provider: str          # "edge" (free) | "elevenlabs" (premium)
    tier: str              # "free" | "premium"
    ref: str               # provider-specific id: Edge voice name, or ElevenLabs voice_id
    style_tags: tuple[str, ...] = field(default_factory=tuple)


# ── The registry ────────────────────────────────────────────────────────────
# ElevenLabs `ref`s are that provider's public default voice ids; override any
# via env if you licence different voices (e.g. ELEVENLABS_VOICE_ID).
_EL_DEFAULT_REF = os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")  # Rachel

VOICES: list[TTSVoice] = [
    # Free — Microsoft Edge (no key, $0). The first entry is the global default.
    TTSVoice("edge-aria",   "Aria — neutral (free)",         "edge", "free", "en-US-AriaNeural",   ("neutral", "clear")),
    TTSVoice("edge-guy",    "Guy — warm (free)",             "edge", "free", "en-US-GuyNeural",    ("warm", "male")),
    TTSVoice("edge-neerja", "Neerja — Indian English (free)","edge", "free", "en-IN-NeerjaNeural", ("indian", "warm")),
    TTSVoice("edge-sonia",  "Sonia — British (free)",        "edge", "free", "en-GB-SoniaNeural",  ("british", "calm")),
    # Premium — ElevenLabs (key + enabled flag required). Add more by appending.
    TTSVoice("el-rachel",   "Rachel — natural (premium)",    "elevenlabs", "premium", _EL_DEFAULT_REF,       ("warm", "natural")),
    TTSVoice("el-adam",     "Adam — deep (premium)",         "elevenlabs", "premium", "pNInz6obpgDQGcFmaJgB", ("deep", "male")),
]

DEFAULT_VOICE_ID = "edge-aria"  # the free default — reproduces today's behaviour

_BY_ID = {v.voice_id: v for v in VOICES}


def get_voice(voice_id: str | None) -> TTSVoice | None:
    return _BY_ID.get(voice_id or "")


def default_voice() -> TTSVoice:
    return _BY_ID[DEFAULT_VOICE_ID]


def list_voices(include_premium: bool = False) -> list[TTSVoice]:
    """Voices offered to a caller. Premium voices are hidden unless allowed."""
    return [v for v in VOICES if include_premium or v.tier == "free"]
