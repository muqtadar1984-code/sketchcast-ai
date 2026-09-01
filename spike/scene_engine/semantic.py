"""Semantic visual plan -> renderer plan. The migration layer.

The director (Gemini) is good at deciding WHAT should be shown and WHY; it is
bad at deciding WHERE. Every geometry bug this engine has had — arrows
converging on one eyeballed point, labels stacked on each other and on the
avatars, parts revealed before they were narrated — came from asking the model
for coordinates. The semantic contract removes that ask:

    director:  "point at the hypotenuse of the triangle"
    vision:    "the hypotenuse is HERE in the generated image"
    renderer:  "so the arrow leaves the label's right edge and lands there"

This module is the joint between the first line and the other two. It accepts a
SEMANTIC plan —

    {"chapters": [{"id", "concept", "transition": "continue|clear_and_redraw",
                   "assets": {key: prompt}, "semantic_regions": [...],
                   "elements": [{"id", "type", "asset"?, "text"?, "role"?}],
                   "steps": [{"segment", "decision", "reason",
                              "actions": [{"verb", "target": {...}, "cue"}]}]}]}

— and returns a plan in the shape `continuity.parse_visual_plan` already
accepts, with geometry supplied by this engine's own layout systems.

TWO MODES, deliberately:
  strict=True   (dev/CI) — any unresolved verb, target, cue or transition
                raises AdapterError. Silent degradation is how a caption
                track, a whole label column and every leader line have gone
                missing in this project before.
  strict=False  (production) — the same problems are salvaged and returned as
                issues for the validation report. A hostile plan must never
                take a lesson down; it must be visible instead.

NOTHING here changes the existing pipeline: the adapter runs only when a
caller asks for it (SEMANTIC_PLAN=1), and its output goes through exactly the
same parse -> compile -> render path as a hand-written plan.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

SEMANTIC_PLAN_VERSION = 1

# world placement for the pieces the director is no longer allowed to place.
# These are STARTING positions: the renderer's own label layout, arrow routing
# and avatar keep-out run afterwards and have the final say.
_ROOT_AT = [600.0, 380.0]
_TITLE_AT = [640.0, 80.0]
_LABEL_X = 95.0
_LABEL_TOP = 140.0
_LABEL_STEP = 78.0


class AdapterError(Exception):
    """Raised in strict mode when a semantic instruction cannot be honoured."""

    def __init__(self, issues: list[dict]):
        self.issues = issues
        super().__init__("; ".join(f"{i['code']}: {i['detail']}" for i in issues))


# ── vocabulary ──────────────────────────────────────────────────────────────

# semantic verb -> engine verb. ARROW and HUMAN_TEACHING_MOMENT are not engine
# verbs at all: they become an arrow ELEMENT and a step-level moment.
_VERBS = {
    "draw": "draw", "write": "write", "reveal": "reveal",
    "point": "circle", "highlight": "highlight", "circle": "circle",
    "underline": "underline", "zoom": "zoom", "pan": "pan",
    "move": "move", "erase": "erase", "fade": "fade", "pulse": "pulse",
}
_TRANSITIONS = {"continue": "carry", "carry": "carry",
                "clear_and_redraw": "clear_and_redraw"}
_DECISIONS = {"NEW_VISUAL", "EXTEND", "CONTINUE", "FOCUS", "TRANSFORM",
              "CLEAR_AND_REDRAW"}
# element types the engine can place from a semantic description alone
_PLACEABLE = {"illustration", "text", "arrow"}
# types the RENDERER owns entirely — the director may mention them, but the
# engine casts, places and animates avatars and their bubbles itself
_RENDERER_OWNED = {"character", "speech_bubble", "avatar"}


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(s or "").strip().lower()).strip("_")


class _Ctx:
    """Accumulates issues so one pass reports everything, not just the first."""

    def __init__(self, strict: bool):
        self.strict = strict
        self.issues: list[dict] = []

    def note(self, code: str, detail: str) -> None:
        self.issues.append({"code": code, "detail": detail})
        logger.warning("SEMANTIC_PLAN_ADAPTER %s: %s", code, detail)


def adapt_semantic_plan(raw: dict, narrations: dict[str, str] | None = None,
                        strict: bool = False) -> tuple[dict, list[dict]]:
    """Semantic plan -> (plan dict in the engine's shape, issues).

    `narrations` (segment_id -> spoken text) is used to verify that every cue
    is a VERBATIM substring of its own segment, which is the whole basis of
    cue timing. Raises AdapterError in strict mode if anything is unresolved.
    """
    ctx = _Ctx(strict)
    narrations = narrations or {}
    if not isinstance(raw, dict) or not isinstance(raw.get("chapters"), list):
        ctx.note("MALFORMED_PLAN", "no 'chapters' list")
        if strict:
            raise AdapterError(ctx.issues)
        return {"chapters": []}, ctx.issues

    chapters = []
    for ci, craw in enumerate(raw["chapters"]):
        ch = _chapter(craw, ci, narrations, ctx)
        if ch is not None:
            chapters.append(ch)
    if strict and ctx.issues:
        raise AdapterError(ctx.issues)
    return {"chapters": chapters}, ctx.issues


def _chapter(craw, ci: int, narrations: dict, ctx: _Ctx) -> dict | None:
    if not isinstance(craw, dict):
        ctx.note("MALFORMED_CHAPTER", f"chapter {ci} is not an object")
        return None
    concept = str(craw.get("concept") or craw.get("id") or f"chapter_{ci + 1}")

    tr_raw = str(craw.get("transition") or "clear_and_redraw").strip().lower()
    transition = _TRANSITIONS.get(tr_raw)
    if transition is None:
        # "continue" silently becoming clear_and_redraw once wiped a board the
        # director asked to preserve — an unknown value is now reported
        ctx.note("INVALID_TRANSITION",
                 f"{concept}: {tr_raw!r} (expected continue|clear_and_redraw)")
        transition = "clear_and_redraw"

    assets = {str(k): str(v) for k, v in (craw.get("assets") or {}).items()
              if isinstance(v, str)}
    # The director declares WHICH regions matter; the vision annotator needs
    # exactly that list to find their geometry. This is the one place the two
    # halves of the contract meet — v2 forbids asking the IMAGE model for
    # layers, and raster_assets strips the tail before generation anyway.
    regions = [str(r) for r in (craw.get("semantic_regions") or [])
               if isinstance(r, str) and r.strip()]
    if regions and assets:
        root_key = _root_asset_key(craw, assets)
        if root_key and "name the layer groups exactly" not in \
                assets[root_key].lower():
            assets[root_key] = (assets[root_key].rstrip().rstrip(".") +
                                ". Name the layer groups exactly: " +
                                ", ".join(regions) + ".")

    elements, by_id, label_for_region = _elements(craw, ctx, concept)
    if not elements:
        ctx.note("EMPTY_CHAPTER", f"{concept}: no placeable elements")
        return None

    root_id = next((e["id"] for e in elements
                    if e.get("type") == "illustration"), None)
    steps, extra_elements = _steps(craw, narrations, ctx, concept, by_id,
                                   root_id, label_for_region, len(elements))
    elements.extend(extra_elements)
    return {"concept": concept, "transition": transition, "assets": assets,
            "elements": elements, "steps": steps}


def _root_asset_key(craw: dict, assets: dict) -> str | None:
    for e in craw.get("elements") or []:
        if isinstance(e, dict) and e.get("role") == "root_visual" \
                and isinstance(e.get("asset"), str) and e["asset"] in assets:
            return e["asset"]
    for e in craw.get("elements") or []:
        if isinstance(e, dict) and e.get("type") == "illustration" \
                and isinstance(e.get("asset"), str) and e["asset"] in assets:
            return e["asset"]
    return next(iter(assets), None)


def _elements(craw: dict, ctx: _Ctx, concept: str):
    """Semantic elements -> engine elements WITH geometry.

    The director supplies id/type/asset/text/role and nothing spatial; every
    coordinate below comes from this engine, and the renderer's label layout,
    arrow routing and avatar keep-out refine them afterwards.
    """
    out: list[dict] = []
    by_id: dict[str, dict] = {}
    label_for_region: dict[str, str] = {}
    label_i = 0
    for e in craw.get("elements") or []:
        if not isinstance(e, dict) or not isinstance(e.get("id"), str):
            ctx.note("MALFORMED_ELEMENT", f"{concept}: element without an id")
            continue
        eid, etype = e["id"], str(e.get("type") or "").strip().lower()
        if eid.startswith("__"):
            ctx.note("RESERVED_ID",
                     f"{concept}: {eid!r} uses the engine's reserved namespace")
            continue
        if etype in _RENDERER_OWNED:
            # not an error: the engine owns avatars and bubbles. Recorded so
            # the director's intent is traceable, then dropped.
            ctx.note("RENDERER_OWNED_ELEMENT",
                     f"{concept}: {eid!r} ({etype}) is placed by the renderer")
            continue
        if etype not in _PLACEABLE:
            ctx.note("UNSUPPORTED_ELEMENT_TYPE", f"{concept}: {eid!r} type {etype!r}")
            continue
        if etype == "illustration":
            if not isinstance(e.get("asset"), str):
                ctx.note("ILLUSTRATION_WITHOUT_ASSET", f"{concept}: {eid!r}")
                continue
            el = {"id": eid, "type": "illustration", "asset": e["asset"],
                  "at": list(_ROOT_AT), "scale": 1.0}
        elif etype == "text":
            text = str(e.get("text") or "").strip()
            if not text:
                ctx.note("TEXT_WITHOUT_CONTENT", f"{concept}: {eid!r}")
                continue
            role = str(e.get("role") or "label").lower()
            if role == "title":
                el = {"id": eid, "type": "text", "text": text, "role": "title",
                      "size": 42, "at": list(_TITLE_AT), "anchor": "mt"}
            else:
                el = {"id": eid, "type": "text", "text": text, "role": "label",
                      "size": 27, "anchor": "lt",
                      "at": [_LABEL_X, _LABEL_TOP + _LABEL_STEP * label_i]}
                label_i += 1
                label_for_region.setdefault(_slug(text), eid)
                label_for_region.setdefault(_slug(eid), eid)
        else:                                   # arrow declared up-front
            el = None                           # built when an action needs it
            by_id[eid] = {"id": eid, "type": "arrow", "_declared": True}
            continue
        out.append(el)
        by_id[eid] = el
    return out, by_id, label_for_region


def _target(t, ctx: _Ctx, where: str, root_id: str | None):
    """Semantic target -> (element_id, region). Accepts {"element"},
    {"asset","region"}, {"element","region"} and a bare string id."""
    if isinstance(t, str):
        return t, None
    if t is None:
        return None, None          # the CALLER decides whether that is legal
    if not isinstance(t, dict) or not t:
        # an empty or malformed target is not "no target" — it is a target the
        # director meant to specify and didn't. Reported, never swallowed.
        ctx.note("UNRESOLVED_TARGET", f"{where}: empty or malformed target")
        return None, None
    el = t.get("element")
    region = t.get("region")
    if el is None and t.get("asset") is not None:
        # an {asset, region} target names the ROOT visual by its asset; the
        # engine anchors by element id + layer
        el = root_id
        if el is None:
            ctx.note("UNRESOLVED_TARGET",
                     f"{where}: asset target with no illustration on the board")
            return None, None
    if el is None:
        ctx.note("UNRESOLVED_TARGET", f"{where}: target names neither element nor asset")
        return None, None
    return str(el), (str(region) if region else None)


def _steps(craw, narrations, ctx, concept, by_id, root_id, label_for_region,
           n_elements):
    steps: list[dict] = []
    extra: list[dict] = []
    made_arrows: set[str] = set()
    for st in craw.get("steps") or []:
        if not isinstance(st, dict):
            ctx.note("MALFORMED_STEP", f"{concept}: step is not an object")
            continue
        seg = st.get("segment")
        sid = f"s{seg:03d}" if isinstance(seg, int) else str(seg or "")
        narration = narrations.get(sid, "")
        decision = str(st.get("decision") or "CONTINUE").upper()
        if decision not in _DECISIONS:
            ctx.note("INVALID_DECISION", f"{concept}/{sid}: {decision!r}")
            decision = "CONTINUE"
        actions: list[dict] = []
        moment = None
        for a in st.get("actions") or []:
            if not isinstance(a, dict):
                ctx.note("MALFORMED_ACTION", f"{concept}/{sid}: not an object")
                continue
            verb_raw = str(a.get("verb") or "").strip().lower()
            where = f"{concept}/{sid}/{verb_raw or '?'}"
            cue = _cue(a.get("cue"), narration, ctx, where)

            if verb_raw == "human_teaching_moment":
                moment = _moment(a, ctx, where)
                continue
            if verb_raw == "clear_and_redraw":
                # a decision expressed as an action — honour it as the
                # decision rather than dropping it
                decision = "CLEAR_AND_REDRAW"
                continue
            if verb_raw == "transform":
                into = a.get("into")
                if isinstance(into, str):
                    el, _ = _target(a.get("target"), ctx, where, root_id)
                    if el:
                        actions.append(_act("morph", el, cue, into=into))
                        continue
                el, _ = _target(a.get("target"), ctx, where, root_id)
                if el:
                    ctx.note("TRANSFORM_WITHOUT_TARGET_FORM",
                             f"{where}: no 'into' — emphasising instead")
                    actions.append(_act("pulse", el, cue))
                continue
            if verb_raw == "arrow":
                el, region = _target(a.get("target"), ctx, where, root_id)
                if not el:
                    continue
                if not region:
                    ctx.note("ARROW_WITHOUT_REGION",
                             f"{where}: an arrow needs a semantic region to point at")
                    continue
                aid = f"arr_{_slug(region)}"
                if aid not in made_arrows:
                    made_arrows.add(aid)
                    lbl = label_for_region.get(_slug(region))
                    tail = ({"el": lbl, "edge": "right", "dx": 6.0} if lbl
                            else [_LABEL_X + 120.0, _LABEL_TOP])
                    extra.append({"id": aid, "type": "arrow", "width": 3.2,
                                  "curve": 0.0, "tail": tail,
                                  "head": {"el": el, "layer": region,
                                           "edge": "center"}})
                actions.append(_act("draw", aid, cue))
                continue

            verb = _VERBS.get(verb_raw)
            if verb is None:
                ctx.note("UNSUPPORTED_VERB", f"{where}: {verb_raw!r}")
                continue
            el, region = _target(a.get("target"), ctx, where, root_id)
            if el is None and verb not in ("camera_reset",):
                if a.get("target") is None:
                    ctx.note("MISSING_TARGET", f"{where}: {verb} needs a target")
                continue
            act = _act(verb, el, cue)
            if region and verb == "draw":
                # narration-ordered drawing: this part is drawn when it is
                # named, which is what the region schedule exists for
                act["region"] = region
            if verb == "move" and isinstance(a.get("path"), list):
                act["path"] = a["path"]
            elif verb == "move":
                ctx.note("MOVE_WITHOUT_PATH", f"{where}: dropped")
                continue
            actions.append(act)

        step = {"segment": seg if isinstance(seg, int) else sid,
                "decision": decision, "reason": str(st.get("reason") or ""),
                "actions": actions}
        if moment:
            step["moment"] = moment
        if isinstance(st.get("key_point"), str):
            step["key_point"] = st["key_point"]
        steps.append(step)
    return steps, extra


def _act(verb: str, target, cue, **extra) -> dict:
    a: dict = {"verb": verb}
    if target:
        a["target"] = target
    if cue:
        a["at"] = {"phrase": cue}
    a.update(extra)
    return a


def _cue(cue, narration: str, ctx: _Ctx, where: str) -> str | None:
    """A cue must be VERBATIM from its own segment — that is the entire basis
    of cue timing. A paraphrase resolves to nothing and the action silently
    falls back to sequence order, which is exactly how visuals drift off the
    speech."""
    if cue is None:
        return None
    cue = " ".join(str(cue).split())
    if not cue:
        return None
    if narration and cue.lower() not in narration.lower():
        ctx.note("CUE_NOT_IN_NARRATION", f"{where}: {cue!r}")
        return None
    return cue


def _moment(a: dict, ctx: _Ctx, where: str) -> dict | None:
    role = str(a.get("role") or a.get("who") or "student").lower()
    if role not in ("student", "teacher"):
        role = "student"
    text = str(a.get("line") or a.get("text") or a.get("purpose") or "").strip()
    if not text:
        ctx.note("MOMENT_WITHOUT_LINE", where)
        return None
    return {"role": role, "text": text[:60]}
