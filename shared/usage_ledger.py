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
``total(generation_id)`` when the job finishes and writes it to the row in
the same shape as before: calls, input_tokens, output_tokens, cost_usd.
"""
from __future__ import annotations

import threading

from shared import phase_clock

_LOCK = threading.Lock()
_TOTALS: dict[str, dict] = {}


def _blank() -> dict:
    return {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}


def start(generation_id: str) -> None:
    """Open (or reopen) a generation's account — a requeued run starts afresh."""
    with _LOCK:
        _TOTALS[str(generation_id)] = _blank()


def add(entry: dict, generation_id: str | None = None) -> None:
    """Fold one usage entry (a token-log line) into its generation's total.
    Entries with no generation to belong to are dropped, not misfiled."""
    gid = generation_id or entry.get("generation_id") or phase_clock.current()
    if not gid:
        return
    gid = str(gid)
    cost = entry.get("estimated_cost_usd")
    if cost is None:
        cost = entry.get("cost_usd")
    with _LOCK:
        tot = _TOTALS.get(gid)
        if tot is None:
            return                       # no account open: a call outside a job
        tot["calls"] += 1
        try:
            tot["input_tokens"] += int(entry.get("input_tokens") or 0)
            tot["output_tokens"] += int(entry.get("output_tokens") or 0)
            tot["cost_usd"] = round(tot["cost_usd"] + float(cost or 0.0), 6)
        except (TypeError, ValueError):
            pass


def total(generation_id: str) -> dict:
    """The running total (a copy); a blank dict when no account is open."""
    with _LOCK:
        return dict(_TOTALS.get(str(generation_id)) or _blank())


def stop(generation_id: str) -> dict:
    """Close the account and return its final total."""
    with _LOCK:
        return dict(_TOTALS.pop(str(generation_id), None) or _blank())


def reset() -> None:
    """Tests: forget every account."""
    with _LOCK:
        _TOTALS.clear()
