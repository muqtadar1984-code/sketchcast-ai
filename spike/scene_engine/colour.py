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

THE PIN. ``params.board_colour`` on ONE generation decides for that
generation either way, flag or no flag — the same lever the subject
profile has (shared/subject_profile.PARAM_KEY). It is how a demo video is
drawn in colour on a flag-off worker, and how one lesson is drawn plain
on a flag-on worker. The worker sets it once per generation
(``set_pin``); it travels in a ContextVar, so the adapter and the
compiler, which run in the generation's own thread, read it without
being handed it.
"""

from __future__ import annotations

import contextvars
import os

FLAG = "FEATURE_BOARD_COLOUR"
PARAM_KEY = "board_colour"

# Phase 2 — restrained colour in the GENERATED PICTURES — is its own switch
# on top of phase 1, so the marks can go live without the pictures and one
# lesson can pin the pictures on while the worker's flag is off. It is read
# by raster_assets (the style asked of the image model, the cutout that
# keeps fills, the cache key) and by the visual library integration (a
# coloured key never hydrates from, or publishes to, the ink library).
PICTURES_FLAG = "FEATURE_BOARD_COLOUR_PICTURES"
PICTURES_PARAM = "board_colour_pictures"

# None: not pinned, the flag decides. True/False: this generation's answer.
_PIN: contextvars.ContextVar[bool | None] = contextvars.ContextVar("board_colour_pin", default=None)
_PICTURES_PIN: contextvars.ContextVar[bool | None] = contextvars.ContextVar("board_colour_pictures_pin", default=None)

# the roles, as the palette (spike/scene_engine/paper.py) names them
ACCENT = "accent"
ACCENT2 = "accent2"
INK = "ink"

# the signs that make a row of texts an equation (word or symbol)
EQUATION_SIGNS = frozenset({"+", "-", "−", "=", "→", "->", "⟶", "×", "÷", "⇌", "≈", "<", ">", "≤", "≥"})


def _truthy(value: object) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def enabled() -> bool:
    pin = _PIN.get()
    if pin is not None:
        return pin
    return _truthy(os.getenv(FLAG, ""))


def pictures_enabled() -> bool:
    """Phase 2: colour in the generated pictures. Its own pin, then its own
    flag; never implied by phase 1."""
    pin = _PICTURES_PIN.get()
    if pin is not None:
        return pin
    return _truthy(os.getenv(PICTURES_FLAG, ""))


def _pin_value(value: object) -> bool | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return bool(value) if isinstance(value, bool) else _truthy(value)


def set_pin(value: object, pictures: object = None) -> contextvars.Token:
    """Pin this generation's answer: True/False from a param value, None
    (or an absent param) to let the flag decide. Returns the token to
    ``reset_pin`` with. ``pictures`` pins phase 2 the same way."""
    _PICTURES_PIN.set(_pin_value(pictures))
    return _PIN.set(_pin_value(value))


def reset_pin(token: contextvars.Token) -> None:
    _PIN.reset(token)
    _PICTURES_PIN.set(None)


def pin_from_params(params: dict | None) -> contextvars.Token:
    """The worker's call: ``params.board_colour`` and
    ``params.board_colour_pictures`` when the generation carries them, else
    unpinned."""
    p = params if isinstance(params, dict) else {}
    return set_pin(p.get(PARAM_KEY), p.get(PICTURES_PARAM))


def snapshot() -> tuple[bool | None, bool | None]:
    """This thread's pins, for a pool thread to ``restore`` — contextvars
    are per thread and a render thread starts empty (raster_assets
    .bind_generation)."""
    return (_PIN.get(), _PICTURES_PIN.get())


def restore(snap: tuple[bool | None, bool | None]) -> tuple[contextvars.Token, contextvars.Token]:
    return (_PIN.set(snap[0]), _PICTURES_PIN.set(snap[1]))


def release(tokens: tuple[contextvars.Token, contextvars.Token]) -> None:
    _PIN.reset(tokens[0])
    _PICTURES_PIN.reset(tokens[1])


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


__all__ = ["FLAG", "PARAM_KEY", "PICTURES_FLAG", "PICTURES_PARAM", "ACCENT", "ACCENT2", "INK",
           "EQUATION_SIGNS", "enabled", "pictures_enabled", "set_pin", "reset_pin",
           "pin_from_params", "snapshot", "restore", "release", "arrow_colour", "tint_arrow",
           "colour_equation_row"]
