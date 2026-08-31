"""Math demo — a Grade 9 CBSE worked example (NCERT Polynomials, Ch. 2).

Factorise x² + 5x + 6 by splitting the middle term. This is the
`worked_example` scene grammar: equation lines write in as fragments (so
arrows and highlights can address individual TERMS, not whole lines), the
number-pair reasoning happens in a circled side note, the split of 5x is
shown with two arrows, the common bracket is highlighted where it appears
twice, and the answer gets the underline. Text is the visual here — §16
allows exactly this: equations ARE the whiteboard content in mathematics.

Every fragment position below is MEASURED (agent5's _font.getlength) so the
split arrows land on `2x`/`3x` and the highlights cover `(x + 2)` exactly.
"""

from __future__ import annotations

from .schema import Scene

ASSET_PROMPTS: dict[str, str] = {}  # pure equation work — no illustrations


def factorise_scene() -> Scene:
    n = ("Let's factorise x squared plus five x plus six — a classic Class "
         "Nine problem. We need two numbers that multiply to give six, and "
         "add to give five. Two and three do the job. So we split the middle "
         "term: five x becomes two x, plus three x. The value hasn't changed — "
         "we've only rewritten five x. Now group the terms. From the first "
         "pair, take out x — that leaves x plus two. From the second pair, "
         "take out three — and again, x plus two. Both groups share x plus "
         "two, so it comes out as a common factor. And there is our answer: "
         "x plus two, times x plus three. That is the factorised form.")

    # measured layout: title prefix 241px, "5x" spans x 421-472 in the title;
    # ln1 fragments start at x=120 with widths 87/44/52/44/75; ln2 21/124/75/124
    T = lambda id_, text, x, y, **kw: {"id": id_, "type": "text", "text": text,
                                       "at": (x, y), "anchor": "lt", **kw}
    return Scene.model_validate({
        "id": "s03_factorise_quadratic",
        "scene_type": "worked_example",
        "style": {"pen_mode": "hand", "hand_scale": 0.75},
        "narration": n,
        "elements": [
            T("title", "Factorise:  x² + 5x + 6", 80, 60,
              role="title", size=38),
            # the number-pair reasoning, boxed off to the side like a margin note
            T("note1", "2 × 3 = 6", 820, 150, size=30, color="muted"),
            T("note2", "2 + 3 = 5", 820, 205, size=30, color="muted"),
            {"id": "note_g", "type": "group", "children": ["note1", "note2"]},
            # line 1: the split, term by term
            T("f1", "x² + ", 120, 250, size=36),
            T("f2", "2x", 207, 250, size=36, color="accent", after={"el": "f1", "gap": 2}),
            T("f3", " + ", 251, 250, size=36, after={"el": "f2", "gap": 2}),
            T("f4", "3x", 303, 250, size=36, color="accent", after={"el": "f3", "gap": 2}),
            T("f5", " + 6", 347, 250, size=36, after={"el": "f4", "gap": 2}),
            # the split arrows: from "5x" in the title down to 2x and 3x
            {"id": "ar1", "type": "arrow", "curve": -10, "color": "muted",
             "tail": {"el": "title", "sub": "5x", "edge": "bottom", "dx": -4, "dy": 6},
             "head": {"el": "f2", "edge": "top", "dy": -6}},
            {"id": "ar2", "type": "arrow", "curve": 12, "color": "muted",
             "tail": {"el": "title", "sub": "5x", "edge": "bottom", "dx": 10, "dy": 6},
             "head": {"el": "f4", "edge": "top", "dy": -6}},
            # line 2: grouping — brackets as their own elements for highlighting
            T("g1", "x", 120, 380, size=36),
            T("g2", "(x + 2)", 141, 380, size=36, after={"el": "g1", "gap": 2}),
            T("g3", " + 3", 265, 380, size=36, after={"el": "g2", "gap": 2}),
            T("g4", "(x + 2)", 340, 380, size=36, after={"el": "g3", "gap": 6}),
            # the answer
            T("ln3", "(x + 2)(x + 3)", 120, 510, role="term", size=40,
              color="accent"),
            {"id": "grp_work", "type": "group",
             "children": ["g1", "g2", "g3", "g4"]},
        ],
        "actions": [
            {"verb": "write", "target": "title", "at": {"phrase": "Let's factorise"}},
            {"verb": "write", "target": "note1", "at": {"phrase": "multiply to give six"}},
            {"verb": "write", "target": "note2", "at": {"phrase": "add to give five"}},
            {"verb": "circle", "target": "note_g", "at": {"phrase": "do the job"}},
            {"verb": "write", "target": "f1", "at": {"phrase": "split the middle term"}},
            {"verb": "draw", "target": "ar1", "at": {"phrase": "becomes two x"}},
            {"verb": "write", "target": "f2"},
            {"verb": "write", "target": "f3"},
            {"verb": "draw", "target": "ar2", "at": {"phrase": "plus three x"}},
            {"verb": "write", "target": "f4"},
            {"verb": "write", "target": "f5"},
            {"verb": "zoom", "scale": 1.4, "target": "grp_work",
             "at": {"phrase": "group the terms"}},
            {"verb": "write", "target": "g1", "at": {"phrase": "take out x"}},
            {"verb": "write", "target": "g2", "at": {"phrase": "leaves x plus two"}},
            {"verb": "write", "target": "g3", "at": {"phrase": "take out three"}},
            {"verb": "write", "target": "g4", "at": {"phrase": "again, x plus two"}},
            {"verb": "highlight", "target": "g2", "at": {"phrase": "Both groups share"}},
            {"verb": "highlight", "target": "g4"},
            {"verb": "camera_reset", "at": {"phrase": "common factor"}},
            {"verb": "write", "target": "ln3",
             "at": {"phrase": "x plus two, times x plus three"}},
            {"verb": "underline", "target": "ln3", "at": {"phrase": "factorised form"}},
        ],
    })


def demo_scenes() -> list[Scene]:
    return [factorise_scene()]
