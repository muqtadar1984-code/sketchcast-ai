"""SK B3 packager — the pure parts: unit loading, grounding, plan verification
(what an LLM proposes is checked and trimmed, never trusted), cue resolution
against word marks, exercise assignment, gestures."""

from sk.packager import (
    Figure,
    Unit,
    assign_exercises,
    cue_ms,
    gestures_for,
    grounding,
    load_units,
    norm_words,
    verify_plan,
)

BLOCKS = [
    {"type": "paragraph", "text": "A microscope makes an image bigger than the real thing."},
    {"type": "key_idea", "text": "magnification = image size ÷ actual size, which can be rearranged to actual size = image size ÷ magnification."},
    {"type": "worked_example", "problem": "A drawing of a cheek cell is 48 mm wide. The magnification is ×800.", "steps": ["48 ÷ 800 = 0.06 mm.", "0.06 × 1000 = 60 µm."], "answer": "60 µm"},
]
UNIT = Unit(key="s5", heading="Magnification and actual size", blocks=BLOCKS, figure_refs=[], node_codes=["7Bs.01"], order=4)
MIC = Figure(ref="fig:x", asset_key="light_microscope", description="d", parts=["eyepiece lens", "objective lens"], kind="ink")


def test_load_units_reads_sections_and_plan():
    draft = {"plan": {"sections": [{"sid": "s1", "purpose": "p", "node_codes": ["7Bs.01"]}]},
             "sections": [{"sid": "s1", "heading": "H", "blocks": [{"type": "figure", "ref": "fig:1"}, {"type": "figure_group", "refs": ["asset:2"]}]}]}
    [u] = load_units(draft)
    assert (u.key, u.heading, u.figure_refs, u.node_codes) == ("s1", "H", ["fig:1", "asset:2"], ["7Bs.01"])


def test_grounding_scores_overlap_with_the_best_block():
    assert grounding("A microscope makes an image bigger than the real thing.", UNIT.text_blocks()) == 1.0
    assert grounding("Mitochondria are the powerhouse of the cell.", UNIT.text_blocks()) < 0.5


def test_verify_plan_drops_ungrounded_sentences_bad_cues_and_unknown_parts():
    plan = {
        "narration": ["A microscope makes an image bigger than the real thing.", "The Eiffel tower is in Paris."],
        "board": [
            {"kind": "illustration", "asset_key": "light_microscope", "cue": "A microscope makes"},
            {"kind": "label", "asset_key": "light_microscope", "part": "eyepiece lens", "text": "eyepiece", "cue": "bigger than"},
            {"kind": "label", "asset_key": "light_microscope", "part": "laser", "text": "laser", "cue": "bigger than"},
            {"kind": "write", "id": "w1", "text": "M = image ÷ actual", "role": "term", "cue": "Eiffel tower"},
            {"kind": "write", "text": "48 ÷ 800 = 0.06 mm", "role": "term", "cue": "real thing"},
            {"kind": "circle", "target": "nope", "cue": "real thing"},
        ],
    }
    clean, problems = verify_plan(plan, UNIT, [MIC])
    assert clean["narration"] == ["A microscope makes an image bigger than the real thing."]
    kinds = [(b["kind"], b.get("part") or b.get("id")) for b in clean["board"]]
    assert kinds == [("illustration", None), ("label", "eyepiece lens"), ("write", "w1")]  # the Eiffel-cued write and the laser label are gone
    assert any("ungrounded" in p for p in problems) and any("laser" in p for p in problems) and any("circle" in p for p in problems)


def test_verify_plan_refuses_labels_without_an_illustration():
    plan = {"narration": ["A microscope makes an image bigger than the real thing."],
            "board": [{"kind": "label", "asset_key": "light_microscope", "part": "eyepiece lens", "text": "eyepiece", "cue": "microscope"}]}
    clean, problems = verify_plan(plan, UNIT, [MIC])
    assert clean["board"] == [] and any("no illustration" in p for p in problems)


def test_cue_resolution_matches_phrases_across_word_marks():
    words = norm_words([{"t": 0.0, "d": 0.3, "w": "Ten"}, {"t": 0.4, "d": 0.3, "w": "times"}, {"t": 0.8, "d": 0.3, "w": "forty"}, {"t": 1.2, "d": 0.3, "w": "gives"}])
    assert cue_ms(words, "times forty") == (400, 1100)
    assert cue_ms(words, "Ten times forty gives") == (0, 1500)
    assert cue_ms(words, "four hundred") is None


def test_exercises_go_to_the_unit_whose_text_they_overlap():
    u1 = Unit(key="s1", heading="Parts of a light microscope", blocks=[{"type": "paragraph", "text": "The eyepiece lens and objective lens are the two lenses."}], figure_refs=[], node_codes=[], order=0)
    ex = [{"stem": "Which lens do you look through? eyepiece or objective"}, {"stem": "A drawing is 48 mm wide and the magnification is ×800. Find the actual size."}]
    assigned = assign_exercises([u1, UNIT], ex)
    assert assigned == {"s1": [0], "s5": [1]}


def test_gestures_follow_the_timeline():
    tl = [{"target": "ill", "verb": "draw", "start_ms": 100, "end_ms": 5000}, {"target": "l1", "verb": "write", "start_ms": 6000, "end_ms": 6600},
          {"target": "w1", "verb": "write", "start_ms": 7000, "end_ms": 9000}, {"target": "w1", "verb": "circle", "start_ms": 9500, "end_ms": 10200}]
    g = gestures_for(tl, True)
    assert [x["kind"] for x in g] == ["point", "point", "write", "point"]
    assert g[2]["until"] == 9000
