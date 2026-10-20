"""Model spend per generation, from every client, every thread.

``jobs.usage`` used to be written from ONE client's ``session_usage`` — the
general artifact client a build returns. A video is written by other
instances (the script role's client, the analysis role's client) and its
pictures and vision calls happen on render threads, so for nine of today's
nine presentations the row said nothing (2026-10-14: 1 of 9 carried usage;
documents, which use the one client, all did).

Every client already reports every call to ``ClaudeClient._log_usage`` (the
token log). This ledger sits beside that: each entry is also added to the
running total of the generation it belongs to, found from the usage labels
(``set_usage_context``, thread-local, set by the job thread) or, on a render
thread that carries no labels, from the phase clock's current generation
(a contextvar the composer binds into its pool threads). The worker reads
``total(generation_id)`` when the job finishes and writes it to the row.

WHAT A TOTAL HOLDS (2026-10-20, for the pricing strategy). The four original
keys — calls, input_tokens, output_tokens, cost_usd — still mean TEXT model
spend, so every reader of the old shape keeps working. Beside them:

* ``by_model`` — the same text spend split by who served it, keyed
  ``"<backend>:<model>"`` (backend = anthropic / bedrock / vertex / gemini),
  so Claude and Gemini separate without guessing from deploy dates. The
  entries sum to the top-level text figures; a call whose entry names no
  model lands under ``"unknown"``.
* ``images_usd`` / ``image_calls`` / ``images`` and ``images_by_model`` —
  picture generation. Image entries used to fold into ``calls`` and add $0
  (their price rides under ``usd``, which this ledger did not read).
* ``tts_usd`` / ``tts_calls`` / ``tts_chars`` and ``tts_by_provider`` —
  narration (shared.tts.cost.record).

``worker.client.set_job_usage`` adds ``total_usd`` (text + images + TTS) when
it writes the row.
"""
from __future__ import annotations

import copy
import threading

from shared import phase_clock

_LOCK = threading.Lock()
_TOTALS: dict[str, dict] = {}

_TEXT_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def _blank() -> dict:
    return {"calls": 0, "input_tokens": 0, "output_tokens": 0,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "cost_usd": 0.0,
            "by_model": {},
            "image_calls": 0, "images": 0, "images_usd": 0.0, "images_by_model": {},
            "tts_calls": 0, "tts_chars": 0, "tts_usd": 0.0, "tts_by_provider": {}}


def _money(entry: dict) -> float:
    for key in ("estimated_cost_usd", "cost_usd", "usd"):
        value = entry.get(key)
        if value is not None:
            return float(value)
    return 0.0


def _add_text(tot: dict, entry: dict) -> None:
    cost = _money(entry)
    model = entry.get("model")
    key = f"{entry.get('backend') or 'unknown'}:{model}" if model else "unknown"
    row = tot["by_model"].setdefault(key, {
        "backend": entry.get("backend") or "unknown", "model": model or "unknown",
        "calls": 0, **{k: 0 for k in _TEXT_KEYS}, "cost_usd": 0.0})
    for bucket in (tot, row):
        bucket["calls"] += 1
        for k in _TEXT_KEYS:
            bucket[k] += int(entry.get(k) or 0)
        bucket["cost_usd"] = round(bucket["cost_usd"] + cost, 6)


def _add_image(tot: dict, entry: dict) -> None:
    usd = _money(entry)
    produced = 1 if entry.get("outcome", "image") == "image" else 0
    key = f"{entry.get('service')}:{entry.get('model') or 'unknown'}"
    row = tot["images_by_model"].setdefault(key, {"calls": 0, "images": 0, "usd": 0.0})
    tot["image_calls"] += 1
    tot["images"] += produced
    tot["images_usd"] = round(tot["images_usd"] + usd, 6)
    row["calls"] += 1
    row["images"] += produced
    row["usd"] = round(row["usd"] + usd, 6)


def _add_tts(tot: dict, entry: dict) -> None:
    usd = _money(entry)
    chars = int(entry.get("chars") or 0)
    provider = str(entry.get("provider") or "unknown")
    if entry.get("family"):
        provider = f"{provider}:{entry['family']}"
    row = tot["tts_by_provider"].setdefault(provider, {"calls": 0, "chars": 0, "usd": 0.0})
    tot["tts_calls"] += 1
    tot["tts_chars"] += chars
    tot["tts_usd"] = round(tot["tts_usd"] + usd, 6)
    row["calls"] += 1
    row["chars"] += chars
    row["usd"] = round(row["usd"] + usd, 6)


def start(generation_id: str) -> None:
    """Open (or reopen) a generation's account — a requeued run starts afresh."""
    with _LOCK:
        _TOTALS[str(generation_id)] = _blank()


def add(entry: dict, generation_id: str | None = None) -> None:
    """Fold one usage entry (a token-log line) into its generation's total.
    Entries with no generation to belong to are dropped, not misfiled.
    The entry's ``service`` says which bucket: ``image.*`` pictures, ``tts``
    narration, anything else (a text call carries no service) text."""
    gid = generation_id or entry.get("generation_id") or phase_clock.current()
    if not gid:
        return
    gid = str(gid)
    service = str(entry.get("service") or "")
    with _LOCK:
        tot = _TOTALS.get(gid)
        if tot is None:
            return                       # no account open: a call outside a job
        try:
            if service.startswith("image"):
                _add_image(tot, entry)
            elif service == "tts":
                _add_tts(tot, entry)
            else:
                _add_text(tot, entry)
        except (TypeError, ValueError):
            pass


def total(generation_id: str) -> dict:
    """The running total (a copy); a blank dict when no account is open."""
    with _LOCK:
        return copy.deepcopy(_TOTALS.get(str(generation_id)) or _blank())


def stop(generation_id: str) -> dict:
    """Close the account and return its final total."""
    with _LOCK:
        return _TOTALS.pop(str(generation_id), None) or _blank()


def reset() -> None:
    """Tests: forget every account."""
    with _LOCK:
        _TOTALS.clear()
