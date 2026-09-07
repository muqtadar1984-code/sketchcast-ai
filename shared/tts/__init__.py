"""Provider-agnostic text-to-speech.

Resolve a registry voice → its provider, enforce the free/premium gate + the
local spend cap server-side, log per-call characters + estimated cost. Free Edge
is never gated. A premium voice renders only when the caller has resolved the
account to a PAID tier (`allow_premium`) AND the voice's provider is enabled on
this worker — otherwise the request lands on the free voice for the lesson's
LANGUAGE, so a downgrade never changes what language the teacher speaks.

    from shared.tts import synthesize, list_voices, DEFAULT_VOICE_ID
    synthesize(text, out, voice_id="edge-aria")                       # free ($0)
    synthesize(text, out, voice_id="el-rachel", allow_premium=True,
               ssml_text=elevenlabs_text, lang="ar")                  # premium (gated)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from . import cost
from .registry import (AUTO_VOICE_ID, DEFAULT_VOICE_ID, PAID_TIERS, STUDENT_ROLE,
                       TTSVoice, default_premium_voice_id_for, default_voice,
                       default_voice_id_for, equivalent_voice_id, get_voice,
                       list_voices, premium_provider)
from ..text_clean import strip_ssml

logger = logging.getLogger("shared.tts")

__all__ = ["synthesize", "resolve_voice", "pick_voice_id", "enabled_providers",
           "list_voices", "get_voice", "DEFAULT_VOICE_ID", "AUTO_VOICE_ID",
           "PAID_TIERS", "TTSVoice"]


def _flag(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in ("1", "true", "yes", "on")


def google_tts_enabled() -> bool:
    """Google is OPT-IN: GOOGLE_TTS_ENABLED must be on AND the worker's
    Application Default Credentials (the chain Vertex uses; no key of its
    own) must exist.

    Credentials alone are not enough. Production already carries
    VERTEX_PROJECT_ID for Gemini-on-Vertex, so inferring 'enabled' from it —
    the first shape — would have switched Google on the day this deployed:
    every stored el-* pick on a paid account remapped to Chirp and billing
    started, while TTS_PREMIUM_PROVIDER, the variable meant to control the
    rollout, stayed unset. Ships dark means dark."""
    creds = any(os.getenv(k) for k in ("GOOGLE_APPLICATION_CREDENTIALS",
                                       "GOOGLE_APPLICATION_CREDENTIALS_JSON",
                                       "VERTEX_PROJECT_ID"))
    return _flag("GOOGLE_TTS_ENABLED") and bool(creds)


def canary_provider_for(owner_id: str | None) -> str | None:
    """The premium provider a CANARY account gets while everyone else stays
    on TTS_PREMIUM_PROVIDER. TTS_PREMIUM_CANARY_OWNERS is a comma-separated
    list of owner ids; TTS_PREMIUM_CANARY_PROVIDER names the family (default
    google). None = not a canary. The global switch flips every paid account
    at once; a canary lets one founder-owned account listen first."""
    if not owner_id:
        return None
    owners = {o.strip() for o in (os.getenv("TTS_PREMIUM_CANARY_OWNERS") or "").split(",") if o.strip()}
    if owner_id not in owners:
        return None
    fam = (os.getenv("TTS_PREMIUM_CANARY_PROVIDER") or "google").strip().lower()
    return fam if fam in ("google", "elevenlabs") else "google"


def enabled_providers() -> frozenset[str]:
    """Providers this worker can actually call. Edge needs nothing. ElevenLabs
    needs its flag AND its key — the same two facts the old deployment-wide
    gate folded into one bool. Google needs its flag AND ADC credentials."""
    on = {"edge"}
    if _flag("ELEVENLABS_ENABLED") and os.getenv("ELEVENLABS_API_KEY"):
        on.add("elevenlabs")
    if google_tts_enabled():
        on.add("google")
    return frozenset(on)


def resolve_voice(voice_id: str | None, allow_premium: bool = False, *,
                  lang: str | None = None,
                  enabled: frozenset[str] | None = None) -> TTSVoice:
    """Registry voice for `voice_id`, with the gate enforced.

    Three things this used to get wrong, each now a test:
      - the downgrade target was English Aria regardless of lesson language,
        so a lapsed Arabic subscriber would have received an English lesson;
      - a premium id passed the gate even when its provider had no key, then
        failed at the provider and fell to Edge with a misleading warning;
      - a premium id from a family that is no longer the active provider (a
        `g-*` stored while Google was default, after a switch back) had nowhere
        sensible to go. It is now REMAPPED to the same voice in the active
        family when that family is enabled, else to the free voice.
    """
    enabled = enabled_providers() if enabled is None else enabled
    free = get_voice(default_voice_id_for(lang)) or default_voice()
    v = get_voice(voice_id)
    if v is None:
        return free
    if v.tier != "premium":
        return v
    # every downgrade below keeps the requested voice's GENDER — the avatar
    # was cast from it
    free = get_voice(default_voice_id_for(lang, gender=v.gender)) or free
    if not allow_premium:
        logger.warning("premium voice %r requested without a paid tier → %s", voice_id, free.voice_id)
        return free
    if v.provider in enabled:
        return v
    # The requested family is not available here. Remap to the same voice in
    # ANY enabled premium family, the active one first. Not only the active
    # one: under `legacy` there is no active family, and the rollback runbook
    # (a `g-*` stored during a Google canary, ElevenLabs enabled) must land on
    # the ElevenLabs equivalent, not on the free voice. premium_provider()
    # decides only what `auto` becomes; it must not veto a remap.
    active = premium_provider()
    candidates = [p for p in (active, "elevenlabs", "google") if p in enabled and p != "edge"]
    for fam in dict.fromkeys(candidates):
        alt = equivalent_voice_id(v.voice_id, fam, lang=lang)
        if alt:
            logger.warning("premium voice %r (%s not enabled) → %r on %s",
                           voice_id, v.provider, alt, fam)
            return get_voice(alt) or free
    logger.warning("premium voice %r requested but %s is not enabled → %s",
                   voice_id, v.provider, free.voice_id)
    return free


def pick_voice_id(requested: str | None, *, lang: str | None, allow_premium: bool,
                  explicit_language: bool = False,
                  enabled: frozenset[str] | None = None,
                  provider: str | None = None) -> str:
    """The concrete registry id a generation renders with, from what the app
    sent. Pure; the worker calls it once per generation.

      auto / None / ""  → for a PAID account, the active premium default for
                          the language when its provider is enabled; otherwise
                          the free default for the language. This is the ONLY
                          place a premium default is ever chosen — the app never
                          sends one, so without this rule flipping the provider
                          variable would change nothing for anyone.
      a free Edge id in the wrong language, on a generation that did not set
      an explicit language → the free default for the language (stale
      pre-language params being regenerated; unchanged behaviour).
      a STUDENT voice id  → treated as `auto`, with a warning. The youthful
                          student voices (registry role "student") exist only
                          for the second speaker of a dialogue and are hidden
                          from every picker and every default; a crafted
                          params.tts_voice naming one would otherwise have
                          narrated a whole lesson in Leda's voice and cast
                          the teacher's face from her gender. resolve_voice
                          still accepts them — that is how the composer
                          renders the student's own lines.
      anything else       → returned as-is; resolve_voice applies the gate.

    `provider` (a canary account's family) overrides TTS_PREMIUM_PROVIDER for
    this one pick; None means the global setting.
    """
    enabled = enabled_providers() if enabled is None else enabled
    req = (requested or "").strip()
    v = get_voice(req) if req else None
    if v is not None and v.role == STUDENT_ROLE:
        logger.warning("narration voice %r is a student voice; it is not a narration option → auto", req)
        req, v = "", None
    if not req or req == AUTO_VOICE_ID:
        if allow_premium:
            prem = default_premium_voice_id_for(lang, provider=provider)
            pv = get_voice(prem) if prem else None
            if pv is not None and pv.provider in enabled:
                return pv.voice_id
        return default_voice_id_for(lang)
    spoken = "ms" if lang == "ms-arab" else (lang or "en")
    if (spoken != "en" and v is not None and v.provider == "edge"
            and v.lang != spoken and not explicit_language):
        return default_voice_id_for(lang)
    return req


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
    lang: str | None = None,
) -> Path:
    """Synthesize `text` to an MP3 at `out_path`. Returns the path.

    `voice_id` is a registry id (default = the free voice for `lang`).
    `allow_premium` is the server-resolved tier gate. `ssml_text` (the copy
    that keeps <break> markup) is used for providers that honour it; Edge
    speaks plain `text`. `stream` is reserved for the future tutor path and
    currently ignored. `lang` is the lesson language: every fallback lands on
    that language's free voice, never on English Aria.

    If `report` (a dict) is passed, it is filled with what ACTUALLY happened —
    {requested, used, provider, downgraded} — so callers can surface a silent
    premium→free downgrade instead of a teacher discovering the wrong voice
    only by listening. `downgraded` is True when a premium voice was requested
    but a free voice was rendered.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    requested = get_voice(voice_id)
    requested_premium = bool(requested and requested.tier == "premium")
    voice = resolve_voice(voice_id, allow_premium=allow_premium, lang=lang)
    free = get_voice(default_voice_id_for(lang, gender=(requested.gender if requested else None))) or default_voice()
    bnd = Path(boundaries_out) if boundaries_out else None

    say = text
    stats: dict = {}
    # Why a premium request did not render premium — the row records it, so
    # a gate decision can be told apart from a cap trip or an outage.
    reason: str | None = None
    if requested_premium and voice.tier != "premium":
        reason = "gate" if not allow_premium else "provider_disabled"
    reserved = 0
    if voice.provider in cost.PAID_PROVIDERS:
        # Paid providers honour <break>; they get the markup copy. The cap is
        # RESERVED (check-and-add under one lock), not merely checked: every
        # pool thread used to pass the same check and overshoot it together.
        say = ssml_text or text
        reserved = len((say or "").strip())
        if not cost.reserve(reserved, voice.provider):
            logger.warning("%s spend cap reached → free voice for this call", voice.provider)
            voice, say, reserved, reason = free, text, 0, "cap"

    def _fallback(exc: Exception, who: str) -> None:
        nonlocal voice, say, reserved, reason
        logger.error("%s failed (%s) → falling back to free Edge", who, exc)
        from .providers import edge
        if reserved:
            cost.release(reserved, voice.provider)
            reserved = 0
        reason = "provider_error"
        # Edge reads SSML tags ALOUD — it only ever gets plain text. The
        # boundaries sink is forwarded, so a provider outage does not also
        # lose word timing.
        voice, say = free, strip_ssml(text)
        edge.synthesize(say, out_path, voice.ref, boundaries_out=bnd)

    if voice.provider == "elevenlabs":
        from .providers import eleven
        try:
            eleven.synthesize(say, out_path, voice.ref)
        except Exception as exc:  # noqa: BLE001 — never fail a generation over TTS
            _fallback(exc, "ElevenLabs")
    elif voice.provider == "google":
        from .providers import google
        try:
            stats = google.synthesize(say, out_path, voice.ref, boundaries_out=bnd) or {}
        except Exception as exc:  # noqa: BLE001 — never fail a generation over TTS
            _fallback(exc, "Google TTS")
    else:
        from .providers import edge
        say = strip_ssml(say)  # Edge reads SSML tags aloud — plain text only
        edge.synthesize(say, out_path, voice.ref, boundaries_out=bnd)

    # Google reports its own billable count (SSML minus the unbilled marks);
    # everything else bills what was sent. A paid render settles its
    # reservation to the real figure; a free one only logs.
    billed = int(stats.get("chars") or len((say or "").strip()))
    if voice.provider in cost.PAID_PROVIDERS:
        cost.settle(reserved, billed, voice.provider, stats.get("family"))
    else:
        cost.record(billed, voice.provider)
    downgraded = requested_premium and voice.tier != "premium"
    if downgraded:
        logger.warning(
            "VOICE DOWNGRADE: requested premium %r but rendered %r (%s) — the account "
            "is not on a paid tier, the provider is not enabled/keyed on the worker, "
            "the spend cap was hit, or the provider call failed.",
            voice_id, voice.voice_id, voice.provider,
        )
    if report is not None:
        report.update(
            {"requested": voice_id, "used": voice.voice_id, "provider": voice.provider,
             "downgraded": downgraded, "reason": reason if downgraded else None,
             "chars": billed, "stats": stats}
        )
    logger.info("TTS ok: voice=%s provider=%s -> %s", voice.voice_id, voice.provider, out_path.name)
    return out_path
