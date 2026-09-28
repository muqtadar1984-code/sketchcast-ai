"""Board colour, phase 1 — engine-owned colour roles for the marks the engine
already draws. Behind FEATURE_BOARD_COLOUR; OFF is the benchmark.

The founder's direction (2026-09-28): colour is presentation, so the engine
owns every colour decision and the director prompt stays colour-blind. The
scene palette already resolves ROLES (ink, accent, muted, marker) to the
theme's RGB at render time; continuity already collapses any colour name a
director emits to one of those roles. Phase 1 adds a second, contrasting
accent and assigns the roles deterministically from what the engine knows:

  * a LEADER arrow (tail is a label element) annotates: accent, the teal
  * a RELATION arrow (tail is a region or a point) shows a flow or a
    relationship: accent2, the coral
  * an EQUATION ROW in a text-only chapter (three or more texts written in
    one step with a sign in the middle) reads left side accent, sign ink,
    right side accent2
  * labels, titles, captions stay ink; the highlighter stays the marker;
    key terms in the caption stream stay accent; sketches stay as they are

Nothing here changes a generated picture: that is phase 2
(raster_assets style suffix + colour-preserving extraction), and it comes
only after this phase has been looked at.

THE BENCHMARK. With the flag off every function here is a no-op — no colour
key is written, the schema defaults apply, and the compiled plan is
byte-identical to today's (tests/test_board_colour.py pins that). Rolling
back is unsetting the variable; no code moves.
"""

from __future__ import annotations

import os

FLAG = "FEATURE_BOARD_COLOUR"

# the roles, as the palette (spike/scene_engine/paper.py) names them
ACCENT = "accent"
ACCENT2 = "accent2"
INK = "ink"

# the signs that make a row of texts an equation (word or symbol)
EQUATION_SIGNS = frozenset({"+", "-", "−", "=", "→", "->", "⟶", "×", "÷", "⇌", "≈", "<", ">", "≤", "≥"})


def enabled() -> bool:
    return os.getenv(FLAG, "").strip().lower() in ("1", "true", "yes", "on")


def arrow_colour(*, leader: bool) -> str | None:
    """The role an arrow element carries, or None to leave the schema
    default (ink) — which is what today's benchmark draws."""
    if not enabled():
        return None
    return ACCENT if leader else ACCENT2


def tint_arrow(el: dict, *, leader: bool) -> dict:
    """Set the arrow's colour role in place (flag on) and return it."""
    role = arrow_colour(leader=leader)
    if role is not None:
        el["color"] = role
    return el


def _is_sign(text: object) -> bool:
    t = " ".join(str(text or "").split())
    return bool(t) and t in EQUATION_SIGNS


def colour_equation_row(row: list[str], texts: dict[str, dict]) -> bool:
    """Colour one row of a text-only chapter when it reads as an equation:
    the texts before the first sign accent, the signs ink, the texts after
    accent2. Returns True when the row was coloured. A row with no sign,
    or nothing on one side of it, is left alone."""
    if not enabled() or len(row) < 3:
        return False
    signs = [i for i, eid in enumerate(row) if _is_sign(texts[eid].get("text"))]
    if not signs or signs[0] == 0 or signs[-1] == len(row) - 1:
        return False
    first = signs[0]
    for i, eid in enumerate(row):
        el = texts[eid]
        if i in signs:
            el["color"] = INK
        elif i < first:
            el["color"] = ACCENT
        else:
            el["color"] = ACCENT2
    return True


__all__ = ["FLAG", "ACCENT", "ACCENT2", "INK", "EQUATION_SIGNS", "enabled",
           "arrow_colour", "tint_arrow", "colour_equation_row"]
