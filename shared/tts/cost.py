"""Per-call character + cost logging and a local per-process runaway cap for
paid TTS, keyed by provider.

Edge is free ($0) and uncapped. Paid providers (ElevenLabs, Google) are
metered: every call logs its character count and estimated cost, and a
running per-provider total is kept so one runaway generation cannot burn a
budget in a loop. Once a provider's cap is hit, paid synthesis is refused and
the caller falls back to the free voice.

WHAT THIS CAP IS AND IS NOT. The total lives in a local JSON file. On Railway
that file is per process and resets on every deploy, and the render pool's
threads share it — so it bounds a single process's spend between restarts,
which is exactly the runaway case, and nothing more. It is not a monthly
ledger and never was; the durable, atomic, per-user ledger is the app's
tts_usage table (migration 0027, tutor_tts_reserve), which the worker
records into after each lesson. Google's own free allowance is tracked by
Google; a GCP budget alert is the belt for it.

RESERVE, THEN SETTLE. The check and the add used to be two separately locked
steps with a network call between them, so every render thread in flight
passed the same check and the cap was overshot by (threads − 1) segments.
``reserve()`` now checks and adds under ONE lock before the call; ``settle()``
replaces the reservation with what the provider actually billed; ``release()``
returns it when the call failed and the segment fell back to Edge.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

logger = logging.getLogger("shared.tts.cost")

# USD per 1,000 characters, by provider (and family for Google, whose
# families bill differently). Overridable per env.
#   ElevenLabs: the code shipped with $0.30/1K, a placeholder 3.5x the real
#   rate — turbo bills 0.5 credit/char and the Pro plan is ~$85/1M. Left as an
#   env override so an account on a different plan can correct it.
#   Google (pricing page, 2026-09-03): Standard/WaveNet $4/1M, Neural2 $16/1M,
#   Chirp 3 HD $30/1M, after the monthly free allowance.
_USD_PER_1K = {
    "elevenlabs": float(os.getenv("ELEVENLABS_USD_PER_1K_CHARS", "0.085")),
    "google:chirp": float(os.getenv("GOOGLE_TTS_USD_PER_1K_CHIRP", "0.030")),
    "google:classic": float(os.getenv("GOOGLE_TTS_USD_PER_1K_CLASSIC", "0.004")),
    "google": float(os.getenv("GOOGLE_TTS_USD_PER_1K_CHARS", "0.030")),
}

# Per-provider, per-process runaway caps (characters). ElevenLabs keeps its
# historical env name; Google's default is ~100 lessons of Chirp.
_CHAR_CAP = {
    "elevenlabs": int(os.getenv("ELEVENLABS_CHAR_CAP", "500000")),
    "google": int(os.getenv("GOOGLE_TTS_CHAR_CAP", "1000000")),
}

_SPEND_FILE = Path(__file__).resolve().parents[2] / "storage" / "tts_spend.json"
# Re-entrant: reserve() holds it while it calls within_cap(), which takes it too.
_LOCK = threading.RLock()

PAID_PROVIDERS = frozenset(_CHAR_CAP)


def estimate_cost_usd(chars: int, provider: str, family: str | None = None) -> float:
    if provider not in PAID_PROVIDERS:
        return 0.0
    key = f"{provider}:{family}" if family and f"{provider}:{family}" in _USD_PER_1K else provider
    return round(chars / 1000.0 * _USD_PER_1K.get(key, 0.0), 4)


def _read_totals() -> dict:
    try:
        data = json.loads(_SPEND_FILE.read_text())
    except Exception:  # noqa: BLE001
        return {}
    # legacy single-provider shape: {"elevenlabs_chars": N}
    totals = {k[:-6]: int(v) for k, v in data.items() if k.endswith("_chars")}
    return totals


def _write_totals(totals: dict) -> None:
    try:
        _SPEND_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {f"{k}_chars": int(v) for k, v in totals.items()}
        payload["updated_at"] = time.time()
        _SPEND_FILE.write_text(json.dumps(payload))
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not persist TTS spend: %s", exc)


def _bump(provider: str, delta: int) -> int:
    """Add `delta` (may be negative) to a paid provider's running total, floored
    at zero. Returns the new total. Caller holds the lock or does not care."""
    with _LOCK:
        totals = _read_totals()
        totals[provider] = max(0, totals.get(provider, 0) + int(delta))
        _write_totals(totals)
        return totals[provider]


def within_cap(chars: int, provider: str = "elevenlabs") -> bool:
    """True if `chars` more characters on `provider` stay within its cap.
    Free providers are never capped. Read-only — see reserve() for the
    check-and-add a concurrent caller needs."""
    if provider not in PAID_PROVIDERS:
        return True
    with _LOCK:
        return _read_totals().get(provider, 0) + max(0, chars) <= _CHAR_CAP[provider]


def reserve(chars: int, provider: str) -> bool:
    """Atomically check the cap AND count `chars` against it. True = go ahead
    (the reservation is booked); False = refused, nothing changed. Free
    providers are always True and never booked. Pair with settle() after the
    provider answers, or release() when the call failed."""
    if provider not in PAID_PROVIDERS:
        return True
    with _LOCK:
        if not within_cap(chars, provider):
            return False
        _bump(provider, max(0, chars))
        return True


def release(chars: int, provider: str) -> None:
    """Give a reservation back (the call failed; the segment went to Edge)."""
    if provider in PAID_PROVIDERS and chars > 0:
        _bump(provider, -int(chars))


def record(chars: int, provider: str, family: str | None = None) -> None:
    """Log the call and, for paid providers, add to the persisted running total."""
    cost = estimate_cost_usd(chars, provider, family)
    if provider in PAID_PROVIDERS:
        total = _bump(provider, max(0, chars))
        logger.info(
            "TTS spend: provider=%s%s chars=%d est=$%.4f  (process total %d/%d chars)",
            provider, f"/{family}" if family else "", chars, cost, total, _CHAR_CAP[provider],
        )
    else:
        logger.info("TTS usage: provider=%s chars=%d est=$0 (free)", provider, chars)


def settle(reserved: int, billed: int, provider: str, family: str | None = None) -> None:
    """Replace a reservation with what the provider actually billed: the
    reserved amount comes back off the total and the billed amount goes on
    through record(), so the log line shows the real figure."""
    release(reserved, provider)
    record(billed, provider, family)
