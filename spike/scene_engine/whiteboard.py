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

from .schema import WORLD_W

_MAX_POINTS = 3
_MAX_POINT_CHARS = 64


def _short(text: str, cap: int = _MAX_POINT_CHARS) -> str:
    # ASCII ellipsis on purpose: the handwriting font has no U+2026 glyph and
    # one such char used to drop the whole line to a fallback typeface
    t = " ".join(str(text).split())
    return t if len(t) <= cap else t[:cap - 3].rstrip() + "..."


def build_whiteboard_scene(segment: dict,
                           avatars: dict | None = None) -> dict | None:
    """A sceneless segment as a whiteboard moment. `avatars` casts the
    persistent teacher (and, when the segment carries dialogue, the student).
    Never returns None any more — the teacher + narration stream make even a
    contentless segment a real card."""
    heading = _short(segment.get("slide_heading") or "", 60)
    visual = segment.get("slide_visual") or {}
    kind = visual.get("kind") if isinstance(visual, dict) else None
    seg_type = str(segment.get("type") or "explore")

    if kind == "quiz" and isinstance(visual, dict):
        return _quiz_scene(segment, heading, visual, avatars)

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
        _add_teacher(segment, elements, actions, avatars)
        if seg_type != "question_hook":
            _add_stream(segment, elements, actions)
        scene = {"id": f"wb_{segment.get('segment_id', 'seg')}",
                 "compiled": True, "scene_type": "generic",
                 "narration": segment.get("text") or "",
                 "elements": elements, "actions": actions}
        _add_sketches(segment, scene, heading_taken=False)
        return scene

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

    _add_teacher(segment, elements, actions, avatars)
    if seg_type != "question_hook":
        # the narration streams on card segments too — every sentence, in
        # the teacher's bubble, as it is spoken
        _add_stream(segment, elements, actions)
    scene = {"id": f"wb_{segment.get('segment_id', 'seg')}", "compiled": True,
             "scene_type": "generic",
             "narration": segment.get("text") or "",
             "elements": elements, "actions": actions}
    _add_sketches(segment, scene, heading_taken=bool(heading or points))
    if heading:
        # The underline is appended LAST, and on a card with points it is
        # NOT cued: it simply follows the last bullet. It used to be cued at
        # 90% of the audio and appended BEFORE the bullets — and the timeline
        # never starts a cued action before the previous board action
        # started, so every bullet cue (18% / 40% / 62%) was dragged to 90%:
        # the card sat idle while the narration ran, then everything landed
        # at once past the end of the audio. Measured on the founder's first
        # Google-voiced lesson (2026-09-04): "narration ahead of the
        # animation" on every card segment, planned scenes in sync. Uncued,
        # it also compresses with the board on a short card instead of
        # anchoring a silent tail after the audio. A big card (no points)
        # keeps its early cue — nothing precedes it but the heading.
        underline = {"verb": "underline", "target": "wb_h"}
        if big:
            underline["at"] = {"frac": 0.28}
        scene["actions"].append(underline)
    return scene


def _add_sketches(segment: dict, scene: dict, heading_taken: bool) -> None:
    """A board with NOTHING written on it gets the things its narration names,
    sketched as they are spoken — an empty board while the voice describes a
    tree and an ant is the medium wasted.

    An EMPTY board only. A card that has already written a heading or points
    has no room left, and a sketch cannot be made to fit one: an auto-sketch is
    sized by WIDTH ALONE (raster_assets sets world_scale = NOMINAL_WORLD_W /
    ink.width), so its rendered HEIGHT is whatever the asset's aspect ratio
    makes it — and that is unknowable here, because this runs per segment just
    before render and the asset may not be generated yet. Measured: sk_plant
    (488x729 ink) at the one-slot scale 0.62 binds to 434x648, and sk_person
    (285x746) at 0.5 binds to 350x916 — taller than the whole 720 canvas. Both
    landed straight over the card's own bullets. The old `heading_taken` nudge
    moved the CENTRE down 70px, which can never clear a 648px-tall drawing off
    text starting at y=240 — and nothing reported it, because the overlap
    audit measures TEXT against TEXT.
    """
    if heading_taken:
        return
    if any(e.get("type") == "illustration" and not _is_avatar(e.get("id"))
           for e in scene["elements"]):
        return
    els, acts, assets = sketch_elements(
        segment.get("text") or "", uid=str(segment.get("segment_id", "seg")),
        limit=2)
    if not els:
        return
    scene["elements"].extend(els)
    scene["actions"] = scene["actions"] + acts
    scene["scene_assets"] = {**(scene.get("scene_assets") or {}), **assets}


def _is_avatar(eid) -> bool:
    return str(eid) in (TEACHER_ID, STUDENT_ID)


# where auto-sketches sit: the open board left of the teacher's panel. One
# sketch owns the middle; two share the space side by side.
_SKETCH_SLOTS = {1: [(470.0, 360.0, 0.62)],
                 2: [(330.0, 360.0, 0.5), (650.0, 360.0, 0.5)]}

# Slots for a board that ALREADY holds a diagram. The centre is spoken for, so
# these tuck into the two top corners, the only regions free on every chapter
# of the measured lesson. Derived, not eyeballed: raster_assets.fit_scale
# bounds an illustration to 700x520 world px, so a sketch at scale s occupies
# +/- (350s, 260s) about its point. Against that:
#   root visual  at (600, 380) scale 1.0 -> x 250..950,  y 120..640
#   caption panel  teacher cx 970 / student cx 310, cy 360, half 223x62
#                              -> x 755..1185 / 95..525, y 298..422
#   avatars      at y 556
# top-right (1114, 142, 0.40) -> x 974..1254, y 38..246
# top-left  (136,  142, 0.32) -> x  24..248,  y 59..225
# Both clear every caption panel and avatar vertically by more than 50px.
#
# Top-right is tried FIRST because continuity._RECAP_AT parks a carried-over
# picture in the top-left one. That is a preference, not an assumption: the
# slot is chosen by MEASURING against the diagrams actually on the board, so a
# recap sitting in the left corner simply fails the overlap test.
_MARGIN_SLOTS = [(1114.0, 142.0, 0.40), (136.0, 142.0, 0.32)]

# An illustration occupies +/- (350*scale, 260*scale) about its point, because
# raster_assets.fit_scale bounds every asset into 700x520 world px whatever
# its aspect ratio.
_ILL_HALF_W, _ILL_HALF_H = 350.0, 260.0


def illustration_box(at, scale: float = 1.0) -> tuple:
    return (at[0] - _ILL_HALF_W * scale, at[1] - _ILL_HALF_H * scale,
            at[0] + _ILL_HALF_W * scale, at[1] + _ILL_HALF_H * scale)


def _overlaps(a: tuple, b: tuple) -> bool:
    return not (a[2] <= b[0] or a[0] >= b[2] or a[3] <= b[1] or a[1] >= b[3])


def free_margin_slots(occupied: list) -> list:
    """The margin slots that clear every box in `occupied`.

    Deriving the slot from a constant root position was wrong: the semantic
    path always places at (600, 380), but a hand-authored plan puts its
    diagram wherever it likes, and a caller measuring against the assumed
    position put a sketch 16px into a real one. Measure the board.
    """
    return [s for s in _MARGIN_SLOTS
            if not any(_overlaps(illustration_box((s[0], s[1]), s[2]), b)
                       for b in occupied)]


def sketch_elements(narration: str, uid: str, exclude: set[str] | None = None,
                    limit: int = 2,
                    slots: list | None = None) -> tuple[list[dict], list[dict], dict]:
    """Hand-drawn sketches of the concrete things this narration NAMES —
    elements, draw actions cued to the words, and their asset prompts.
    Empty when the narration names nothing sketchable.

    `slots` overrides the placement: pass _MARGIN_SLOTS for a board that
    already carries a diagram, so the sketch tucks into a corner instead of
    landing on the picture the lesson is about."""
    from .sketchables import find_sketchables

    found = find_sketchables(narration, limit=limit, exclude=exclude)
    if not found:
        return [], [], {}
    slots = slots or _SKETCH_SLOTS[min(len(found), 2)]
    els: list[dict] = []
    acts: list[dict] = []
    assets: dict[str, str] = {}
    for i, f in enumerate(found[:len(slots)]):
        x, y, scale = slots[i]
        eid = f"sk_{uid}_{i}"
        # hud: a corner sketch is SCREEN-fixed like the avatars and captions.
        # Placed in world space it was carried off-canvas by every planned
        # zoom — a potted plant drawn with its leaves past the top-left edge
        # (founder, 2026-09-04) sat inside the canvas in world coordinates and
        # only the camera cut it.
        els.append({"id": eid, "type": "illustration", "asset": f["key"],
                    "at": [x, y], "scale": scale, "hud": True})
        acts.append({"verb": "draw", "target": eid,
                     "at": {"phrase": f["cue"], "offset": -0.35}})
        assets[f["key"]] = f["prompt"]
    return els, acts, assets


def _add_stream(segment: dict, elements: list[dict],
                actions: list[dict]) -> None:
    """The full narration streams on cards too — unless the segment carries
    DIALOGUE, which the composer injects later with real audio offsets."""
    if segment.get("dialogue"):
        return
    narr = segment.get("text") or ""
    bold = {s for s in [select_key_sentence(narr)] if s}
    uid = f"c_{segment.get('segment_id', 'seg')}"
    nb_els, nb_acts = narration_stream(narr, uid=uid, bold=bold)
    elements.extend(nb_els)
    actions.extend(nb_acts)


def _add_teacher(segment: dict, elements: list[dict], actions: list[dict],
                 avatars: dict | None = None) -> None:
    """The persistent teacher joins every whiteboard card — simply THERE
    from the first frame (founder: no draw-in animation). Dialogue segments
    seat the student too."""
    elements.append(teacher_element(
        (avatars or {}).get("teacher", "avatar_teacher")))
    if segment.get("dialogue"):
        elements.append(student_element(
            (avatars or {}).get("student", "avatar_student")))


def _quiz_scene(segment: dict, heading: str, visual: dict,
                avatars: dict | None = None) -> dict:
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
    _add_teacher(segment, elements, actions, avatars)
    return {"id": f"wb_{segment.get('segment_id', 'quiz')}", "compiled": True,
            "scene_type": "generic",
            "narration": segment.get("text") or "",
            "elements": elements, "actions": actions}


# ── narration key statements ─────────────────────────────────────────────────
# The founder's rule: a speech bubble must say WHAT THE NARRATION IS SAYING,
# when it is being said — anything else is a disconnect. So bubble text is
# always a VERBATIM narration sentence: model-planned key_points get snapped
# to the closest actual sentence, and segments the model didn't plan get one
# selected by IMPORTANCE (definition patterns, causal links), not by count.

import re as _re

_DEF_PAT = _re.compile(
    r"\b(is|are|means|meaning|called|controls?|provides?|gives?|stores?|"
    r"makes?|releases?|protects?|allows?|contains?|holds?|surrounds?|"
    r"keeps?|lets?|acts?|works?|needs?)\b", _re.I)
_EMPH_PAT = _re.compile(
    r"\b(remember|notice|important|key|every|each|all|main|basic|"
    r"because|so that|which means|this is why)\b", _re.I)
_META_PAT = _re.compile(r"tap continue|anything you'd want|pause here", _re.I)


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _re.split(r"(?<=[.!?])\s+", text or "")
            if s.strip()]


def sentence_importance(s: str) -> int:
    """0 = not bubble-worthy. Meta lines belong to nobody; definitions,
    causal statements AND the hook questions that open a lesson are what a
    teacher voices (the founder wants the openings spoken too)."""
    words = s.split()
    if not (4 <= len(words) <= 20) or _META_PAT.search(s):
        return 0
    score = 0
    if _DEF_PAT.search(s):
        score += 2
    if _EMPH_PAT.search(s):
        score += 1
    if s.endswith("?"):
        score += 1                     # a good hook question earns its bubble
    if 5 <= len(words) <= 14:
        score += 1
    return score


def select_key_sentence(text: str, min_score: int = 3) -> str | None:
    best, best_score = None, min_score - 1
    for s in split_sentences(text):
        sc = sentence_importance(s)
        if sc > best_score:
            best, best_score = s, sc
    return best


def snap_to_narration(candidate: str, narration: str) -> str | None:
    """The narration sentence the candidate is (a paraphrase of): exact
    substring first, then best token overlap. None when nothing is close —
    the caller then either selects by importance or drops the bubble."""
    if not candidate or not narration:
        return None
    cand = " ".join(candidate.split()).rstrip(".!…").lower()
    if cand:
        # a substring hit expands to the WHOLE containing sentence — the
        # bubble should show the narration's statement, not a fragment of it
        for s in split_sentences(narration):
            if cand in s.lower():
                return s
    ct = set(_re.findall(r"[a-z']+", cand))
    if not ct:
        return None
    best, best_j = None, 0.44          # below ~0.45 overlap it's a different claim
    for s in split_sentences(narration):
        stoks = set(_re.findall(r"[a-z']+", s.lower()))
        if not stoks:
            continue
        j = len(ct & stoks) / len(ct | stoks)
        if j > best_j:
            best, best_j = s, j
    return best


# ── speech bubbles (shared with avatar teaching moments) ─────────────────────

# Bubble footprint. The founder's report: bubbles "take up a lot of space on
# the whiteboard and in some instances hide the image being drawn underneath".
# A bubble is speech — it must be readable, but it is not the lesson. The
# previous 24px text on a 600px-wide, 132px-tall panel could cover a third of
# the board; the drawing underneath is the thing the child came for.
#
# Reduced together, because these four numbers are one decision: shrinking the
# font without narrowing the wrap just makes a wide box of small text, and
# narrowing the wrap without capping lines makes a tall one.
BUBBLE_SIZE = 19               # was 24
_BUBBLE_CAP = 120              # was 150
_LINE_CHARS = 34               # was 40
_BUBBLE_MAX_W = 430.0          # was 600
_BUBBLE_LINE_H = 32            # was 42
_BUBBLE_H1 = 74.0              # was 92
_BUBBLE_H = 112.0              # was 132


def _bubble_lines(text: str) -> list[str]:
    """Greedy wrap at _LINE_CHARS, at most three lines — the remainder is
    ellipsized, never dumped into one over-wide line (a 70-char third line
    once rendered outside its bubble and tripped the safe-area clamp)."""
    words = _short(text, _BUBBLE_CAP).split()
    lines, cur = [], ""
    for i, w in enumerate(words):
        if cur and len(cur) + 1 + len(w) > _LINE_CHARS:
            lines.append(cur)
            cur = w
            if len(lines) == 3:
                break
        else:
            cur = f"{cur} {w}".strip()
    if len(lines) == 3:
        lines[2] = _short(lines[2] + " ...", _LINE_CHARS + 4)
        return lines
    if cur:
        lines.append(cur)
    return lines


def bubble_elements(bubble_id: str, text: str, at: tuple[float, float],
                    tail_to: tuple[float, float]) -> list[dict]:
    """A speech bubble that APPEARS (it is speech, not a drawing — the hand
    never draws or letters it): rounded outline + tail toward the speaker +
    up to two lines of the VERBATIM narration text."""
    lines = _bubble_lines(text)
    longest = max(len(l) for l in lines)
    # 9.4 px/char tracks Caveat at BUBBLE_SIZE the way 11.8 tracked it at 24
    w = max(170.0, min(_BUBBLE_MAX_W, 26 + longest * 9.4))
    cx, cy = at
    # never past the canvas edges — a wide line at the teacher-bubble
    # position once touched the right border
    w = min(w, 2 * min(cx - 24.0, (WORLD_W - 24.0) - cx))
    h = _BUBBLE_H1 if len(lines) == 1 else _BUBBLE_H
    x0, y0 = cx - w / 2, cy - h / 2
    outline = [
        [x0 + 18, y0], [x0 + w - 18, y0], [x0 + w, y0 + 18],
        [x0 + w, y0 + h - 18], [x0 + w - 18, y0 + h],
        [x0 + 40, y0 + h], [x0 + 18, y0 + h],
        [x0, y0 + h - 18], [x0, y0 + 18], [x0 + 18, y0],
    ]
    tail_base_x = x0 + w * (0.75 if tail_to[0] > cx else 0.25)
    tail = [[tail_base_x - 14, y0 + h], list(tail_to), [tail_base_x + 16, y0 + h]]
    els = [
        {"id": bubble_id, "type": "shape", "shape": "path", "points": outline,
         "closed": True, "width": 3.4, "fill": "paper"},
        {"id": f"{bubble_id}_tail", "type": "shape", "shape": "path",
         "points": tail, "width": 3.4},
    ]
    for i, line in enumerate(lines):
        # centred on cy: the old fixed offsets put a third line at cy+63 in a
        # box half-height 66, i.e. hard against the outline
        ly = cy + (i - (len(lines) - 1) / 2.0) * _BUBBLE_LINE_H
        els.append({"id": f"{bubble_id}_txt{i or ''}", "type": "text",
                    "text": line, "size": BUBBLE_SIZE, "at": [cx, ly],
                    "anchor": "mm"})
    return els


def bubble_cue_phrase(text: str, narration: str | None = None) -> str:
    """The shortest sentence prefix that is UNIQUE within the narration —
    two sentences sharing an opening once collapsed both captions onto the
    first occurrence (cues resolve to the first match)."""
    words = text.split()
    n = 5
    phrase = " ".join(words[:n])
    if narration:
        hay = narration.lower()
        while hay.count(phrase.lower()) > 1 and n < min(12, len(words)):
            n += 1
            phrase = " ".join(words[:n])
    return phrase


def speech_secs(text: str) -> float:
    """Rough speaking time of the line, for scheduling the fade-out."""
    return min(5.0, 0.42 * len(text.split()) + 1.4)


# ── avatar teaching moments (§12-18) ─────────────────────────────────────────

# Hand-drawn characters matching the board's line language — generated once,
# cached forever, reused across every lesson. The hardened no-text suffix in
# raster_assets applies on top of these.
#
# The roster is a MATRIX, not a pair: the teacher matches the narration
# VOICE (male/female), and the student matches the learner's GRADE BAND.
# Bands beyond school (undergrad/grad/doctorate) are maintained for the
# future even though they are out of scope today.
_STYLE = ("friendly approachable cartoon character, waist-up, consistent "
          "storybook character style")
AVATAR_PROMPTS = {
    # teachers, by voice gender
    "avatar_teacher": (          # legacy key == male teacher
        "A friendly male teacher character with glasses, dark short hair, "
        "waist-up, one hand raised in an explaining gesture, warm confident "
        "expression."),
    "avatar_teacher_female": (
        f"A friendly female teacher character with shoulder-length hair and "
        f"glasses, {_STYLE}, one hand raised in an explaining gesture, warm "
        f"confident expression."),
    # students: grade band x gender (the engine picks a gender per lesson,
    # deterministically seeded so retries render identically)
    "avatar_student": (          # legacy key == grades 5-7 boy
        "A friendly schoolboy around 11 years old with short brown hair and "
        "a backpack, waist-up, looking up curiously with one hand slightly "
        "raised as if asking a question, cheerful expression."),
    "avatar_student_5_7_f": (
        f"A friendly schoolgirl around 11 years old with a ponytail and a "
        f"backpack, {_STYLE}, looking up curiously with one hand slightly "
        f"raised as if asking a question, cheerful expression."),
    "avatar_student_8_10_m": (
        f"A curious teenage schoolboy around 14 years old, {_STYLE}, casual "
        f"shirt, one hand slightly raised as if asking a question, engaged "
        f"expression."),
    "avatar_student_8_10_f": (
        f"A curious teenage schoolgirl around 14 years old with "
        f"shoulder-length hair, {_STYLE}, casual shirt, one hand slightly "
        f"raised as if asking a question, engaged expression."),
    "avatar_student_11_12_m": (
        f"A male senior high-school student around 17 years old, {_STYLE}, "
        f"holding a notebook in one arm, other hand slightly raised as if "
        f"asking a question, thoughtful expression."),
    "avatar_student_11_12_f": (
        f"A female senior high-school student around 17 years old, "
        f"{_STYLE}, holding a notebook in one arm, other hand slightly "
        f"raised as if asking a question, thoughtful expression."),
    "avatar_student_undergrad_m": (
        f"A male university undergraduate around 20 years old, {_STYLE}, "
        f"casual hoodie, one hand slightly raised as if asking a question, "
        f"curious expression."),
    "avatar_student_undergrad_f": (
        f"A female university undergraduate around 20 years old, {_STYLE}, "
        f"casual cardigan, one hand slightly raised as if asking a "
        f"question, curious expression."),
    "avatar_student_grad_m": (
        f"A male graduate student around 25 years old, {_STYLE}, "
        f"smart-casual shirt, one hand slightly raised as if making a "
        f"point, focused expression."),
    "avatar_student_grad_f": (
        f"A female graduate student around 25 years old, {_STYLE}, "
        f"smart-casual blouse, one hand slightly raised as if making a "
        f"point, focused expression."),
    "avatar_student_doctorate_m": (
        f"A male doctoral researcher around 28 years old wearing a lab "
        f"coat, {_STYLE}, one hand slightly raised as if making a point, "
        f"sharp attentive expression."),
    "avatar_student_doctorate_f": (
        f"A female doctoral researcher around 28 years old wearing a lab "
        f"coat, {_STYLE}, one hand slightly raised as if making a point, "
        f"sharp attentive expression."),
}

# Edge voice-name fragments that identify a FEMALE voice; anything else maps
# to the male teacher. Extend as the product's voice registry grows.
_FEMALE_VOICE_HINTS = ("aria", "jenny", "emma", "ana", "ava", "michelle",
                       "sonia", "libby", "maisie", "natasha", "clara",
                       "female", "neerja", "swara", "aisha", "salma")


def teacher_avatar_for_voice(voice_id: str | None) -> str:
    """The teacher avatar matching a voice. The registry's `gender` field is
    the source of truth; the name-fragment list below is only a fallback for
    ids the registry does not know (raw provider refs in dev scripts).

    Measured before the field existed: eight female voices — every non-English
    default and Rachel — were absent from the fragment list and cast the male
    teacher. Pass the RESOLVED voice (after the tier gate), not the requested
    one, or a downgraded premium pick casts the wrong avatar over Aria."""
    try:
        from shared.tts.registry import get_voice
        v = get_voice(voice_id)
        if v is not None:
            return "avatar_teacher_female" if v.gender == "f" else "avatar_teacher"
    except Exception:  # noqa: BLE001 — casting must never fail a render
        pass
    s = (voice_id or "").lower()
    if any(h in s for h in _FEMALE_VOICE_HINTS):
        return "avatar_teacher_female"
    return "avatar_teacher"


def student_avatar_for_grade(grade, seed: str = "") -> str:
    """Grade band + a deterministic per-lesson gender pick -> student avatar
    key. `seed` (e.g. the script/lesson id) decides male vs female so the
    system varies between lessons but a RETRY of the same lesson renders the
    identical character. Unknown grades keep the youngest — wrong-young is
    friendlier than wrong-old."""
    import zlib
    sex = "f" if zlib.crc32(str(seed).encode("utf-8")) & 1 else "m"
    try:
        g = int(str(grade).strip().split()[-1])
    except (ValueError, IndexError, TypeError):
        g = 6
    if g <= 7:
        return "avatar_student" if sex == "m" else "avatar_student_5_7_f"
    if g <= 10:
        band = "8_10"
    elif g <= 12:
        band = "11_12"
    elif g <= 16:
        band = "undergrad"
    elif g <= 18:
        band = "grad"
    else:
        band = "doctorate"
    return f"avatar_student_{band}_{sex}"

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
# founder-approved size/placement from the stream preview: bigger presence,
# bubble panel fully clear of the face
TEACHER_AT = (1150.0, 556.0)
TEACHER_SCALE = 0.30
_TEACH_BUBBLE_AT = (970.0, 360.0)
_TEACH_BUBBLE_TAIL = (1105.0, 462.0)


def teacher_element(asset: str = "avatar_teacher") -> dict:
    return {"id": TEACHER_ID, "type": "illustration", "asset": asset,
            "at": list(TEACHER_AT), "scale": TEACHER_SCALE}


# panel geometry for the running narration stream (founder-approved preview):
# a fixed-width bubble above each speaker; one sentence at a time, replaced
# exactly when the voice moves on. Ids carry timing.CAPTION_PREFIX so the
# renderer treats them as a parallel speech track (no teaching-order clamp,
# no compression).
_STREAM_W = 470.0
_STREAM_PANEL = {
    "teacher": {"cx": 970.0, "cy": 360.0, "tail": (1105.0, 462.0),
                "ftail": 0.78},
    "student": {"cx": 310.0, "cy": 360.0, "tail": (175.0, 462.0),
                "ftail": 0.22},
}
STUDENT_ID = "__stud_av"
STUDENT_AT = (130.0, 556.0)
AVATAR_SCALE = 0.30


def _stream_bubble(uid: str, who: str, text: str, key: bool) -> list[dict]:
    g = _STREAM_PANEL["student" if who == "student" else "teacher"]
    cx, cy = g["cx"], g["cy"]
    lines = _bubble_lines(text)
    longest = max(len(l) for l in lines)
    w = max(390.0, min(560.0, 40 + longest * 11.8))
    w = min(w, 2 * min(cx - 24.0, (WORLD_W - 24.0) - cx))
    h = {1: 96.0, 2: 134.0}.get(len(lines), 172.0)
    x0, y0 = cx - w / 2, cy - h / 2
    outline = [[x0 + 18, y0], [x0 + w - 18, y0],
               [x0 + w, y0 + 18], [x0 + w, y0 + h - 18],
               [x0 + w - 18, y0 + h], [x0 + 40, y0 + h],
               [x0 + 18, y0 + h], [x0, y0 + h - 18], [x0, y0 + 18],
               [x0 + 18, y0]]
    tx = x0 + w * g["ftail"]
    tail = [[tx - 14, y0 + h], list(g["tail"]), [tx + 16, y0 + h]]
    els = [{"id": uid, "type": "shape", "shape": "path", "points": outline,
            "closed": True, "width": 3.4, "fill": "paper"},
           {"id": f"{uid}_tail", "type": "shape", "shape": "path",
            "points": tail, "width": 3.4}]
    for i, line in enumerate(lines):
        ly = cy + (i - (len(lines) - 1) / 2.0) * 42.0
        els.append({"id": f"{uid}_t{i}", "type": "text", "text": line,
                    "size": 23, "at": [cx, ly], "anchor": "mm",
                    **({"role": "term", "color": "accent"} if key else {})})
    els.append({"id": f"{uid}_grp", "type": "group",
                "children": [e["id"] for e in els]})
    return els


def narration_stream(narration: str, uid: str,
                     bold: set[str] | None = None,
                     dialogue: list[dict] | None = None,
                     line_starts: list[float] | None = None,
                     total_secs: float | None = None
                     ) -> tuple[list[dict], list[dict]]:
    """The continuous speech-caption track for ONE segment.

    Single narrator: every sentence of `narration` appears in the teacher's
    bubble, cued by its own opening words (word-accurate at render when TTS
    boundaries exist) and replaced when the next sentence starts. Sentences
    in `bold` (verbatim) render bold + accent.

    Dialogue mode: `dialogue` = [{"who", "line"}, ...] with `line_starts`
    (measured seconds, from per-line TTS) — each line's bubble appears beside
    ITS speaker at the exact offset. Bubble ids carry CAPTION_PREFIX."""
    bold = bold or set()
    if dialogue and line_starts and len(line_starts) == len(dialogue):
        entries, cues = [], []
        for i, d in enumerate(dialogue):
            who = str(d.get("who") or "teacher")
            line = " ".join(str(d.get("line") or "").split())
            if not line:
                continue
            start = line_starts[i]
            end = line_starts[i + 1] if i + 1 < len(line_starts)                 else (total_secs or start + 4.0)
            sents = split_sentences(line) or [line]
            span, pos = max(0.5, end - start), 0
            for s in sents:
                frac_off = pos / max(1, len(line))
                entries.append((who, s))
                cues.append({"sec": max(0.05, start + span * frac_off)})
                pos += len(s) + 1
    else:
        sents = split_sentences(narration)
        entries = [("teacher", s) for s in sents]
        cues = [{"phrase": bubble_cue_phrase(s, narration), "offset": -0.05}
                for s in sents]
    elements: list[dict] = []
    actions: list[dict] = []
    norm_bold = {_norm_stream(b) for b in bold}
    for i, (who, text) in enumerate(entries):
        if not text:
            continue
        nt = _norm_stream(text)
        key = bool(nt) and nt in norm_bold
        bid = f"__nb_{uid}_{i}"
        elements += _stream_bubble(bid, who, text, key)
        actions.append({"verb": "reveal", "target": f"{bid}_grp",
                        "duration": 0.22, "at": cues[i]})
        if i + 1 < len(entries):
            nxt = dict(cues[i + 1])
            if "offset" in nxt:
                nxt["offset"] = nxt["offset"] - 0.07
            elif "sec" in nxt:
                nxt["sec"] = max(0.1, nxt["sec"] - 0.07)
            actions.append({"verb": "fade", "target": f"{bid}_grp",
                            "to": 0.0, "duration": 0.16, "at": nxt})
        else:
            end = {"sec": max(0.5, (total_secs or 0.0) - 0.35)} \
                if total_secs else {"frac": 0.985}
            actions.append({"verb": "fade", "target": f"{bid}_grp",
                            "to": 0.0, "duration": 0.4, "at": end})
    return elements, actions


def _norm_stream(s: str) -> str:
    # unicode-aware: Arabic/Devanagari/CJK sentences must not collapse to ""
    # (an empty norm once matched EVERY sentence and bolded the whole lesson)
    return _re.sub(r"[^\w]+", " ", (s or "").lower(), flags=_re.UNICODE).strip()


def student_element(asset: str = "avatar_student") -> dict:
    return {"id": STUDENT_ID, "type": "illustration", "asset": asset,
            "at": list(STUDENT_AT), "scale": AVATAR_SCALE}


def student_voice_for_avatar(avatar_key: str, lang: str = "en") -> str:
    """A student TTS voice matching the avatar's age band (English only —
    other languages reuse the lesson voice, since age-matched voices are not
    guaranteed per locale)."""
    if lang != "en":
        return ""
    young = avatar_key in ("avatar_student", "avatar_student_5_7_f",
                           "avatar_student_8_10_m", "avatar_student_8_10_f")
    return "en-US-AnaNeural" if young else "en-US-EmmaNeural"


def key_point_choreo(text: str, uid: str) -> tuple[list[dict], list[dict]]:
    """Elements + actions for one teacher line: the bubble APPEARS beside
    the ever-present teacher exactly as the words are spoken (it is speech,
    not a drawing — no pen, no lettering), the teacher pulses, and the
    bubble fades on its own once the sentence has been said. `text` must be
    a VERBATIM narration sentence — snapping happens at the call sites."""
    bub_id = f"__kp_{uid}"
    grp_id = f"__kp_{uid}_grp"
    elements = bubble_elements(bub_id, text, _TEACH_BUBBLE_AT,
                               _TEACH_BUBBLE_TAIL)
    kids = [e["id"] for e in elements]
    elements.append({"id": grp_id, "type": "group", "children": kids})
    cue = bubble_cue_phrase(text)
    actions = [
        {"verb": "reveal", "target": grp_id, "duration": 0.35,
         "at": {"phrase": cue, "offset": -0.15}},
        {"verb": "pulse", "target": TEACHER_ID, "times": 2, "duration": 1.2},
        {"verb": "fade", "target": grp_id, "to": 0.0, "duration": 0.45,
         "at": {"phrase": cue, "offset": speech_secs(text)}},
    ]
    return elements, actions


def human_moment(role: str, text: str, uid: str = "hm",
                 asset: str | None = None) -> tuple[list[dict], list[dict], str]:
    """Elements + actions for one HUMAN_TEACHING_MOMENT, plus the avatar
    asset key. The avatar fades in, their bubble APPEARS with the line
    (speech is never hand-drawn), they pulse while 'speaking', then
    everything fades and the board takes the focus back. All scoped to ONE
    segment — moments never persist onto the board."""
    role = role if role in ("student", "teacher") else "student"
    asset = asset or f"avatar_{role}"
    av_id = f"__{uid}_av"
    bub_id = f"__{uid}_bub"
    bgrp_id = f"__{uid}_bub_grp"
    grp_id = f"__{uid}_grp"
    elements = [{"id": av_id, "type": "illustration", "asset": asset,
                 "at": list(_AV_AT), "scale": _AV_SCALE}]
    bub_els = bubble_elements(bub_id, text, _BUBBLE_AT, _BUBBLE_TAIL_TO)
    elements += bub_els
    elements.append({"id": bgrp_id, "type": "group",
                     "children": [e["id"] for e in bub_els]})
    elements.append({"id": grp_id, "type": "group",
                     "children": [av_id] + [e["id"] for e in bub_els]})
    cue = bubble_cue_phrase(text)
    actions = [
        {"verb": "reveal", "target": av_id, "at": {"frac": 0.06},
         "duration": 0.5},
        # the line appears when it is spoken (verbatim narration lines cue
        # by phrase; invented dialogue falls back to just after the avatar)
        {"verb": "reveal", "target": bgrp_id, "duration": 0.35,
         "at": {"phrase": cue, "offset": -0.15}},
        {"verb": "pulse", "target": av_id, "times": 3, "duration": 1.6},
        {"verb": "fade", "target": grp_id, "to": 0.0, "duration": 0.6,
         "at": {"frac": 0.86}},
    ]
    return elements, actions, asset
