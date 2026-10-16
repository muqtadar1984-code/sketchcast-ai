"""The Kit Translator, worker side (catalogue.translate, catalogue.translate_check
and the ``script_override`` seam of worker.process — plan revision 3, T0).

Everything CALLS things: the database is tests/catalogue_fakes.FakeSB, the
model is a fake translator that rewrites each string with an Arabic marker
(and, where a test wants it, Arabic-Indic numerals, so numeric EQUIVALENCE
is what is checked, never identical characters), and every heavy render
stage is the recorder tests/test_catalogue_kit.py already uses. No network,
no model, no live Supabase."""

from __future__ import annotations

import copy
import json

import pytest

from catalogue import translate as T
from catalogue import translate_check as C
from tests.catalogue_fakes import FakeSB
from tests.test_catalogue_kit import (ARTICLE, FIGURES, KIT, PARAMS, _gen, _job, _presentation_fakes, _sb,
                                      _worker_env)

ARABIC_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")


def _arabize(s: str) -> str:
    """The fake translation: an Arabic marker in front, the numerals in
    Arabic-Indic digits, the words otherwise kept (so a test can still read
    them)."""
    return "ترجمة " + s.translate(ARABIC_DIGITS)


class FakeTranslator:
    model = "fake-translator"

    def __init__(self, transform=None):
        self.transform = transform or _arabize
        self.calls: list[str] = []
        self.session_usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}

    def analyze(self, prompt, system="", max_tokens=0, response_schema=None, **kw):
        self.calls.append(prompt)
        self.session_usage["calls"] += 1
        payload = json.loads(prompt[prompt.rindex("INPUT:") + len("INPUT:"):].strip())
        return {"data": {"items": [self.transform(s) for s in payload]}, "usage": {}}


# ── translate_check ──────────────────────────────────────────────────────


class TestNumerals:
    def test_arabic_indic_and_devanagari_digits_are_the_same_values(self):
        assert C.number_values("P = πr + 2r = 41.12 cm") == ["2", "41.12"]
        assert C.number_values("المحيط ٤١٫١٢ سم و ٢") == ["41.12", "2"]
        assert C.number_values("परिमाप ४१.१२") == ["41.12"]
        ok, missing, extra = C.numbers_equivalent("3.14 and 2,500 and 7", "٣٫١٤ و ٢٥٠٠ و ٧")
        assert ok and not missing and not extra

    def test_a_french_decimal_comma_is_a_decimal_mark(self):
        assert C.number_values("1,5 cm") == ["1.5"]
        assert C.number_values("41,120 people") == ["41120"]

    def test_a_changed_number_is_caught_either_way(self):
        ok, missing, extra = C.numbers_equivalent("x = 5", "س = ٦")
        assert not ok and missing == ["5"] and extra == ["6"]

    def test_curriculum_codes_are_invariant_tokens(self):
        assert C.code_tokens("covers 7Gg.12 and cbse:7:GEO:01") == ["7Gg.12", "cbse:7:GEO:01"]


class TestEnglishLeftover:
    def test_latin_letters_in_an_arabic_target_are_flagged_but_protected_maths_is_not(self):
        assert C.english_leftover("The perimeter of the semicircle is", "ar") is not None
        assert C.english_leftover("محيط نصف الدائرة هو P = πr + 2r", "ar", protected=["P = πr + 2r"]) is None
        assert C.english_leftover("محيط نصف الدائرة", "ar") is None

    def test_english_function_words_in_a_latin_target_are_flagged(self):
        assert C.english_leftover("The cell is the smallest unit of life and this is that from which all are made", "fr")
        assert C.english_leftover("La cellule est la plus petite unité du vivant, et tout organisme en est fait.", "fr") is None


def _seg(sid, text, *, who=("teacher", "student"), points=("one", "two"), expr=None, label="Nucleus"):
    lines = [{"who": w, "line": f"{text} {w}"} for w in who]
    scene = {"elements": [{"id": "t1", "type": "text", "text": label, "at": [0, 0]}]}
    if expr:
        scene["elements"].append({"id": "m1", "type": "math", "expr": expr, "at": [0, 1]})
    return {"segment_id": sid, "type": "explore", "text": " ".join(d["line"] for d in lines),
            "elevenlabs_text": text, "slide_heading": f"Heading {text}", "slide_points": list(points),
            "dialogue": lines, "scene": scene, "estimated_duration_seconds": 20}


SRC = {"segments": [_seg("s001", "The cell has 2 parts and 41.12 cm", expr="P = πr + 2r"),
                    _seg("s002", "Nucleus holds DNA")]}


def _translated(src, transform=_arabize):
    dst = copy.deepcopy(src)
    for seg in dst["segments"]:
        for d in seg["dialogue"]:
            d["line"] = transform(d["line"])
        seg["text"] = " ".join(d["line"] for d in seg["dialogue"])
        seg["slide_heading"] = transform(seg["slide_heading"])
        seg["slide_points"] = [transform(p) for p in seg["slide_points"]]
        for e in seg["scene"]["elements"]:
            if e["type"] == "text":
                e["text"] = transform(e["text"])
    return dst


class TestCheckScript:
    def test_a_faithful_translation_passes_with_only_the_leftover_flag(self):
        r = C.check_script(SRC, _translated(SRC), language="ar", glossary=[("nucleus", "النواة")])
        assert r["ok"] and r["blocking"] == []
        whats = {f["what"] for f in r["flags"]}
        # the fake keeps the English words, so the leftover and the glossary
        # flags fire — flags, not blocks (D3: the reviewer decides)
        assert "still reads as English" in whats and "glossary term not used" in whats

    def test_a_dropped_speaker_turn_blocks(self):
        dst = _translated(SRC)
        dst["segments"][1]["dialogue"].pop()
        r = C.check_script(SRC, dst, language="ar")
        assert not r["ok"] and any(f["what"] == "speaker turns differ" and f["where"] == "s002" for f in r["blocking"])

    def test_a_changed_number_blocks(self):
        dst = _translated(SRC, lambda s: _arabize(s.replace("41.12", "41.21")))
        r = C.check_script(SRC, dst, language="ar")
        assert any(f["what"] == "numbers differ" and "41.12" in f["missing"] for f in r["blocking"])

    def test_an_altered_math_expression_blocks(self):
        dst = _translated(SRC)
        dst["segments"][0]["scene"]["elements"][1]["expr"] = "P = 2πr"
        r = C.check_script(SRC, dst, language="ar")
        assert any(f["what"] == "protected expression altered" for f in r["blocking"])

    def test_reordered_segments_block_in_strict_mode_only(self):
        dst = _translated(SRC)
        dst["segments"].reverse()
        assert not C.check_script(SRC, dst, language="ar")["ok"]
        assert C.check_script(SRC, dst, language="ar", strict=False)["ok"]

    def test_the_lenient_mode_flags_a_retypeset_board_expression_instead_of_blocking(self):
        # the Circle pilot (2026-10-16): the Arabic board was recompiled by a
        # newer compiler and typeset the same maths a little differently —
        # the record itself was intact, so this is the reviewer's to judge
        dst = _translated(SRC)
        dst["segments"][0]["scene"]["elements"][1]["expr"] = "P = π·r + 2·r"
        strict = C.check_script(SRC, dst, language="ar")
        assert any(f["what"] == "protected expression altered" for f in strict["blocking"])
        lenient = C.check_script(SRC, dst, language="ar", strict=False)
        assert lenient["ok"]
        assert any(f["what"].startswith("board expressions typeset differently") and f["examples"] == ["P = πr + 2r"]
                   for f in lenient["flags"])
        # a caller-protected expression (the record's own problem) still blocks
        refused = C.check_script(SRC, dst, language="ar", strict=False, protected={"s001": ["P = πr + 2r"]})
        assert not refused["ok"]

    def test_the_lenient_mode_still_catches_a_lost_number(self):
        dst = _translated(SRC, lambda s: _arabize(s.replace("41.12", "")))
        r = C.check_script(SRC, dst, language="ar", strict=False)
        assert any(f["what"] == "numbers differ" for f in r["blocking"])


class TestQuestionProse:
    def test_maths_spans_keep_the_maths_and_free_the_words(self):
        assert C.maths_spans("Find the area of a circle with a radius of 6 cm. (Use π = 3.14)") == ["6", "π = 3.14"]
        assert C.maths_spans("Solve 2x + 3 = 11 for x.") == ["2x + 3 = 11"]
        assert C.maths_spans("A semicircle has a radius of 8 cm. Find its total perimeter.") == ["8"]
        assert C.maths_spans("Identify which of these shapes represents a circle") == []

    def test_a_translated_question_passes_when_its_maths_survives_and_blocks_when_not(self):
        src = "Find the area of a circle with a radius of 6 cm. (Use π = 3.14)"
        ok, lost = C.prose_maths_equivalent(src, "أوجد مساحة دائرة نصف قطرها 6 سم. (استخدم π = 3.14)")
        assert ok and lost == []
        ok, lost = C.prose_maths_equivalent(src, "أوجد مساحة دائرة نصف قطرها ٦ سم. (استخدم π = ٣٫١٤)")
        assert ok, "Arabic-Indic numerals are the same values"
        ok, lost = C.prose_maths_equivalent(src, "أوجد مساحة دائرة نصف قطرها 6 سم. (استخدم باي = 3.14)")
        assert not ok and lost == ["π = 3.14"], "the symbol was spelled out — the board would lose the expression"
        ok, lost = C.prose_maths_equivalent("Solve 2x + 3 = 11 for x.", "حل 2x + 3 = 12 بالنسبة إلى x.")
        assert not ok and "2x + 3 = 11" in lost

    def test_check_maths_lesson_judges_the_question_by_its_spans(self):
        src = {"examples": [{"label": "Example 1", "problem": "Find the area of a circle with a radius of 6 cm. (Use π = 3.14)",
                             "steps": [], "final_answer": ["A = 113.04"]}]}
        good = C.check_maths_lesson(src, {"examples": [{**src["examples"][0], "label": "مثال 1",
                                                        "problem": "أوجد مساحة دائرة نصف قطرها 6 سم. (استخدم π = 3.14)"}]})
        assert good["ok"], good
        bad = C.check_maths_lesson(src, {"examples": [{**src["examples"][0], "problem": "أوجد مساحة دائرة نصف قطرها 7 سم."}]})
        assert any(f["what"] == "maths in the question altered" for f in bad["blocking"])


class TestCheckMathsLesson:
    LESSON = {"topic": "Linear equations", "hook": [{"who": "teacher", "line": "Double it and add 5 gives 21."}],
              "examples": [{"label": "Example 1", "problem": "2x + 3 = 11", "steps": [
                  {"operation": "subtract 3", "before": ["2x + 3 = 11"], "after": ["2x = 8"],
                   "speech": "Take 3 from both sides: 2x equals 8."}], "final_answer": ["x = 4"]}]}

    def test_the_maths_is_untouched_and_the_speech_equivalent(self):
        dst = T.apply_strings(self.LESSON, [(p, _arabize(t)) for p, t in T.lesson_text_items(self.LESSON)])
        r = C.check_maths_lesson(self.LESSON, dst)
        assert r["ok"], r
        assert dst["examples"][0]["steps"][0]["after"] == ["2x = 8"]
        assert dst["examples"][0]["steps"][0]["speech"].startswith("ترجمة")

    def test_an_altered_after_line_or_a_lost_value_blocks(self):
        dst = copy.deepcopy(self.LESSON)
        dst["examples"][0]["steps"][0]["after"] = ["2x = 9"]
        assert any(f["what"] == "protected field altered" for f in C.check_maths_lesson(self.LESSON, dst)["blocking"])
        dst = copy.deepcopy(self.LESSON)
        dst["hook"][0]["line"] = "ضعفه زائد خمسة"
        assert any(f["what"] == "numbers differ" for f in C.check_maths_lesson(self.LESSON, dst)["blocking"])


class TestItems:
    SRC = {"id": "q-1", "item_type": "mcq", "answer_mode": "objective", "marks": 1, "difficulty": 2, "cognitive_level": "recall",
           "objective_ref": "o1", "stem": "A circle has a radius of 7 cm. Its diameter is?",
           "options": [{"key": "A", "text": "14 cm"}, {"key": "B", "text": "7 cm"}, {"key": "C", "text": "3.5 cm"}, {"key": "D", "text": "22 cm"}],
           "answer": {"key": "A"}, "marking_scheme": [], "distractor_rationale": {"B": {"why_wrong": "that is the radius"}},
           "explanation": "The diameter is twice the radius."}

    def test_item_text_items_take_the_words_and_leave_the_keys(self):
        paths = [p for p, _ in T.item_text_items(self.SRC)]
        assert "stem" in paths and "options[0].text" in paths and "distractor_rationale.B.why_wrong" in paths and "explanation" in paths
        assert not any(p.endswith(".key") for p in paths)
        dst = T.translated_item(self.SRC, [(p, _arabize(t)) for p, t in T.item_text_items(self.SRC)])
        assert dst["options"][0]["key"] == "A" and dst["options"][0]["text"].startswith("ترجمة") and dst["answer"] == {"key": "A"}
        assert C.check_item(self.SRC, dst)["ok"]

    def test_check_item_blocks_a_changed_number_a_changed_key_and_collapsed_options(self):
        dst = T.translated_item(self.SRC, [(p, _arabize(t)) for p, t in T.item_text_items(self.SRC)])
        dst["options"][0]["text"] = "ترجمة 15 cm"
        assert any(f["what"] == "numbers differ" for f in C.check_item(self.SRC, dst)["blocking"])
        dst = T.translated_item(self.SRC, [(p, _arabize(t)) for p, t in T.item_text_items(self.SRC)])
        dst["answer"] = {"key": "B"}
        assert any(f["what"] == "answer key changed" for f in C.check_item(self.SRC, dst)["blocking"])
        dst = T.translated_item(self.SRC, [(p, _arabize(t)) for p, t in T.item_text_items(self.SRC)])
        dst["options"][1]["text"] = dst["options"][0]["text"]
        r = C.check_item(self.SRC, dst)
        assert any(f["what"] == "translated options are no longer distinct" for f in r["blocking"])

    def test_a_translated_items_hash_tells_arabic_stems_apart_and_is_scoped_to_the_kit(self):
        # catalogue.key.canonical_key folds non-Latin text to "" — the stock
        # content_hash would give every Arabic item of the bank one hash
        a = {"item_type": "mcq", "stem": "ما هو نصف قطر الدائرة؟"}
        b = {"item_type": "mcq", "stem": "ما هي مساحة الدائرة؟"}
        assert T._item_hash(a, "kit-1") != T._item_hash(b, "kit-1")
        assert T._item_hash(a, "kit-1") == T._item_hash({"item_type": "mcq", "stem": "  ما هو نصف قطر الدائرة  "}, "kit-1")
        assert T._item_hash(a, "kit-1") != T._item_hash(a, "kit-2"), "a re-translation for a new kit must not collide with the retired kit's items"

    def test_a_match_items_pairs_read_the_same_in_options_and_answer(self):
        pairs = [{"left": "Radius", "right": "centre to the boundary"}, {"left": "Chord", "right": "two boundary points"}]
        src = {"id": "m-1", "item_type": "match", "answer_mode": "objective", "marks": 2, "difficulty": 1,
               "cognitive_level": "recall", "objective_ref": "o1", "stem": "Match each term to its description.",
               "options": {"pairs": [dict(p) for p in pairs]}, "answer": {"pairs": [dict(p) for p in pairs]},
               "marking_scheme": [], "distractor_rationale": None, "explanation": None}
        seen: dict[str, int] = {}

        def numbered(s: str) -> str:            # a different rendering every time it is asked (no digits: those are checked)
            seen[s] = seen.get(s, 0) + 1
            return f"ترجمة {s} {'ـ' * seen[s]}"

        c = FakeTranslator(numbered)
        words = T.item_text_items(src)
        got = T.translate_strings(c, [w for _, w in words], "ar", context=T.ITEM_RULE)
        dst = T.translated_item(src, list(zip([p for p, _ in words], got)))
        assert dst["options"]["pairs"] == dst["answer"]["pairs"], "the same term must read the same twice"
        assert all(n == 1 for n in seen.values()), "each distinct string is asked once"
        assert C.check_item(src, dst)["ok"]

    def test_fidelity_scores_a_faithful_back_translation_high_and_flags_a_stray_one(self):
        rep = C.fidelity_report([("s1", "The cell is the smallest unit of life", "The cell is the smallest unit of life."),
                                 ("s2", "Plants have a cell wall and chloroplasts", "The weather was fine today")])
        assert rep["ok"] and rep["samples"] == 2 and [f["where"] for f in rep["flags"]] == ["s2"]

    def test_the_composer_accepts_staged_items_of_the_kit_only(self):
        from catalogue import composer as CO
        sb = FakeSB()
        sb.tables["topic_questions"] = [
            {"id": "a", "topic_id": "t", "language": "ar", "status": "approved"},
            {"id": "b", "topic_id": "t", "language": "ar", "status": "staged", "translation_kit_id": "kit-ar"},
            {"id": "c", "topic_id": "t", "language": "ar", "status": "staged", "translation_kit_id": "kit-other"},
        ]
        assert [r["id"] for r in CO.load_items_by_id(sb, ["a", "b"], topic_id="t", language="ar", staged_for="kit-ar")] == ["a", "b"]
        with pytest.raises(RuntimeError, match="no longer approved"):
            CO.load_items_by_id(sb, ["a", "b"], topic_id="t", language="ar")
        with pytest.raises(RuntimeError, match="no longer approved"):
            CO.load_items_by_id(sb, ["c"], topic_id="t", language="ar", staged_for="kit-ar")
        assert {r["id"] for r in CO.load_bank(sb, "t", "ar", staged_for="kit-ar")} == {"a", "b"}
        assert {r["id"] for r in CO.load_bank(sb, "t", "ar")} == {"a"}


class TestPostRender:
    def test_drift_and_silence_flag_and_a_silent_segment_blocks(self):
        script = {"segments": [{"segment_id": "s1", "text": "a"}, {"segment_id": "s2", "text": "b"},
                               {"segment_id": "s3", "text": "c"}]}
        video = {"segments": [{"segment_id": "s1", "audio_duration_seconds": 10.0, "clip_duration_seconds": 10.5},
                              {"segment_id": "s2", "audio_duration_seconds": 10.0, "clip_duration_seconds": 18.0},
                              {"segment_id": "s3", "audio_duration_seconds": 0.0, "clip_duration_seconds": 4.0}]}
        r = C.post_render(script, video, overlaps=[{"segment_id": "s1", "pair": ["a", "b"]}])
        assert not r["ok"]
        assert {f["what"] for f in r["blocking"]} == {"no audio for a spoken segment"}
        assert {(f["where"], f["what"]) for f in r["flags"]} >= {
            ("s2", "clip and narration drift"), ("s2", "long silence after the narration"),
            ("s1", "text overlap reported by acceptance")}

    def test_merge_reports_unions_the_findings(self):
        m = C.merge_reports({"check": "A", "ok": True, "blocking": [], "flags": [{"what": "x"}]},
                            {"check": "C", "ok": False, "blocking": [{"what": "y"}], "flags": []})
        assert m["ok"] is False and [f["what"] for f in m["blocking"]] == ["y"] and len(m["checks"]) == 2


# ── the words in, the words out ─────────────────────────────────────────


class TestFieldSelection:
    def test_a_science_segment_round_trips_and_the_narration_follows_the_dialogue(self):
        seg = SRC["segments"][0]
        items = T.segment_text_items(seg)
        paths = [p for p, _ in items]
        assert "dialogue[0].line" in paths and "slide_points[1]" in paths and "scene.elements[0].text" in paths
        assert not any(".expr" in p for p in paths), "a math element is never sent to the translator"
        out = T._finish_segment(T.apply_strings(seg, [(p, _arabize(t)) for p, t in items]))
        assert out["text"] == " ".join(d["line"] for d in out["dialogue"]) and out["text"].startswith("ترجمة")
        assert out["elevenlabs_text"] == out["text"]
        assert out["scene"]["elements"][1]["expr"] == "P = πr + 2r"

    def test_lesson_text_items_take_the_spoken_fields_and_leave_the_maths(self):
        from tests.test_maths_lesson import LESSON
        paths = [p for p, _ in T.lesson_text_items(LESSON)]
        assert "hook[0].line" in paths and "method.steps[0]" in paths and "closing" in paths
        assert "examples[0].steps[0].speech" in paths and "try_it.speech" in paths
        assert any(p.endswith("common_mistake.why_wrong") for p in paths) and any(p.endswith("common_mistake.speech") for p in paths)
        # a QUESTION with words is translated (the founder's review of the
        # first Arabic video); a bare equation ("3x + 5 = 20", this fixture)
        # is typeset and stays; the working stays maths
        assert not any(p.endswith(".problem") for p in paths), "the fixture's questions are bare equations"
        import copy as _copy
        prose = _copy.deepcopy(LESSON)
        prose["examples"][0]["problem"] = "A coin has a radius of 4 cm. Find its circumference. (Use π = 3.14)"
        assert "examples[0].problem" in [p for p, _ in T.lesson_text_items(prose)]
        assert not any(p.endswith((".before[0]", ".after[0]", ".final_answer[0]", ".operation", ".givens[0]")) for p in paths)

    def test_article_text_items_leave_ids_codes_and_figure_keys(self):
        paths = [p for p, _ in T.article_text_items(ARTICLE)]
        assert "title" in paths and "sections[0].heading" in paths and "sections[0].body_md" in paths
        assert "glossary[0].term" in paths and "glossary[0].definition" in paths and "claims[0].text" in paths
        assert not any("figure_keys" in p or "covers" in p or p.endswith(".id") for p in paths)

    def test_translate_strings_keeps_order_skips_empties_and_chunks(self, monkeypatch):
        monkeypatch.setattr(T, "CHUNK", 3)
        c = FakeTranslator(lambda s: s.upper())
        out = T.translate_strings(c, ["a", "", "b", "c", "d", "e"], "fr")
        assert out == ["A", "", "B", "C", "D", "E"] and len(c.calls) == 2

    def test_a_reply_with_the_wrong_count_is_asked_again_then_refused(self):
        class Short(FakeTranslator):
            def analyze(self, prompt, **k):
                self.calls.append(prompt)
                return {"data": {"items": ["only one"]}, "usage": {}}
        with pytest.raises(RuntimeError, match="did not return 2 items"):
            T.translate_strings(Short(), ["a", "b"], "fr")


# ── one part: the maths path recompiles the board ───────────────────────


def _maths_body():
    from maths.lesson import to_episode_script
    from maths.schema import Lesson
    from tests.test_maths_lesson import LESSON
    ep = to_episode_script(Lesson.model_validate(LESSON), {"status": "verified"}, book_id="catalogue-t-lin",
                           chapter_num=-1, episode_num=1, title="Linear Equations in One Variable",
                           avatars={"teacher": "avatar_teacher_female__face_f1", "student": "avatar_student_male__face_m1"},
                           language="en").model_dump()
    return {"part": 1, "of": 1, "language": "en", "voice": "g-en-f", "student_voice": "g-en-student-m",
            "avatars": ep["avatars"], "script": ep, "video": {"segments": []}}


class TestTranslateEpisodeMaths:
    def test_the_board_is_recompiled_in_the_language_and_the_maths_is_identical(self):
        body = _maths_body()
        c = FakeTranslator()
        dst, report = T.translate_episode(c, body, "ar", [("equation", "معادلة")], context="")
        src = body["script"]
        assert report["ok"], report["blocking"]
        assert dst["maths"]["language"] == "ar"
        assert dst["maths"]["lesson"]["examples"][0]["steps"][0]["after"] == src["maths"]["lesson"]["examples"][0]["steps"][0]["after"]
        assert dst["maths"]["lesson"]["try_it"]["problem"] == src["maths"]["lesson"]["try_it"]["problem"]
        assert dst["maths"]["lesson"]["hook"][0]["line"].startswith("ترجمة")
        assert dst["avatars"] == src["avatars"], "the same faces in every language"
        # the board's own words come from maths.i18n, not the translator:
        # the try-it board says "try it" in Arabic, never in English
        board = " ".join(e.get("text") or "" for s in dst["segments"]
                         for e in ((s.get("scene") or {}).get("elements") or []) if e.get("type") == "text")
        assert "جرّب" in board and "Try it" not in board and "Pause the video" not in board
        assert abs(len(dst["segments"]) - len(src["segments"])) <= 2
        # the structural model accepts what the compiler produced
        from agent3_scripts.models import EpisodeScript
        EpisodeScript.model_validate(dst)

    def test_every_example_of_an_extended_record_reaches_the_recompiled_board(self):
        """The English kit's record may hold more than the schema's four
        examples (the length floor appends verified ones after validation);
        the recompile must keep them all — the Circle pilot lost four."""
        import copy as _copy
        from maths.schema import Lesson
        from tests.test_maths_lesson import LESSON
        rec = _copy.deepcopy(LESSON)
        base = rec["examples"][0]
        rec["examples"] = [{**_copy.deepcopy(base), "label": f"Example {i}", "problem": f"{i}x + 1 = {i * 3 + 1}",
                            "final_answer": ["x = 3"]} for i in range(1, 7)]
        assert len(Lesson.model_validate(rec).examples) == 4, "the schema caps a model reply at four"
        built = T.lesson_from_record(rec)
        assert [e.label for e in built.examples] == [f"Example {i}" for i in range(1, 7)]
        body = {"part": 1, "of": 1, "language": "en", "avatars": {}, "video": {},
                "script": {"script_id": "sc", "book_id": "b", "chapter_num": -1, "episode_num": 1,
                           "episode_title": "Linear", "generated_at": "now", "narrator_persona": "x",
                           "segments": [], "visual_plan": None, "avatars": None,
                           "maths": {"lesson": rec, "verification": {}, "language": "en"},
                           "total_estimated_duration_seconds": 1, "question_hook_count": 0}}
        dst = T.recompile_maths(body["script"], rec, "ar", "خطي")
        board = " ".join(e.get("expr") or "" for s in dst["segments"]
                         for e in ((s.get("scene") or {}).get("elements") or []) if e.get("type") == "math")
        assert all(f"{i}x + 1 = {i * 3 + 1}" in board for i in range(1, 7)), board

    def test_a_translation_that_changes_a_spoken_number_is_retried_then_refused(self):
        body = _maths_body()
        c = FakeTranslator(lambda s: _arabize(s.replace("five", "5").replace("x equals 5", "x equals 6")) if "equals" in s else _arabize(s))
        with pytest.raises(T.TranslateRefused, match="pre-check A"):
            T.translate_episode(c, body, "ar", [], context="")
        assert c.session_usage["calls"] >= 2, "the part is asked again once before the job gives up"


class TestTranslateEpisodeScience:
    def test_segments_keep_ids_turns_and_scene_plan(self):
        body = {"part": 1, "of": 1, "language": "en", "avatars": {}, "video": {},
                "script": {"script_id": "sc", "book_id": "b", "chapter_num": -1, "episode_num": 1,
                           "episode_title": "Cells", "generated_at": "now", "narrator_persona": "x",
                           "segments": copy.deepcopy(SRC["segments"]), "visual_plan": None, "avatars": None,
                           "total_estimated_duration_seconds": 40, "question_hook_count": 0}}
        dst, report = T.translate_episode(FakeTranslator(), body, "ar", [], context="")
        assert report["ok"], report["blocking"]
        assert [s["segment_id"] for s in dst["segments"]] == ["s001", "s002"]
        assert dst["segments"][0]["scene"]["elements"][1]["expr"] == "P = πr + 2r"
        assert dst["segments"][0]["dialogue"][1]["who"] == "student"
        assert dst["episode_title"].startswith("ترجمة")


# ── the job ──────────────────────────────────────────────────────────────

SCRIPT_PATH = "sys/gen-p/script.json"


def _job_sb(*, kit_status="approved", with_assets=True):
    sb = _sb(kit_row={**KIT, "status": kit_status, "source_kit_id": None, "voice_pair": {"teacher": "g-en-f", "student": "g-en-student-m"}})
    sb.tables["artifacts"] = [{"id": "a1", "generation_id": "gen-p", "kind": "video_mp4", "storage_path": "sys/gen-p/lesson.mp4"},
                              {"id": "a2", "generation_id": "gen-p", "kind": "script_json", "storage_path": SCRIPT_PATH}]
    body = _maths_body()
    sb.files[("artifacts", SCRIPT_PATH)] = json.dumps(body).encode("utf-8")
    sb.tables["topic_term_translations"] = []
    # the roster rows as the library keeps them: asset_key = the ROSTER key,
    # canonical_key = the LIBRARY normaliser's spelling (avatar_female_teacher,
    # not the topic-key one's), the face key's suffix = the row id's prefix
    from shared.asset_keys import canonical_key as _ck
    assert _ck("avatar_teacher_female") == "avatar_female_teacher"
    sb.tables["visual_assets"] = ([{"id": "f1a2b3c4-0000-4000-8000-000000000001", "asset_key": "avatar_teacher_female",
                                    "canonical_key": _ck("avatar_teacher_female"), "storage_path": "lib/f1.png", "status": "approved"},
                                   {"id": "m1a2b3c4-0000-4000-8000-000000000002", "asset_key": "avatar_student_male",
                                    "canonical_key": _ck("avatar_student_male"), "storage_path": "lib/m1.png", "status": "approved"}]
                                  if with_assets else [])
    sb.tables["jobs"] = [{"id": "job-t", "type": "kit_translate", "status": "processing", "generation_id": None,
                          "params": {"source_kit_id": "kit-1", "language": "ar"}}]
    # the English bank: 12 approved items (two kinds), and the source kit's
    # composed quiz — a worksheet with a question set of 4 of them
    sb.tables["topic_questions"] = [_item(i) for i in range(1, 13)]
    sb.tables["topic_kits"][0]["doc_generation_ids"] = {**KIT["doc_generation_ids"], "quiz": "gen-quiz", "question_bank": "gen-bank"}
    sb.tables["question_sets"] = [{"id": "set-en", "blueprint_id": "bp-1", "topic_ids": ["t-cell"], "language": "en",
                                   "question_ids": ["q-3", "q-1", "q-8", "q-5"], "seed": 42}]
    sb.tables["generations"] += [
        {"id": "gen-quiz", "kind": "worksheet", "status": "done", "owner_id": "sys", "book_id": None, "chapter_ref": None,
         "params": {"catalogue": True, "topic_id": "t-cell", "article_id": "art-1", "language": "en",
                    "question_set_id": "set-en", "blueprint_id": "bp-1", "seed": 42}},
        {"id": "gen-bank", "kind": "question_bank", "status": "done", "owner_id": "sys", "book_id": None, "chapter_ref": None,
         "params": {**PARAMS}},
    ]
    return sb


_PARTS = ["nucleus", "membrane", "wall", "vacuole", "ribosome", "cytoplasm",
          "chloroplast", "mitochondrion", "lysosome", "nucleolus", "vesicle", "centriole"]


def _item(i: int) -> dict:
    # stems differ in WORDS, not only in a number: content_hash canonicalises
    # the stem, so two stems that differ only by a digit are one item
    mcq = i % 2 == 1
    part = _PARTS[(i - 1) % len(_PARTS)]
    return {"id": f"q-{i}", "topic_id": "t-cell", "article_id": "art-1", "language": "en", "status": "approved",
            "item_type": "mcq" if mcq else "fill_blank", "answer_mode": "objective", "difficulty": 1 + i % 3,
            "cognitive_level": "recall", "marks": 1, "est_seconds": 60, "objective_ref": "o1", "claim_ref": None,
            "stem": f"Which part of a cell is the {part} and what does it do?" if mcq else f"The {part} of a cell is ____.",
            "options": ([{"key": "A", "text": "a nucleus"}, {"key": "B", "text": "a wall"},
                         {"key": "C", "text": "a membrane"}, {"key": "D", "text": "a vacuole"}] if mcq else None),
            "answer": {"key": "A"} if mcq else {"text": "a nucleus"},
            "marking_scheme": [], "distractor_rationale": ({"B": {"why_wrong": "walls are plant only"}} if mcq else None),
            "explanation": "Because the nucleus holds the DNA.", "tags": [], "content_hash": f"h{i}"}


def _uploads(monkeypatch):
    uploads = []
    from worker import client as db

    def _upload(_sb, local_path, dest):
        uploads.append((dest, json.loads(open(local_path, encoding="utf-8").read())))
        return dest
    monkeypatch.setattr(db, "upload_artifact", _upload)
    return uploads


class TestTranslateJob:
    def test_a_kit_is_translated_end_to_end_without_a_single_render_call(self, monkeypatch, tmp_path):
        sb = _job_sb()
        uploads = _uploads(monkeypatch)
        monkeypatch.setattr("spike.scene_engine.raster_assets.cache_dir_for", lambda key, cache_dir=None: tmp_path / "nocache" / key)
        c = FakeTranslator()

        summary = T.run_translate_job(sb, sb.tables["jobs"][0], client=c)

        assert summary and summary["step"] == "done" and summary["language"] == "ar" and summary["precheck_ok"] is True
        job = sb.tables["jobs"][0]
        assert job["status"] == "done" and job["error"] is None
        # the translated kit row: lineage, staged, the language's voices
        kits = [k for k in sb.tables["topic_kits"] if k.get("source_kit_id") == "kit-1"]
        assert len(kits) == 1
        k = kits[0]
        assert (k["language"], k["status"], k["translation_status"]) == ("ar", "generating", "staged")
        assert k["voice_pair"] == {"teacher": "g-ar-f", "student": "g-ar-student-m"}
        # the article: staged, the source named, the figures copied
        arts = [a for a in sb.tables["topic_articles"] if a["language"] == "ar"]
        assert len(arts) == 1 and arts[0]["status"] == "staged" and arts[0]["source_article_id"] == "art-1"
        assert arts[0]["title"].startswith("ترجمة") and arts[0]["sections"][0]["id"] == "s1"
        assert arts[0]["sections"][1]["figure_keys"] == ["animal_cell", "plant_cell"]
        assert k["article_id"] == arts[0]["id"]
        copied = [f for f in sb.tables["article_figures"] if f["article_id"] == arts[0]["id"]]
        assert len(copied) == len(FIGURES) and all(f["caption"].startswith("ترجمة") for f in copied if f["caption"])
        # the glossary, pinned first
        terms = sb.tables["topic_term_translations"]
        assert {t["term_en"] for t in terms} == {"cell", "organelle"} and all(t["language"] == "ar" and t["source"] == "model" for t in terms)
        assert "cell → ترجمة cell" in c.calls[-1] or any("cell → ترجمة cell" in p for p in c.calls)
        # the bank (D4): every English item staged for THIS kit, by source id
        staged = [q for q in sb.tables["topic_questions"] if q.get("translation_kit_id") == k["id"]]
        assert len(staged) == 12 and all(q["status"] == "staged" and q["language"] == "ar" and q["article_id"] == arts[0]["id"] for q in staged)
        by_src = {q["source_item_id"]: q for q in staged}
        assert by_src["q-1"]["stem"].startswith("ترجمة") and by_src["q-1"]["answer"] == {"key": "A"}
        assert [o["key"] for o in by_src["q-1"]["options"]] == ["A", "B", "C", "D"] and by_src["q-1"]["options"][0]["text"].startswith("ترجمة")
        assert by_src["q-2"]["answer"]["text"].startswith("ترجمة"), "a text answer is translated"
        assert by_src["q-1"]["content_hash"] != "h1", "the hash is the translated stem's"
        # the quiz: the SAME four questions, by id, in the source set's order
        sets = [s for s in sb.tables["question_sets"] if s["language"] == "ar"]
        assert len(sets) == 1 and sets[0]["question_ids"] == [by_src[i]["id"] for i in ("q-3", "q-1", "q-8", "q-5")]
        assert sets[0]["blueprint_id"] == "bp-1" and sets[0]["seed"] == 42
        # the generations: every piece of the source kit
        gens = [g for g in sb.tables["generations"] if (g["params"].get("translation") or {}).get("kit_id") == k["id"]]
        labels = {("quiz" if g["params"].get("question_set_id") else g["kind"]): g for g in gens}
        assert set(labels) == {"presentation", "deck", "activity", "case_study", "worksheet", "question_bank", "quiz"}
        p = labels["presentation"]["params"]
        assert p["catalogue"] is True and p["language"] == "ar" and p["tts_voice"] == "g-ar-f" and p["article_id"] == arts[0]["id"]
        assert p["translation"]["source_kit_id"] == "kit-1" and p["translation"]["scripts"] == [f"sys/{k['id']}/translated/script.json"]
        assert "scripts" not in labels["deck"]["params"]["translation"]
        assert labels["presentation"]["owner_id"] == "sys" and labels["presentation"]["book_id"] is None
        q = labels["quiz"]["params"]
        assert "kit_id" not in q and q["question_set_id"] == sets[0]["id"] and q["translation"]["kit_id"] == k["id"], "a composed set has no kit_id; the marker ties it to the kit"
        assert k["presentation_generation_id"] == labels["presentation"]["id"]
        assert set(k["doc_generation_ids"]) == {"deck", "activity", "case_study", "worksheet", "question_bank", "quiz"}
        # pre-check B ran on a sample and flagged nothing (the fake keeps the words)
        assert k["precheck"]["bank"] == {"source_items": 12, "staged": 12, "reused": 0}
        fid = next(c for c in k["precheck"]["checks"] if c.get("check") == "B")
        assert fid["samples"] >= 4 and fid["mean_score"] is not None and fid["mean_score"] > 0.9
        # the staged script: the translated lesson, Arabic board words
        assert [d for d, _ in uploads] == [f"sys/{k['id']}/translated/script.json"]
        staged = uploads[0][1]
        assert staged["language"] == "ar" and staged["script"]["maths"]["language"] == "ar"
        # provenance and the pre-check on the kit
        tr = k["translation"]
        assert tr["source_scripts"][0]["path"] == SCRIPT_PATH and tr["assets"] and tr["cost"]["translation"]["usage"]["calls"] >= 3
        assert k["precheck"]["ok"] is True and k["precheck"]["parts"] == [{"part": 1, "ok": True, "blocking": 0, "flags": k["precheck"]["parts"][0]["flags"]}]
        # nothing from the English pipeline was asked for
        assert ("select", "books") not in sb.calls
        assert sb.tables["topics"][0]["status"] == TOPIC_STATUS_UNCHANGED(sb)

    def test_a_missing_picture_fails_the_job_and_the_kit_instead_of_regenerating(self, monkeypatch, tmp_path):
        sb = _job_sb(with_assets=False)
        _uploads(monkeypatch)
        monkeypatch.setattr("spike.scene_engine.raster_assets.cache_dir_for", lambda key, cache_dir=None: tmp_path / "nocache" / key)

        assert T.run_translate_job(sb, sb.tables["jobs"][0], client=FakeTranslator()) is None

        job = sb.tables["jobs"][0]
        assert job["status"] == "error" and "cannot be found" in job["error"] and "avatar_teacher_female__face_f1" in job["error"]
        k = next(k for k in sb.tables["topic_kits"] if k.get("source_kit_id") == "kit-1")
        assert k["status"] == "failed" and k["translation_status"] == "failed" and "cannot be found" in k["notes"]
        assert not [g for g in sb.tables["generations"] if g["params"].get("kit_id") == k["id"]], "nothing was queued to render"

    def test_a_pre_check_refusal_leaves_its_findings_on_the_kit(self, monkeypatch, tmp_path):
        sb = _job_sb()
        _uploads(monkeypatch)
        monkeypatch.setattr("spike.scene_engine.raster_assets.cache_dir_for", lambda key, cache_dir=None: tmp_path / "nocache" / key)
        c = FakeTranslator(lambda s: _arabize(s.replace("five", "5").replace("x equals 5", "x equals 6")) if "equals" in s else _arabize(s))

        assert T.run_translate_job(sb, sb.tables["jobs"][0], client=c) is None

        k = next(k for k in sb.tables["topic_kits"] if k.get("source_kit_id") == "kit-1")
        assert k["status"] == "failed" and k["translation_status"] == "failed"
        assert k["precheck"]["ok"] is False and k["precheck"]["part"] == 1
        assert any(f["what"] == "numbers differ" for f in k["precheck"]["blocking"])
        assert "pre-check A" in sb.tables["jobs"][0]["error"]

    @pytest.mark.parametrize("params, why", [
        ({"source_kit_id": "kit-1", "language": "en"}, "already English"),
        ({"source_kit_id": "kit-1", "language": "xx"}, "not a lesson language"),
        ({"source_kit_id": "nope", "language": "ar"}, "not found"),
        ({"language": "ar"}, "source_kit_id"),
    ])
    def test_refusals_before_any_call(self, params, why, monkeypatch):
        sb = _job_sb()
        sb.tables["jobs"][0]["params"] = params
        c = FakeTranslator()
        assert T.run_translate_job(sb, sb.tables["jobs"][0], client=c) is None
        assert why in sb.tables["jobs"][0]["error"] and c.calls == []

    def test_an_unapproved_source_kit_is_refused(self):
        sb = _job_sb(kit_status="in_review")
        c = FakeTranslator()
        assert T.run_translate_job(sb, sb.tables["jobs"][0], client=c) is None
        assert "not approved" in sb.tables["jobs"][0]["error"] and c.calls == []

    def test_a_source_without_a_script_json_is_refused(self):
        sb = _job_sb()
        sb.tables["artifacts"] = [a for a in sb.tables["artifacts"] if a["kind"] != "script_json"]
        assert T.run_translate_job(sb, sb.tables["jobs"][0], client=FakeTranslator()) is None
        assert "script_json" in sb.tables["jobs"][0]["error"]


def TOPIC_STATUS_UNCHANGED(sb):
    # the source topic is wherever it was (published, say): a translation
    # never moves it — the fixture's topic says "generating" and so must it
    return "generating"


# ── the render: script_override in worker.process ───────────────────────

TRANSLATED_SCRIPT_PATH = "sys/kit-ar/translated/script.json"


def _override_script():
    heads = ["ما هي الخلية", "تراكيب مشتركة", "الخلايا النباتية"]
    segs = [{"segment_id": f"s1{i:02d}", "type": "explore", "text": f"المعلم {i}. يسأل الطالب.",
             "elevenlabs_text": f"المعلم {i}. يسأل الطالب.", "slide_heading": h, "slide_points": [],
             "dialogue": [{"who": "teacher", "line": f"المعلم {i}."}, {"who": "student", "line": "يسأل الطالب."}],
             "estimated_duration_seconds": 50} for i, h in enumerate(heads, start=1)]
    return {"script_id": "sc-ar", "book_id": "catalogue-t-cell", "chapter_num": -1, "episode_num": 1,
            "episode_title": "الخلايا", "generated_at": "now", "narrator_persona": "conversational",
            "segments": segs, "visual_plan": None,
            "avatars": {"teacher": "avatar_teacher_female__face_f1", "student": "avatar_student_male__face_m1"},
            "total_estimated_duration_seconds": 150, "question_hook_count": 0}


def _render_sb():
    sb = _sb(statuses={"gen-p": "done"})
    sb.tables["topic_articles"].append({**ARTICLE, "id": "art-ar", "language": "ar", "status": "staged",
                                        "source_article_id": "art-1", "title": "الخلايا"})
    sb.tables["topic_kits"][0]["status"] = "approved"
    sb.tables["topic_kits"].append({**KIT, "id": "kit-ar", "article_id": "art-ar", "language": "ar", "status": "generating",
                                    "source_kit_id": "kit-1", "translation_status": "staged",
                                    "presentation_generation_id": "gen-par", "doc_generation_ids": {"deck": "gen-dar"},
                                    "translation": {"source_kit_id": "kit-1"}, "precheck": {"ok": True, "blocking": [], "flags": []}})
    tr = {"source_kit_id": "kit-1", "source_generation_id": "gen-p", "source_language": "en", "scripts": [TRANSLATED_SCRIPT_PATH]}
    params = {**PARAMS, "kit_id": "kit-ar", "article_id": "art-ar", "language": "ar", "tts_voice": "g-ar-f",
              "student_voice": "g-ar-student-m", "translation": tr}
    sb.tables["generations"].append({"id": "gen-par", "kind": "presentation", "status": "processing", "owner_id": "sys",
                                     "book_id": None, "chapter_ref": None, "params": params})
    sb.tables["generations"].append({"id": "gen-dar", "kind": "deck", "status": "queued", "owner_id": "sys",
                                     "book_id": None, "chapter_ref": None,
                                     "params": {**params, "translation": {k: v for k, v in tr.items() if k != "scripts"}}})
    sb.files[("artifacts", TRANSLATED_SCRIPT_PATH)] = json.dumps(
        {"part": 1, "of": 1, "language": "ar", "script": _override_script()}).encode("utf-8")
    job = _job("job-par", "gen-par", "presentation")
    job["params"]["kit_id"] = "kit-ar"
    sb.tables["jobs"] = [job]
    return sb


class TestScriptOverrideRender:
    def test_a_translated_presentation_renders_the_staged_script_with_no_analysis_and_no_script_call(self, monkeypatch, tmp_path):
        sb = _render_sb()
        process, uploads, analysis_calls = _worker_env(monkeypatch, sb)
        fakes = _presentation_fakes(monkeypatch, tmp_path)
        forbidden: list[bool] = []
        monkeypatch.setattr("spike.scene_engine.raster_assets.set_generation_forbidden",
                            lambda on, generation_id=None: forbidden.append(bool(on)))

        process.process_generation(sb, sb.tables["jobs"][0], "gen-par")

        assert analysis_calls == [], "a translated build never analyses"
        assert fakes["scripts"] == [], "a translated build never writes a script"
        assert forbidden == [True, False], "image generation forbidden for the build, released after"
        # the slides took the translated segments, the composer the Arabic voices
        assert fakes["slide_inputs"] == [["s101", "s102", "s103"]]
        assert fakes["compose_calls"] == [{"student_voice": "g-ar-student-m", "tts_voice": "g-ar-f", "lang": "ar"}]
        # the part's own script_json: Arabic, the source kit's faces
        body = json.loads(next(raw for _, d, raw in uploads if d.endswith("gen-par/script.json")).decode("utf-8"))
        assert body["language"] == "ar" and body["voice"] == "g-ar-f"
        assert body["script"]["segments"][0]["slide_heading"] == "ما هي الخلية"
        assert body["avatars"] == _override_script()["avatars"]
        # the kit lifecycle ran: timestamps, a lesson plan in the language
        # carrying the translation marker without the scripts
        k = next(k for k in sb.tables["topic_kits"] if k["id"] == "kit-ar")
        assert [c["part"] for c in k["chapters"]] == [1] and k["chapters"][0]["chapters"][0]["label"] == "ما هي الخلية"
        lp = next(g for g in sb.tables["generations"] if g["kind"] == "lesson_plan")
        assert lp["params"]["language"] == "ar" and lp["params"]["article_id"] == "art-ar"
        assert lp["params"]["translation"] == {"source_kit_id": "kit-1", "source_generation_id": "gen-p", "source_language": "en"}
        assert k["doc_generation_ids"]["lesson_plan"] == lp["id"]
        # pre-check C recorded on the kit; the English kit and its topic untouched
        post = k["precheck"]["post_render"]
        assert post["ok"] is True and post["checks"][0]["check"] == "C" and k["precheck"]["ok"] is True
        assert sb.tables["topic_kits"][0]["status"] == "approved"
        gen = next(g for g in sb.tables["generations"] if g["id"] == "gen-par")
        assert gen["status"] == "done"
        assert ("select", "books") not in sb.calls

    def test_a_translated_kit_in_review_carries_translation_status_in_review(self):
        """approve_topic_kit's translation branch accepts in_review only, so
        the lifecycle column follows the kit's status; an English kit (no
        translation_status) is untouched."""
        from catalogue import kit as K
        sb = _render_sb()
        k = sb.tables["topic_kits"][1]
        k["doc_generation_ids"] = {"deck": "gen-dar", "lesson_plan": "gen-lar"}
        for gid in ("gen-par", "gen-dar"):
            next(g for g in sb.tables["generations"] if g["id"] == gid)["status"] = "done"
        sb.tables["generations"].append({"id": "gen-lar", "kind": "lesson_plan", "status": "done", "owner_id": "sys",
                                         "book_id": None, "chapter_ref": None, "params": {}})
        assert K.sync_kit_completion(sb, k) is True
        row = next(r for r in sb.tables["topic_kits"] if r["id"] == "kit-ar")
        assert (row["status"], row["translation_status"]) == ("in_review", "in_review")
        assert sb.tables["topics"][0]["status"] == "generating", "the topic is not a translation's to move"
        en = sb.tables["topic_kits"][0]
        assert "translation_status" not in en or en["translation_status"] is None

    def test_a_staged_article_is_accepted_only_for_a_translation(self):
        from catalogue import kit as K
        sb = _render_sb()
        gen = next(g for g in sb.tables["generations"] if g["id"] == "gen-par")
        prepared = K.prepare(sb, gen)
        assert prepared.translation["scripts"] == [TRANSLATED_SCRIPT_PATH] and prepared.language == "ar"
        # the same staged article behind a kit that is NOT a translation is refused
        plain = {**gen, "params": {k: v for k, v in gen["params"].items() if k != "translation"}}
        sb.tables["topic_kits"][1]["source_kit_id"] = None
        with pytest.raises(K.CatalogueRefused, match="not approved"):
            K.prepare(sb, plain)
