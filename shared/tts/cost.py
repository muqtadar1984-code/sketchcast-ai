"""Per-call character + cost logging and a local spend cap for paid TTS.

Edge is free ($0) and uncapped. ElevenLabs is metered: every call logs its
character count + estimated cost, and a running character total is persisted so
a runaway generation can't burn the budget — once the cap is hit, paid synthesis
is refused and the caller falls back to the free voice.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger("shared.tts.cost")

# Rough ElevenLabs price (USD per 1000 chars). Override via env to match your plan.
_USD_PER_1K = float(os.getenv("ELEVENLABS_USD_PER_1K_CHARS", "0.30"))
# Local safety cap on total ElevenLabs characters (across runs). Generous but bounded.
_CHAR_CAP = int(os.getenv("ELEVENLABS_CHAR_CAP", "500000"))

_SPEND_FILE = Path(__file__).resolve().parents[2] / "storage" / "tts_spend.json"


def estimate_cost_usd(chars: int, provider: str) -> float:
    if provider != "elevenlabs":
        return 0.0
    return round(chars / 1000.0 * _USD_PER_1K, 4)


def _read_total() -> int:
    try:
        return int(json.loads(_SPEND_FILE.read_text()).get("elevenlabs_chars", 0))
    except Exception:  # noqa: BLE001
        return 0


def _write_total(total: int) -> None:
    try:
        _SPEND_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SPEND_FILE.write_text(json.dumps({"elevenlabs_chars": total, "updated_at": time.time()}))
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not persist TTS spend: %s", exc)


def within_cap(chars: int) -> bool:
    """True if `chars` more ElevenLabs characters stay within the local cap."""
    return _read_total() + max(0, chars) <= _CHAR_CAP


def record(chars: int, provider: str) -> None:
    """Log the call and, for paid providers, add to the persisted running total."""
    cost = estimate_cost_usd(chars, provider)
    if provider == "elevenlabs":
        total = _read_total() + max(0, chars)
        _write_total(total)
        logger.info(
            "TTS spend: provider=%s chars=%d est=$%.4f  (cumulative %d/%d chars)",
            provider, chars, cost, total, _CHAR_CAP,
        )
    else:
        logger.info("TTS usage: provider=%s chars=%d est=$0 (free)", provider, chars)
