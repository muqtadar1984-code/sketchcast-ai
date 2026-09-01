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
        # nothing to write — but the board is never EMPTY any more: the
        # persistent teacher still stands there. Returning None here once
        # dropped a contentless segment to the legacy renderer, failing the
        # whole lesson's visual-language validation and blinking the teacher
        # out for one segment.
        elements, actions = [], []
        _add_teacher(segment, elements, actions)
        return {"id": f"wb_{segment.get('segment_id', 'seg')}",
                "compiled": True, "scene_type": "generic",
                "narration": segment.get("text") or "",
                "elements": elements, "actions": actions}

    elements: list[dict] = []
    actions: list[dict] = []
    big = seg_type in ("hook", "question_hook", "preview") and not points
    if heading:
        # cards are sparse by design — a centred heading owns the space
        # (the old top-left placement read as a lost caption on an empty
        # board, per founder screenshot)
        elements.append({"id": "wb_h", "type": "text", "text": heading,
                         "role": "title", "size": 48 if big else 42,
                         "at": [WORLD_W / 2, 250 if big else 90],
                         "anchor": "mt"})
        actions.append({"verb": "write", "target": "wb_h"})
        actions.append({"verb": "underline", "target": "wb_h",
                        "at": {"frac": 0.28 if big else 0.9}})
    for i, p in enumerate(points):
        pid = f"wb_p{i}"
        # a short hand-drawn dash bullets each point — drawn, not templated
        elements.append({"id": f"wb_d{i}", "type": "shape", "shape": "line",
                         "width": 4.0, "color": "accent",
                         "points": [[186, 268 + 96 * i], [218, 262 + 96 * i]]})
        elements.append({"id": pid, "type": "text", "text": p, "size": 27,
                         "role": "caption", "at": [240, 240 + 96 * i],
                         "anchor": "lt"})
        actions.append({"verb": "draw", "target": f"wb_d{i}",
                        "at": {"frac": min(0.85, 0.18 + 0.22 * i)}})
        actions.append({"verb": "write", "target": pid})

    _add_teacher(segment, elements, actions)
    return {"id": f"wb_{segment.get('segment_id', 'seg')}", "compiled": True,
            "scene_type": "generic",
            "narration": segment.get("text") or "",
            "elements": elements, "actions": actions}


def _add_teacher(segment: dict, elements: list[dict],
                 actions: list[dict]) -> None:
    """The persistent teacher joins every whiteboard card. On the LESSON'S
    FIRST segment the hand draws them in (the founder's 'avatar at the
    start'); everywhere else they are simply already there."""
    elements.append(teacher_element())
    if str(segment.get("segment_id") or "") == "s001":
        actions.insert(0, {"verb": "draw", "target": TEACHER_ID,
                           "at": {"sec": 0.3}})


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
    _add_teacher(segment, elements, actions)
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

_AV_AT = (178.0, 568.0)      # lower-LEFT: the persistent teacher owns the
_AV_SCALE = 0.38             # lower-right corner now
_BUBBLE_AT = (445.0, 285.0)
_BUBBLE_TAIL_TO = (255.0, 462.0)

# ── the persistent teacher ───────────────────────────────────────────────────
# One teacher, present from the first frame to the last, tucked into the
# bottom-right corner where lesson content never reaches. Speech bubbles are
# drawn beside them whenever a step carries a key_point; the bubble fades,
# the teacher STAYS.
TEACHER_ID = "__teach_av"
# sized/placed so the FULL figure sits inside the safe area at the real
# avatar aspect (~1.1 h/w) — the first placement cropped the lower third
# under the player chrome
TEACHER_AT = (1172.0, 582.0)
TEACHER_SCALE = 0.22          # small enough to never crowd the board
_TEACH_BUBBLE_AT = (975.0, 440.0)
_TEACH_BUBBLE_TAIL = (1120.0, 528.0)


def teacher_element() -> dict:
    return {"id": TEACHER_ID, "type": "illustration",
            "asset": "avatar_teacher", "at": list(TEACHER_AT),
            "scale": TEACHER_SCALE}


def key_point_choreo(text: str, uid: str) -> tuple[list[dict], list[dict]]:
    """Elements + actions for one teacher key point: the hand draws a bubble
    beside the ever-present teacher, writes the SHORT verbatim line, the
    teacher 'speaks' (pulse), then the bubble fades — the teacher remains.
    The bubble draw is cued to the line's own words in the narration, so it
    lands exactly when the statement is spoken."""
    text = _short(text, 60)
    bub_id = f"__kp_{uid}"
    grp_id = f"__kp_{uid}_grp"
    elements = bubble_elements(bub_id, text, _TEACH_BUBBLE_AT,
                               _TEACH_BUBBLE_TAIL)
    elements.append({"id": grp_id, "type": "group",
                     "children": [bub_id, f"{bub_id}_tail", f"{bub_id}_txt"]})
    actions = [
        {"verb": "draw", "target": bub_id, "at": {"phrase": text,
                                                  "offset": -0.8}},
        {"verb": "draw", "target": f"{bub_id}_tail"},
        {"verb": "write", "target": f"{bub_id}_txt"},
        {"verb": "pulse", "target": TEACHER_ID, "times": 2, "duration": 1.2},
        {"verb": "fade", "target": grp_id, "to": 0.0, "duration": 0.6,
         "at": {"frac": 0.9}},
    ]
    return elements, actions


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
