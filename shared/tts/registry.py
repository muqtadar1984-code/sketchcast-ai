"""Voice registry — the single source of selectable narration voices.

Adding a new voice = adding one entry here. The worker reads this list; the
app keeps its OWN copy in sketchcast-app/src/utils/narration.ts (and the
tutor a third in utils/tutor/models.ts), so an entry added here must be
mirrored there or the picker cannot offer it. A `/api/voices` that serves this
list is the ticketed follow-up.

Gating rule (enforced server-side in ``shared.tts.resolve_voice``): entries
with ``tier='premium'`` render only for a PAID account AND only when their
provider is enabled on the worker. The free tier gets ``tier='free'`` voices,
and the free voice for the lesson's language is always the default.

The premium PROVIDER is one variable, ``TTS_PREMIUM_PROVIDER``:

    legacy      today's behaviour — no premium default; premium only on an
                explicit pick. The documented rollback target, because it is
                the only state that has ever run in production.
    google      Google Cloud TTS is the premium default (entries land in 1b).
    elevenlabs  ElevenLabs is the premium default — a state that has never
                run; it needs a durable cap first.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# The app sends this for a FORM DEFAULT (teacher did not touch the picker);
# a literal id means an explicit pick. The worker resolves it per account tier
# and lesson language. Until the app sends it, None/"" mean the same thing.
AUTO_VOICE_ID = "auto"

# Tiers that unlock premium voices. An explicit allow-list: `promo` (the
# expired launch trial, branch still live in plan_tier) and any unknown string
# are FREE. Mirrors the app's plan vocabulary in migration 0086; `staff`
# (0109: platform_admins members) hears the premium voices like a paid plan.
PAID_TIERS = frozenset({"pro", "pro_plus", "family", "homeschool", "school", "staff"})

_PROVIDERS = ("legacy", "google", "elevenlabs")


def premium_provider() -> str:
    """Which family a PAID account's `auto` resolves to. Unknown values fall
    back to `legacy` (no premium default) — the safe direction."""
    v = (os.getenv("TTS_PREMIUM_PROVIDER") or "legacy").strip().lower()
    return v if v in _PROVIDERS else "legacy"


@dataclass(frozen=True)
class TTSVoice:
    voice_id: str          # our STABLE id (stored in generation params) — never the raw provider id
    label: str             # human label for the picker
    provider: str          # "edge" (free) | "elevenlabs" | "google" (premium)
    tier: str              # "free" | "premium"
    ref: str               # provider-specific id: Edge voice name, ElevenLabs voice_id, Google voice name
    style_tags: tuple[str, ...] = field(default_factory=tuple)
    lang: str = "en"       # ISO 639-1 — pairs the voice with a lesson language; "*" = multilingual
    # Explicit, not inferred. Avatar casting used to substring-match voice
    # names against a list of female first names; eight female voices (every
    # non-English default, and Rachel) were missing from it and cast the male
    # teacher. A field cannot be forgotten the way a list entry can.
    gender: str = "f"      # "f" | "m"
    # Who the voice is FOR. "narrator" voices are the ones a teacher may pick
    # and `auto` may resolve to; "student" voices (catalogue Phase 3) read the
    # student's lines in a two-voice dialogue and are hidden from every picker
    # and every default — a youthful voice must never become a lesson's
    # narrator because it sorted first in a pool.
    role: str = "narrator"  # "narrator" | "student"


# ── The registry ────────────────────────────────────────────────────────────
# ElevenLabs `ref`s are that provider's public default voice ids; override any
# via env if you licence different voices (e.g. ELEVENLABS_VOICE_ID).
_EL_DEFAULT_REF = os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")  # Rachel

VOICES: list[TTSVoice] = [
    # Free — Microsoft Edge (no key, $0). The first entry is the global default.
    TTSVoice("edge-aria",   "Aria — neutral (free)",         "edge", "free", "en-US-AriaNeural",   ("neutral", "clear")),
    TTSVoice("edge-guy",    "Guy — warm (free)",             "edge", "free", "en-US-GuyNeural",    ("warm", "male"), gender="m"),
    TTSVoice("edge-neerja", "Neerja — Indian English (free)","edge", "free", "en-IN-NeerjaNeural", ("indian", "warm")),
    TTSVoice("edge-sonia",  "Sonia — British (free)",        "edge", "free", "en-GB-SoniaNeural",  ("british", "calm")),
    # Free — per-language Edge voices (auto-picked when a lesson's language
    # matches; see shared/languages.py). Female default + one male option each.
    TTSVoice("edge-yasmin",    "Yasmin — Bahasa Melayu (free)",   "edge", "free", "ms-MY-YasminNeural",    ("warm",), "ms"),
    TTSVoice("edge-osman",     "Osman — Bahasa Melayu (free)",    "edge", "free", "ms-MY-OsmanNeural",     ("male",), "ms", gender="m"),
    TTSVoice("edge-zariyah",   "Zariyah — العربية (free)",         "edge", "free", "ar-SA-ZariyahNeural",   ("warm",), "ar"),
    TTSVoice("edge-hamed",     "Hamed — العربية (free)",           "edge", "free", "ar-SA-HamedNeural",     ("male",), "ar", gender="m"),
    TTSVoice("edge-denise",    "Denise — Français (free)",        "edge", "free", "fr-FR-DeniseNeural",    ("warm",), "fr"),
    TTSVoice("edge-henri",     "Henri — Français (free)",         "edge", "free", "fr-FR-HenriNeural",     ("male",), "fr", gender="m"),
    TTSVoice("edge-elvira",    "Elvira — Español (free)",         "edge", "free", "es-ES-ElviraNeural",    ("warm",), "es"),
    TTSVoice("edge-alvaro",    "Álvaro — Español (free)",         "edge", "free", "es-ES-AlvaroNeural",    ("male",), "es", gender="m"),
    TTSVoice("edge-francisca", "Francisca — Português (free)",    "edge", "free", "pt-BR-FranciscaNeural", ("warm",), "pt"),
    TTSVoice("edge-antonio",   "Antônio — Português (free)",      "edge", "free", "pt-BR-AntonioNeural",   ("male",), "pt", gender="m"),
    TTSVoice("edge-shruti",    "Shruti — తెలుగు (free)",           "edge", "free", "te-IN-ShrutiNeural",    ("warm",), "te"),
    TTSVoice("edge-mohan",     "Mohan — తెలుగు (free)",            "edge", "free", "te-IN-MohanNeural",     ("male",), "te", gender="m"),
    TTSVoice("edge-aarohi",    "Aarohi — मराठी (free)",            "edge", "free", "mr-IN-AarohiNeural",    ("warm",), "mr"),
    TTSVoice("edge-manohar",   "Manohar — मराठी (free)",           "edge", "free", "mr-IN-ManoharNeural",   ("male",), "mr", gender="m"),
    TTSVoice("edge-swara",     "Swara — हिन्दी (free)",             "edge", "free", "hi-IN-SwaraNeural",     ("warm",), "hi"),
    TTSVoice("edge-madhur",    "Madhur — हिन्दी (free)",            "edge", "free", "hi-IN-MadhurNeural",    ("male",), "hi", gender="m"),
    # Premium — ElevenLabs (key + enabled flag required). lang="*": the
    # configured model is multilingual (eleven_turbo_v2_5 as of 2026-09-03).
    TTSVoice("el-rachel",   "Rachel — natural (premium)",    "elevenlabs", "premium", _EL_DEFAULT_REF,       ("warm", "natural"), "*"),
    TTSVoice("el-adam",     "Adam — deep (premium)",         "elevenlabs", "premium", "pNInz6obpgDQGcFmaJgB", ("deep", "male"),    "*", gender="m"),
    # Premium — Google Cloud TTS. Voice names were read from the live
    # voices.list on 2026-09-03 (not typed from memory): Chirp 3 HD ships an
    # Achernar (female) and Achird (male) in every locale we teach except
    # Malay, which has no Chirp at all and gets WaveNet. Chirp ignores
    # <mark>, so its word timing comes from per-sentence synthesis (see
    # shared/tts/chunks.py); WaveNet gets exact per-word marks.
    TTSVoice("g-en-f",    "Achernar — natural (premium)",        "google", "premium", "en-US-Chirp3-HD-Achernar", ("natural", "warm")),
    TTSVoice("g-en-m",    "Achird — natural (premium)",          "google", "premium", "en-US-Chirp3-HD-Achird",   ("natural", "male"), gender="m"),
    TTSVoice("g-en-gb-f", "Achernar — British (premium)",        "google", "premium", "en-GB-Chirp3-HD-Achernar", ("british",)),
    TTSVoice("g-en-gb-m", "Achird — British (premium)",          "google", "premium", "en-GB-Chirp3-HD-Achird",   ("british", "male"), gender="m"),
    TTSVoice("g-en-in-f", "Achernar — Indian English (premium)", "google", "premium", "en-IN-Chirp3-HD-Achernar", ("indian",)),
    TTSVoice("g-en-in-m", "Achird — Indian English (premium)",   "google", "premium", "en-IN-Chirp3-HD-Achird",   ("indian", "male"), gender="m"),
    TTSVoice("g-ms-f",    "WaveNet A — Bahasa Melayu (premium)", "google", "premium", "ms-MY-Wavenet-A",          ("warm",), "ms"),
    TTSVoice("g-ms-m",    "WaveNet B — Bahasa Melayu (premium)", "google", "premium", "ms-MY-Wavenet-B",          ("male",), "ms", gender="m"),
    TTSVoice("g-ar-f",    "Achernar — العربية (premium)",         "google", "premium", "ar-XA-Chirp3-HD-Achernar", ("warm",), "ar"),
    TTSVoice("g-ar-m",    "Achird — العربية (premium)",           "google", "premium", "ar-XA-Chirp3-HD-Achird",   ("male",), "ar", gender="m"),
    TTSVoice("g-fr-f",    "Achernar — Français (premium)",       "google", "premium", "fr-FR-Chirp3-HD-Achernar", ("warm",), "fr"),
    TTSVoice("g-fr-m",    "Achird — Français (premium)",         "google", "premium", "fr-FR-Chirp3-HD-Achird",   ("male",), "fr", gender="m"),
    TTSVoice("g-es-f",    "Achernar — Español (premium)",        "google", "premium", "es-ES-Chirp3-HD-Achernar", ("warm",), "es"),
    TTSVoice("g-es-m",    "Achird — Español (premium)",          "google", "premium", "es-ES-Chirp3-HD-Achird",   ("male",), "es", gender="m"),
    TTSVoice("g-pt-f",    "Achernar — Português (premium)",      "google", "premium", "pt-BR-Chirp3-HD-Achernar", ("warm",), "pt"),
    TTSVoice("g-pt-m",    "Achird — Português (premium)",        "google", "premium", "pt-BR-Chirp3-HD-Achird",   ("male",), "pt", gender="m"),
    TTSVoice("g-te-f",    "Achernar — తెలుగు (premium)",          "google", "premium", "te-IN-Chirp3-HD-Achernar", ("warm",), "te"),
    TTSVoice("g-te-m",    "Achird — తెలుగు (premium)",            "google", "premium", "te-IN-Chirp3-HD-Achird",   ("male",), "te", gender="m"),
    TTSVoice("g-mr-f",    "Achernar — मराठी (premium)",           "google", "premium", "mr-IN-Chirp3-HD-Achernar", ("warm",), "mr"),
    TTSVoice("g-mr-m",    "Achird — मराठी (premium)",             "google", "premium", "mr-IN-Chirp3-HD-Achird",   ("male",), "mr", gender="m"),
    TTSVoice("g-hi-f",    "Achernar — हिन्दी (premium)",           "google", "premium", "hi-IN-Chirp3-HD-Achernar", ("warm",), "hi"),
    TTSVoice("g-hi-m",    "Achird — हिन्दी (premium)",             "google", "premium", "hi-IN-Chirp3-HD-Achird",   ("male",), "hi", gender="m"),
    # Student voices (catalogue Phase 3, 2026-09-06): the SECOND voice of a
    # two-voice dialogue, the one that reads the student's lines. Chirp 3 HD
    # ships the same 30 names in every locale; Leda (female) and Puck (male)
    # are its youthful pair. role="student" keeps them out of list_voices()
    # and out of every narration default — a kit's params name them
    # explicitly (`student_voice`), the OTHER gender to the teacher's voice.
    # Four locales for now (en, plus ar/fr/es for Phase 5's translations);
    # a language without a student entry falls back to today's Edge student
    # voice and the generation records student_voice_fallback.
    TTSVoice("g-en-student-f", "Leda — student (premium)",  "google", "premium", "en-US-Chirp3-HD-Leda", ("youthful",), "en", role="student"),
    TTSVoice("g-en-student-m", "Puck — student (premium)",  "google", "premium", "en-US-Chirp3-HD-Puck", ("youthful", "male"), "en", gender="m", role="student"),
    TTSVoice("g-ar-student-f", "Leda — student, العربية (premium)",  "google", "premium", "ar-XA-Chirp3-HD-Leda", ("youthful",), "ar", role="student"),
    TTSVoice("g-ar-student-m", "Puck — student, العربية (premium)",  "google", "premium", "ar-XA-Chirp3-HD-Puck", ("youthful", "male"), "ar", gender="m", role="student"),
    TTSVoice("g-fr-student-f", "Leda — student, Français (premium)", "google", "premium", "fr-FR-Chirp3-HD-Leda", ("youthful",), "fr", role="student"),
    TTSVoice("g-fr-student-m", "Puck — student, Français (premium)", "google", "premium", "fr-FR-Chirp3-HD-Puck", ("youthful", "male"), "fr", gender="m", role="student"),
    TTSVoice("g-es-student-f", "Leda — student, Español (premium)",  "google", "premium", "es-ES-Chirp3-HD-Leda", ("youthful",), "es", role="student"),
    TTSVoice("g-es-student-m", "Puck — student, Español (premium)",  "google", "premium", "es-ES-Chirp3-HD-Puck", ("youthful", "male"), "es", gender="m", role="student"),
]

STUDENT_ROLE = "student"


def student_voice_id_for(lang: str | None, gender: str) -> str | None:
    """The premium STUDENT voice of `gender` for a language, or None when the
    registry has none (the caller then keeps today's Edge student voice)."""
    want = _spoken_lang(lang)
    for v in VOICES:
        if v.role == STUDENT_ROLE and v.lang == want and v.gender == gender:
            return v.voice_id
    return None

DEFAULT_VOICE_ID = "edge-aria"  # the free English default — reproduces today's behaviour

_BY_ID = {v.voice_id: v for v in VOICES}


def _spoken_lang(lang: str | None) -> str:
    """Jawi (ms-arab) is written Malay in the Arabic script — SPOKEN it's Malay."""
    return "ms" if lang == "ms-arab" else (lang or "en")


def default_voice_id_for(lang: str | None, gender: str | None = None) -> str:
    """The free default voice for a lesson language (English → global default).

    With `gender`, the free voice of that gender for the language when one
    exists. Every downgrade of a premium voice lands here, and the teacher
    avatar was cast from the REQUESTED voice before the lesson rendered — a
    male premium pick that fell back to the language's first (female) free
    entry put a male avatar on a female voice for the whole lesson."""
    want = _spoken_lang(lang)
    pool = [v for v in VOICES if v.tier == "free" and v.lang == want]
    if gender:
        for v in pool:
            if v.gender == gender:
                return v.voice_id
    return pool[0].voice_id if pool else DEFAULT_VOICE_ID


def default_premium_voice_id_for(lang: str | None, gender: str = "f",
                                 provider: str | None = None) -> str | None:
    """The premium voice a PAID account's `auto` resolves to for a language,
    from the ACTIVE premium provider — or None when there is none (the
    `legacy` setting, or a family with no entry for the language). The caller
    then uses the free default. Enablement (key present, flag on) is checked
    by shared.tts, not here. `provider` overrides the global setting for one
    generation — the per-account canary (TTS_PREMIUM_CANARY_OWNERS)."""
    provider = provider or premium_provider()
    if provider == "legacy":
        return None
    want = _spoken_lang(lang)
    # Narrator voices only: a student voice is premium and per-language too,
    # and without this filter Leda would be a candidate for `auto`.
    pool = [v for v in VOICES if v.provider == provider and v.tier == "premium"
            and v.role != STUDENT_ROLE]
    for exact_lang in (True, False):
        for v in pool:
            if (v.lang == want if exact_lang else v.lang == "*") and v.gender == gender:
                return v.voice_id
    for exact_lang in (True, False):        # any gender rather than nothing
        for v in pool:
            if v.lang == want if exact_lang else v.lang == "*":
                return v.voice_id
    return None


def equivalent_voice_id(voice_id: str | None, provider: str,
                        lang: str | None = None) -> str | None:
    """The same voice, as near as the registry can say, in another PREMIUM
    family: same gender, same language (a multilingual entry matches any).

    This is what makes a provider switch honest in BOTH directions. A
    generation created while Google was the default stores a `g-*` id, and
    "New version" copies params forward; after a switch back that id must land
    on its ElevenLabs counterpart (or the free voice), never on a disabled
    provider and never on English Aria for an Arabic lesson. Going forward, a
    stored `el-*` is multilingual and says nothing about the lesson, so the
    LESSON language decides which Google entry it becomes — without it every
    `el-adam` on a Hindi lesson became en-US Achird, an English reader for
    Hindi text."""
    v = get_voice(voice_id)
    if v is None or v.tier != "premium":
        return None
    want = v.lang if v.lang != "*" else _spoken_lang(lang)
    # Same role on both sides: a narrator remaps to a narrator, a student
    # voice to a student voice (or nothing), never across.
    pool = [x for x in VOICES
            if x.provider == provider and x.tier == "premium" and x.gender == v.gender
            and x.role == v.role]
    for x in pool:
        if x.lang == want:
            return x.voice_id
    for x in pool:
        if x.lang == "*":
            return x.voice_id
    return None


def get_voice(voice_id: str | None) -> TTSVoice | None:
    return _BY_ID.get(voice_id or "")


def default_voice() -> TTSVoice:
    return _BY_ID[DEFAULT_VOICE_ID]


def list_voices(include_premium: bool = False) -> list[TTSVoice]:
    """Voices offered to a caller. Premium voices are hidden unless allowed;
    student voices are never offered — they are cast by the catalogue kit,
    not picked by a teacher."""
    return [v for v in VOICES
            if (include_premium or v.tier == "free") and v.role != STUDENT_ROLE]
