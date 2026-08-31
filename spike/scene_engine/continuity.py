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
                prev_board, assets_seen, cam, scenes, assets_by_seg, report)
        except Exception:
            logger.exception("chapter %r failed to compile; skipping it", ch.concept)
            report.append(f"CHAPTER {ch.concept} | COMPILE FAILED — skipped, "
                          f"board carried unchanged")
    return scenes, assets_by_seg, report


def _compile_chapter(ch: VisualChapter, narrations, all_segments, skip_hold,
                     prev_board, assets_seen, cam, scenes, assets_by_seg,
                     report):
    roster = {e["id"]: e for e in ch.elements}
    # ONE root visual per chapter, enforced: independently generated images do
    # not compose — a model that declares an illustration per organelle gets
    # its first kept and the rest dropped (their actions degrade to holds via
    # the dangling-reference filter), never a stacked tangle on screen
    ills = [eid for eid, e in roster.items() if e.get("type") == "illustration"]
    if len(ills) > 1:
        # the ROOT is the illustration the plan DRAWS the most — a model that
        # declares four visuals but lavishes 8 draw actions on one has told us
        # which visual the chapter is actually about
        draws = {eid: 0 for eid in ills}
        for st_ in ch.steps:
            for a_ in st_.actions:
                if a_.get("verb") == "draw" and a_.get("target") in draws:
                    draws[a_["target"]] += 1
        ills.sort(key=lambda eid: -draws[eid])
    for extra in ills[1:]:
        del roster[extra]
        report.append(f"CHAPTER {ch.concept} | DROPPED illustration {extra!r} "
                      f"(one root visual per chapter — use named layers on "
                      f"{ills[0]!r} instead)")
    introduced: set[str] = set()
    drawn_layers: dict[str, list[str]] = {}
    base_frac: dict[str, float] = {}      # raster progress carried from before
    draw_counts: dict[str, int] = {}
    draws_done: dict[str, int] = {}
    erased: set[str] = set()

    assets_seen.update(ch.assets)
    seg_assets = dict(assets_seen)        # cumulative: carried boards resolve too

    # a carry chapter continues on the same board: previous elements join the
    # roster with their state imported, so EVERY step keeps them
    if prev_board and ch.transition == "carry":
        for e in prev_board:
            eid = e["id"]
            if eid in roster:
                continue        # chapter redeclares the id — its version wins
            clean = {k: v for k, v in e.items()
                     if k not in ("drawn_layers", "drawn_frac")}
            roster[eid] = clean
            if e.get("drawn_layers"):
                drawn_layers[eid] = list(e["drawn_layers"])
                base_frac[eid] = float(e.get("drawn_frac", 0.0))
            else:
                introduced.add(eid)

    def expand(tgt) -> list[str]:
        return ([tgt] + _group_children(roster, tgt)) if tgt else []

    for st in ch.steps:
        for a in st.actions:
            if a.get("verb") == "draw" and a.get("target") in roster and \
                    roster[a["target"]].get("type") == "illustration":
                draw_counts[a["target"]] = draw_counts.get(a["target"], 0) + 1

    def carry(eid: str, e: dict) -> dict:
        el = dict(e)
        if e.get("type") == "illustration" and eid in drawn_layers \
                and eid not in introduced:
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
                and (eid in introduced or eid in drawn_layers)]

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
                    and (eid in introduced or eid in drawn_layers)}
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
        for a in kept_actions:
            a = dict(a)
            if a.get("verb") == "draw" and a.get("target") in draw_counts:
                tid = a["target"]
                bf = base_frac.get(tid, 0.0)
                n = draw_counts[tid]
                k = draws_done.get(tid, 0) + step_done.get(tid, 0)
                w = (1.0 - bf) / n
                a["slice"] = (round(bf + k * w, 4), round(w, 4))
                step_done[tid] = step_done.get(tid, 0) + 1
            step_actions.append(a)
        for tid, c in step_done.items():
            draws_done[tid] = draws_done.get(tid, 0) + c

        moment_note = ""
        if st.moment:
            from .whiteboard import human_moment
            hm_els, hm_acts, hm_asset = human_moment(
                st.moment["role"], st.moment["text"], uid=f"hm_{seg_id}")
            elements.extend(hm_els)
            step_actions = step_actions + hm_acts
            seg_assets = {**seg_assets}
            from .whiteboard import AVATAR_PROMPTS
            seg_assets[hm_asset] = AVATAR_PROMPTS[hm_asset]
            moment_note = f" | HUMAN_TEACHING_MOMENT ({st.moment['role']}: "                           f"{st.moment['text']!r})"

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
            elif v == "fade" and tgt and float(a.get("to", 0.0)) == 0.0:
                for t in expand(tgt):
                    erased.add(t)
                    introduced.discard(t)
                    drawn_layers.pop(t, None)
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
    }
