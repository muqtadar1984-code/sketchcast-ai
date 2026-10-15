"""The vertex letter of a fan of angles keeps clear of the measure labels on
the video board, and a revealed value keeps clear of the vertex letter.

"Types of Angles: Angles on a Straight Line and Around a Point" (kit
12631c9f, 2026-10-15 02:02 UTC) failed its acceptance gate: BLOCKING
overlapping_text=4/13 — "120°"+"O", "110°"+"O", "3z"+"O" on three figures and
"O"+"126°" where the proved value was written over its symbol. The layout had
passed every one of them with print's clearance."""
from __future__ import annotations

import pytest

from maths.board import _M
from maths.geometry import parse_question, verify_question
from maths.geometry.board_adapter import LABEL_PX, figure_board, op_actions
from spike.scene_engine.render import SceneRenderer
from spike.scene_engine.schema import Scene

PANEL = (60.0, 118.0, 470.0, 452.0)
LABELS = ("A", "B", "C", "D", "E")


def fan(values, unknown):
    n = len(values)
    ids = [f"p_{l.lower()}" for l in LABELS[:n]]
    pts = [{"id": "p_o", "label": "O"}] + [{"id": i, "label": l} for i, l in zip(ids, LABELS)]
    angles, measures = [], []
    for i in range(n):
        aid = f"ang_{i}"
        angles.append({"id": aid, "rays": [["p_o", ids[i]], ["p_o", ids[(i + 1) % n]]]})
        measures.append({"target": aid, "value": str(values[i]),
                         "role": "unknown" if str(values[i]) == unknown else "given"})
    known = sum(v for v in values if not isinstance(v, str))
    return {"schema_version": "geometry.figure.v3", "id": "q", "figure_role": "reasoning", "prompt": f"Find {unknown}.",
            "figure": {"bind": {unknown: 360 - known}, "points": pts,
                       "objects": [{"id": "fan", "make": "angles_at_point", "vertex": "p_o", "points": ids}],
                       "angles": angles, "measures": measures},
            "steps": [{"kind": "deduce", "theorem": "angles_at_point", "uses": [a["id"] for a in angles],
                       "after": [" + ".join(str(v) for v in values) + " = 360"]},
                      {"kind": "transform", "after": [f"{unknown} = {360 - known}"]}],
            "answer": {"kind": "number", "value": str(360 - known)}}


def _scene(fb, els=(), acts=()):
    return Scene.model_validate({"id": "t", "scene_type": "worked_example", "narration": "n",
                                 "elements": fb.elements + list(els), "actions": fb.actions + list(acts)})


def _overlaps(fb, els=(), acts=()):
    r = SceneRenderer(_scene(fb, els, acts))
    r.compile(20.0)
    names = {e["id"]: e["text"] for e in fb.elements + list(els) if e["type"] == "text"}
    out = []
    for w in r.audit()["warnings"]:
        if w.startswith("TEXT_OVERLAP"):
            a, b = w.split()[1].split("+")
            out.append((names.get(a, a), names.get(b, b)))
    return out


CASES = {
    "four angles, 120 given": ([120, 90, 24, "z"], "z"),       # s005: "120°" + "O"
    "four angles, 110 given": ([110, 70, "y", 100], "y"),      # s008: "110°" + "O"
    "three angles, 126 given": ([126, "x", 110], "x"),
    "four angles, 126 given": ([126, 94, "x", 60], "x"),
    "three angles, two reflex-ish": ([150, 150, "x"], "x"),
    "five angles": ([60, 70, 80, 90, "x"], "x"),               # O had no room at all
    "four right angles": ([90, 90, 90, "x"], "x"),
}


@pytest.mark.parametrize("name", list(CASES))
def test_the_vertex_letter_and_the_measures_never_touch(name):
    vals, unk = CASES[name]
    q = fan(vals, unk)
    rep = verify_question(q)
    assert rep.ok, rep.refusal
    ref = parse_question(q).figures[0]
    m = rep.models[ref.id]
    fb = figure_board(m, ref.figure, panel=PANEL, text_metric=_M.text_box)    # no layout_collision
    assert _overlaps(fb) == []
    o = next(e for e in fb.elements if e["type"] == "text" and e["text"] == "O")
    assert o["size"] >= LABEL_PX * 0.5
    # the proved value written where its symbol was, pushed clear of O
    target = next(ms["target"] for ms in q["figure"]["measures"] if ms["role"] == "unknown")
    value = str(360 - sum(v for v in vals if not isinstance(v, str)))
    els, acts = op_actions(fb, m, {"op": "reveal_measure", "target": target, "value": value})
    assert [e for e in els if e["type"] == "text" and e["text"] == f"{value}°"]
    assert _overlaps(fb, els, acts) == []


def test_a_revealed_value_never_moves_inward():
    """A value written over its symbol stays put when it fits, else steps
    AWAY from the vertex — never towards it."""
    q = fan([120, 90, 24, "z"], "z")
    rep = verify_question(q)
    ref = parse_question(q).figures[0]
    m = rep.models[ref.id]
    fb = figure_board(m, ref.figure, panel=PANEL, text_metric=_M.text_box)
    target = next(ms["target"] for ms in q["figure"]["measures"] if ms["role"] == "unknown")
    ox, oy = fb.to_board(m.xy("p_o"))
    sym = fb.label_at[next(t for t, eid in fb.labels.items()
                           if next(e for e in fb.elements if e["id"] == eid)["text"] == "z")]
    for value in ("9", "126"):
        els, _ = op_actions(fb, m, {"op": "reveal_measure", "target": target, "value": value})
        el = next(e for e in els if e["type"] == "text")
        d_sym = (sym[0] - ox) ** 2 + (sym[1] - oy) ** 2
        d_new = (el["at"][0] - ox) ** 2 + (el["at"][1] - oy) ** 2
        assert d_new >= d_sym - 1e-6, (value, el["at"], sym)


def test_a_narrow_wedge_still_lays_out_with_prints_clearance():
    """B8's 30° wedge has no room for the board's clearance; it keeps the
    layout it always had instead of being refused."""
    import json
    from pathlib import Path
    corpus = json.loads((Path(__file__).parent / "fixtures" / "geometry_corpus_v1.json").read_text(encoding="utf-8"))
    items = {it["question"]["id"]: it["question"] for it in corpus["items"]}
    for qid, q in items.items():
        rep = verify_question(q)
        if not rep.ok:
            continue
        pq = parse_question(q)
        for ref in pq.figures:
            figure_board(rep.models[ref.id], ref.figure, panel=PANEL, text_metric=_M.text_box)
