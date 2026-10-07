"""Figure-bearing questions for a worksheet or test paper.

One model call asks for ``geometry.figure.v1`` records on the topic; each
comes back as model output and is therefore hostile: it is normalised from
the closed reply shape, verified by the chain (maths.geometry.verify) and
only a VERIFIED question is rendered — under the policy its role calls
for — and printed. A question the engine refuses is logged with the code
and the reason, and never reaches the page (fail over degrade).

The reply schema is closed on purpose (every property named, every value
a string the engine parses): Vertex constrains decoding to it, so a bent
shape is impossible rather than repairable; a schema the API dislikes
falls back to unconstrained JSON inside the client. Construction
parameters vary by construction, so the construction object lists every
parameter any construction takes, all optional; empties are stripped
before parsing.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

from maths.geometry.constructions import CONSTRUCTIONS, exact_value
from maths.geometry.errors import GeometryRefusal
from maths.geometry.properties import PROPERTIES
from maths.geometry.realise import realise
from maths.geometry.render_static import render_figure
from maths.geometry.spec import SCHEMA_VERSION, parse_question
from maths.geometry.theorems import REASONS, THEOREMS
from maths.geometry.verify import QuestionReport, verify_question
from maths.pretty import pretty

logger = logging.getLogger("worker")

MAX_TOKENS = 16000
MAX_PER_CALL = 8


def strict_schema() -> bool:
    """Constrained decoding for the geometry call. Measured 2026-10-07:
    with every construction parameter optional, gemini-3.5-flash under
    constrained decoding OMITTED the parameters (`sides` is required ×8 in
    one probe) while the Lite emitted them; unconstrained JSON with the
    prompt's examples is the default until a schema the API constrains
    WELL exists. GEOMETRY_STRICT_SCHEMA=1 switches it on without a deploy."""
    return (os.getenv("GEOMETRY_STRICT_SCHEMA") or "0").strip().lower() in ("1", "true", "yes", "on")

# ── the closed reply schema ───────────────────────────────────────────────

_S = {"type": "string"}
_I = {"type": "integer"}
_N = {"type": "number"}
_B = {"type": "boolean"}


def _arr(item: dict) -> dict:
    return {"type": "array", "items": item}


def _obj(props: dict, required: tuple = ()) -> dict:
    return {"type": "object", "properties": props, "required": list(required)}


def _enum(*values: str) -> dict:
    return {"type": "string", "enum": list(values)}


_PAIR = _arr(_S)
_CONSTRUCTION = _obj({
    "make": _enum(*sorted(CONSTRUCTIONS)), "id": _S, "vertices": _arr(_S), "points": _arr(_S),
    "sides": _arr(_S), "angles": _arr(_S), "angle": _S, "side": _S, "legs": _arr(_S), "base": _S,
    "apex_angle": _S, "base_angle": _S, "hyp": _S, "width": _S, "height": _S, "parallel_sides": _arr(_S),
    "offset": _S, "n": _I, "turns": _arr(_S), "radius": _S, "centre": _S, "circle": _S, "vertex": _S,
    "from_ray": _arr(_S), "to": _S, "length": _S, "segment": _arr(_S), "beyond": _S, "ratio": _S,
    "of": _arr(_S), "line": _S, "point": _S, "lines": _arr(_S), "gap": _S, "hidden": _B,
    "rows": _I, "cols": _I, "cells": _arr(_arr(_S)), "palette": _arr(_S), "through": _S,
}, ("make",))
_ANGLE = _obj({"id": _S, "rays": _arr(_arr(_S)), "region": _enum("interior", "reflex")}, ("id", "rays"))
_SEGMENT = _obj({"id": _S, "points": _arr(_S)}, ("id", "points"))
_MEASURE = _obj({"target": _S, "value": _S, "unit": _enum("deg", "cm", "mm", "m", "units"),
                 "role": _enum("given", "unknown", "derived")}, ("target", "value", "role"))
_RELATION = _obj({
    "id": _S, "kind": _enum("collinear", "parallel", "perpendicular", "equal_length", "equal_angle", "midpoint",
                            "on_segment", "on_circle", "right_angle", "angle_value", "length_value"),
    "given": _B, "points": _arr(_S), "lines": _arr(_S), "segments": _arr(_arr(_S)), "angles": _arr(_S),
    "point": _S, "segment": _arr(_S), "circle": _S, "angle": _S, "value": _S,
}, ("kind",))
_MARK = _obj({
    "kind": _enum("angle_arc", "right_angle_square", "equal_ticks", "parallel_arrows", "point_dot", "construction_arc"),
    "target": _S, "segments": _arr(_arr(_S)), "lines": _arr(_S), "centre": _S, "through": _S, "count": _I,
}, ("kind",))
_FIGURE = _obj({
    "units": _enum("cm", "mm", "m", "units"), "orientation": _N,
    "bind": _arr(_obj({"name": _S, "value": _S}, ("name", "value"))),
    "points": _arr(_obj({"id": _S, "label": _S}, ("id",))),
    "objects": _arr(_CONSTRUCTION), "angles": _arr(_ANGLE), "segments": _arr(_SEGMENT),
    "measures": _arr(_MEASURE), "relations": _arr(_RELATION), "marks": _arr(_MARK),
}, ("objects",))
_ASKS = _obj({"property": _enum("none", *sorted(PROPERTIES)), "over": _arr(_S), "select": _S, "fill": _S,
              "equals": _I, "greater_than": _I, "less_than": _I}, ("property",))
_ANSWER = _obj({
    "kind": _enum("number", "values", "label_set", "label_map", "value_set"),
    "value": _S, "unit": _S,
    "values": _arr(_obj({"name": _S, "value": _S}, ("name", "value"))),
    "labels": _arr(_S),
    "map": _arr(_obj({"label": _S, "value": _S}, ("label", "value"))),
}, ("kind",))
_STEP = _obj({
    "kind": _enum("deduce", "transform", "check", "setup"), "theorem": _enum(*sorted(THEOREMS)),
    "uses": _arr(_S), "before": _arr(_S), "after": _arr(_S), "speech": _S,
    "figure_ops": _arr(_obj({"op": _S, "target": _S}, ("op", "target"))),
}, ("kind",))
_QUESTION = _obj({
    "id": _S, "difficulty": _I, "figure_role": _enum("evidence", "reasoning", "illustration"), "prompt": _S,
    "figures": _arr(_obj({"id": _S, "label": _S, "figure": _FIGURE}, ("id", "figure"))),
    "asks": _ASKS, "parts": _arr(_obj({"asks": _ASKS, "answer": _ANSWER}, ("asks", "answer"))),
    "steps": _arr(_STEP), "answer": _ANSWER,
}, ("id", "difficulty", "figure_role", "prompt", "figures", "asks", "answer"))
# `answer` is REQUIRED: the first live probe (gemini-3.5-flash-lite,
# 2026-10-07) left it out of every evidence question, and a question whose
# answer the engine computes still needs the model's claim to check against.
GEOMETRY_SET_SCHEMA = _obj({"questions": _arr(_QUESTION)}, ("questions",))


# ── normalisation: the reply shape -> the spec shape ─────────────────────

def _strip(d: dict) -> dict:
    """Drop the empties a closed schema makes a model emit ("" / [] / null)."""
    out = {}
    for k, v in d.items():
        if v is None or v == "" or v == [] or v == {}:
            continue
        out[k] = v
    return out


_ROLES = ("given", "unknown", "derived")


def _measure(m: dict) -> dict:
    """A measure with its role folded into the enum: a model's 'find',
    'required' or 'target' is an unknown when the value carries a symbol,
    a given otherwise (the enum is the engine's; the engine checks both)."""
    out = _strip(dict(m))
    role = str(out.get("role") or "").strip().lower()
    if role not in _ROLES:
        try:
            syms = exact_value(out.get("value"), where="measure").free_symbols
        except GeometryRefusal:
            syms = set()
        out["role"] = "unknown" if syms else "given"
    return out


def _answer(a: Optional[dict], figure_labels: Optional[list[str]] = None) -> Optional[dict]:
    if not isinstance(a, dict) or not a.get("kind"):
        return None
    kind = a["kind"]
    out: dict[str, Any] = {"kind": kind}
    if a.get("unit"):
        out["unit"] = a["unit"]
    # unconstrained JSON arrives in its natural shapes (a dict in `value`, a
    # list of labels); the closed schema's list-of-pairs shapes are mapped too
    value = a.get("value")
    if kind == "number":
        out["value"] = "" if value is None else (str(value) if not isinstance(value, (dict, list)) else "")
    elif kind == "values":
        if isinstance(value, dict):
            out["value"] = {str(k): str(v) for k, v in value.items()}
        else:
            out["value"] = {x["name"]: x["value"] for x in (a.get("values") or []) if isinstance(x, dict)}
            if not out["value"] and value not in (None, ""):
                out["value"] = {"x": str(value)}
    elif kind in ("label_set", "value_set"):
        labels = a.get("labels") or []
        if not labels and isinstance(value, list):
            labels = [str(v) for v in value]
        elif not labels and value not in (None, ""):
            labels = [s.strip() for s in str(value).replace(";", ",").split(",") if s.strip()]
        out["value"] = labels
    elif kind == "label_map":
        mp = a.get("map")
        if isinstance(value, dict):
            out["value"] = {str(k): str(v) for k, v in value.items()}
        elif isinstance(mp, dict):
            out["value"] = {str(k): str(v) for k, v in mp.items()}
        else:
            out["value"] = {x["label"]: x["value"] for x in (mp or []) if isinstance(x, dict)}
        if not out["value"] and figure_labels:
            # the values listed in figure order, no labels: zipped with the figures
            vals = a.get("labels") or [s.strip() for s in str(a.get("value") or "").replace(";", ",").split(",") if s.strip()]
            if len(vals) == len(figure_labels):
                out["value"] = dict(zip(figure_labels, vals))
    return out


def _asks(a: Optional[dict], figure_ids: list[str]) -> Optional[dict]:
    if not isinstance(a, dict) or not a.get("property") or a.get("property") == "none":
        return None
    out = _strip(dict(a))
    if not out.get("over"):
        out["over"] = list(figure_ids)   # asked of every figure unless said otherwise
    sel = out.get("select")
    if isinstance(sel, str) and sel.strip().lower() in ("true", "false"):
        out["select"] = sel.strip().lower() == "true"
    return out


def normalise_question(raw: dict) -> tuple[dict, int]:
    """(spec-shaped question, difficulty). The reply's list-shaped binds,
    answers and maps become the spec's dicts; empties go."""
    q = dict(raw or {})
    difficulty = q.pop("difficulty", 2)
    try:
        difficulty = max(1, min(4, int(difficulty)))
    except (TypeError, ValueError):
        difficulty = 2
    figures = []
    for ref in q.get("figures") or []:
        if not isinstance(ref, dict) or not isinstance(ref.get("figure"), dict):
            continue
        fig = dict(ref["figure"])
        bind = fig.get("bind")
        if isinstance(bind, list):
            fig["bind"] = {b["name"]: b["value"] for b in bind if isinstance(b, dict) and b.get("name")}
        fig["points"] = [_strip(p) for p in (fig.get("points") or []) if isinstance(p, dict)]
        fig["objects"] = [_strip(o) for o in (fig.get("objects") or []) if isinstance(o, dict)]
        fig["angles"] = [_strip(a) for a in (fig.get("angles") or []) if isinstance(a, dict)]
        fig["segments"] = [_strip(s) for s in (fig.get("segments") or []) if isinstance(s, dict)]
        fig["measures"] = [_measure(m) for m in (fig.get("measures") or []) if isinstance(m, dict)]
        fig["relations"] = [_strip(r) for r in (fig.get("relations") or []) if isinstance(r, dict)]
        fig["marks"] = [_strip(m) for m in (fig.get("marks") or []) if isinstance(m, dict)]
        fig = _strip(fig)
        if not fig.get("objects"):
            # a padding entry (a closed schema invites them): no construction,
            # no figure — dropped, so it cannot sink the question
            continue
        entry = {"id": ref.get("id") or f"fig_{len(figures) + 1}", "figure": fig}
        if ref.get("label"):
            entry["label"] = ref["label"]
        figures.append(entry)
    out: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "id": str(q.get("id") or "q"),
        "figure_role": q.get("figure_role") or "reasoning",
        "prompt": str(q.get("prompt") or "")[:600],
        "figures": figures,
    }
    fig_ids = [f["id"] for f in figures]
    fig_labels = [f.get("label") or f["id"] for f in figures]
    asks = _asks(q.get("asks"), fig_ids)
    if asks:
        out["asks"] = asks
    parts = []
    for p in q.get("parts") or []:
        if isinstance(p, dict):
            pa, an = _asks(p.get("asks"), fig_ids), _answer(p.get("answer"), fig_labels)
            if pa and an:
                parts.append({"asks": pa, "answer": an})
    if parts:
        out["parts"] = parts
    steps = []
    for st in q.get("steps") or []:
        if not isinstance(st, dict) or not st.get("kind"):
            continue
        s = _strip(dict(st))
        if "before" in s and not s["before"]:
            del s["before"]
        steps.append(s)
    # an evidence question is answered by the property the engine computes;
    # steps a model attaches to it are commentary, not a chain to prove
    if steps and not (asks or parts):
        out["steps"] = steps
    ans = _answer(q.get("answer"), fig_labels)
    if ans:
        out["answer"] = ans
    _bind_from_answer(out)
    return out, difficulty


def _bind_from_answer(q: dict) -> None:
    """A figure whose givens are in x but names no bind draws with the
    answer's x — which the chain must then prove (bind_mismatch otherwise)."""
    ans = q.get("answer") or {}
    stated: dict[str, str] = {}
    if ans.get("kind") == "number" and ans.get("value") not in (None, ""):
        stated["*"] = str(ans["value"])
    elif ans.get("kind") == "values" and isinstance(ans.get("value"), dict):
        stated = {k: str(v) for k, v in ans["value"].items()}
    if not stated:
        return
    for ref in q.get("figures") or []:
        fig = ref["figure"]
        bind = dict(fig.get("bind") or {})
        free: set[str] = set()
        for ms in fig.get("measures") or []:
            try:
                free |= {str(s) for s in exact_value(ms.get("value"), where="measure").free_symbols}
            except GeometryRefusal:
                continue
        missing = sorted(s for s in free if s not in bind)
        for s in missing:
            if s in stated:
                bind[s] = stated[s]
            elif "*" in stated and len(missing) == 1:
                bind[s] = stated["*"]
        if bind:
            fig["bind"] = bind


# ── the prompt ────────────────────────────────────────────────────────────

_SYSTEM = ("You are an experienced mathematics teacher writing geometry questions with diagrams for a school "
           "worksheet. You describe each diagram as a CONSTRUCTION — what to build, from which measurements — "
           "never as coordinates. A deterministic engine builds, checks and draws it; a diagram it cannot build "
           "or a claim it cannot prove is thrown away, so write only what the construction library can make.")

_SIGNATURES = {
    "line_through": "points:[p,q,r...] (collinear, in order)",
    "ray_at_angle": "vertex, from_ray:[vertex,p], angle:<angle id with a measure>, side:left|right, to:<new point>",
    "extend": "segment:[a,b], beyond:b, to:<new point> (a-b-to collinear)",
    "angles_at_point": "vertex, points:[a,b,c...] round the vertex; consecutive angles need measures (all but one)",
    "parallel_through": "point, line:<line id>, points:[...] on the new line",
    "perpendicular_through": "point (on the line), line, points:[...]",
    "parallels_transversal": "points:[a,b,c,d,e,f,p,q] — line a-p-b above, c-q-d below, transversal e-p-q-f; angle:<one of the 8 angles, with a measure>",
    "intersection": "id:<new point>, of:[ray or line id, ray or line id]",
    "midpoint": "id:<new point>, segment:[a,b]",
    "angle_bisector": "angle:<angle id>, to:<new point>",
    "triangle_sss": "vertices:[a,b,c], sides:[AB,BC,CA]",
    "triangle_sas": "vertices, sides:[AB,BC], angle:<at B>",
    "triangle_asa": "vertices, angles:[at A, at B], side:AB",
    "triangle_aas": "vertices, angles:[at A, at B], side:BC",
    "triangle_rhs": "vertices, right angle at B: hyp:AC + side:AB, or legs:[AB,BC]",
    "triangle_isosceles": "vertices (apex first), legs, and one of base | apex_angle | base_angle",
    "triangle_equilateral": "vertices, side",
    "square": "vertices, side", "rectangle": "vertices, width, height",
    "parallelogram": "vertices, sides:[AB,BC], angle:<at B>", "rhombus": "vertices, side, angle",
    "trapezium": "vertices, parallel_sides:[AB,DC], height, offset", "kite": "vertices, sides:[AB,BC], angle:<at B>",
    "regular_polygon": "n, side", "turtle_polygon": "sides:[...], turns:[...] (left turns positive; must close)",
    "polyline_open": "sides, turns (an open path — not a polygon)",
    "quadrilateral_by_angles": "vertices, angles:[A,B,C,D] (sum 360), sides:[AB,BC]",
    "circle": "id, centre:<point>, radius", "point_on_circle": "id, circle, angle", "chord": "points:[a,b], circle",
    "grid_pattern": "rows, cols, cells:[[colour letters, '*' for the blank]], palette:[letters]",
}

_EXAMPLE_REASONING = (
    '{"id":"q1","difficulty":2,"figure_role":"reasoning","prompt":"ABC is a straight line. Find x.",'
    '"figures":[{"id":"fig","figure":{"points":[{"id":"p_a","label":"A"},{"id":"p_b","label":"B"},{"id":"p_c","label":"C"},{"id":"p_d","label":"D"}],'
    '"objects":[{"id":"l_abc","make":"line_through","points":["p_a","p_b","p_c"]},'
    '{"id":"r_bd","make":"ray_at_angle","vertex":"p_b","from_ray":["p_b","p_a"],"angle":"angle_abd","side":"left","to":"p_d"}],'
    '"angles":[{"id":"angle_abd","rays":[["p_b","p_a"],["p_b","p_d"]]},{"id":"angle_dbc","rays":[["p_b","p_d"],["p_b","p_c"]]}],'
    '"measures":[{"target":"angle_abd","value":"70","unit":"deg","role":"given"},{"target":"angle_dbc","value":"x","unit":"deg","role":"unknown"}],'
    '"relations":[{"id":"r1","kind":"collinear","points":["p_a","p_b","p_c"],"given":true}]}}],'
    '"steps":[{"kind":"deduce","theorem":"angles_on_line","uses":["r1","angle_abd","angle_dbc"],"after":["70 + x = 180"]},'
    '{"kind":"transform","after":["x = 110"]}],"answer":{"kind":"number","value":"110","unit":"deg"}}'
)
_EXAMPLE_EVIDENCE = (
    '{"id":"q2","difficulty":1,"figure_role":"evidence","prompt":"Which of these triangles are isosceles?",'
    '"figures":[{"id":"fig_a","label":"A","figure":{"units":"cm","objects":[{"id":"t","make":"triangle_sss","sides":["4","5","6"]}]}},'
    '{"id":"fig_b","label":"B","figure":{"units":"cm","objects":[{"id":"t","make":"triangle_isosceles","legs":"5","base":"3"}],"orientation":30}},'
    '{"id":"fig_c","label":"C","figure":{"units":"cm","objects":[{"id":"t","make":"triangle_rhs","legs":["3","3"]}]}}],'
    '"asks":{"property":"triangle_class_by_sides","over":["fig_a","fig_b","fig_c"],"select":"isosceles"},'
    '"answer":{"kind":"label_set","labels":["B","C"]}}'
)


def geometry_prompt(*, topic: str, level: Optional[str], language: str, n: int, chapter_context: str,
                    kind: str) -> str:
    sigs = "\n".join(f"  - {name}: {sig}" for name, sig in _SIGNATURES.items())
    theorems = "\n".join(f"  - {t}: {REASONS[t]}" for t in THEOREMS)
    ctx = [f"TOPIC: {topic}", f"LEARNER LEVEL: {level or 'school'}", f"LANGUAGE of the question text: {language or 'en'}",
           f"DOCUMENT: {'a practice worksheet' if kind == 'worksheet' else 'a test paper'}"]
    if chapter_context:
        ctx.append(chapter_context[:6000])
    return "\n\n".join([
        "\n".join(ctx),
        f"Write {n} geometry questions on this topic, each with its diagram described as a construction in the "
        "geometry.figure.v1 format. Two kinds:\n"
        "  * figure_role 'evidence' — the student reads the answer FROM the diagram (classify these triangles, "
        "count the right angles, how many lines of symmetry, which are polygons). Give several labelled figures "
        "(A, B, C...) in 'figures' and ask a PROPERTY in 'asks' ('select' picks the figures with that value; "
        "no 'select' asks the value of each). Answer kinds: label_set (labels), label_map (map), number (value).\n"
        "  * figure_role 'reasoning' — the student must DEDUCE an unknown (find x). One figure; every angle the "
        "working uses is in 'angles' (id 'angle_<name>', two rays [vertex, point]); 'measures' give the values "
        "('given') and name the unknown ('unknown', value 'x'); 'steps' prove it: a 'deduce' step cites one "
        "theorem id and the angle ids it applies to, 'after' is the equation the theorem gives (write an angle's "
        "measure as ang(abc)); 'transform' steps do the algebra; the 'answer' is the proved value. If a given is an "
        "expression in x (e.g. '2x + 10'), put x's value in the figure's 'bind'.",
        "Point ids 'p_a', labels 'A'. Every measurement is a STRING ('70', '2x + 10', '6'). Angles in degrees, "
        "lengths in the figure's 'units' (cm for anything a student measures). Property values are English ids "
        "(isosceles, scalene, equilateral, acute, right, obtuse, true, false) even when the question text is not. "
        "An evidence question has ONE shape per figure (three triangles = three figures) and always fills 'asks'; a "
        "reasoning question sets asks.property to 'none'. Every construction carries its parameters as listed "
        "below — a triangle_sss without 'sides' or a ray_at_angle without 'angle' is thrown away.",
        "CONSTRUCTIONS (the only ones that exist; parameters as listed):\n" + sigs,
        "THEOREMS a deduce step may cite (the only reasons that exist):\n" + theorems,
        "PROPERTIES an evidence question may ask: " + ", ".join(sorted(PROPERTIES)) + ".",
        "Rules the engine enforces: a construction never takes a coordinate; a triangle from two sides and a "
        "non-included angle is refused; an evidence question never marks the property it asks (no right-angle "
        "squares on a count-the-right-angles question); sides meant to differ differ by at least 0.5 cm; a "
        "question with no diagram does not belong here. Difficulty 1-2 for evidence and one-theorem reasoning, "
        "3-4 for two or more theorems or algebra in x.",
        "Every GIVEN must be realised by the construction: a find-x triangle is built FROM its given angles "
        "(triangle_asa / triangle_aas / triangle_isosceles with apex_angle), never from side lengths with angles "
        "declared on top. An isosceles triangle's 'legs' is ONE number ('5'). Every question states its answer: "
        "an evidence question's answer is what the figures show (the engine recomputes it and checks), a "
        "reasoning question's answer is the value its steps prove.",
        "EXAMPLES (one of each kind):\n" + _EXAMPLE_REASONING + "\n" + _EXAMPLE_EVIDENCE,
        "=== OUTPUT ===\nReturn ONLY one minified JSON object: {\"questions\": [ ... ]}. Unused fields are empty "
        "strings or empty lists.",
    ])


# ── the items ─────────────────────────────────────────────────────────────

@dataclass
class FigureImage:
    label: Optional[str]
    png: bytes
    width_mm: float
    height_mm: float
    true_scale: bool


@dataclass
class GeometryItem:
    id: str
    prompt: str
    role: str
    difficulty: int
    spec: dict
    report: QuestionReport
    images: list[FigureImage] = field(default_factory=list)
    key_images: list[FigureImage] = field(default_factory=list)   # the metric figure of a reasoning question

    @property
    def marks(self) -> int:
        if self.role == "reasoning":
            return 3 + (1 if len(self.spec.get("steps") or []) >= 3 else 0)
        return max(2, len(self.spec.get("parts") or []) * 2)

    @property
    def lines(self) -> int:
        return 5 if self.role == "reasoning" else 2


def _policy_for(role: str) -> str:
    return "assessment_schematic" if role == "reasoning" else "instructional_metric"


def render_item(item: GeometryItem, *, note: Optional[str] = None) -> None:
    """The item's figures as print-ready PNGs: schematic for a reasoning
    figure the student answers from, true-scale metric for evidence; the
    metric reasoning figure goes to the answer key."""
    q = parse_question(item.spec)
    for ref in q.figures:
        policy = _policy_for(q.figure_role)
        metric = item.report.models[ref.id]
        m = realise(ref.figure, policy, metric=metric)
        r = render_figure(m, ref.figure, role=q.figure_role, policy=policy, note=note)
        item.images.append(FigureImage(ref.label, r.png, r.width_mm, r.height_mm, r.true_scale))
        if policy == "assessment_schematic":
            rk = render_figure(metric, ref.figure, role=q.figure_role, policy="instructional_metric")
            item.key_images.append(FigureImage(ref.label, rk.png, rk.width_mm, rk.height_mm, rk.true_scale))


def geometry_items(client, *, topic: str, level: Optional[str], language: str, n: int,
                   chapter_context: str = "", kind: str = "worksheet",
                   note: Optional[str] = None, rounds: int = 2) -> tuple[list[GeometryItem], dict]:
    """Up to ``n`` verified, rendered figure questions; one model call,
    plus one repair round carrying the refusals back when short.
    ``note`` is the "Not drawn to scale" text in the document's language."""
    if n <= 0:
        return [], {"asked": 0, "verified": 0, "rejected": []}
    kept: list[GeometryItem] = []
    rejected: list[str] = []
    seen_prompts: set[str] = set()
    asked = 0
    base_prompt = geometry_prompt(topic=topic, level=level, language=language, n=min(MAX_PER_CALL, n + 1),
                                  chapter_context=chapter_context, kind=kind)
    for round_ in range(rounds):
        if round_ and (len(kept) >= n or not rejected):
            break
        prompt = base_prompt
        if round_:
            # the repair round: the refusals, with their reasons, go back —
            # the same loop the algebra ladder runs
            prompt += ("\n\nTHE ENGINE REFUSED THESE LAST TIME (fix the fault or replace the question; "
                       f"write {min(MAX_PER_CALL, n - len(kept) + 1)} questions):\n"
                       + "\n".join(f"  - {r}" for r in rejected[-MAX_PER_CALL:]))
        result = client.analyze(prompt=prompt, system=_SYSTEM, max_tokens=MAX_TOKENS,
                                response_schema=GEOMETRY_SET_SCHEMA, strict_schema=strict_schema())
        if result.get("truncated"):
            logger.warning("geometry questions for %r: the reply was cut off; nothing parsed from it is complete", topic)
            rejected.append("the model's reply was cut off at the output cap")
            continue
        data = result.get("data", result)
        raw = data.get("questions") if isinstance(data, dict) else None
        if raw is None:
            # unconstrained JSON can arrive malformed beyond repair (the Lite,
            # 2026-10-07): the call is lost, the repair round asks again. An
            # explicit empty list is an answer — the chapter has no diagrams.
            logger.warning("geometry questions for %r: the reply carried no questions (malformed or empty)", topic)
            rejected.append("the reply carried no questions — it was malformed or empty; write them again")
            continue
        _take(raw, n, kept, rejected, seen_prompts, note, asked)
        asked += len(raw)
    kept.sort(key=lambda it: it.difficulty)
    logger.info("geometry questions for %r: %d returned, %d verified and drawn, %d rejected", topic,
                asked, len(kept), len(rejected))
    return kept, {"asked": asked, "verified": len(kept), "rejected": rejected}


def _take(raw: list, n: int, kept: list, rejected: list, seen_prompts: set, note: Optional[str], offset: int) -> None:
    for i, entry in enumerate(raw, offset + 1):
        if not isinstance(entry, dict):
            rejected.append(f"question {i}: not an object")
            continue
        spec, difficulty = normalise_question(entry)
        spec["id"] = f"g{i}"
        label = (spec.get("prompt") or spec["id"])[:60]
        if label.lower() in seen_prompts:
            continue   # the repair round re-sent a question already kept
        rep = verify_question(spec)
        if not rep.ok:
            code = (rep.refusal or {}).get("code")
            msg = (rep.refusal or {}).get("message", "")
            logger.info("geometry question rejected (%s): %r — %s", code, label, msg[:200])
            rejected.append(f"{label}: {code}: {msg[:200]}")
            continue
        item = GeometryItem(spec["id"], spec.get("prompt") or "", spec["figure_role"], difficulty, spec, rep)
        try:
            render_item(item, note=note)
        except GeometryRefusal as exc:
            logger.info("geometry question rejected at render (%s): %r — %s", exc.code, label, exc.message[:200])
            rejected.append(f"{label}: {exc.code}: {exc.message[:200]}")
            continue
        if len(kept) < n:
            kept.append(item)
            seen_prompts.add(label.lower())


# ── the answer key ────────────────────────────────────────────────────────

def _fmt(value) -> str:
    if isinstance(value, dict):
        return "; ".join(f"{k}: {_fmt(v)}" for k, v in value.items())
    if isinstance(value, (list, set, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value)


def key_lines(item: GeometryItem, *, answer_word: str = "Answer", reasons: bool = True) -> list[str]:
    """The answer key's lines: an evidence question's computed answer; a
    reasoning question's proof, each deduce line with its reason (English
    only — the reasons table is not yet localised), then the answer."""
    spec = item.spec
    if item.role != "reasoning":
        parts = spec.get("parts") or ([{"asks": spec.get("asks"), "answer": spec.get("answer")}]
                                      if spec.get("asks") else [])
        if len(parts) == 1:
            return [f"{answer_word}: {_fmt(parts[0]['answer']['value'])}"]
        return [f"({i}) {_fmt(p['answer']['value'])}" for i, p in enumerate(parts, 1)]
    lines: list[str] = []
    reason_iter = iter(item.report.reasons_given)
    for st in spec.get("steps") or []:
        after = st.get("after") or []
        if not after:
            continue
        text = "; ".join(_pretty_line(x) for x in after)
        if st.get("kind") == "deduce":
            reason = next(reason_iter, None)
            if reasons and reason:
                text = f"{text}   ({reason})"
        lines.append(text)
    proved = item.report.proved
    if proved:
        unit = (spec.get("answer") or {}).get("unit")
        tail = "°" if unit == "deg" else ""
        lines.append(f"{answer_word}: " + ", ".join(f"{k} = {v}{tail}" for k, v in proved.items()))
    return lines


def _pretty_line(text: str) -> str:
    s = str(text)
    try:
        return pretty(s.replace("ang(", "∠(")) if "ang(" not in s else s.replace("ang(", "∠").replace(")", "")
    except Exception:  # noqa: BLE001
        return s


__all__ = ["GEOMETRY_SET_SCHEMA", "GeometryItem", "FigureImage", "geometry_items", "geometry_prompt",
           "key_lines", "normalise_question", "render_item"]
