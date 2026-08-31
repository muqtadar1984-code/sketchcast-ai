"""Cue resolution + timeline compilation.

Input: a Scene, the MEASURED narration length (seconds — from the actual MP3,
never the script estimate), and a per-action "workload" hint (path length in
px for draws, character count for writes) supplied by the renderer's bind step.

Output: a list of TimedActions with absolute start/duration, guaranteed to
finish `min_hold` seconds before the narration ends (so the finished visual
dwells while the voice completes — same principle as native_render's write cap,
generalized). If the natural durations overrun, everything compresses
proportionally down to a floor; if the scene is silent, total length is simply
animation + hold and the encoder pads accordingly.
"""

from __future__ import annotations

from dataclasses import dataclass

from .schema import Action, Cue, Scene

# duration defaults (seconds) — tuned to feel like drawing, not appearing
_PX_PER_SEC = 550.0        # pen speed for draw/trace workloads
_CHARS_PER_SEC = 14.0      # handwriting speed for writes
_VERB_DEFAULT = {
    "draw": 2.2, "write": 1.2, "reveal": 0.5, "erase": 1.0, "move": 1.6,
    "highlight": 1.0, "circle": 0.9, "underline": 0.6, "pulse": 0.9,
    "fade": 0.6, "morph": 1.0, "zoom": 1.2, "pan": 1.1, "camera_reset": 1.0,
}
_VERB_MIN = {"draw": 0.8, "write": 0.6, "move": 0.7, "zoom": 0.8, "pan": 0.8,
             "camera_reset": 0.8}
_VERB_MAX = {"draw": 7.0, "write": 4.5, "move": 6.0}
_GAP = 0.12                # breath between auto-sequenced actions
_COMPRESS_FLOOR = 0.35     # never compress below 35% of natural pace


@dataclass(frozen=True)
class TimedAction:
    action: Action
    start: float
    duration: float

    @property
    def end(self) -> float:
        return self.start + self.duration


def resolve_cue(cue: Cue, narration: str, audio_secs: float) -> float | None:
    """A cue's start time in seconds, or None when it cannot resolve (unknown
    phrase) — the caller then falls back to sequence order. Phrase resolution
    is by character midpoint: close enough for teaching sync without word-level
    timestamps (a v1 requirement)."""
    if audio_secs <= 0:
        # silent scene: fractions/phrases are meaningless and absolute cues
        # would pile everything at their raw times against no voice — fall
        # back to sequence order for all of them
        return None
    if cue.sec is not None:
        # never schedule far past the narration: a stray cue must not balloon
        # the clip (frames render to total_secs = anim end)
        return max(0.0, min(cue.sec, audio_secs + 10.0))
    if cue.frac is not None:
        return cue.frac * audio_secs
    if cue.phrase:
        hay, needle = narration.lower(), cue.phrase.lower()
        i = hay.find(needle)
        if i < 0 or not narration:
            return None
        mid = (i + len(needle) / 2) / len(narration)
        return mid * audio_secs
    return None


def natural_duration(action: Action, workload: float) -> float:
    """Un-compressed duration for an action given its workload hint (px of
    path for draw-like verbs, characters for write; 0 = no hint)."""
    verb = action.verb
    if action.duration is not None:
        return action.duration
    d = _VERB_DEFAULT.get(verb, 1.0)
    if workload > 0:
        if verb in ("draw", "highlight", "erase"):
            d = workload / _PX_PER_SEC
        elif verb == "write":
            d = workload / _CHARS_PER_SEC
    lo = _VERB_MIN.get(verb, 0.3)
    hi = _VERB_MAX.get(verb, 8.0)
    return min(hi, max(lo, d))


def compile_timeline(scene: Scene, audio_secs: float,
                     workloads: dict[int, float] | None = None) -> list[TimedAction]:
    """Absolute-time timeline for a scene.

    `workloads` maps action index -> workload hint. Actions with a cue start at
    the cue (but never before the previous action's start — teaching order is
    sacred even when a cue phrase appears early); actions without one chain
    after the previous action plus a small gap.
    """
    workloads = workloads or {}
    timeline: list[TimedAction] = []
    cursor = 0.15  # settle beat before the first mark
    for i, action in enumerate(scene.actions):
        dur = natural_duration(action, workloads.get(i, 0.0))
        start = None
        if action.at is not None:
            start = resolve_cue(action.at, scene.narration, audio_secs)
        if start is None:
            start = cursor + (_GAP if timeline else 0.0)
        start = max(start, cursor - 1e-9) if action.at is None else max(start, 0.0)
        # a cued action may overlap earlier ones (that is the point of cues) but
        # never runs before the previous action *started* — order stays readable
        if timeline and start < timeline[-1].start:
            start = timeline[-1].start
        timeline.append(TimedAction(action=action, start=start, duration=dur))
        cursor = max(cursor, start + dur)

    if not timeline:
        return timeline

    total = max(t.end for t in timeline)
    if audio_secs > 0:
        budget = audio_secs - scene.min_hold
        if budget > 0.5 and total > budget:
            f = max(_COMPRESS_FLOOR, budget / total)
            timeline = [TimedAction(t.action, t.start * f, t.duration * f) for t in timeline]
    return timeline


def animation_end(timeline: list[TimedAction]) -> float:
    return max((t.end for t in timeline), default=0.0)
