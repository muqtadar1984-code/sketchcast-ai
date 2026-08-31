"""Whiteboard-native fallbacks — ONE visual language, no exceptions.

Segments outside the visual plan (hooks, intros, quizzes, recaps, previews)
used to fall through to the legacy slide renderer — the whiteboard world
vanished into a teal slide for a beat and came back. That style switch is the
exact thing the product forbids: when the engine cannot draw something rich,
it simplifies WITHIN the whiteboard language — a handwritten heading, a few
short handwritten points, an underline — never into another visual system.

Builders here turn a plain ScriptSegment (dict) into a deterministic
whiteboard Scene: no AI assets, no plan required, same hand, same Caveat
lettering, same canvas. Also home of the speech-bubble geometry shared with
avatar teaching moments.
"""

from __future__ import annotations

from .schema import WORLD_H, WORLD_W

_MAX_POINTS = 3
_MAX_POINT_CHARS = 64


def _short(text: str, cap: int = _MAX_POINT_CHARS) -> str:
    t = " ".join(str(text).split())
    return t if len(t) <= cap else t[:cap - 1].rstrip() + "…"


def build_whiteboard_scene(segment: dict) -> dict | None:
    """A sceneless segment as a whiteboard moment. Returns None only when the
    segment has literally nothing to show (then a silent board beats noise)."""
    heading = _short(segment.get("slide_heading") or "", 60)
    visual = segment.get("slide_visual") or {}
    kind = visual.get("kind") if isinstance(visual, dict) else None
    seg_type = str(segment.get("type") or "explore")

    if kind == "quiz" and isinstance(visual, dict):
        return _quiz_scene(segment, heading, visual)

    points: list[str] = []
    if kind == "takeaways" and isinstance(visual, dict):
        points = [_short(n) for n in (visual.get("nodes") or [])][:4]
    if not points:
        points = [_short(p) for p in (segment.get("slide_points") or [])][:_MAX_POINTS]
    if not points and isinstance(visual, dict):
        body = visual.get("body") or visual.get("caption")
        if body:
            points = [_short(body, 90)]

    if not heading and not points:
        return None

    elements: list[dict] = []
    actions: list[dict] = []
    big = seg_type in ("hook", "question_hook", "preview") and not points
    if heading:
        elements.append({"id": "wb_h", "type": "text", "text": heading,
                         "role": "title", "size": 46 if big else 40,
                         "at": [WORLD_W / 2, 250] if big else [80, 64],
                         "anchor": "mt" if big else "lt"})
        actions.append({"verb": "write", "target": "wb_h"})
        actions.append({"verb": "underline", "target": "wb_h",
                        "at": {"frac": 0.28 if big else 0.9}})
    for i, p in enumerate(points):
        pid = f"wb_p{i}"
        # a short hand-drawn dash bullets each point — drawn, not templated
        elements.append({"id": f"wb_d{i}", "type": "shape", "shape": "line",
                         "width": 4.0, "color": "accent",
                         "points": [[96, 232 + 96 * i], [128, 226 + 96 * i]]})
        elements.append({"id": pid, "type": "text", "text": p, "size": 27,
                         "role": "caption", "at": [150, 204 + 96 * i],
                         "anchor": "lt"})
        actions.append({"verb": "draw", "target": f"wb_d{i}",
                        "at": {"frac": min(0.85, 0.18 + 0.22 * i)}})
        actions.append({"verb": "write", "target": pid})

    return {"id": f"wb_{segment.get('segment_id', 'seg')}", "compiled": True,
            "scene_type": "generic",
            "narration": segment.get("text") or "",
            "elements": elements, "actions": actions}


def _quiz_scene(segment: dict, heading: str, visual: dict) -> dict:
    """The quiz, handwritten: question + lettered options. No answer reveal —
    the player pauses here and the student thinks."""
    question = _short(str(visual.get("caption") or heading or
                          segment.get("slide_heading") or "Quick check"), 90)
    options = [_short(o, 48) for o in (visual.get("options") or [])][:4]
    elements: list[dict] = [
        {"id": "wb_q", "type": "text", "text": question, "role": "title",
         "size": 34, "at": [80, 70], "anchor": "lt"},
    ]
    actions: list[dict] = [{"verb": "write", "target": "wb_q"}]
    letters = "ABCD"
    for i, opt in enumerate(options):
        oid = f"wb_o{i}"
        elements.append({"id": oid, "type": "text",
                         "text": f"{letters[i]}.  {opt}", "size": 28,
                         "at": [140, 220 + 92 * i], "anchor": "lt"})
        actions.append({"verb": "write", "target": oid,
                        "at": {"frac": min(0.9, 0.25 + 0.18 * i)}})
    return {"id": f"wb_{segment.get('segment_id', 'quiz')}", "compiled": True,
            "scene_type": "generic",
            "narration": segment.get("text") or "",
            "elements": elements, "actions": actions}


# ── speech bubbles (shared with avatar teaching moments) ─────────────────────

def bubble_elements(bubble_id: str, text: str, at: tuple[float, float],
                    tail_to: tuple[float, float]) -> list[dict]:
    """A hand-drawn speech bubble: an organic outline with a tail toward the
    speaker, plus the SHORT handwritten line inside. Same primitives as
    everything else on the board — the hand draws it, the hand writes it."""
    text = _short(text, 60)
    w = max(180.0, min(560.0, 26 + len(text) * 12.5))
    h = 96.0
    cx, cy = at
    x0, y0 = cx - w / 2, cy - h / 2
    # bubble outline as a closed rounded path + a two-stroke tail
    outline = [
        [x0 + 18, y0], [x0 + w - 18, y0], [x0 + w, y0 + 18],
        [x0 + w, y0 + h - 18], [x0 + w - 18, y0 + h],
        [x0 + 40, y0 + h], [x0 + 18, y0 + h],
        [x0, y0 + h - 18], [x0, y0 + 18], [x0 + 18, y0],
    ]
    tail_base_x = x0 + w * (0.75 if tail_to[0] > cx else 0.25)
    tail = [[tail_base_x - 14, y0 + h], list(tail_to), [tail_base_x + 16, y0 + h]]
    return [
        {"id": bubble_id, "type": "shape", "shape": "path", "points": outline,
         "closed": True, "width": 3.4},
        {"id": f"{bubble_id}_tail", "type": "shape", "shape": "path",
         "points": tail, "width": 3.4},
        {"id": f"{bubble_id}_txt", "type": "text", "text": text, "size": 26,
         "at": [cx, cy], "anchor": "mm"},
    ]


def bubble_actions(bubble_id: str, start_frac: float = 0.12) -> list[dict]:
    return [
        {"verb": "draw", "target": bubble_id, "at": {"frac": start_frac}},
        {"verb": "draw", "target": f"{bubble_id}_tail"},
        {"verb": "write", "target": f"{bubble_id}_txt"},
    ]


# ── avatar teaching moments (§12-18) ─────────────────────────────────────────

# Hand-drawn characters matching the board's line language — generated once,
# cached forever, reused across every lesson. The hardened no-text suffix in
# raster_assets applies on top of these.
AVATAR_PROMPTS = {
    "avatar_student": (
        "A friendly school student character, waist-up, simple hand-drawn "
        "cartoon in clean black ink line art, looking up curiously with one "
        "hand slightly raised as if asking a question, cheerful expression."),
    "avatar_teacher": (
        "A friendly teacher character with glasses, waist-up, simple "
        "hand-drawn cartoon in clean black ink line art, one hand raised in "
        "an explaining gesture, warm confident expression."),
}

_AV_AT = (1105.0, 520.0)     # lower-right, off the main drawing area
_AV_SCALE = 0.42             # ~300 world px wide at nominal asset width
_BUBBLE_AT = (880.0, 200.0)
_BUBBLE_TAIL_TO = (1040.0, 380.0)


def human_moment(role: str, text: str, uid: str = "hm") -> tuple[list[dict], list[dict], str]:
    """Elements + actions for one HUMAN_TEACHING_MOMENT, plus the avatar asset
    key. The choreography is the spec's: avatar appears -> hand draws the
    bubble -> hand writes the SHORT line -> avatar 'speaks' (subtle pulse) ->
    everything fades and the board takes the focus back. All scoped to ONE
    segment — moments never persist onto the board."""
    role = role if role in ("student", "teacher") else "student"
    asset = f"avatar_{role}"
    av_id = f"__{uid}_av"
    bub_id = f"__{uid}_bub"
    grp_id = f"__{uid}_grp"
    elements = [{"id": av_id, "type": "illustration", "asset": asset,
                 "at": list(_AV_AT), "scale": _AV_SCALE}]
    elements += bubble_elements(bub_id, text, _BUBBLE_AT, _BUBBLE_TAIL_TO)
    elements.append({"id": grp_id, "type": "group",
                     "children": [av_id, bub_id, f"{bub_id}_tail",
                                  f"{bub_id}_txt"]})
    actions = [{"verb": "reveal", "target": av_id, "at": {"frac": 0.06},
                "duration": 0.5}]
    actions += bubble_actions(bub_id, start_frac=0.16)
    actions += [
        {"verb": "pulse", "target": av_id, "times": 3, "duration": 1.6},
        {"verb": "fade", "target": grp_id, "to": 0.0, "duration": 0.6,
         "at": {"frac": 0.86}},
    ]
    return elements, actions, asset
