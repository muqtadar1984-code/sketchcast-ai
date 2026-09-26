"""A zoom keeps a picture's labels in the frame with it.

States of Matter (2026-09-26, kit 56b9b04d): a 1.4x zoom on the phase chart
kept the chart in frame and pushed its labels, laid out in the margin
columns either side of it, off both edges — "…ce" clipped at the left, "Ev…"
at the right — while their leader lines still entered the frame. The zoom
cap measured the picture's ink alone; it now measures the picture with its
labels and centres on that.
"""

from __future__ import annotations

import pytest

from spike.scene_engine.render import SceneRenderer
from spike.scene_engine.schema import WORLD_H, WORLD_W, Scene
from tests.test_board_quality import _disc_asset


def _scene(labels: bool, zoom: float = 1.6, pic_scale: float = 1.2) -> Scene:
    elements = [{"id": "pic", "type": "illustration", "asset": "disc",
                 "at": [600, 380], "scale": pic_scale}]
    actions = [{"verb": "draw", "target": "pic", "duration": 1.0}]
    if labels:
        elements += [
            {"id": "lbl_l", "type": "text", "text": "Freezing", "at": [40, 380],
             "role": "label", "anchor": "lt"},
            {"id": "lbl_r", "type": "text", "text": "Evaporation", "at": [1130, 380],
             "role": "label", "anchor": "lt"},
            {"id": "ar_l", "type": "arrow", "curve": 0,
             "tail": {"el": "lbl_l", "edge": "right", "dx": 6},
             "head": {"el": "pic", "edge": "left"}},
            {"id": "ar_r", "type": "arrow", "curve": 0,
             "tail": {"el": "lbl_r", "edge": "left", "dx": -6},
             "head": {"el": "pic", "edge": "right"}},
        ]
        actions += [{"verb": "write", "target": "lbl_l"}, {"verb": "draw", "target": "ar_l"},
                    {"verb": "write", "target": "lbl_r"}, {"verb": "draw", "target": "ar_r"}]
    actions.append({"verb": "zoom", "target": "pic", "scale": zoom, "duration": 1.0})
    return Scene.model_validate({"id": "z", "narration": "look at the chart",
                                 "elements": elements, "actions": actions})


def _render(labels: bool, **kw):
    r = SceneRenderer(_scene(labels, **kw), asset_resolver=lambda k: ("raster", _disc_asset()))
    r.compile(10.0)
    return r, r.cam.state_at(10.0)


def _in_frame(box, cam) -> bool:
    hw, hh = WORLD_W / (2 * cam.scale), WORLD_H / (2 * cam.scale)
    return (cam.cx - hw <= box[0] and box[2] <= cam.cx + hw
            and cam.cy - hh <= box[1] and box[3] <= cam.cy + hh)


class TestTheLabelsStayInTheFrame:
    def test_a_labelled_picture_is_zoomed_only_as_far_as_its_labels_allow(self):
        r, cam = _render(True)
        assert cam.scale < 1.6
        assert any(w.startswith("ZOOM_CLAMPED_FOR_LABELS pic 1.60->") for w in r._warned), r._warned
        for lid in ("lbl_l", "lbl_r"):
            assert _in_frame(r.bound[lid].box, cam), (lid, r.bound[lid].box, cam)
        ink = r._ink_box(r.bound["pic"]) or r.bound["pic"].box
        assert _in_frame(ink, cam)

    def test_the_camera_centres_on_the_picture_with_its_labels(self):
        r, cam = _render(True)
        ext = r._annotated_extent("pic", r._ink_box(r.bound["pic"]) or r.bound["pic"].box)
        assert cam.cx == pytest.approx((ext[0] + ext[2]) / 2, abs=1.0)

    def test_the_same_picture_without_labels_zooms_as_directed(self):
        r, cam = _render(False)
        assert cam.scale == pytest.approx(1.6)
        assert not any(w.startswith("ZOOM_CLAMPED") for w in r._warned), r._warned

    def test_a_zoom_that_would_only_jitter_is_skipped(self):
        # labels already span the whole board: the fit is ~1.0x
        r, cam = _render(True, pic_scale=2.4)
        assert cam.scale == pytest.approx(1.0)
        assert any("->1.00" in w and w.startswith("ZOOM_CLAMPED") for w in r._warned), r._warned

    def test_a_dropped_label_does_not_widen_the_extent(self):
        r, _ = _render(True)
        ink = r._ink_box(r.bound["pic"]) or r.bound["pic"].box
        full = r._annotated_extent("pic", ink)
        r._dropped.update({"ar_l", "lbl_l", "ar_r", "lbl_r"})
        assert r._annotated_extent("pic", ink) == tuple(ink) != full


class TestTheGateCountsIt:
    def test_zooms_clamped_for_labels_are_reported(self):
        from spike.scene_engine.validate import validate_visual_language
        manifest = {"segments": [{"segment_id": "s001", "renderer": "scene", "audio_path": "a.mp3",
                                  "scene_audit": ["ZOOM_CLAMPED_FOR_LABELS pic 1.60->1.21 (labels would leave the frame)"]}]}
        r = validate_visual_language(manifest, None)
        assert r["zooms_clamped_for_labels"] == \
            ["s001: ZOOM_CLAMPED_FOR_LABELS pic 1.60->1.21 (labels would leave the frame)"]
