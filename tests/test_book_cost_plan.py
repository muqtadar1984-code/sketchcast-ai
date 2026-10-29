"""Book cost plan (founder 2026-10-26): one-pass drafting, review + gates in one
call, re-check only what a revise round changed, earlier chapters' questions
and pictures told to the author, book images at 2K. Fake clients only: no model,
image, database or production call is made."""
import copy
import json
import re

import pytest

from publishing.manuscript import author as A
from publishing.manuscript import validate as V
from publishing.tools import book_chapters as BC

WORDS = " ".join(["Forces push or pull on objects and change how they move."] * 50)   # 550 words


def _ex(xid, tier, stem):
    return {"xid": xid, "tier": tier, "item_type": "short_answer", "stem": stem, "answer": "a push or a pull",
            "worked_solution": "A force is a push or a pull.", "marks": 1, "node_code": "7Pf.01",
            "marking_scheme": [{"point": "states that a force is a push or a pull", "marks": 1}],
            "source_refs": ["claim:a"]}


EXERCISES = [_ex("x1", "guided", "Name the kind of force a magnet exerts on a paper clip held near it."),
             _ex("x2", "independent", "Explain why a cyclist slows down when she stops pedalling on flat ground."),
             _ex("x3", "challenge", "A lift cable snaps in a cartoon. Evaluate the claim that nothing acts on it.")]
EARLIER_STEM = "Describe what happens to the size of the gap between particles when ice melts into liquid water."


def _dossier(with_book_pic: bool) -> dict:
    lib = [{"id": "asset:p1", "kind": "photo"}, {"id": "asset:p2", "kind": "photo"},
           {"id": "asset:old", "kind": "photo"}]
    if with_book_pic:
        lib.append({"id": "book:spring_ch02", "kind": "illustration"})
    return {"edition_id": 1, "work": {"title": "Forces", "age_band": "11-14", "grade_band": "7",
                                      "curriculum": "CIE", "slug": "forces-book"},
            "chapter": {"uid": "CH2", "number": 2, "title": "Forces", "node_codes": ["7Pf.01"],
                        "objectives": [{"code": "7Pf.01", "text": "forces are pushes or pulls"}],
                        "skills": [{"code": "7TWs.01", "text": "sort and classify"}], "purpose": "forces",
                        "blueprint_sections": [], "assessment": "", "illustration_needs": []},
            "topics": [{"sections": [], "claims": [{"id": "claim:a"}], "glossary": [], "misconceptions": [],
                        "worked_examples": [], "figures": [], "bank_items": [], "videos": []}],
            "library_assets": lib}


class _Rows:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeConn:
    def execute(self, sql, params=None):
        return _Rows()


class FakeClient:
    """Answers by the phase card in the system prompt; records every call."""

    def __init__(self, script):
        self.model, self.script, self.calls = "fake", script, []

    def analyze(self, task, system="", max_tokens=0, cache_prefix=None):
        card = re.search(r"PHASE: (\w+)", system).group(1)
        self.calls.append({"card": card, "task": task, "max_tokens": max_tokens})
        data = self.script(card, task, self)
        return {"data": data, "usage": {"input_tokens": 100, "output_tokens": 10, "estimated_cost_usd": 0.01},
                "truncated": False}


def _section(task):
    sid = "s1" if '"sid": "s1"' in task else "s2"
    blocks = [{"type": "paragraph", "text": WORDS, "source_refs": ["claim:a"]},
              {"type": "paragraph", "text": WORDS, "source_refs": ["claim:a"]}]
    if sid == "s1":
        blocks.insert(0, {"type": "figure", "ref": "book:spring_ch02", "caption": "A stretched spring.",
                          "source_refs": ["book:spring_ch02"], "layout": {"placement": "full"}})
    if "Revise this section" in task:
        blocks.append({"type": "key_idea", "text": "A force can change speed or direction.", "source_refs": ["claim:a"]})
    return {"sid": sid, "heading": f"Heading {sid}", "blocks": blocks}


@pytest.fixture
def harness(tmp_path, monkeypatch):
    out = tmp_path / "book"
    out.mkdir()
    # an earlier chapter: its picture and its question must not come back in chapter 2
    (out / "ch01-draft.json").write_text(json.dumps({
        "sections": [{"sid": "s1", "blocks": [{"type": "figure", "ref": "asset:old", "caption": "ice"}]}],
        "exercises": [_ex("x1", "guided", EARLIER_STEM)]}), encoding="utf-8")
    builds = []

    def build(conn, slug, chapter, env_file):
        builds.append(chapter)
        return _dossier(with_book_pic=len(builds) > 1)

    reviews = []

    def author(card, task, cli):
        if card == "chapter_plan":
            return {"opening": "Pushes and pulls.", "exercise_plan": {}, "visual_gaps": [],
                    "sections": [{"sid": "s1", "heading": "Heading s1", "node_codes": ["7Pf.01"],
                                  "source_refs": ["claim:a"], "figure_refs": [], "target_words": 1100},
                                 {"sid": "s2", "heading": "Heading s2", "node_codes": ["7Pf.01"],
                                  "source_refs": ["claim:a"], "figure_refs": [], "target_words": 1100}],
                    "new_pictures": [{"key": "book:spring", "sid": "s1", "subject": "a stretched spring",
                                      "teaches": "a pull stretches", "label": "spring"}]}
        if card == "draft_section":
            return _section(task)
        if card == "exercises":
            return {"exercises": copy.deepcopy(EXERCISES)}
        raise AssertionError(f"author asked for {card}")

    def reviewer(card, task, cli):
        assert card == "review_gates", "review and gates must be ONE reviewer call"
        reviews.append(task)
        base = {"blind_answers": [{"xid": x["xid"], "answer": "a pull"} for x in EXERCISES], "overall": "ok",
                "coverage": [{"objective": "7Pf.01", "component": "forces", "taught_in": ["s1"], "verdict": "covered"}],
                "beyond_scope": [], "coverage_gate": "pass", "assessment_gate": "pass",
                "assessment": [{"xid": x["xid"], "verdict": "pass", "failed": [], "fix": ""} for x in EXERCISES]}
        if len(reviews) == 1:     # the first review: one blocker on s2, and a minor one on s1
            return [{**base, "findings": [
                {"unit": "s2", "kind": "accuracy", "severity": "blocker", "quote": "x", "detail": "unclear step"},
                {"unit": "s1", "kind": "clarity", "severity": "minor", "quote": "y", "detail": "polish"}]}]
        return {"findings": [], "overall": "ok", "blind_answers": [],
                "gates": {k: base[k] for k in ("coverage", "beyond_scope", "coverage_gate", "assessment_gate")}
                | {"assessment": []}}

    cli_a, cli_r = FakeClient(author), FakeClient(reviewer)
    monkeypatch.setattr(A, "author_client", lambda p: cli_a if p == "anthropic" else cli_r)
    monkeypatch.setattr(A, "_load_model_env", lambda env: None)
    monkeypatch.setattr(A.D, "build", build)
    monkeypatch.setattr(A, "store", lambda conn, d, doc: None)
    fits, vexes, pics = [], [], []

    def fit(run, sections, env_file):
        fits.append([s["sid"] for s in sections])
        return {"gaps": [], "findings": [], "removed": set(), "human_review": [],
                "verdicts": [{"ref": b["ref"], "unit": s["sid"], "shows": "ok"} for s in sections
                             for b in s["blocks"] if b.get("type") == "figure"]}

    def vex(run, exercises, env_file):
        vexes.append([x["xid"] for x in exercises])
        return {"findings": [], "human_review": [], "verdicts": []}

    monkeypatch.setattr(A, "figure_fit", fit)
    monkeypatch.setattr(A, "visual_exercises", vex)
    import publishing.guard
    import publishing.manuscript.book_assets as B
    import publishing.manuscript.theme
    monkeypatch.setattr(publishing.guard, "production_queue", lambda env: {"busy": 0, "types": []})
    monkeypatch.setattr(publishing.manuscript.theme, "for_work", lambda slug: {})

    def gen(conn, key, concept, subject, out_dir, by, theme=None):
        pics.append((key, by))
        return {"key": key, "model": "fake@2K", "score": 9}

    monkeypatch.setattr(B, "generate", gen)
    return {"out": out, "author": cli_a, "reviewer": cli_r, "reviews": reviews, "fits": fits, "vexes": vexes,
            "pics": pics, "builds": builds}


def test_one_pass_draft_meets_the_page_target_without_an_expand_call(harness):
    doc = A.draft_chapter(FakeConn(), "forces-book", 2, harness["out"] / "env", harness["out"])
    calls = harness["author"].calls
    plan = next(c for c in calls if c["card"] == "chapter_plan")
    assert "Page target: about 6 to 8 printed pages" in plan["task"] and "1980 to 2640 words" in plan["task"]
    assert "Picture budget: up to 3" in plan["task"]
    secs = [c for c in calls if c["card"] == "draft_section" and "Revise" not in c["task"]]
    assert len(secs) == 2 and all("about 1100 words" in c["task"] for c in secs)
    assert all(c["max_tokens"] == A.SECTION_TOKENS for c in secs)
    s1 = next(c for c in secs if '"sid": "s1"' in c["task"])
    assert "book:spring_ch02" in s1["task"], "the new picture is handed to its section"
    assert not any(c["card"] == "expand" for c in calls)
    # the picture was made under this chapter's own key and the dossier rebuilt to offer it
    assert harness["pics"] == [("book:spring_ch02", "founder:draft-ch02")] and len(harness["builds"]) == 2
    op = doc["one_pass"]
    assert op["met"] and op["words"] >= A.ONE_PASS_MIN_WORDS and op["target_words"] == 2200
    assert [p["key"] for p in op["pictures_made"]] == ["book:spring_ch02"]
    assert BC.needs_expand(doc) == (False, f"skipped: the one-pass draft met the page target ({op['words']} words)")
    # the record keeps its shape: costs entries as before, the work checkpoint removed
    assert set(doc["costs"][0]) == {"stage", "unit", "provider", "model", "in", "out", "usd", "secs"}
    assert not (harness["out"] / "ch02-draft-work.json").exists()
    assert json.loads((harness["out"] / "ch02-draft.json").read_text(encoding="utf-8"))["one_pass"]["met"]


def test_review_and_gates_are_one_call_split_into_the_old_shapes(harness):
    doc = A.draft_chapter(FakeConn(), "forces-book", 2, harness["out"] / "env", harness["out"])
    first = harness["reviews"][0]
    # the learner's view first, the key last
    assert first.index("WITHOUT answers") < first.index("ANSWER KEY")
    assert "worked_solution" not in first[:first.index("ANSWER KEY")]
    assert "worked_solution" in first[first.index("ANSWER KEY"):]
    assert all(c["max_tokens"] == A.REVIEW_TOKENS for c in harness["reviewer"].calls)
    g = doc["review"]["gates"]
    assert g["coverage_gate"] == "pass" and g["assessment_gate"] == "pass" and g["content_gate"] == "pass"
    assert g["visual_gate"] == "pass" and "visual" in g
    assert [b["xid"] for b in doc["review"]["blind_answers"]] == ["x1", "x2", "x3"]
    assert doc["review"]["ready_for_gate_d"] is True
    assert {c["stage"] for c in doc["costs"] if c["provider"] == "gemini"} == {"review_gates"}


def test_split_review_reads_flat_nested_and_list_wrapped_replies():
    flat = {"findings": [{"unit": "s1"}], "blind_answers": [], "coverage_gate": "pass", "assessment_gate": "fail",
            "assessment": [{"xid": "x1", "verdict": "fail"}]}
    r, g = A.split_review(flat)
    assert r == {"findings": [{"unit": "s1"}], "blind_answers": []}
    assert g["assessment_gate"] == "fail" and g["assessment"][0]["xid"] == "x1"
    r2, g2 = A.split_review([{"review": {"findings": [], "overall": "fine"}, "gates": {"coverage_gate": "pass"}}])
    assert r2["overall"] == "fine" and g2 == {"coverage_gate": "pass"}
    r3, g3 = A.split_review({"overall": "no findings key"})
    assert r3["findings"] == [] and g3 == {}


def test_a_revise_round_rechecks_only_the_units_it_changed(harness):
    doc = A.draft_chapter(FakeConn(), "forces-book", 2, harness["out"] / "env", harness["out"])
    revised = [c for c in harness["author"].calls if c["card"] == "draft_section" and "Revise" in c["task"]]
    assert len(revised) == 1 and '"sid": "s2"' in revised[0]["task"]
    assert len(harness["reviews"]) == 2
    recheck = harness["reviews"][1]
    assert "RE-CHECK after a revision round" in recheck
    revised_part = recheck[recheck.index("Revised sections:"):recheck.index("Revised exercises")]
    assert '"sid": "s2"' in revised_part and '"sid": "s1"' not in revised_part
    assert "unclear step" in recheck, "the findings the revision had to resolve are shown"
    # the pictures: s1 was looked at once, the revised s2 (no pictures) again; exercises not re-looked at
    assert harness["fits"] == [["s1", "s2"], ["s2"]]
    assert harness["vexes"] == [["x1", "x2", "x3"]]
    rv = doc["review"]
    assert rv["recheck"] == ["s2"]
    # s1's minor finding carries forward; s2's blocker is gone (re-judged); verdicts of unchanged exercises kept
    assert [f["detail"] for f in rv["findings"]] == ["polish"]
    assert [a["xid"] for a in rv["gates"]["assessment"]] == ["x1", "x2", "x3"]
    assert [b["xid"] for b in rv["blind_answers"]] == ["x1", "x2", "x3"]
    assert [v["unit"] for v in rv["gates"]["visual"]["figures"]] == ["s1"]
    assert len(rv["rounds"]) == 1 and rv["ready_for_gate_d"] is True


def test_changed_units_and_merge_recheck():
    s = [{"sid": "s1", "blocks": [1]}, {"sid": "s2", "blocks": [2]}]
    x = [{"xid": "x1", "stem": "a"}, {"xid": "x2", "stem": "b"}]
    s2, x2 = copy.deepcopy(s), copy.deepcopy(x)
    s2[1]["blocks"].append(3)
    x2.append({"xid": "x3", "stem": "c"})
    ch = A.changed_units(s, s2, x, x2)
    assert ch == {"sections": {"s2"}, "exercises": {"x3"}}
    prev_r = {"findings": [{"unit": "s1.b2", "detail": "keep"}, {"unit": "s2", "detail": "re-judged"},
                           {"unit": "s9", "detail": "section gone"}],
              "blind_answers": [{"xid": "x1", "answer": "old"}, {"xid": "x2", "answer": "old"}]}
    prev_g = {"coverage_gate": "fail", "assessment": [{"xid": "x1", "verdict": "pass"}, {"xid": "x2", "verdict": "pass"}]}
    new_r = {"findings": [{"unit": "x3", "detail": "new"}], "blind_answers": [{"xid": "x3", "answer": "new"}]}
    new_g = {"coverage_gate": "pass", "assessment_gate": "fail", "assessment": [{"xid": "x3", "verdict": "fail"}]}
    r, g = A.merge_recheck(prev_r, prev_g, new_r, new_g, ch, ["x1", "x2", "x3"], {"s1", "s2", "x1", "x2", "x3"})
    assert [f["detail"] for f in r["findings"]] == ["new", "keep"]
    assert [b["answer"] for b in r["blind_answers"]] == ["old", "old", "new"]
    assert g["coverage_gate"] == "pass" and g["assessment_gate"] == "fail"
    assert [a["verdict"] for a in g["assessment"]] == ["pass", "pass", "fail"]


def test_earlier_questions_and_pictures_reach_the_chapter_prompts(harness):
    doc = A.draft_chapter(FakeConn(), "forces-book", 2, harness["out"] / "env", harness["out"])
    calls = harness["author"].calls
    plan = next(c for c in calls if c["card"] == "chapter_plan")
    ex = next(c for c in calls if c["card"] == "exercises")
    sec = next(c for c in calls if c["card"] == "draft_section")
    for c in (plan, ex, sec):
        assert "asset:old" in c["task"] and "book:hero_ch01" in c["task"], "earlier pictures (and opener art)"
    assert EARLIER_STEM in ex["task"], "earlier questions go to the exercises call"
    assert EARLIER_STEM not in plan["task"]
    assert doc["one_pass"]["earlier_questions_checked"] == 1
    assert doc["one_pass"]["earlier_pictures_avoided"] == 2


def test_a_cross_chapter_repeat_is_a_failed_check_at_draft_time():
    earlier = [(1, "x1", "", {"stem": EARLIER_STEM, "answer": "it gets bigger"})]
    from publishing.tools.uniqueness import _content
    earlier = [(1, "x1", _content(e[3]), e[3]) for e in earlier]
    sections = [{"sid": "s1", "blocks": [{"type": "figure", "ref": "asset:old"},
                                         {"type": "think", "text": EARLIER_STEM, "answer": "bigger"}]}]
    exercises = [{"xid": "x4", "stem": EARLIER_STEM, "answer": "it gets bigger"},
                 {"xid": "x5", "stem": "Name the force that slows a sliding book.", "answer": "friction",
                  "figure_refs": ["asset:p1"]}]
    f = V.check_cross_chapter(sections, exercises, {"asset:old"}, earlier)
    assert sorted((c["unit"], c["verdict"]) for c in f) == [("s1", "fail"), ("s1", "fail"), ("x4", "fail")]
    assert any("asset:old" in c["detail"] for c in f) and any("Think box" in c["detail"] for c in f)
    assert V.check_cross_chapter(sections[:0], exercises[1:], {"asset:old"}, earlier) == []


def test_needs_expand_keeps_legacy_drafts_and_short_drafts_on_the_old_path():
    assert BC.needs_expand({"sections": []})[0] is True                       # made before one-pass drafting
    assert BC.needs_expand({"one_pass": {"met": False, "words": 900}})[0] is True
    assert BC.needs_expand({"one_pass": {"met": True, "words": 2100}})[0] is False
    assert BC.needs_expand({"one_pass": {"met": True}}, "always")[0] is True
    assert BC.needs_expand({"one_pass": {"met": False}}, "never")[0] is False
    assert BC.needs_expand({"expansion": {"x": 1}}, "always") == (False, "already expanded")


def test_picture_keys_belong_to_their_chapter_and_are_never_overwritten(monkeypatch):
    assert A.picture_key("spring", 3) == "book:spring_ch03"
    assert A.picture_key("book:spring_ch03", 3) == "book:spring_ch03"

    class Conn:
        def execute(self, sql, params):
            owner = {"book:a_ch03": "founder:draft-ch03", "book:b_ch03": "founder:expand-ch01"}.get(params[0])
            return _Rows([{"approved_by": owner}] if owner else [])

    import publishing.guard
    import publishing.manuscript.book_assets as B
    monkeypatch.setattr(publishing.guard, "production_queue", lambda env: {"busy": 2, "types": ["kit"]})
    monkeypatch.setattr(B, "generate", lambda *a, **k: pytest.fail("must not generate"))
    made, refused = A.make_pictures(Conn(), "x", 3, [{"key": "a", "subject": "s"}, {"key": "b", "subject": "s"}],
                                    3, 0, None)
    assert made[0]["reused"] and "belongs to founder:expand-ch01" in refused[0]["error"]
    with pytest.raises(RuntimeError, match="not generating images"):    # production busy: never starve it
        A.make_pictures(Conn(), "x", 3, [{"key": "c", "subject": "s"}], 3, 0, None)


def test_book_images_are_requested_at_2k_by_default(monkeypatch):
    from publishing.manuscript import book_assets as B
    from spike.scene_engine import raster_assets as R
    for k in ("IMAGE_SIZE_FIGURE", "IMAGE_MODEL_PROFILE", "IMAGE_MODEL_FIGURE", "GEMINI_IMAGE_MODEL",
              "LLM_BACKEND", "PUBLISH_GEMINI_MODEL"):
        monkeypatch.delenv(k, raising=False)
    asked = []

    class Reviewer:
        def analyze(self, *a, **k):
            return {"data": {"genre": "apparatus"}}

        def analyze_image(self, *a, **k):
            return {"data": {"depicts_subject": False}}

    import publishing.blueprint
    monkeypatch.setattr(publishing.blueprint, "author_client", lambda p: Reviewer())
    monkeypatch.setattr(R, "_vertex_call", lambda prompt, model, aspect: asked.append(model) or b"png")
    got = B.generate(None, "book:x_ch01", "c", "a ripple tank", None, "test")
    assert "error" in got and B.BOOK_IMAGE_SIZE == "2K"
    assert asked and all(m.size == "2K" for m in asked)
    assert all(m.output_tokens == 1120 for m in asked), "2K is billed as 1K on Pro; 4K would be 2000"


def test_the_plan_request_names_the_catalogue_figures_on_offer():
    from publishing.manuscript.author import figures_note
    d = {"topics": [{"figures": [{"id": "fig:a", "caption": "A cost graph"}, {"id": "fig:b", "key": "tariff"}]}]}
    note = figures_note(d, {"fig:b"})
    assert "fig:a: A cost graph" in note and "fig:b" not in note and "visual_gaps" in note
    assert figures_note({"topics": [{"figures": []}]}, set()) == ""


def test_the_layout_search_tries_tighter_spacing_and_applies_it():
    from publishing.manuscript import render
    names = [n for n, _ in render._alternatives({"sections": []})]
    assert names[:3] == ["as authored", "as authored, snug spacing", "as authored, tight spacing"]
    assert set(render.DENSITY_CSS) == {"snug", "tight"}
