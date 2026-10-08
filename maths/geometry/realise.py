"""Render policies: the numbers a figure is DRAWN with.

The canonical model is exact. A policy only decides what the renderer sees:

* ``instructional_metric`` — every measurement at its true value. For a
  worked example the picture should reinforce the mathematics, and for an
  EVIDENCE figure (classify this triangle, count the right angles) the
  student reads the answer off the drawing, so it must be true.
* ``assessment_schematic`` — a REASONING figure while the student is
  answering (a worksheet, a test, a video's try-it). The constructions are
  re-run with jittered measurements, so everything a construction
  guarantees survives — incidence, collinearity, which side a ray falls on,
  parallels and perpendiculars — while every numerical angle and length,
  the givens included, is drawn off its value. A given is a LABEL, not a
  drawn size: preserving the givens together with straightness would force
  the unknown to its true size and a protractor would recover it. The
  jitter keeps an angle's class (acute stays acute, a right angle stays
  right) so the picture stays plausible. Seeds are tried in order until
  every unknown is hidden by at least HIDE_ANGLE_DEG / HIDE_LENGTH_REL;
  if none hides it the figure is ``not_schematisable``.
"""

from __future__ import annotations

import hashlib
from typing import Optional

import sympy as sp

from maths.geometry.compiler import Resolver, compile_figure, realised
from maths.geometry.errors import GeometryRefusal
from maths.geometry.model import Model, to_float
from maths.geometry.spec import FigureSpec

POLICIES = ("instructional_metric", "assessment_schematic")

HIDE_ANGLE_DEG = 8.0      # an unknown angle is drawn at least this far from its value
HIDE_LENGTH_REL = 0.15    # an unknown length is drawn at least this fraction off
MAX_SEEDS = 24
_ANGLE_FLOOR = 15.0       # the picture stays plausible: no sliver angles
_ANGLE_CEIL = 165.0


def _factor(seed: int, where: str, kind: str, lo: float, hi: float) -> float:
    """A deterministic multiplier in [lo, hi] for this measurement."""
    h = hashlib.sha256(f"{seed}|{where}|{kind}".encode()).digest()
    u = int.from_bytes(h[:8], "big") / float(1 << 64)
    # avoid the middle: a factor near 1 hides nothing
    u = u * 0.5 if u < 0.5 else 0.5 + u * 0.5
    return lo + (hi - lo) * u


def jitter_angle(v: float, f: float) -> float:
    """An angle moved by factor f, keeping its class."""
    for fixed in (90.0, 180.0, 360.0):
        if abs(v - fixed) < 1e-9:
            return v
    if v < 90.0:
        return min(85.0, max(_ANGLE_FLOOR, v * f))
    if v < 180.0:
        return min(_ANGLE_CEIL, max(95.0, v * f))
    return min(345.0, max(195.0, v * f))


def schematic_resolver(bind: dict[str, sp.Expr], seed: int) -> Resolver:
    def resolve(exact: sp.Expr, kind: str, free: bool, where: str) -> float:
        v = to_float(exact, bind)
        if not free:
            return v
        # wide bands: an unknown DERIVED from a jittered given (an exterior
        # angle from an apex) moves by only a fraction of the given's move,
        # so the given must move a lot for the unknown to hide (B11)
        if kind == "angle":
            return jitter_angle(v, _factor(seed, where, kind, 0.55, 1.55))
        if kind == "length":
            return v * _factor(seed, where, kind, 0.6, 1.5)
        return v
    return resolve


def unknown_targets(spec: FigureSpec) -> list[str]:
    return [ms.target for ms in spec.measures if ms.role in ("unknown", "derived")]


def hidden_enough(metric: Model, drawn: Model, targets: list[str]) -> Optional[str]:
    """None when every unknown is drawn far from its value; else which one
    is not."""
    for t in targets:
        true, shown = realised(metric, t), realised(drawn, t)
        if t in metric.angle_ids:
            if abs(true - shown) < HIDE_ANGLE_DEG:
                return t
        elif abs(true - shown) < HIDE_LENGTH_REL * max(1e-9, abs(true)):
            return t
    return None


def realise(spec: FigureSpec, policy: str, *, metric: Optional[Model] = None) -> Model:
    """The model to draw under a policy. The metric model is compiled with
    its checks; the schematic one is a re-run of the same constructions
    with jittered numbers, checked only for hiding the unknowns."""
    if policy not in POLICIES:
        raise GeometryRefusal("bad_schema", f"render policy {policy!r} is not one of {POLICIES}")
    if metric is None:
        metric = compile_figure(spec)
    if policy == "instructional_metric":
        return metric
    if metric.axes is not None:
        # v2: the grid IS the ruler — a coordinate figure drawn off its
        # coordinates is wrong, not schematic. Metric under every policy.
        return metric
    targets = unknown_targets(spec)
    if not targets:
        # nothing to hide: the metric figure is the schematic figure
        return metric
    last = None
    for seed in range(MAX_SEEDS):
        try:
            drawn = compile_figure(spec, schematic_resolver(metric.bind, seed),
                                   check_givens=False, check_relations=False)
        except GeometryRefusal as exc:
            last = exc.message
            continue
        culprit = hidden_enough(metric, drawn, targets)
        if culprit is None:
            return drawn
        last = f"{culprit} still drawn near its value"
    raise GeometryRefusal("not_schematisable", f"no distortion hides the unknown ({last})")
