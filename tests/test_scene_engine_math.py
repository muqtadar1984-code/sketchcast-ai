"""The math element on the board: typeset, written on progressively, a term
addressable by an arrow, unreadable notation degrading to text."""

from __future__ import annotations

from PIL import Image

from spike.scene_engine.render import SceneRenderer
from spike.scene_engine.schema import Scene


def _ink(frame: Image.Image, box=None) -> int:
    """Dark pixels in the frame (or a world box of it)."""
    g = frame.convert("L")
    if box:
        g = g.crop(tuple(int(v) for v in box))
    return sum(1 for v in g.getdata() if v < 110)


def _frame_at(r: SceneRenderer, t: float, secs: float) -> Image.Image:
    frames = list(r.frames(secs, 4))
    k = min(len(frames) - 1, int(t * 4))
    return frames[k]


def _scene(expr="3x + 5 = 20", extra_elements=(), extra_actions=()):
    return Scene.model_validate({
        "id": "m", "narration": "Three x plus five equals twenty. Subtract five.",
        "min_hold": 0.2,
        "elements": [{"id": "q", "type": "math", "expr": expr, "at": (80, 80), "size": 40},
                     *extra_elements],
        "actions": [{"verb": "write", "target": "q", "duration": 2.0}, *extra_actions],
    })


def test_a_math_element_binds_typeset_and_writes_on_over_time():
    r = SceneRenderer(_scene())
    r.compile(4.0)
    b = r.bound["q"]
    assert b.math is not None and b.text is None
    assert [ru.text for ru in b.math.layout.runs][:3] == ["3", "x", "+"]
    x0, y0, x1, y1 = b.box
    assert x0 == 80 and y0 == 80 and x1 - x0 > 150
    blank = _ink(_frame_at(r, 0.0, 4.0), (x0, y0, x1 + 4, y1 + 4))
    half = _ink(_frame_at(r, 1.0, 4.0), (x0, y0, x1 + 4, y1 + 4))
    full = _ink(_frame_at(r, 3.0, 4.0), (x0, y0, x1 + 4, y1 + 4))
    assert blank < half < full, (blank, half, full)
    assert r.workloads[0] == b.math.layout.units


def test_a_fraction_and_a_power_leave_ink_above_and_below_the_baseline():
    r = SceneRenderer(_scene("(x + 1)/2 = x^2"))
    r.compile(3.0)
    b = r.bound["q"]
    assert len(b.math.layout.strokes) == 1, "one fraction bar"
    x0, y0, x1, y1 = b.box
    assert y1 - y0 > 40 * 1.5, "taller than one line"
    fr = _frame_at(r, 2.8, 3.0)
    assert _ink(fr, (x0, y0, x1, (y0 + y1) / 2)) > 0 and _ink(fr, (x0, (y0 + y1) / 2, x1, y1)) > 0


def test_an_arrow_can_anchor_to_a_term_by_notation():
    sc = _scene(extra_elements=[
        {"id": "note", "type": "text", "text": "subtract 5", "at": (420, 160), "size": 24, "color": "muted"},
        {"id": "ar", "type": "arrow", "color": "muted", "tail": {"el": "note", "edge": "left"},
         "head": {"el": "q", "sub": "5", "edge": "bottom", "dy": 4}},
    ], extra_actions=[{"verb": "write", "target": "note"}, {"verb": "draw", "target": "ar"}])
    r = SceneRenderer(sc)
    r.compile(4.0)
    five = r._sub_box(r.bound["q"], "5")
    assert five is not None
    qx0, qy0, qx1, qy1 = r.bound["q"].box
    assert qx0 < five[0] < five[2] < qx1, "the term sits inside the equation"
    three_x = r._sub_box(r.bound["q"], "3x")
    assert three_x is not None and three_x[2] < five[0]
    assert r._sub_box(r.bound["q"], "9") is None


def test_unreadable_notation_binds_as_plain_text_and_warns():
    r = SceneRenderer(_scene("subtract five from both sides"))
    r.compile(2.0)
    b = r.bound["q"]
    assert b.math is None and b.text is not None
    assert any(w.startswith("MATH_UNREADABLE") for w in r.audit()["warnings"])


def test_math_survives_highlight_underline_and_fade():
    sc = _scene(extra_actions=[{"verb": "highlight", "target": "q"},
                               {"verb": "underline", "target": "q"},
                               {"verb": "fade", "target": "q", "to": 0.4, "duration": 0.3}])
    r = SceneRenderer(sc)
    r.compile(5.0)
    assert list(r.frames(5.0, 4))


def test_a_decoration_leaves_with_the_element_it_decorates():
    """An underline under a line the board has since erased must not stay
    (maths demo 2026-09-24: a squiggle under nothing after a wipe)."""
    r = SceneRenderer(_scene(extra_actions=[
        {"verb": "underline", "target": "q", "at": {"frac": 0.3}},
        {"verb": "erase", "target": "q", "at": {"frac": 0.7}, "duration": 0.4}]))
    r.compile(8.0)
    x0, y0, x1, y1 = r.bound["q"].box
    under = (x0 - 8, y1 + 2, x1 + 8, y1 + 16)
    assert _ink(_frame_at(r, 4.5, 8.0), under) > 0, "underline drawn"
    assert _ink(_frame_at(r, 7.6, 8.0), under) == 0, "gone with the line"


def test_a_marker_shape_is_translucent_and_fades():
    hl = {"id": "hl", "type": "shape", "shape": "line", "width": 26, "color": "marker",
          "points": [[80, 100], [300, 100]]}
    r = SceneRenderer(_scene(extra_elements=[hl], extra_actions=[
        {"verb": "draw", "target": "hl", "at": {"frac": 0.2}, "duration": 0.5},
        {"verb": "fade", "target": "hl", "to": 0.0, "at": {"frac": 0.8}, "duration": 0.2}]))
    r.compile(8.0)
    px = _frame_at(r, 4.0, 8.0).convert("RGB").getpixel((150, 100))
    assert px[0] > 200 and px[2] < 200, px          # yellow wash
    assert px[2] > 60, "translucent, not the solid marker colour"
    assert _frame_at(r, 7.8, 8.0).convert("RGB").getpixel((150, 100))[2] > 200, "faded away"


def test_a_fixed_text_stays_under_the_caption_band():
    from spike.scene_engine.schema import Scene
    els = [{"id": "__nb_0", "type": "text", "text": "cap", "at": [970, 360], "role": "caption"},
           {"id": "free", "type": "text", "text": "moves", "at": [900, 350]},
           {"id": "pinned", "type": "text", "text": "stays", "at": [900, 400], "fixed": True}]
    r = SceneRenderer(Scene.model_validate({"id": "f", "narration": "cap", "elements": els,
                                            "actions": [{"verb": "write", "target": "free"},
                                                        {"verb": "write", "target": "pinned"}]}))
    r.compile(4.0)
    assert r.bound["free"].box[1] < 300, "an unpinned label is moved off the band"
    b = r.bound["pinned"].box
    assert abs((b[1] + b[3]) / 2 - 400) < 2.0, "a fixed label keeps its place"
