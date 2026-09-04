"""Pen / hand sprite — the visible instrument at the draw frontier.

Abstraction, not a hardcoded animation (§12): every drawing-ish action carries
a pen mode (auto/pen/hand/none); "auto" defers to the scene style. The sprite
is SCREEN-space (a hand does not balloon when the camera zooms) and sits at
the exact arc-length frontier of whatever is being revealed, so the ink always
appears at the nib.

"hand" mode composites a raster hand-holding-pen asset (AI-generated through
raster_assets under the key "hand_pen", tip offset stored in its meta); when
that asset is absent it degrades to the vector pen — a lesson never fails
because a hand image does (§20).
"""

from __future__ import annotations

from PIL import Image, ImageDraw

Point = tuple[float, float]

_HAND_KEY = "hand_pen"
_HAND_HEIGHT = 190.0  # px on the (unsupersampled) screen


def resolve_mode(action_pen: str, style_pen: str) -> str:
    return style_pen if action_pen == "auto" else action_pen


def draw_vector_pen(draw: ImageDraw.ImageDraw, x: float, y: float, ss: int = 1) -> None:
    """A marker pen, nib at (x, y). Screen coords already supersampled by ss."""
    s = ss
    # shadow, barrel, cap band, highlight, nib — angled up-right like a hand hold
    draw.line([(x + 4 * s, y + 5 * s), (x + 42 * s, y - 96 * s)], fill=(0, 0, 0, 60), width=13 * s)
    draw.line([(x, y), (x + 38 * s, y - 101 * s)], fill=(38, 40, 46), width=13 * s)
    draw.line([(x + 26 * s, y - 68 * s), (x + 38 * s, y - 101 * s)], fill=(70, 74, 82), width=13 * s)
    draw.line([(x + 4 * s, y - 10 * s), (x + 32 * s, y - 84 * s)], fill=(128, 132, 142), width=3 * s)
    draw.polygon([(x - 6 * s, y - 3 * s), (x + 8 * s, y + 2 * s), (x + 1 * s, y + 12 * s)],
                 fill=(16, 17, 20))


def draw_eraser(draw: ImageDraw.ImageDraw, x: float, y: float, ss: int = 1) -> None:
    s = ss
    draw.rectangle([x - 26 * s, y - 16 * s, x + 26 * s, y + 16 * s], fill=(235, 233, 228),
                   outline=(120, 120, 126), width=2 * s)
    draw.rectangle([x - 26 * s, y - 16 * s, x + 26 * s, y - 6 * s], fill=(52, 56, 64))


class PenSprite:
    """Loads the optional hand asset once; stamps whichever instrument the
    frame needs. `hand_loader` is injected (raster_assets provides it) so this
    module stays import-light and testable without network or PIL file IO."""

    def __init__(self, hand_loader=None):
        self._hand: Image.Image | None = None
        self._tip: Point = (0.0, 0.0)
        # the hand at each (w, h) it has been stamped at, with its scaled tip
        # offset. The sprite is a uniform scale of ONE source image, so the
        # same LANCZOS call on the same input reproduces the same bytes — a
        # cache hit is pixel-identical to the resize it replaces, and the
        # resize was 11-16 ms on every pen frame (about half of all frames).
        # One PenSprite per SceneRenderer per segment thread: no lock needed.
        self._scaled: dict[tuple[int, int], tuple[Image.Image, float, float]] = {}
        if hand_loader is not None:
            try:
                loaded = hand_loader(_HAND_KEY)
                if loaded is not None:
                    self._hand, self._tip = loaded
            except Exception:
                self._hand = None  # degrade silently: vector pen still teaches

    def stamp(self, frame: Image.Image, mode: str, x: float, y: float, ss: int,
              erasing: bool = False, scale: float = 1.0) -> None:
        if mode == "none":
            return
        d = ImageDraw.Draw(frame, "RGBA")
        if erasing:
            draw_eraser(d, x, y, ss)
            return
        if mode == "hand" and self._hand is not None:
            h = int(_HAND_HEIGHT * scale * ss)
            k = h / self._hand.height
            w = max(1, int(self._hand.width * k))
            entry = self._scaled.get((w, h))
            if entry is None:
                hand = self._hand.resize((w, h), Image.LANCZOS)
                entry = self._scaled[(w, h)] = (hand, self._tip[0] * k, self._tip[1] * k)
            frame.paste(entry[0], (int(x - entry[1]), int(y - entry[2])), entry[0])
            return
        draw_vector_pen(d, x, y, ss)
