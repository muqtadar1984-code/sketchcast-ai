"""Physics demo — Grade 9 CBSE, Force and Laws of Motion (NCERT Ch. 9).

Newton's second law shown, not stated: two trolleys on two tracks, the SAME
drawn force arrow on each, and the acceleration made visible — the light
trolley speeds away (`ease_in` = accelerating motion), the double-mass trolley
covers half the distance in the same time. The force arrow and the mass label
ride along via a GROUP move, so the push reads as continuously applied.
Motion here carries the causality (§27): same cause, different mass, different
effect — then and only then does F = m × a get written.
"""

from __future__ import annotations

from .schema import Scene

ASSET_PROMPTS: dict[str, str] = {}  # drawn entirely from primitives


def newton_scene() -> Scene:
    n = ("Why do things speed up when you push them? Let's find out. Here is "
         "a smooth track, carrying a light trolley of mass m. Give it a "
         "steady push... and watch — it accelerates, moving faster and "
         "faster along the track. Now a second track, with a heavier trolley "
         "— double the mass. Push it with exactly the same force... and "
         "look: it accelerates only half as much. The same push moves more "
         "mass more slowly. This is Newton's second law: force equals mass "
         "times acceleration. Double the mass, and you halve the "
         "acceleration.")
    return Scene.model_validate({
        "id": "s04_newton_second_law",
        "scene_type": "process",
        "style": {"pen_mode": "hand", "hand_scale": 0.8},
        "narration": n,
        "elements": [
            {"id": "title", "type": "text", "text": "Force and Motion",
             "at": (80, 44), "role": "title", "size": 38, "anchor": "lt"},
            # lane 1: light trolley
            {"id": "track1", "type": "shape", "shape": "line", "width": 3.5,
             "points": [(120, 300), (1160, 300)]},
            {"id": "blk1", "type": "shape", "shape": "path", "width": 3.5,
             "points": [(200, 230), (270, 230), (270, 300), (200, 300)],
             "closed": True, "fill": True},
            {"id": "lbl1", "type": "text", "text": "m", "at": (235, 263),
             "size": 30, "anchor": "mm"},
            {"id": "ar_f1", "type": "arrow", "tail": (118, 265),
             "head": (194, 265), "width": 5.5, "color": "accent"},
            {"id": "g1", "type": "group", "children": ["blk1", "lbl1", "ar_f1"]},
            # lane 2: double-mass trolley
            {"id": "track2", "type": "shape", "shape": "line", "width": 3.5,
             "points": [(120, 560), (1160, 560)]},
            {"id": "blk2", "type": "shape", "shape": "path", "width": 3.5,
             "points": [(200, 450), (310, 450), (310, 560), (200, 560)],
             "closed": True, "fill": True},
            {"id": "lbl2", "type": "text", "text": "2m", "at": (255, 503),
             "size": 30, "anchor": "mm"},
            {"id": "ar_f2", "type": "arrow", "tail": (118, 505),
             "head": (194, 505), "width": 5.5, "color": "accent"},
            {"id": "g2", "type": "group", "children": ["blk2", "lbl2", "ar_f2"]},
            # the law, written once the effect has been SEEN
            {"id": "law", "type": "text", "text": "F = m × a", "at": (640, 385),
             "role": "term", "size": 46, "color": "accent", "anchor": "mm"},
        ],
        "actions": [
            {"verb": "write", "target": "title",
             "at": {"phrase": "Why do things speed up"}},
            {"verb": "draw", "target": "track1", "at": {"phrase": "smooth track"}},
            {"verb": "draw", "target": "blk1", "at": {"phrase": "light trolley"}},
            {"verb": "write", "target": "lbl1", "at": {"phrase": "mass m"}},
            {"verb": "draw", "target": "ar_f1", "at": {"phrase": "steady push"}},
            {"verb": "move", "target": "g1", "duration": 2.6, "easing": "ease_in",
             "path": [(235, 265), (1000, 265)],
             "at": {"phrase": "faster and faster"}},
            {"verb": "draw", "target": "track2", "at": {"phrase": "second track"}},
            {"verb": "draw", "target": "blk2", "at": {"phrase": "heavier trolley"}},
            {"verb": "write", "target": "lbl2", "at": {"phrase": "double the mass"}},
            {"verb": "draw", "target": "ar_f2",
             "at": {"phrase": "exactly the same force"}},
            {"verb": "move", "target": "g2", "duration": 2.6, "easing": "ease_in",
             "path": [(255, 505), (637, 505)],
             "at": {"phrase": "only half as much"}},
            {"verb": "write", "target": "law",
             "at": {"phrase": "force equals mass times acceleration"}},
            {"verb": "circle", "target": "law", "padding": 24,
             "at": {"phrase": "halve the acceleration"}},
        ],
    })


def demo_scenes() -> list[Scene]:
    return [newton_scene()]
