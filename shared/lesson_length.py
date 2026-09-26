"""Lesson length floor — a catalogue video runs at least LESSON_MIN_MINUTES.

Why this exists (Aerobic Respiration, 2026-09-26): the analyser handed the
script model a 9.0-minute target for a 1,170-word article and the model
returned 620 words of dialogue — 170 seconds of audio. The draft named 11 of
14 topics (0.786, "ok") at 260 characters per topic (not thin), so every
gate in shared/coverage.py passed it, and nothing anywhere compared what was
written to the minutes that were asked for. Across the 25 catalogue kits
before it, every video shipped between 2.6 and 5.8 minutes while the
articles implied 5 to 15.

So this module measures the one number those gates could not see — how long
the SPOKEN script will run — and the founder's floor for it. Pure: no model
call, no I/O.

The rate is measured, not assumed. agent2_analysis plans at 130 words per
minute, which is a reading pace; the production voices run much faster once
silence is trimmed. Spoken characters against audio seconds on recent kits:

    chars   audio    chars/min
    3,494   170.8 s   1,227
    4,292   215.6 s   1,194
    3,303   159.0 s   1,246
    5,772   297.8 s   1,163
    6,503   327.7 s   1,191
    6,802   348.5 s   1,171

CHARS_PER_MINUTE is the FASTEST of those rounded up, so the floor is
conservative: a script that clears it runs at least the floor even on the
quickest voice, and at the typical rate lands ten percent over.
"""

from __future__ import annotations

import os

# The founder's floor for a catalogue video. Overridable for an experiment
# (LESSON_MIN_MINUTES), never below zero; zero turns the rule off.
MIN_LESSON_MINUTES = 5.0

# Spoken characters per minute of finished audio — the fastest measured rate
# (table above), so chars ≥ minutes × this guarantees the minutes.
CHARS_PER_MINUTE = 1300

# For the prompt only: how many words the character floor is, roughly. English
# dialogue runs ~6 characters a word including the space.
_CHARS_PER_WORD = 6

# How many further script calls a draft under the floor may earn. Each names
# the measured shortfall; the cheapest call in the pipeline, spent before a
# single character of TTS or a single frame.
MAX_LENGTH_RETRIES = 2


def min_minutes() -> float:
    """The floor in minutes; 0.0 when the rule is off."""
    raw = os.getenv("LESSON_MIN_MINUTES", "").strip()
    if not raw:
        return MIN_LESSON_MINUTES
    try:
        return max(0.0, float(raw))
    except ValueError:
        return MIN_LESSON_MINUTES


def min_chars(minutes: float) -> int:
    """The spoken characters a ``minutes``-long lesson needs at the fastest
    measured rate."""
    return int(round(max(0.0, minutes) * CHARS_PER_MINUTE))


def min_words(minutes: float) -> int:
    return int(round(min_chars(minutes) / _CHARS_PER_WORD))


def spoken_text(script: dict) -> str:
    """What the voices will actually say: each segment's narration, or its
    dialogue lines when the narration field is empty (the semantic prompt
    writes dialogue and leaves ``text`` blank until the adapter flattens it).
    On-screen labels and slide text are NOT counted — they take no time."""
    out: list[str] = []
    for seg in (script or {}).get("segments") or []:
        if not isinstance(seg, dict):
            continue
        text = str(seg.get("text") or "").strip()
        if not text:
            lines = seg.get("dialogue") or []
            text = " ".join(
                str(ln.get("line") or ln.get("text") or "") if isinstance(ln, dict) else str(ln or "")
                for ln in lines
            ).strip()
        if text:
            out.append(text)
    return " ".join(out)


def measure(script: dict, minutes: float | None = None) -> dict:
    """``{chars, est_minutes, min_minutes, min_chars, under}`` for a script.
    ``under`` is False whenever the floor is off (zero minutes)."""
    floor = min_minutes() if minutes is None else max(0.0, float(minutes))
    chars = len(spoken_text(script))
    need = min_chars(floor)
    return {
        "chars": chars,
        "est_minutes": round(chars / CHARS_PER_MINUTE, 2),
        "min_minutes": floor,
        "min_chars": need,
        "under": floor > 0 and chars < need,
    }


def prompt_block(minutes: float) -> str:
    """The instruction that puts the floor in front of the script model. Stated
    in words AND characters: a model asked for minutes alone wrote three of
    nine (the incident above); one told how many words that is can count."""
    return (
        f"\n\nMINIMUM LENGTH — this lesson must run at least {minutes:g} minutes "
        f"when spoken. On this pipeline's voices that is at least "
        f"{min_words(minutes):,} words of dialogue ({min_chars(minutes):,} "
        f"characters) across all segments. A shorter script is REJECTED and "
        f"regenerated. Reach the length by teaching, never by filler: for each "
        f"concept explain the mechanism, why it matters, a worked example or "
        f"analogy, and the misconception a learner holds and its correction. "
        f"Use as many teaching segments as the length needs."
    )


def shortfall_block(measured: dict) -> str:
    """The retry instruction: what the last draft measured against the floor."""
    return (
        f"\n\nLENGTH — a previous draft of this script ran about "
        f"{measured.get('est_minutes', 0):g} minutes when spoken "
        f"({measured.get('chars', 0):,} characters of dialogue) against a floor of "
        f"{measured.get('min_minutes', 0):g} minutes ({measured.get('min_chars', 0):,} "
        f"characters). It was rejected for length. This draft MUST reach the "
        f"floor: teach every concept more fully and add the teaching segments "
        f"the extra minutes need. Do not shorten anything that was already there."
    )


def summary(measured: dict) -> str:
    return (f"{measured.get('chars', 0)} spoken characters ≈ "
            f"{measured.get('est_minutes', 0):g} min against a floor of "
            f"{measured.get('min_minutes', 0):g} min ({measured.get('min_chars', 0)} characters)")
