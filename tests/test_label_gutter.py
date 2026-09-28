"""Labels keep a gutter from each other, on the top row above all.

Transport in Plants (2026-09-28, kit c565b020): the root hair cell filled
the board, so its labels spilled to the row above the picture, where the
spill took the first slot that did not OVERLAP a placed label — a slot
flush against it passed. With the handwriting face's overshoot drawn,
"Root Hair Cell" ran straight into "Carrier Proteins" and "Active Transport
(Minerals)" into "Osmosis (H2O)", and the audit, which tested the same
strict overlap, read clean.
"""

from __future__ import annotations

import itertools

from spike.scene_engine.render import SceneRenderer
from spike.scene_engine.schema import Scene
from tests.test_board_quality import _disc_asset

LABELS = ["Root Hair Cell", "Carrier Proteins", "Soil Particles",
          "Active Transport (Minerals)", "Osmosis (H2O)", "O2 for Respiration"]


def _scene() -> Scene:
    # a picture wide enough that neither column has room: everything spills
    elements = [{"id": "pic", "type": "illustration", "asset": "disc", "at": [640, 400], "scale": 4.6}]
    actions = [{"verb": "draw", "target": "pic", "duration": 1.0}]
    for i, text in enumerate(LABELS):
        elements += [{"id": f"lbl{i}", "type": "text", "text": text, "at": [95, 140 + 50 * i],
                      "role": "label", "anchor": "lt"},
                     {"id": f"ar{i}", "type": "arrow", "curve": 0,
                      "tail": {"el": f"lbl{i}", "edge": "right", "dx": 6},
                      "head": {"el": "pic", "edge": "left"}}]
        actions += [{"verb": "write", "target": f"lbl{i}"}, {"verb": "draw", "target": f"ar{i}"}]
    return Scene.model_validate({"id": "g", "narration": "the root hair cell in the soil",
                                 "elements": elements, "actions": actions})


def _render():
    r = SceneRenderer(_scene(), asset_resolver=lambda k: ("raster", _disc_asset()))
    r.compile(12.0)
    return r


def _gap(a, b) -> float:
    """The clear distance between two boxes (negative when they overlap)."""
    dx = max(b[0] - a[2], a[0] - b[2])
    dy = max(b[1] - a[3], a[1] - b[3])
    return max(dx, dy)


class TestTheTopRowKeepsAGutter:
    def test_no_two_labels_sit_closer_than_the_gutter(self):
        r = _render()
        boxes = {eid: r.bound[eid].box for eid in (f"lbl{i}" for i in range(len(LABELS)))}
        for (a_id, a), (b_id, b) in itertools.combinations(boxes.items(), 2):
            assert _gap(a, b) >= r._TEXT_GUTTER - 0.5, f"{a_id} {a} sits against {b_id} {b}"

    def test_the_audit_is_clean_only_because_they_really_are_apart(self):
        r = _render()
        assert not [w for w in r._warned if w.startswith("TEXT_OVERLAP")], r._warned


class TestTheAuditCountsTouchingText:
    def test_boxes_a_pixel_apart_are_an_overlap(self):
        r = _render()
        a = r.bound["lbl0"]
        b = r.bound["lbl1"]
        b.box = (a.box[2] + 1.0, a.box[1], a.box[2] + 1.0 + (b.box[2] - b.box[0]), a.box[3])
        r._warned = {w for w in r._warned if not w.startswith("TEXT_OVERLAP")}
        r._audit_text_overlaps()
        assert "TEXT_OVERLAP lbl0+lbl1" in r._warned

    def test_boxes_a_gutter_apart_are_not(self):
        r = _render()
        a = r.bound["lbl0"]
        b = r.bound["lbl1"]
        b.box = (a.box[2] + r._TEXT_GUTTER, a.box[1], a.box[2] + r._TEXT_GUTTER + (b.box[2] - b.box[0]), a.box[3])
        r._warned = {w for w in r._warned if not w.startswith("TEXT_OVERLAP")}
        r._audit_text_overlaps()
        assert "TEXT_OVERLAP lbl0+lbl1" not in r._warned
