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
    lang: str = "en"       # ISO 639-1 — pairs the voice with a lesson language


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
    # Free — per-language Edge voices (auto-picked when a lesson's language
    # matches; see shared/languages.py). Female default + one male option each.
    TTSVoice("edge-yasmin",    "Yasmin — Bahasa Melayu (free)",   "edge", "free", "ms-MY-YasminNeural",    ("warm",), "ms"),
    TTSVoice("edge-osman",     "Osman — Bahasa Melayu (free)",    "edge", "free", "ms-MY-OsmanNeural",     ("male",), "ms"),
    TTSVoice("edge-zariyah",   "Zariyah — العربية (free)",         "edge", "free", "ar-SA-ZariyahNeural",   ("warm",), "ar"),
    TTSVoice("edge-hamed",     "Hamed — العربية (free)",           "edge", "free", "ar-SA-HamedNeural",     ("male",), "ar"),
    TTSVoice("edge-denise",    "Denise — Français (free)",        "edge", "free", "fr-FR-DeniseNeural",    ("warm",), "fr"),
    TTSVoice("edge-henri",     "Henri — Français (free)",         "edge", "free", "fr-FR-HenriNeural",     ("male",), "fr"),
    TTSVoice("edge-elvira",    "Elvira — Español (free)",         "edge", "free", "es-ES-ElviraNeural",    ("warm",), "es"),
    TTSVoice("edge-alvaro",    "Álvaro — Español (free)",         "edge", "free", "es-ES-AlvaroNeural",    ("male",), "es"),
    TTSVoice("edge-francisca", "Francisca — Português (free)",    "edge", "free", "pt-BR-FranciscaNeural", ("warm",), "pt"),
    TTSVoice("edge-antonio",   "Antônio — Português (free)",      "edge", "free", "pt-BR-AntonioNeural",   ("male",), "pt"),
    TTSVoice("edge-shruti",    "Shruti — తెలుగు (free)",           "edge", "free", "te-IN-ShrutiNeural",    ("warm",), "te"),
    TTSVoice("edge-mohan",     "Mohan — తెలుగు (free)",            "edge", "free", "te-IN-MohanNeural",     ("male",), "te"),
    TTSVoice("edge-aarohi",    "Aarohi — मराठी (free)",            "edge", "free", "mr-IN-AarohiNeural",    ("warm",), "mr"),
    TTSVoice("edge-manohar",   "Manohar — मराठी (free)",           "edge", "free", "mr-IN-ManoharNeural",   ("male",), "mr"),
    TTSVoice("edge-swara",     "Swara — हिन्दी (free)",             "edge", "free", "hi-IN-SwaraNeural",     ("warm",), "hi"),
    TTSVoice("edge-madhur",    "Madhur — हिन्दी (free)",            "edge", "free", "hi-IN-MadhurNeural",    ("male",), "hi"),
    # Premium — ElevenLabs (key + enabled flag required; voices are multilingual).
    TTSVoice("el-rachel",   "Rachel — natural (premium)",    "elevenlabs", "premium", _EL_DEFAULT_REF,       ("warm", "natural")),
    TTSVoice("el-adam",     "Adam — deep (premium)",         "elevenlabs", "premium", "pNInz6obpgDQGcFmaJgB", ("deep", "male")),
]

DEFAULT_VOICE_ID = "edge-aria"  # the free default — reproduces today's behaviour

_BY_ID = {v.voice_id: v for v in VOICES}


def default_voice_id_for(lang: str | None) -> str:
    """The free default voice for a lesson language (English → global default).

    Jawi (ms-arab) is written Malay in the Arabic script — SPOKEN it's Malay, so
    its narration uses the Malay voice.
    """
    want = "ms" if lang == "ms-arab" else (lang or "en")
    for v in VOICES:
        if v.tier == "free" and v.lang == want:
            return v.voice_id
    return DEFAULT_VOICE_ID


def get_voice(voice_id: str | None) -> TTSVoice | None:
    return _BY_ID.get(voice_id or "")


def default_voice() -> TTSVoice:
    return _BY_ID[DEFAULT_VOICE_ID]


def list_voices(include_premium: bool = False) -> list[TTSVoice]:
    """Voices offered to a caller. Premium voices are hidden unless allowed."""
    return [v for v in VOICES if include_premium or v.tier == "free"]
