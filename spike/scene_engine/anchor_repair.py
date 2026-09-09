"""What the plan PROMISED to point at, checked against what the artwork can
actually deliver — while both are still free to change.

There is exactly one moment in a lesson build where this check is possible and
free. After `warm_lesson_assets` returns, every picture is on disk with its
`meta.json` regions written; before the render pool starts, not one frame has
been rasterised and not one character of TTS has been bought. The promise (the
plan's part names) and the delivery (the artwork's real regions) are both in
hand, and neither has been paid for yet.

Until now nothing looked. A plan could name `nucleus_region` on a picture
whose regions were `{}` and the first anyone knew of it was a leader line
stabbing the edge of a diagram in a finished video — recorded by
`validate.anchor_edge_fallbacks`, which nothing gates on.

WHY THE REPAIR CANNOT LIVE WHERE THE FAULT IS DETECTED. `_resolve_point` runs
inside `_bind()`, in a spawned child re-bound with `allow_generate=False`,
whose whole contract is no model gate, no rate limiter and no spend
attribution. The one place that sees the defect is structurally forbidden to
fix it. So the check runs here, in the parent, before the pool exists.

NOTHING IN THIS MODULE KNOWS WHAT A CELL IS. A want is `(asset_key, layer)`
carried verbatim from the plan; a repair asks vision for that name and files
the answer under it. Names are counted, passed and stored — never compared to
a vocabulary, lowercased into a rule, or matched against a list of subjects.
The only decision this module makes is the boolean `anchor_match.resolves()`,
which is the renderer's own predicate.
"""

from __future__ import annotations

import logging
from typing import Iterable

logger = logging.getLogger(__name__)

__all__ = ["scenes_of", "plan_anchor_wants", "unresolved_wants"]


def _asset_of(scene: dict) -> dict[str, str]:
    """element id -> asset key, for the illustrations of one scene."""
    out: dict[str, str] = {}
    for el in (scene.get("elements") or []):
        if not isinstance(el, dict) or el.get("type") != "illustration":
            continue
        eid, ak = el.get("id"), el.get("asset")
        if isinstance(eid, str) and isinstance(ak, str) and ak:
            out[eid] = ak
    return out


def _anchor_refs(node) -> Iterable[dict]:
    """Every AnchorRef-shaped dict anywhere in a scene.

    A walk rather than a list of known keys: anchors ride on arrow heads and
    tails today, and the schema is `extra="allow"`, so a structural walk keeps
    finding them when a new element type starts carrying one.
    """
    if isinstance(node, dict):
        if isinstance(node.get("el"), str) and isinstance(node.get("layer"), str):
            yield node
        for v in node.values():
            yield from _anchor_refs(v)
    elif isinstance(node, list):
        for v in node:
            yield from _anchor_refs(v)


def plan_anchor_wants(scenes: Iterable[dict]) -> dict[str, set[str]]:
    """asset key -> the set of part names the plan points at on it.

    Keyed by ASSET, not by element. A `clear_and_redraw` boundary copies a
    board forward as `prev__<id>`, renaming the element but carrying `asset`
    through verbatim, so `cell_city` and `prev__cell_city` are ONE picture and
    one repair — not two of everything.
    """
    wants: dict[str, set[str]] = {}
    for scene in scenes:
        if not isinstance(scene, dict):
            continue
        by_el = _asset_of(scene)
        for ref in _anchor_refs(scene):
            ak = by_el.get(ref["el"])
            if ak:
                wants.setdefault(ak, set()).add(ref["layer"])
    return wants


def unresolved_wants(wants: dict[str, set[str]],
                     regions_of) -> dict[str, list[str]]:
    """The subset of `wants` the artwork cannot answer, asset by asset.

    `regions_of(asset_key)` returns that asset's region names, or None when the
    picture is not available at all (a deferred generation) — those are somebody
    else's problem and are skipped rather than reported as anchor faults.

    The test is `anchor_match.resolves`, deliberately: a want must count as
    resolved under exactly the ladder the renderer will use, or a repair pass
    would buy boxes for names that already worked, or miss ones that did not.
    """
    from .anchor_match import resolves

    out: dict[str, list[str]] = {}
    for ak in sorted(wants):
        names = regions_of(ak)
        if names is None:
            continue
        missing = sorted(l for l in wants[ak] if not resolves(list(names), l))
        if missing:
            out[ak] = missing
    return out


def scenes_of(slide_segments, script_segments) -> list[dict]:
    """The scene dict of each segment, addressed exactly as
    `asset_warm.segment_asset_keys` addresses it, so the repair pass and the
    warm pass can never disagree about which pictures a lesson uses."""
    out: list[dict] = []
    for ss in slide_segments or []:
        seg = (script_segments or {}).get(
            str((ss or {}).get("segment_id") or "")) or {}
        scene = seg.get("scene") or {}
        if isinstance(scene, dict) and scene:
            out.append(scene)
    return out
