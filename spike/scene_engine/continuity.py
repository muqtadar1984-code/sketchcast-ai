"""Visual continuity: one persistent teaching canvas across narration segments.

A narration segment is an AUDIO unit. A visual chapter is a TEACHING unit.
They are not the same thing: a teacher draws a plant cell once and then
explains, extends, points, zooms and annotates it across many sentences —
erasing only when the concept genuinely changes.

The model plans ONCE for the whole lesson (it already writes all segments in
one reply, so full look-ahead is free): a VisualPlan of chapters, each with a
shared element roster (stable ids) and per-segment STEPS carrying a decision —

    NEW_VISUAL        first drawing of the chapter's root visual
    EXTEND            add to the existing board (new layer/element/label)
    CONTINUE          the board already explains it; light emphasis at most
    FOCUS             camera/highlight on something already drawn
    TRANSFORM         meaningful modification (zoom into, morph, animate)
    CLEAR_AND_REDRAW  the concept changed; wipe and start a new visual

This module's COMPILER is deterministic: it walks the plan chaining board
state (which layers of which elements are already drawn, what the camera is
doing, what is on the board from the previous chapter) and expands each step
into a standard per-segment Scene dict that CARRIES its inherited state.
Per-segment rendering and Agent 8 concat stay untouched: every compiled scene
is self-contained, so segments still render in parallel.

The plan is MODEL OUTPUT and therefore hostile input: actions are sanitized
value-by-value at parse (a null fade target once crashed the compiler and the
blanket except dropped the whole lesson's plan), chapters validate and salvage
individually, duplicate steps merge, and references to things not on the board
drop with a report line instead of producing schema-invalid scenes.
"""

from __future__ import annotations

import logging
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from .schema import WORLD_H, WORLD_W

logger = logging.getLogger(__name__)

Decision = Literal["NEW_VISUAL", "EXTEND", "CONTINUE", "FOCUS", "TRANSFORM",
                   "CLEAR_AND_REDRAW"]
_INTRODUCERS = {"draw", "write", "reveal"}
_CAMERA = {"zoom", "pan", "camera_reset"}
_DEFAULT_CAM = {"cx": WORLD_W / 2, "cy": WORLD_H / 2, "scale": 1.0}

# Where the outgoing picture goes when a chapter carries the board but brings
# its own diagram. Top-left, small enough to clear the stage: an illustration
# occupies +/- (350*scale, 260*scale), so at 0.32 the box is x 24..248,
# y 59..225 — 2px clear of the root visual's left edge (x 250) and well above
# the caption panels (y 298). whiteboard._MARGIN_SLOTS deliberately leaves
# this corner alone and puts auto-sketches in the top-RIGHT one.
_RECAP_AT = (136.0, 142.0)
_RECAP_SCALE = 0.32


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="allow")

    # the model references segments by 1-BASED INDEX (it never sees the ids
    # the parser assigns later); an "s003"-style id is accepted too
    segment: int | str
    decision: Decision = "CONTINUE"
    reason: str = ""                   # planner's why — surfaces in the report
    actions: list[dict] = Field(default_factory=list)
    # HUMAN_TEACHING_MOMENT (optional, selective): {"role": "student"|
    # "teacher", "text": "<short spoken line>"} — expands into avatar +
    # hand-drawn speech bubble choreography scoped to this segment only
    moment: Optional[dict] = None
    # KEY POINT (optional): a short VERBATIM quote of this segment's most
    # important sentence — the ever-present teacher speaks it in a drawn
    # bubble, cued to the words in the narration
    key_point: Optional[str] = None

    @property
    def segment_id(self) -> str:
        if isinstance(self.segment, int):
            return f"s{self.segment:03d}"
        return str(self.segment)


class VisualChapter(BaseModel):
    model_config = ConfigDict(extra="allow")

    concept: str
    transition: Literal["clear_and_redraw", "carry"] = "clear_and_redraw"
    assets: dict[str, str] = Field(default_factory=dict)
    elements: list[dict] = Field(default_factory=list)  # shared roster, stable ids
    steps: list[PlanStep] = Field(default_factory=list)


class VisualPlan(BaseModel):
    model_config = ConfigDict(extra="allow")

    chapters: list[VisualChapter] = Field(default_factory=list)


# ── action sanitization (the model's dicts, value by value) ──────────────────

def _guess_part_name(eid: str) -> str:
    """'lbl_cell_membrane' / 'arr_nucleus' / 'nucleus_obj' / 'brain_label'
    -> the part name the id is talking about.

    Only PREFIXES were stripped for the naming convention the prompt's own
    example uses. A director that names labels by SUFFIX instead — measured
    on a live lesson: brain_label, heart_label, lungs_label — yielded
    'brain label', which matches no part, so every one of those labels lost
    its leader line silently. The suffix list mirrors the prefix list."""
    s = str(eid).lower()
    for p in ("arr_auto_", "arr_", "arrow_", "ar_", "lbl_", "label_", "lb_",
              "txt_"):
        if s.startswith(p):
            s = s[len(p):]
            break
    words = s.replace("_", " ").split()
    while len(words) > 1 and words[-1] in (
            "obj", "objs", "object", "item", "el", "elem", "img", "image",
            "shape", "part", "label", "labels", "lbl", "text", "txt",
            "name", "tag", "caption"):
        words.pop()
    return " ".join(words).strip()


def _norm_name(s: str) -> str:
    import re as _re
    return _re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


def _pt(v) -> list[float] | None:
    try:
        if isinstance(v, (list, tuple)) and len(v) == 2:
            return [float(v[0]), float(v[1])]
    except (TypeError, ValueError):
        pass
    return None


_VERB_ALIASES = {"clear": "erase", "remove": "erase", "delete": "erase",
                 "add": "draw", "focus": "zoom", "point": "circle",
                 "annotate": "write", "label": "write"}
_KNOWN_VERBS = {"draw", "write", "reveal", "erase", "move", "highlight",
                "circle", "underline", "pulse", "fade", "morph", "zoom",
                "pan", "camera_reset"}


def _clean_action(a) -> dict | None:
    if not isinstance(a, dict) or not isinstance(a.get("verb"), str):
        return None
    verb = _VERB_ALIASES.get(a["verb"].strip().lower(), a["verb"].strip().lower())
    if verb not in _KNOWN_VERBS:
        # an invented verb ("sketch", "show") must cost ITSELF, not the scene
        # — an unknown verb once reached schema validation and dropped a whole
        # segment to the legacy renderer
        return None
    out: dict = {"verb": verb}
    if isinstance(a.get("target"), str):
        out["target"] = a["target"]
    if isinstance(a.get("at"), dict):
        out["at"] = a["at"]
    if isinstance(a.get("layers"), list):
        layers = [str(l) for l in a["layers"] if isinstance(l, str)][:12]
        if layers:
            out["layers"] = layers
    if verb == "zoom":
        try:
            out["scale"] = min(2.5, max(1.0, float(a.get("scale", 1.6))))
        except (TypeError, ValueError):
            out["scale"] = 1.5
        c = _pt(a.get("center"))
        if c:
            out["center"] = c
    elif verb == "pan":
        c = _pt(a.get("center"))
        if c is None:
            return None
        out["center"] = c
    elif verb == "fade":
        try:
            out["to"] = min(1.0, max(0.0, float(a.get("to", 0.0))))
        except (TypeError, ValueError):
            out["to"] = 0.0
    elif verb == "move":
        path = [_pt(p) for p in (a.get("path") or [])]
        path = [p for p in path if p is not None]
        if len(path) < 2:
            return None
        out["path"] = path
        for k in ("stagger", "stop_frac"):
            if k in a:
                try:
                    out[k] = float(a[k])
                except (TypeError, ValueError):
                    pass
    elif verb == "morph":
        if not isinstance(a.get("into"), str):
            return None
        out["into"] = a["into"]
    for k in ("duration", "easing", "pen", "padding", "times"):
        if k in a and isinstance(a[k], (int, float, str)):
            out[k] = a[k]
    if verb in ("draw", "highlight", "circle", "underline", "zoom")             and isinstance(a.get("region"), str) and a["region"].strip():
        # a semantic {asset, region} draw carries WHICH part is being drawn;
        # dropping it here silently reverted narration-ordered drawing to a
        # uniform slice (the adapter's whole point, lost at the boundary)
        out["region"] = a["region"].strip()
    return out


_COLOR_ROLES = {"ink", "muted", "accent"}
_COLOR_MAP = {"black": "ink", "dark": "ink", "navy": "ink", "blue": "accent",
              "green": "accent", "teal": "accent", "red": "accent",
              "orange": "accent", "gray": "muted", "grey": "muted",
              "light": "muted"}
_SHAPE_ALIASES = {"oval": "ellipse", "circle": "ellipse", "rect": "path",
                  "rectangle": "path", "polygon": "path", "curve": "path"}


def _role(c) -> str:
    c = str(c or "ink").strip().lower()
    return c if c in _COLOR_ROLES else _COLOR_MAP.get(c, "ink")


def _clean_element(e) -> dict | None:
    """Element values, clamped the way actions are — width 40, color 'black'
    and shape 'oval' once killed five scenes to the legacy renderer because
    elements skipped sanitization entirely."""
    if not isinstance(e, dict) or not isinstance(e.get("id"), str):
        return None
    if e["id"].startswith("__"):
        # double-underscore ids are the ENGINE's namespace (captions, the
        # persistent avatars, moment overlays) — a model squatting one once
        # produced duplicate element ids and killed the whole scene
        return None
    t = str(e.get("type") or "").strip().lower()
    out = dict(e)
    out["type"] = t

    def clampf(key, lo, hi, default=None):
        try:
            out[key] = min(hi, max(lo, float(out[key])))
        except (KeyError, TypeError, ValueError):
            if default is not None:
                out[key] = default
            else:
                out.pop(key, None)

    if t == "illustration":
        if not isinstance(out.get("asset"), str) or _pt(out.get("at")) is None:
            return None
        out["at"] = _pt(out["at"])
        clampf("scale", 0.05, 8.0, 1.0)
    elif t == "text":
        if not isinstance(out.get("text"), str) or _pt(out.get("at")) is None:
            return None
        out["at"] = _pt(out["at"])
        clampf("size", 10.0, 72.0, 26.0)
        if "color" in out:
            out["color"] = _role(out["color"])
        if out.get("role") not in ("title", "label", "term", "caption"):
            out.pop("role", None)
        if out.get("anchor") not in ("lt", "mt", "rt", "lm", "mm", "rm"):
            out.pop("anchor", None)
    elif t == "arrow":
        for k in ("tail", "head"):
            v = out.get(k)
            if isinstance(v, dict) and isinstance(v.get("el"), str):
                continue
            p = _pt(v)
            if p is None:
                return None
            out[k] = p
        clampf("width", 1.0, 10.0, 3.2)
        clampf("curve", -200.0, 200.0, 0.0)
        if "color" in out:
            out["color"] = _role(out["color"])
    elif t == "shape":
        shp = str(out.get("shape") or "path").strip().lower()
        out["shape"] = _SHAPE_ALIASES.get(shp, shp)
        if out["shape"] not in ("path", "ellipse", "line"):
            out["shape"] = "path"
        clampf("width", 0.5, 20.0, 3.0)
        if "color" in out:
            out["color"] = _role(out["color"])
        if out["shape"] == "ellipse":
            c = _pt(out.get("center"))
            if c is None:
                return None
            out["center"] = c
            clampf("rx", 3.0, 640.0, 40.0)
            clampf("ry", 3.0, 640.0, 40.0)
        else:
            pts = [_pt(p) for p in (out.get("points") or [])]
            pts = [p for p in pts if p is not None]
            if len(pts) < 2:
                return None
            out["points"] = pts
    elif t == "particles":
        pts = [_pt(p) for p in (out.get("spawn") or [])]
        pts = [p for p in pts if p is not None]
        if not pts:
            return None
        out["spawn"] = pts[:24]
        clampf("radius", 2.0, 30.0, 7.0)
        if "color" in out:
            out["color"] = _role(out["color"])
        if out.get("glyph") not in ("dot", "ring", "blob"):
            out.pop("glyph", None)
    elif t == "group":
        kids = [c for c in (out.get("children") or []) if isinstance(c, str)]
        if not kids:
            return None
        out["children"] = kids
    else:
        return None
    return out


def parse_visual_plan(raw) -> Optional[VisualPlan]:
    """Trust boundary for the model-emitted plan. Chapters validate and
    salvage INDIVIDUALLY (one bad chapter must not cost the lesson its plan),
    actions sanitize value-by-value, duplicate steps for one segment merge."""
    if not isinstance(raw, dict) or not isinstance(raw.get("chapters"), list):
        return None
    decisions = {"NEW_VISUAL", "EXTEND", "CONTINUE", "FOCUS", "TRANSFORM",
                 "CLEAR_AND_REDRAW"}
    chapters: list[VisualChapter] = []
    for i, craw in enumerate(raw["chapters"]):
        # pre-clamp step fields BEFORE pydantic: an unknown decision (the
        # model once leaked "GHOST_ONLY" from the visual_action vocabulary)
        # clamps to CONTINUE — it must never cost the chapter, let alone the
        # plan
        if isinstance(craw, dict) and isinstance(craw.get("steps"), list):
            craw = dict(craw)
            steps = []
            for st in craw["steps"]:
                if not isinstance(st, dict) or st.get("segment") in (None, ""):
                    continue
                st = dict(st)
                if st.get("decision") not in decisions:
                    st["decision"] = "CONTINUE"
                if st.get("transition") is not None:
                    st.pop("transition", None)
                m = st.get("moment")
                if isinstance(m, dict) and isinstance(m.get("text"), str)                         and m["text"].strip():
                    st["moment"] = {"role": m.get("role") if m.get("role")
                                    in ("student", "teacher") else "student",
                                    "text": m["text"].strip()[:60]}
                else:
                    st["moment"] = None
                kp = st.get("key_point")
                st["key_point"] = (" ".join(kp.split())[:60]
                                   if isinstance(kp, str) and kp.strip()
                                   else None)
                steps.append(st)
            craw["steps"] = steps
        if isinstance(craw, dict) and \
                craw.get("transition") not in ("clear_and_redraw", "carry"):
            craw = dict(craw)
            craw.pop("transition", None)
        try:
            ch = VisualChapter.model_validate(craw)
        except Exception as e:
            logger.warning("visual_plan chapter %d rejected (%s); salvaging rest", i, e)
            continue
        ch.elements = [c for c in (_clean_element(e) for e in ch.elements)
                       if c is not None]
        merged: dict[str, PlanStep] = {}
        order: list[str] = []
        for st in ch.steps:
            st.actions = [c for c in (_clean_action(a) for a in st.actions)
                          if c is not None]
            sid = st.segment_id
            if sid in merged:   # duplicate step: merge, first decision wins
                merged[sid].actions.extend(st.actions)
            else:
                merged[sid] = st
                order.append(sid)
        ch.steps = [merged[sid] for sid in order]
        if ch.steps and ch.elements:
            chapters.append(ch)
    if not chapters:
        return None
    return VisualPlan(chapters=chapters)


# ── camera chaining (mirrors CameraTrack's end-state logic, deterministically) ─

def seed_moment(plan: VisualPlan, narrations: dict[str, str]) -> str | None:
    """A lesson should carry at least one HUMAN_TEACHING_MOMENT. When the
    model plans none, seed ONE student moment from a real interrogative
    sentence in a mid-lesson planned segment — the question is lifted from the
    narration, never invented. Returns the segment id used, or None."""
    import re
    if any(st.moment for ch in plan.chapters for st in ch.steps):
        return None
    steps = [st for ch in plan.chapters for st in ch.steps]
    if len(steps) < 2:
        return None

    def _questions(sid: str) -> list[str]:
        out = []
        for m in re.finditer(r"([A-Z][^.!?]*\?)", narrations.get(sid, "")):
            q = " ".join(m.group(1).split())
            if len(q.split()) >= 3 and "tap continue" not in q.lower() \
                    and "anything you'd want" not in q.lower():
                out.append(q)
        return out

    # pass 1: a question already short enough to letter into a speech bubble
    for st in steps[1:]:
        for q in _questions(st.segment_id):
            if len(q.split()) <= 8 and len(q) <= 60:
                st.moment = {"role": "student", "text": q}
                return st.segment_id
    # pass 2: shorten the first real question found
    for st in steps[1:]:
        qs = _questions(st.segment_id)
        if qs:
            q = " ".join(qs[0].split()[:8]).rstrip(",;:?") + "?"
            st.moment = {"role": "student", "text": q[:60]}
            return st.segment_id
    return None


def seed_key_points(plan: VisualPlan, narrations: dict[str, str],
                    cap: int = 3) -> int:
    """The persistent teacher should SPEAK at important statements. When the
    model plans no key_points, seed up to `cap` from definition-like
    sentences in the planned segments' own narration (verbatim, never
    invented) — at most one per chapter, spread across the lesson."""
    import re
    if any(getattr(st, "key_point", None)
           for ch in plan.chapters for st in ch.steps):
        return 0
    pat = re.compile(
        r"([A-Z][^.!?]{2,70}?\b(?:is|are|means|called|controls?|makes?|"
        r"protects?|stores?|contains?)\b[^.!?]{3,70}[.!])")
    seeded = 0
    for ch in plan.chapters:
        if seeded >= cap:
            break
        for st in ch.steps:
            text = narrations.get(st.segment_id, "")
            m = pat.search(text)
            if not m or st.moment:
                continue
            line = " ".join(m.group(1).split()).rstrip(".!")
            if len(line) > 60:
                line = line[:60].rsplit(" ", 1)[0]
            if len(line.split()) < 4:
                continue
            st.key_point = line
            seeded += 1
            break               # one per chapter keeps it selective
    return seeded


def _chain_camera(cam: dict, actions: list[dict], elements: list[dict]) -> dict:
    pos = {e.get("id"): e.get("at") for e in elements if isinstance(e, dict)}
    for a in actions:
        v = a.get("verb")
        if v == "camera_reset":
            cam = dict(_DEFAULT_CAM)
        elif v == "zoom":
            c = a.get("center")
            if c is None and a.get("target") in pos:
                c = _pt(pos[a["target"]])
            cam = {"cx": float(c[0]) if c else cam["cx"],
                   "cy": float(c[1]) if c else cam["cy"],
                   "scale": min(2.5, max(1.0, float(a.get("scale", 1.6))))}
        elif v == "pan" and a.get("center"):
            cam = {**cam, "cx": float(a["center"][0]), "cy": float(a["center"][1])}
    return cam


# ── the compiler ─────────────────────────────────────────────────────────────

def compile_plan(plan: VisualPlan, narrations: dict[str, str],
                 all_segments: list[str] | None = None,
                 skip_hold: set[str] | None = None,
                 avatars: dict | None = None,
                 style: str = "socratic",
                 ) -> tuple[dict[str, dict], dict[str, dict[str, str]], list[str]]:
    """VisualPlan -> (scene dict per segment_id, scene_assets per segment_id,
    debug report lines).

    Board-state chaining rules:
      * an element introduced (draw/write/reveal) in an earlier step appears in
        later scenes WITHOUT its introducer — visible and complete at t=0;
      * an illustration drawn layer-by-layer carries `drawn_layers` (vector)
        and `drawn_frac` + explicit per-action `slice` (raster);
      * erased/faded-out elements leave the board; a later introducer brings
        them back (erase -> correct is a legal teaching move);
      * groups expand for introduce/erase/fade alike;
      * a clear_and_redraw boundary injects the previous board RENAMED
        (prev__*) so ids can never collide, fully drawn, faded out on screen,
        with the camera reset animating rather than cutting; the boundary
        scene's asset map includes every asset seen so far;
      * a carry chapter merges the previous board into its roster so the
        board persists through ALL its steps;
      * unplanned segments inside a chapter's span get HOLD scenes (the board,
        unchanged) unless listed in `skip_hold` (quizzes, question hooks);
      * actions referencing something not on the board and not introduced in
        the same step are DROPPED with a report line — a scene must never
        fail validation over a model's dangling reference.
    """
    scenes: dict[str, dict] = {}
    assets_by_seg: dict[str, dict[str, str]] = {}
    report: list[str] = []

    prev_board: list[dict] = []
    assets_seen: dict[str, str] = {}
    cam = dict(_DEFAULT_CAM)

    for ch in plan.chapters:
        try:
            prev_board, cam = _compile_chapter(
                ch, narrations, all_segments, skip_hold or set(),
                prev_board, assets_seen, cam, scenes, assets_by_seg, report,
                avatars=avatars, style=style)
        except Exception:
            logger.exception("chapter %r failed to compile; skipping it", ch.concept)
            report.append(f"CHAPTER {ch.concept} | COMPILE FAILED — skipped, "
                          f"board carried unchanged")
    # ── the persistent teacher ──────────────────────────────────────────────
    # One teacher on every compiled scene, whole lesson (whiteboard-fallback
    # segments add their own copy in build_whiteboard_scene). Injected OUTSIDE
    # the chapter machinery on purpose: the teacher is not board content — it
    # never fades at a chapter boundary, never renames to prev__*, never
    # counts against the one-root rule. On the lesson's first segment the
    # hand draws them in; everywhere else they are simply present at t=0.
    from .whiteboard import (AVATAR_PROMPTS, STUDENT_ID, TEACHER_ID,
                             narration_stream, select_key_sentence,
                             snap_to_narration, student_element,
                             teacher_element)
    teach_key = (avatars or {}).get("teacher", "avatar_teacher")
    stud_key = (avatars or {}).get("student", "avatar_student")
    # which sentences the stream should BOLD, per segment: the model's
    # key_points and teacher-role moments, snapped to real narration; plus
    # the importance scorer's pick where the model marked nothing
    step_by_sid = {st.segment_id: st for c in plan.chapters for st in c.steps}
    for sid, sc in scenes.items():
        if not any(e.get("id") == TEACHER_ID for e in sc["elements"]):
            sc["elements"].append(teacher_element(teach_key))
            assets_by_seg.setdefault(sid, {})
            assets_by_seg[sid] = {**assets_by_seg[sid],
                                  teach_key: AVATAR_PROMPTS[teach_key]}
        if style == "conversational":
            # the student is a PERMANENT speaker too; the caption stream is
            # injected at COMPOSE time, once per-line audio offsets exist
            if not any(e.get("id") == STUDENT_ID for e in sc["elements"]):
                sc["elements"].append(student_element(stud_key))
                assets_by_seg[sid] = {**assets_by_seg.get(sid, {}),
                                      stud_key: AVATAR_PROMPTS[stud_key]}
            continue
        narr = narrations.get(sid, "")
        st = step_by_sid.get(sid)
        bold: set[str] = set()
        if st is not None and st.key_point:
            b = snap_to_narration(st.key_point, narr)
            if b:
                bold.add(b)
        if st is not None and st.moment and st.moment["role"] == "teacher":
            b = snap_to_narration(st.moment["text"], narr)
            if b:
                bold.add(b)
        if not bold:
            b = select_key_sentence(narr)
            if b:
                bold.add(b)
        nb_els, nb_acts = narration_stream(narr, uid=sid, bold=bold)
        if nb_els:
            sc["elements"].extend(nb_els)
            sc["actions"] = sc["actions"] + nb_acts
            report.append(f"SEGMENT {sid} | STREAM {len(nb_acts) // 2} "
                          f"sentence(s), bold: {sorted(bold) if bold else '—'}")
    _add_auto_sketches(scenes, narrations, assets_by_seg, report)
    return scenes, assets_by_seg, report


def _add_auto_sketches(scenes: dict, narrations: dict, assets_by_seg: dict,
                       report: list) -> None:
    """The concrete things a narration NAMES, sketched as they are spoken
    (founder: 'the hand draws images of items wherever it can').

    This used to skip any board that already carried a drawing, which on the
    semantic path meant EVERY board — a measured lesson produced zero sketches
    because all twelve of its chapters had a root diagram. So an occupied
    board now gets its sketches in the top-corner margin slots instead of
    being skipped. The centre slots are still used on an empty board.

    Two bounds, because each new sketch key costs one image generation the
    first time it is ever seen (and nothing thereafter — the cache is shared
    across lessons and schools):
      - a per-lesson cap, so sketches cannot crowd out the planned diagrams
        inside IMAGE_CALLS_PER_LESSON
      - lesson-wide dedupe, so a narration that says 'cell' in nine segments
        draws it once rather than nine times
    """
    import os

    from .whiteboard import free_margin_slots, illustration_box, sketch_elements
    cap = max(0, int(os.getenv("SKETCHES_PER_LESSON", "6")))
    drawn: set[str] = set()
    for sid in sorted(scenes):
        if len(drawn) >= cap:
            break
        sc = scenes[sid]
        boxes = [illustration_box(e["at"], float(e.get("scale", 1.0)))
                 for e in sc["elements"]
                 if e.get("type") == "illustration"
                 and not str(e.get("id", "")).startswith("__")
                 and e.get("at")]
        occupied = bool(boxes)
        slots = free_margin_slots(boxes) if occupied else None
        if occupied and not slots:
            continue      # the board is genuinely full; a sketch would cover it
        els, acts, assets = sketch_elements(
            narrations.get(sid, ""), uid=sid, exclude=drawn,
            limit=1 if occupied else 2, slots=slots)
        if not els:
            continue
        els = els[:max(0, cap - len(drawn))]
        if not els:
            break
        keep = {e["id"] for e in els}
        acts = [a for a in acts if a.get("target") in keep]
        assets = {e["asset"]: assets[e["asset"]] for e in els}
        sc["elements"].extend(els)
        sc["actions"] = sc["actions"] + acts
        assets_by_seg[sid] = {**assets_by_seg.get(sid, {}), **assets}
        drawn.update(e["asset"] for e in els)
        report.append(f"SEGMENT {sid} | SKETCHED {[e['asset'] for e in els]}"
                      f"{' (margin)' if occupied else ''}")


def _compile_chapter(ch: VisualChapter, narrations, all_segments, skip_hold,
                     prev_board, assets_seen, cam, scenes, assets_by_seg,
                     report, avatars=None, style="socratic"):
    roster = {e["id"]: e for e in ch.elements}
    # ONE root visual per chapter, enforced: independently generated images do
    # not compose — a model that declares an illustration per organelle gets
    # its first kept and the rest dropped (their actions degrade to holds via
    # the dangling-reference filter), never a stacked tangle on screen
    ills = [eid for eid, e in roster.items() if e.get("type") == "illustration"]
    if len(ills) > 1:
        # the ROOT is the illustration the plan REFERENCES the most (draws
        # weighted double) — draw-count alone once kept a 2-draw practical
        # diagram over the cell the chapter spends six highlights and eight
        # labels teaching, leaving every label floating on an empty board
        score = {eid: 0 for eid in ills}
        for st_ in ch.steps:
            for a_ in st_.actions:
                t_ = a_.get("target")
                if t_ in score:
                    score[t_] += 2 if a_.get("verb") == "draw" else 1
        ills.sort(key=lambda eid: -score[eid])
    # per-part HANDLES vs stacked tangles: a model that declares seven
    # "illustrations" all sharing the root's asset means "draw THAT part of
    # the one diagram now" — convert instead of amputate. Their actions
    # retarget to the root; the guessed part names are appended to the asset
    # prompt as its layer-group tail so vision annotation can find them.
    alias_parts: dict[str, str] = {}
    merged_foreign_asset = False
    root_asset = str(roster[ills[0]].get("asset") or "") if ills else ""
    label_parts = [_guess_part_name(eid) for eid, e in roster.items()
                   if e.get("type") in ("text", "arrow")]
    label_parts = [p for p in label_parts if p]

    def _label_part_for(guess: str) -> str | None:
        g = _norm_name(guess)
        if not g:
            return None
        for lp in label_parts:
            n = _norm_name(lp)
            if n == g or n in g or g in n:
                return lp
        return None

    # a chapter that declares MANY part-like illustrations and NO labels at
    # all (observed: cell_nucleus/cell_vacuole/... with per-part assets) is
    # the handle pattern with the naming stripped — treat every extra as a
    # handle; label synthesis below then rebuilds the labels from the parts
    # the narration actually names
    mass_handles = len(ills) >= 4 and not label_parts
    for extra in ills[1:]:
        guess = _guess_part_name(extra)
        lbl_part = _label_part_for(guess)
        if str(roster[extra].get("asset") or "") == root_asset:
            alias_parts[extra] = lbl_part or guess
        elif lbl_part is not None or (mass_handles and guess):
            # a different asset but a name the chapter LABELS (or the
            # mass-handle pattern): still a per-part handle, and the root
            # image must be regenerated to actually contain the part
            alias_parts[extra] = lbl_part or guess
            merged_foreign_asset = True
        else:
            report.append(f"CHAPTER {ch.concept} | DROPPED illustration "
                          f"{extra!r} (one root visual per chapter — use "
                          f"named layers on {ills[0]!r} instead)")
            del roster[extra]
            continue
        report.append(f"CHAPTER {ch.concept} | MERGED handle {extra!r} "
                      f"into root {ills[0]!r} as part "
                      f"{alias_parts[extra]!r}")
        del roster[extra]
    if alias_parts:
        prompt0 = ch.assets.get(root_asset, "")
        names = [_guess_part_name(ills[0])] + list(alias_parts.values())
        seen_n: set[str] = set()
        names = [n for n in names if n and not (n in seen_n or seen_n.add(n))]
        if merged_foreign_asset and prompt0:
            # a NEW key: the root's cached image predates the merge and may
            # depict only its own part — the merged diagram must exist
            new_key = root_asset + "__merged"
            ch.assets[new_key] = (
                prompt0.rstrip().rstrip(".") +
                ". The diagram MUST clearly contain all of: " +
                ", ".join(names) + " — as DRAWINGS only; the image must not "
                "contain any written words, captions or labels. "
                "Name the layer groups exactly: " + ", ".join(names) + ".")
            roster[ills[0]] = dict(roster[ills[0]])
            roster[ills[0]]["asset"] = new_key
            report.append(f"CHAPTER {ch.concept} | ROOT ASSET rebuilt as "
                          f"{new_key!r} with parts {names}")
        elif prompt0 and "name the layer groups exactly" not in prompt0.lower():
            ch.assets[root_asset] = (prompt0.rstrip().rstrip(".") +
                                     ". Name the layer groups exactly: " +
                                     ", ".join(names) + ".")
        for st_ in ch.steps:
            for a_ in st_.actions:
                if a_.get("target") in alias_parts and a_.get("verb") in \
                        ("draw", "zoom", "highlight", "circle", "pulse"):
                    a_["target"] = ills[0]
    introduced: set[str] = set()
    drawn_layers: dict[str, list[str]] = {}
    drawn_regions: dict[str, list[str]] = {}   # vision-region draw progress
    region_reach: dict[str, float] = {}   # bare-draw progress on scheduled roots
    base_frac: dict[str, float] = {}      # raster progress carried from before
    draw_counts: dict[str, int] = {}
    draws_done: dict[str, int] = {}
    erased: set[str] = set()

    assets_seen.update(ch.assets)
    seg_assets = dict(assets_seen)        # cumulative: carried boards resolve too

    # a carry chapter continues on the same board: previous elements join the
    # roster with their state imported, so EVERY step keeps them
    if prev_board and ch.transition == "carry":
        # Does the incoming chapter bring a picture of ITS OWN? If so the
        # stage is taken, and the outgoing picture has to move or the two
        # coincide exactly — every illustration is placed at the same world
        # point (semantic._ROOT_AT) at scale 1.0, so a naive carry stacks two
        # 700x520 rasters on top of each other and nothing measures it.
        incoming_ill = any(e.get("type") == "illustration" and
                           not str(e.get("id", "")).startswith("__")
                           for e in ch.elements)
        recapped = False
        for e in prev_board:
            eid = e["id"]
            if eid in roster:
                continue        # chapter redeclares the id — its version wins
            if incoming_ill and e.get("type") != "illustration":
                # Labels and leader lines are positioned FOR the picture they
                # annotate. Carried at full size onto a new diagram they would
                # sit over it, naming parts no longer on screen — worse than
                # the wipe this replaces. The recap is the picture alone.
                continue
            if incoming_ill and e.get("type") == "illustration":
                if recapped:
                    continue     # one recap only; older ones have had their turn
                recapped = True
            clean = {k: v for k, v in e.items()
                     if k not in ("drawn_layers", "drawn_frac")}
            if incoming_ill and e.get("type") == "illustration":
                clean["at"] = list(_RECAP_AT)
                clean["scale"] = _RECAP_SCALE
                report.append(f"CHAPTER {ch.concept} | RECAP {eid} kept at "
                              f"{_RECAP_SCALE:g}x in the corner")
            # drawn_regions state lives in the tracker; region_order stays on
            # the element (bucket layout must be identical across chapters)
            clean.pop("drawn_regions", None)
            roster[eid] = clean
            if e.get("drawn_layers"):
                drawn_layers[eid] = list(e["drawn_layers"])
                base_frac[eid] = float(e.get("drawn_frac", 0.0))
            elif e.get("drawn_regions") is not None:
                drawn_regions[eid] = list(e["drawn_regions"])
                if e.get("drawn_frac"):
                    region_reach[eid] = float(e["drawn_frac"])
            else:
                introduced.add(eid)

    def expand(tgt) -> list[str]:
        return ([tgt] + _group_children(roster, tgt)) if tgt else []

    for st in ch.steps:
        for a in st.actions:
            if a.get("verb") == "draw" and a.get("target") in roster and \
                    roster[a["target"]].get("type") == "illustration":
                draw_counts[a["target"]] = draw_counts.get(a["target"], 0) + 1

    # ── quality pass: real part geometry ────────────────────────────────────
    # The root asset's prompt names its parts ("name the layer groups exactly:
    # ..."); the renderer resolves those names against vision-annotated
    # regions. Here the compiler (a) re-anchors every arrow whose id names a
    # part onto that part — the model's eyeballed pixel guesses are what made
    # all seven arrows converge on one spot — and (b) schedules the root's
    # draw actions region-by-region in narration order, so the nucleus
    # appears when the nucleus is being said, not at a uniform slice.
    root_id = ills[0] if ills and ills[0] in roster else None
    part_names: list[str] = []
    if root_id:
        try:
            from .raster_assets import part_names_from_prompt
            part_names = part_names_from_prompt(
                str(seg_assets.get(str(roster[root_id].get("asset") or ""), "")))
        except Exception:  # noqa: BLE001 — auto-anchoring is best-effort
            part_names = []
    if root_id and not part_names:
        # the prompt never named its parts, but the chapter's OWN labels and
        # arrows do (lbl_nucleus, arrow_wall, ...) — they are the ground
        # truth of what gets taught, so they become the layer-group tail.
        # Without this, every arrow falls back to eyeballed coordinates.
        cand: list[str] = []
        for eid, e in roster.items():
            if e.get("type") in ("text", "arrow"):
                p = _guess_part_name(eid)
                if p and p not in cand and _norm_name(eid) != _norm_name(p):
                    cand.append(p)
        akey = str(roster[root_id].get("asset") or "")
        prompt0 = ch.assets.get(akey, "")
        if len(cand) >= 2 and prompt0:
            ch.assets[akey] = (prompt0.rstrip().rstrip(".") +
                               ". Name the layer groups exactly: " +
                               ", ".join(cand) + ".")
            seg_assets[akey] = ch.assets[akey]
            assets_seen[akey] = ch.assets[akey]
            part_names = cand
            report.append(f"CHAPTER {ch.concept} | PART NAMES from labels: "
                          f"{cand}")

    def _match_part(name: str) -> str | None:
        if not name or not part_names:
            return None
        from .vector_assets import match_layer_ids
        m = match_layer_ids(part_names, [name])
        if m:
            return m[0]
        # 'cell wall' (from an element id) must find 'cell_wall' (a prompt's
        # layer-group name) — separator style is model whim, never semantics
        import re as _re

        def norm(s: str) -> str:
            return _re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()

        by_norm = {norm(p): p for p in part_names}
        return by_norm.get(norm(name))

    if root_id and part_names:
        root_at = _pt(roster[root_id].get("at")) or [640, 340]
        labels = {eid: e for eid, e in roster.items() if e.get("type") == "text"}

        # parts the narration teaches but nobody labelled: one run declared
        # a full plant cell with NO labels or arrows at all. Synthesize the
        # label (left column, written in the step whose OWN narration first
        # names the part) — the arrow-synthesis pass below then arms it.
        covered = {p for lid in labels
                   for p in [_match_part(_guess_part_name(lid))] if p}
        # Start BELOW whatever already occupies the left column. The semantic
        # adapter lays declared labels out on this same column and pitch, so
        # starting at the top wrote synthesized labels exactly on top of them
        # — a rendered frame showed "Plant Cell" and "Cytoplasm" printed into
        # each other. Read the column off the board instead of assuming it is
        # empty, which holds however the labels got there.
        _used_y = [float(e["at"][1]) for e in labels.values()
                   if isinstance(e.get("at"), (list, tuple)) and len(e["at"]) == 2
                   and abs(float(e["at"][0]) - 95.0) < 60.0]
        _top = (max(_used_y) + 78.0) if _used_y else 140.0
        stack = 0
        for part in part_names:
            if part in covered:
                continue
            target_step = next(
                (s for s in ch.steps
                 if _norm_name(part) in _norm_name(
                     narrations.get(s.segment_id, ""))), None)
            if target_step is None:
                continue
            lid = "lbl_auto_" + _norm_name(part).replace(" ", "_")
            if lid in roster:
                continue
            roster[lid] = {"id": lid, "type": "text",
                           "text": part.replace("_", " ").strip().title(),
                           "at": [95.0, _top + 78.0 * stack],
                           "role": "label", "anchor": "lt"}
            labels[lid] = roster[lid]
            target_step.actions.append({"verb": "write", "target": lid})
            stack += 1
            report.append(f"CHAPTER {ch.concept} | SYNTHESIZED {lid} "
                          f"(narration names {part!r})")
        for eid, e in list(roster.items()):
            if e.get("type") != "arrow":
                continue
            part = _match_part(_guess_part_name(eid))
            if part is None:
                continue
            e = dict(e)
            head = e.get("head")
            if isinstance(head, dict) and isinstance(head.get("el"), str):
                head.setdefault("layer", part)   # keep an explicit anchor
            else:
                e["head"] = {"el": root_id, "layer": part, "edge": "center"}
            tail = e.get("tail")
            if not (isinstance(tail, dict) and isinstance(tail.get("el"), str)):
                lbl = next((lid for lid, le in labels.items()
                            if _match_part(_guess_part_name(lid)) == part
                            or _match_part(str(le.get("text", ""))) == part),
                           None)
                if lbl is not None:
                    at = _pt(labels[lbl].get("at")) or root_at
                    side = "right" if at[0] < root_at[0] else "left"
                    e["tail"] = {"el": lbl, "edge": side,
                                 "dx": 6.0 if side == "right" else -6.0}
            roster[eid] = e
            report.append(f"CHAPTER {ch.concept} | ANCHORED {eid} -> "
                          f"{root_id}.{part}")

        # a labelled part with NO arrow gets one synthesized — a label
        # floating in a margin column teaches nothing; a leader line to the
        # actual structure is the whole point of annotating a diagram
        anchored_parts = {str(e["head"]["layer"]).lower()
                          for e in roster.values()
                          if e.get("type") == "arrow"
                          and isinstance(e.get("head"), dict)
                          and e["head"].get("layer")}
        for lid, le in list(labels.items()):
            part = _match_part(_guess_part_name(lid))
            if part is None or part.lower() in anchored_parts:
                continue
            aid = f"arr_auto_{lid}"
            if aid in roster:
                continue
            write_step = next(
                (st_ for st_ in ch.steps
                 if any(a_.get("verb") == "write" and a_.get("target") == lid
                        for a_ in st_.actions)), None)
            if write_step is None:
                continue        # never point at a label that never appears
            at = _pt(le.get("at")) or root_at
            side = "right" if at[0] < root_at[0] else "left"
            roster[aid] = {"id": aid, "type": "arrow", "width": 3.2,
                           "curve": 0.0,
                           "tail": {"el": lid, "edge": side,
                                    "dx": 6.0 if side == "right" else -6.0},
                           "head": {"el": root_id, "layer": part,
                                    "edge": "center"}}
            write_step.actions.append({"verb": "draw", "target": aid})
            anchored_parts.add(part.lower())
            report.append(f"CHAPTER {ch.concept} | SYNTHESIZED {aid} -> "
                          f"{root_id}.{part}")

    # narration-ordered region schedule: each step that DRAWS the root gets
    # the parts its own labels/arrows introduce, one region per draw action.
    # Parts never assigned stay in the base bucket (visible from the first
    # stroke) — a label can then never point at something still hidden.
    region_sched: list[str] = []
    step_regions: dict[str, list[str]] = {}   # seg_id -> regions for its draws
    if root_id and part_names and draw_counts.get(root_id, 0) > 1:
        already = list(drawn_regions.get(root_id, []))   # from a carry chapter
        written_labels = {a.get("target") for st in ch.steps
                          for a in st.actions if a.get("verb") == "write"}
        drawn_arrows = {a.get("target") for st in ch.steps
                        for a in st.actions if a.get("verb") == "draw"}
        for st in ch.steps:
            n_draws = sum(1 for a in st.actions
                          if a.get("verb") == "draw" and a.get("target") == root_id)
            if not n_draws:
                continue
            parts: list[str] = []
            for a in st.actions:
                if a.get("verb") not in _INTRODUCERS:
                    continue
                tgt = a.get("target")
                if not tgt or tgt == root_id:
                    continue
                p = _match_part(_guess_part_name(str(tgt)))
                if p and p not in region_sched and p not in parts \
                        and p not in already:
                    parts.append(p)
            if not parts:
                # the model declared labels/arrows but never USED them (a
                # whole run once drew the root seven times and wrote nothing)
                # — the step's own narration says which part it teaches, so
                # the declared label and its anchored arrow join this step
                narr = _norm_name(narrations.get(st.segment_id, ""))
                for lid, le in roster.items():
                    if len(parts) >= n_draws:
                        break
                    if le.get("type") != "text" or lid in written_labels:
                        continue
                    p = _match_part(_guess_part_name(lid))
                    if not p or p in region_sched or p in parts \
                            or p in already:
                        continue
                    if _norm_name(p) not in narr:
                        continue
                    st.actions.append({"verb": "write", "target": lid})
                    written_labels.add(lid)
                    arr = next(
                        (aid for aid, ae in roster.items()
                         if ae.get("type") == "arrow" and aid not in drawn_arrows
                         and isinstance(ae.get("head"), dict)
                         and str(ae["head"].get("layer", "")).lower() == p.lower()),
                        None)
                    if arr is not None:
                        st.actions.append({"verb": "draw", "target": arr})
                        drawn_arrows.add(arr)
                    parts.append(p)
                    report.append(f"CHAPTER {ch.concept} | INJECTED "
                                  f"write->{lid}"
                                  + (f" + draw->{arr}" if arr else "")
                                  + f" (narration names {p!r})")
            # NOTE: no "__base" scheduling — the renderer folds unassigned
            # (outline/backdrop) points into the FIRST region's bucket, so
            # the first part's draw includes the outline. A dedicated __base
            # draw once painted scattered leftover specks first (the floaty
            # opening the founder screenshotted).
            assigned = parts[:n_draws]
            step_regions[st.segment_id] = assigned
            region_sched.extend(p for p in assigned if p != "__base")
        if not region_sched:
            # nothing real was scheduled — tagging just "__base" would carry
            # drawn_regions with NO region_order stamped, and the renderer
            # can compute no pre_frac from that (a lesson once went blank
            # after its draws ended). Untagged draws keep plain whole-asset
            # semantics instead.
            step_regions.clear()
        if region_sched:
            # the bucket layout must be IDENTICAL in every scene the root
            # appears in — a carried order is extended, never replaced
            prev_order = list(roster[root_id].get("region_order") or [])
            roster[root_id] = dict(roster[root_id])
            roster[root_id]["region_order"] = prev_order + [
                p for p in region_sched if p not in prev_order]
            report.append(f"CHAPTER {ch.concept} | REGION SCHEDULE "
                          f"{root_id}: {roster[root_id]['region_order']}")

    def carry(eid: str, e: dict) -> dict:
        el = dict(e)
        if e.get("type") == "illustration" and eid not in introduced:
            if eid in drawn_regions:
                # region carry is the sole record for a region-scheduled
                # root — the uniform drawn_frac estimate would fight the
                # renderer's real span-based pre_frac. Bare-draw progress
                # (region_reach) rides along as drawn_frac; the renderer
                # takes the max of both.
                el["drawn_regions"] = list(drawn_regions[eid])
                if region_reach.get(eid):
                    el["drawn_frac"] = region_reach[eid]
            elif eid in drawn_layers:
                el["drawn_layers"] = list(drawn_layers[eid])
                if draw_counts.get(eid) or base_frac.get(eid):
                    bf = base_frac.get(eid, 0.0)
                    n = draw_counts.get(eid, 0)
                    done = draws_done.get(eid, 0)
                    el["drawn_frac"] = round(
                        bf + (done / n) * (1.0 - bf) if n else bf, 4)
        return el

    def board_now() -> list[dict]:
        return [carry(eid, e) for eid, e in roster.items()
                if eid not in erased
                and (eid in introduced or eid in drawn_layers
                     or eid in drawn_regions)]

    # work order: plan steps + HOLD entries for unplanned span segments
    step_by_id = {st.segment_id: st for st in ch.steps}
    if all_segments:
        stepped = [sid for sid in all_segments if sid in step_by_id]
        span = (all_segments[all_segments.index(stepped[0]):
                             all_segments.index(stepped[-1]) + 1]
                if stepped else [])
        work = [(sid, step_by_id.get(sid)) for sid in span
                if sid in step_by_id or sid not in skip_hold]
        known = {w[0] for w in work}
        work += [(st.segment_id, st) for st in ch.steps
                 if st.segment_id not in known]
    else:
        work = [(st.segment_id, st) for st in ch.steps]

    first = True
    for seg_id, st in work:
        if st is None:
            held = board_now()
            if held:
                scenes[seg_id] = {"id": f"vc_{seg_id}", "compiled": True, "scene_type": "process",
                                  "narration": narrations.get(seg_id, ""),
                                  "camera_start": dict(cam),
                                  "elements": held, "actions": []}
                assets_by_seg[seg_id] = dict(seg_assets)
                report.append(f"SEGMENT {seg_id} | chapter: {ch.concept} "
                              f"| decision: HOLD (board persists)")
            continue

        elements: list[dict] = []
        boundary_actions: list[dict] = []

        # chapter boundary: previous board fades out ON SCREEN, renamed so a
        # reused element id can never collide with this chapter's roster
        boundary = first and prev_board and ch.transition == "clear_and_redraw"
        if boundary:
            ren: dict[str, str] = {}
            for e in prev_board:
                nid = f"prev__{e['id']}"
                while nid in roster or nid in ren.values():
                    nid = "_" + nid
                ren[e["id"]] = nid
            for e in prev_board:
                el = dict(e)
                el["id"] = ren[el["id"]]
                if el.get("type") == "group":
                    kids = [ren[c] for c in el.get("children") or [] if c in ren]
                    if not kids:
                        continue
                    el["children"] = kids
                # anchor REFS must rename with their targets — a synthesized
                # arrow once became prev__arr_* while its tail still said
                # label_*, failing schema validation at the boundary scene
                for k in ("tail", "head", "after"):
                    v = el.get(k)
                    if isinstance(v, dict) and v.get("el") in ren:
                        el[k] = {**v, "el": ren[v["el"]]}
                elements.append(el)
            gid = "prev__board"
            while gid in roster or gid in ren.values():
                gid = "_" + gid
            elements.append({"id": gid, "type": "group",
                             "children": [e["id"] for e in elements
                                          if e.get("type") != "group"]})
            boundary_actions = [
                {"verb": "fade", "target": gid, "to": 0.0, "duration": 0.9,
                 "at": {"sec": 0.15}},
                {"verb": "camera_reset", "duration": 0.9, "at": {"sec": 0.15}},
            ]

        # what this step legitimately touches: things on the board, plus
        # things it INTRODUCES itself. Anything else it references is a
        # dangling model reference — dropped, reported, scene stays valid.
        intro_targets: set[str] = set()
        for a in st.actions:
            if a.get("verb") in _INTRODUCERS:
                intro_targets.update(t for t in expand(a.get("target"))
                                     if t in roster)
        on_board = {eid for eid in roster
                    if eid not in erased
                    and (eid in introduced or eid in drawn_layers
                         or eid in drawn_regions)}
        valid = on_board | intro_targets
        kept_actions: list[dict] = []
        for a in st.actions:
            tgt = a.get("target")
            if a["verb"] in _CAMERA or tgt is None or tgt in valid \
                    or (tgt in roster and roster[tgt].get("type") == "group"
                        and set(_group_children(roster, tgt)) & valid):
                kept_actions.append(a)
            else:
                report.append(f"SEGMENT {seg_id} | DROPPED {a['verb']}->{tgt} "
                              f"(not on the board, not introduced this step)")

        for eid, e in roster.items():
            if eid in erased and eid not in intro_targets:
                continue
            relevant = (eid in on_board or eid in intro_targets
                        or any(eid in _group_children(roster, a.get("target"))
                               or a.get("target") == eid
                               for a in kept_actions))
            if not relevant:
                continue
            elements.append(carry(eid, e) if eid in on_board else dict(e))

        # explicit raster slices: segment k resumes where k-1 stopped,
        # offset by any fraction carried in from a previous (carry) chapter
        step_actions: list[dict] = []
        step_done: dict[str, int] = {}
        step_regs = list(step_regions.get(seg_id, []))
        for a in kept_actions:
            a = dict(a)
            if a.get("verb") == "highlight" and a.get("target") == root_id:
                # a marker swipe across a full illustration renders as a
                # yellow bar over the art (founder review) — the root
                # 'speaks' with a subtle pulse instead
                a = {"verb": "pulse", "target": root_id, "times": 2,
                     "duration": 1.2, **({"at": a["at"]} if a.get("at") else {})}
            if a.get("verb") == "draw" and a.get("target") in draw_counts:
                tid = a["target"]
                bf = base_frac.get(tid, 0.0)
                n = draw_counts[tid]
                k = draws_done.get(tid, 0) + step_done.get(tid, 0)
                w = (1.0 - bf) / n
                a["slice"] = (round(bf + k * w, 4), round(w, 4))
                step_done[tid] = step_done.get(tid, 0) + 1
                # narration-scheduled region: the renderer prefers this over
                # the uniform slice whenever the asset has vision regions
                if tid == root_id and step_regs:
                    a["region"] = step_regs.pop(0)
                elif tid == root_id and region_sched:
                    # a bare draw on a region-scheduled root advances by its
                    # uniform slice; the REACH (not "introduced") is what
                    # carries — marking it introduced once fully revealed
                    # every organelle from the chapter's very first stroke
                    lo, w_ = a["slice"]
                    region_reach[tid] = max(region_reach.get(tid, 0.0),
                                            round(lo + w_, 4))
            step_actions.append(a)
        for tid, c in step_done.items():
            draws_done[tid] = draws_done.get(tid, 0) + c

        if not elements:
            # every declared visual of this step was dropped/converted away —
            # an empty scene fails schema validation downstream and once fell
            # all the way to the LEGACY renderer. No scene at all lets the
            # segment take the whiteboard-native fallback instead. Checked
            # BEFORE moment/key_point overlays so a bubble can never prop up
            # an otherwise-empty board.
            report.append(f"SEGMENT {seg_id} | chapter: {ch.concept} | "
                          f"SKIPPED empty scene (whiteboard fallback)")
            first = False
            continue

        narration_here = narrations.get(seg_id, "")
        moment_note = ""
        # STUDENT moments still appear as a set piece (left panel avatar +
        # bubble) — except in conversational style, where the student is
        # already a permanent speaker. Teacher-role moments and key_points
        # are IMPORTANCE MARKERS now: the narration stream (added in
        # compile_plan's final pass) bolds the matching sentence instead of
        # spawning a competing bubble.
        if st.moment and st.moment["role"] == "student" \
                and style != "conversational":
            from .whiteboard import (AVATAR_PROMPTS, human_moment,
                                     snap_to_narration)
            m_text = snap_to_narration(st.moment["text"], narration_here) \
                or st.moment["text"]
            hm_asset = (avatars or {}).get("student", "avatar_student")
            hm_els, hm_acts, _ = human_moment(
                "student", m_text, uid=f"hm_{seg_id}", asset=hm_asset)
            seg_assets = {**seg_assets}
            seg_assets[hm_asset] = AVATAR_PROMPTS.get(
                hm_asset, AVATAR_PROMPTS["avatar_student"])
            elements.extend(hm_els)
            step_actions = step_actions + hm_acts
            moment_note = f" | HUMAN_TEACHING_MOMENT (student: {m_text!r})"
        scenes[seg_id] = {"id": f"vc_{seg_id}", "compiled": True, "scene_type": "process",
                          "narration": narrations.get(seg_id, ""),
                          "camera_start": dict(cam),
                          "elements": elements,
                          "actions": boundary_actions + step_actions}
        assets_by_seg[seg_id] = dict(seg_assets)

        # advance the board with what actually kept
        for a in step_actions:
            v, tgt = a.get("verb"), a.get("target")
            if v in _INTRODUCERS and tgt in roster:
                for t in expand(tgt):
                    erased.discard(t)   # erase -> redraw is legal teaching
                if v == "draw" and roster[tgt].get("type") == "illustration" \
                        and a.get("region"):
                    # region draws carry EXACTLY what was narrated. This
                    # branch must beat `layers`: the model stamps both, and
                    # layer-carry's uniform drawn_frac once under-revealed a
                    # region-reordered trace every segment start (the board
                    # flickered sparse until the next draw caught up)
                    drawn_regions.setdefault(tgt, [])
                    if a["region"] not in drawn_regions[tgt]:
                        drawn_regions[tgt].append(a["region"])
                elif v == "draw" and tgt == root_id and region_sched:
                    # bare draw on a scheduled root: reach-carried, never
                    # 'introduced' (see the slice loop above)
                    drawn_regions.setdefault(tgt, [])
                elif v == "draw" and roster[tgt].get("type") == "illustration" \
                        and a.get("layers"):
                    # layer draws NEVER complete the asset — marker-carry is
                    # the permanent record of exactly what was drawn
                    drawn_layers.setdefault(tgt, [])
                    for l in a["layers"]:
                        if l not in drawn_layers[tgt]:
                            drawn_layers[tgt].append(l)
                else:
                    for t in expand(tgt):
                        introduced.add(t)
                    if v == "draw" and roster[tgt].get("type") == "illustration":
                        drawn_layers.pop(tgt, None)
            elif v == "erase" and tgt:
                for t in expand(tgt):
                    erased.add(t)
                    introduced.discard(t)
                    drawn_layers.pop(t, None)
                    drawn_regions.pop(t, None)
                    region_reach.pop(t, None)
            elif v == "fade" and tgt and float(a.get("to", 0.0)) == 0.0:
                for t in expand(tgt):
                    erased.add(t)
                    introduced.discard(t)
                    drawn_layers.pop(t, None)
                    drawn_regions.pop(t, None)
                    region_reach.pop(t, None)
        if boundary:
            cam = dict(_DEFAULT_CAM)    # the inserted reset ran on screen
        cam = _chain_camera(cam, step_actions, ch.elements)

        report.append(
            f"SEGMENT {seg_id} | chapter: {ch.concept} | decision: "
            f"{'CLEAR_AND_REDRAW' if boundary else st.decision}"
            + (f" | reason: {st.reason}" if st.reason else "")
            + f" | actions: {[a.get('verb') + '->' + str(a.get('target', '')) for a in step_actions]}"
            + moment_note)
        first = False

    return board_now(), cam


def _group_children(roster: dict, tgt) -> list[str]:
    e = roster.get(tgt)
    if isinstance(e, dict) and e.get("type") == "group":
        return [c for c in (e.get("children") or []) if isinstance(c, str)]
    return []


def plan_stats(plan: VisualPlan) -> dict:
    """The §26 acceptance numbers."""
    decisions = [s.decision for c in plan.chapters for s in c.steps]
    return {
        "segments_planned": len(decisions),
        "visual_chapters": len(plan.chapters),
        "root_visuals": sum(1 for d in decisions if d == "NEW_VISUAL"),
        "extensions": sum(1 for d in decisions if d == "EXTEND"),
        "focus_transform": sum(1 for d in decisions if d in ("FOCUS", "TRANSFORM", "CONTINUE")),
        "full_redraws": sum(1 for d in decisions if d == "CLEAR_AND_REDRAW")
                        + max(0, len(plan.chapters) - 1),
        "human_teaching_moments": sum(1 for c in plan.chapters
                                      for s in c.steps if s.moment),
        "teacher_key_points": sum(1 for c in plan.chapters for s in c.steps
                                  if getattr(s, "key_point", None)),
    }
