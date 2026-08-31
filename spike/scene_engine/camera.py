"""Camera: a keyframed (center, scale) track over the logical canvas.

The camera exists to direct attention (zoom toward the membrane while the
molecules interact, come back out for the whole cell) — it is deliberately
constrained: scale <= 2.5, eased moves, and every transform maps the WORLD
(1280x720 logical canvas) to the SCREEN so vector content re-rasterizes crisp
at any zoom.

screen = (world - center) * scale + (W/2, H/2)
"""

from __future__ import annotations

from dataclasses import dataclass

from .geometry import ease, lerp
from .schema import WORLD_H, WORLD_W
from .timing import TimedAction

Point = tuple[float, float]


@dataclass(frozen=True)
class CameraState:
    cx: float = WORLD_W / 2
    cy: float = WORLD_H / 2
    scale: float = 1.0

    def to_screen(self, p: Point) -> Point:
        return ((p[0] - self.cx) * self.scale + WORLD_W / 2,
                (p[1] - self.cy) * self.scale + WORLD_H / 2)

    def clamped(self) -> "CameraState":
        """Keep the viewport inside the world so zooms never show void: at
        scale s the half-viewport in world units is (W/2s, H/2s)."""
        s = max(1.0, self.scale)
        hw, hh = WORLD_W / (2 * s), WORLD_H / (2 * s)
        cx = min(WORLD_W - hw, max(hw, self.cx))
        cy = min(WORLD_H - hh, max(hh, self.cy))
        return CameraState(cx, cy, s)


@dataclass(frozen=True)
class _Key:
    t: float
    state: CameraState
    easing: str


class CameraTrack:
    """Built from the camera actions on a compiled timeline. Between keys the
    state eases; before the first and after the last it holds."""

    def __init__(self, timed: list[TimedAction],
                 focus_center: dict[int, Point] | None = None):
        """`focus_center` maps timeline index -> resolved center for zoom
        actions whose center came from a target element's bbox (the renderer
        resolves geometry; the camera only interpolates)."""
        focus_center = focus_center or {}
        self._keys: list[_Key] = [_Key(0.0, CameraState(), "linear")]
        state = CameraState()
        for i, ta in enumerate(timed):
            a = ta.action
            if a.verb == "zoom":
                c = a.center or focus_center.get(i) or (state.cx, state.cy)
                state = CameraState(c[0], c[1], a.scale).clamped()
            elif a.verb == "pan":
                state = CameraState(a.center[0], a.center[1], state.scale).clamped()
            elif a.verb == "camera_reset":
                state = CameraState()
            else:
                continue
            # key pair: hold previous state until the move starts, arrive at
            # the end. Key times must stay MONOTONIC or state_at interpolates
            # the wrong pair and the camera teleports — a camera action cued
            # inside the previous move's duration is legal upstream, so it is
            # serialized here: it begins when the previous move lands.
            prev = self._keys[-1].state
            start = max(ta.start, self._keys[-1].t)
            end = max(ta.end, start + 1e-3)
            self._keys.append(_Key(start, prev, "linear"))
            self._keys.append(_Key(end, state, a.easing))

    def state_at(self, t: float) -> CameraState:
        keys = self._keys
        if t <= keys[0].t:
            return keys[0].state
        for i in range(len(keys) - 1):
            k0, k1 = keys[i], keys[i + 1]
            if t <= k1.t:
                span = k1.t - k0.t
                u = 1.0 if span <= 1e-9 else ease(k1.easing, (t - k0.t) / span)
                return CameraState(
                    lerp(k0.state.cx, k1.state.cx, u),
                    lerp(k0.state.cy, k1.state.cy, u),
                    lerp(k0.state.scale, k1.state.scale, u),
                )
        return keys[-1].state
