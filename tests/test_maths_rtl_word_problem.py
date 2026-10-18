"""A script word problem's last line is cut by MEASURED width.

The Arabic trigonometry kit (a4ebfab8, 2026-10-18) failed acceptance twice
with overlapping_text=8/13: in four worked-example scenes the word problem's
second line q1 ran ~245 px under the method card (card_0+q1, card_1+q1).
_fit_text measured the overflow and derived a character cap, then _short
multiplied that cap by its 1.6 script allowance, so the "fitting" cut was
1.6x too long and nothing re-measured it. English never takes the allowance,
which is why the English kit passed with the same layout.
"""
from __future__ import annotations

from maths.board import CARD_X, Q_AT, _M, _cut_to_width, _fit_text, compile_lesson
from maths.schema import Lesson, MethodCard, Step, WorkedExample
from spike.scene_engine.director import parse_scene_response
from spike.scene_engine.render import SceneRenderer

# the kit's own words (translated script, examples 1 and 2; method card)
_PROBLEMS = [
    "مثلثان قائما الزاوية لهما نفس الزوايا، أحدهما صغير والآخر كبير. أيّهما يكون أصغر زاوية فيه 36.87 درجة، "
    "بحيث تكون نسبة أقصر ضلع إلى الوتر متساوية في كليهما؟",
    "في مثلث قائم الزاوية ABC الزاوية القائمة عند B، وAB تساوي 3 cm وBC تساوي 4 cm. باستخدام الزاوية عند A، "
    "جيب A هو الضلع المقابل على الوتر. أوجد طول AC، أي الوتر.",
]
_METHOD = MethodCard(title="الحل باستخدام علم المثلثات", steps=[
    "ارسم شكلاً توضيحياً وضع تسميات للأضلاع", "اختر الجيب، أو جيب التمام، أو الظل", "اكتب المعادلة",
    "أعد الترتيب للحل", "قرّب فقط الإجابة النهائية"])
# the word problem's lines are wrapped to end before the card (_problem_elements)
_Q_MAX_W = CARD_X - 16 - 24 - Q_AT[0]


def test_an_arabic_cut_fits_the_width_it_was_cut_for():
    tail = _PROBLEMS[0].split("كبير.")[1]
    assert _M.text_box(tail, 27, True)[0] > _Q_MAX_W     # the scenario is a line that overflows
    t, size = _fit_text(tail, 27, _Q_MAX_W, bold=True, min_size=27)
    assert size == 27
    assert _M.text_box(t, 27, True)[0] <= _Q_MAX_W, t
    assert t.endswith("...")                            # the script faces have no "…"
    assert "......" not in t and len(t) > 20            # cut once from the whole text, not to nothing


def test_a_latin_cut_still_fits_and_keeps_the_ellipsis():
    text = "A ladder leans against a vertical wall with its foot two metres from the wall " * 3
    t = _cut_to_width(text, 27, 500, bold=True)
    assert _M.text_box(t, 27, True)[0] <= 500
    assert t.endswith("…")


def test_a_short_text_is_left_alone():
    assert _cut_to_width("اكتب المعادلة", 21, 900) == "اكتب المعادلة"


def test_the_arabic_word_problems_stay_clear_of_the_method_card():
    steps = [Step(kind="setup", operation="write the ratio", before=[], after=["sin(A) = 4/5"], speech="s"),
             Step(operation="divide", before=["sin(A) = 4/5"], after=["sin(A) = 0.8"], speech="x")]
    examples = [WorkedExample(label=f"مثال {i + 1}", task="evaluate", problem=p, givens=["sin(A) = 4/5"],
                              target="sin(A)", steps=steps, final_answer=["sin(A) = 0.8"],
                              answer_speech="z", intro_speech="intro")
                for i, p in enumerate(_PROBLEMS)]
    lesson = Lesson(topic="النسب المثلثية", method=_METHOD, concept_points=["جيب", "جيب التمام", "الظل"],
                    misconceptions=["جيب", "الظل"], examples=examples)
    checked = 0
    for s in compile_lesson(lesson, language="ar"):
        sc = s["scene"]
        if sc.get("scene_type") != "worked_example":
            continue
        r = SceneRenderer(parse_scene_response(sc, s["text"]))
        r.compile(30.0)
        overlaps = [w for w in r.audit()["warnings"] if w.startswith("TEXT_OVERLAP")]
        assert not overlaps, (s["segment_id"], overlaps)
        box = next(e for e in sc["elements"] if e["id"] == "card_box")
        card_left = min(p[0] for p in box["points"])
        for eid in ("q0", "q1"):
            b = r.bound.get(eid)
            if b is not None and b.box:
                assert b.box[2] < card_left, (s["segment_id"], eid, b.box, card_left)
                checked += 1
    assert checked >= 4, "both word problems wrapped to two lines beside the card"
