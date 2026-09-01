"""Background + palette. Reuses agent5's theme so the video, the deck and the
app stay one brand; adds only the faint paper grain that makes the surface
read as a drawing surface instead of a slide."""

from __future__ import annotations

import random

from PIL import Image, ImageDraw

from agent5_slides.theme import CANVAS, GRAPHITE, INK, TEAL, TEAL_DK, TEAL_MIST, WHITE

PALETTE = {
    "ink": INK,
    "accent": TEAL_DK,       # accent LINES use the dark teal (legible on white)
    "accent_bright": TEAL,
    "accent_mist": TEAL_MIST,
    "muted": GRAPHITE,
    "marker": (255, 214, 74),  # highlighter yellow — the one non-theme color,
                               # because a teal highlighter reads as drawing
    "paper": CANVAS,           # opaque fill matching the board — speech
                               # bubbles use it to occlude what sits behind
}

_GRAIN_SEED = 7


def make_background(w: int, h: int, kind: str = "canvas") -> Image.Image:
    """The whiteboard/paper surface. Deterministic: same size, same pixels."""
    base = WHITE if kind == "white" else CANVAS
    img = Image.new("RGB", (w, h), base)
    if kind == "white":
        return img
    # sparse grain: a few hundred barely-visible flecks. Enough that the eye
    # reads "paper", never enough to read "noise".
    rng = random.Random(_GRAIN_SEED)
    d = ImageDraw.Draw(img)
    fleck = tuple(max(0, c - 6) for c in base)
    for _ in range(int(w * h / 2800)):
        x, y = rng.randrange(w), rng.randrange(h)
        d.point((x, y), fill=fleck)
    return img


def role_color(role: str, style_ink=None, style_accent=None) -> tuple[int, int, int]:
    if role == "ink" and style_ink:
        return tuple(style_ink)
    if role == "accent" and style_accent:
        return tuple(style_accent)
    return PALETTE.get(role, INK)
