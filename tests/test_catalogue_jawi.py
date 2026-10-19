"""Jawi (ms-arab) as a Kit Translator target, derived from the approved MALAY
kit (plan §5.11, catalogue.jawi): the voice keeps the Malay kit's Rumi, every
written surface is Jawi, the bubbles show Jawi through the display copies,
and the Jawi kit's lineage is the Malay kit.

The model is a fake transliterator: on an ms-arab call it rewrites every
Latin word of three letters or more into Arabic letters (numbers, symbols,
single letters and expressions are left as written, as the rule demands); on
a call back into Malay it returns the Rumi it was given a marker for. No
network, no model, no live Supabase."""

from __future__ import annotations

import copy
import json
import re

import pytest

from catalogue import jawi as J
from catalogue import translate as T
from tests.test_catalogue_translate import SCRIPT_PATH, _job_sb, _maths_body, _seg, _uploads

_ARABIC = re.compile(r"[؀-ۿݐ-ݿ]")
_TO_JAWI = str.maketrans("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
                         "ابچدافݢهيجکلمنوڤقرستوۏوکيزابچدافݢهيجکلمنوڤقرستوۏوکيز")


def _jawify(s: str) -> str:
    return re.sub(r"[A-Za-z]{3,}", lambda m: m.group(0).translate(_TO_JAWI), s)


class FakeJawi:
    """Transliterates on an ms-arab call; on the read-back into Rumi returns
    the source sentence it can recover (the fidelity sample), else the input."""
    model = "fake-jawi"

    def __init__(self, transform=None):
        self.transform = transform or _jawify
        self.calls: list[str] = []
        self.back: dict[str, str] = {}
        self.session_usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}

    def analyze(self, prompt, system="", max_tokens=0, response_schema=None, **kw):
        self.calls.append(prompt)
        self.session_usage["calls"] += 1
        payload = json.loads(prompt[prompt.rindex("INPUT:") + len("INPUT:"):].strip())
        if "into Malay (Jawi)" in prompt:
            out = [self.transform(s) for s in payload]
            for src, dst in zip(payload, out):
                for w in re.findall(r"[A-Za-z]{3,}", src):
                    self.back.setdefault(_jawify(w), w)
        else:
            out = [re.sub(r"[؀-ۿݐ-ݿ]+", lambda m: self.back.get(m.group(0), m.group(0)), s)
                   for s in payload]
        return {"data": {"items": out}, "usage": {}}


def _spoken(seg: dict) -> list[str]:
    return [seg.get("text") or "", seg.get("elevenlabs_text") or ""] + [
        d.get("line") or "" for d in seg.get("dialogue") or []]


# ── the language and the rule ──────────────────────────────────────────


class TestTheRule:
    def test_jawi_is_derived_from_malay_and_everything_else_from_english(self):
        assert J.is_derived("ms-arab") and J.is_derived(" MS-ARAB ")
        assert not J.is_derived("ms") and not J.is_derived("ar") and not J.is_derived(None)
        assert J.source_language_for("ms-arab") == "ms"
        assert J.source_language_for("ar") == "en" and J.source_language_for("ms") == "en"

    def test_every_ms_arab_call_carries_the_transliteration_rule(self):
        c = FakeJawi()
        T.translate_strings(c, ["Sel ialah unit asas"], "ms-arab", context="ctx")
        assert "ALREADY Malay" in c.calls[-1] and "JAWI" in c.calls[-1] and "ctx" in c.calls[-1]
        assert "Western digits" in c.calls[-1]
        T.translate_strings(c, ["The cell"], "ar", context="ctx")
        assert "ALREADY Malay" not in c.calls[-1], "only Jawi gets it"


# ── the science path: written in Jawi, spoken in Rumi ────────────────────


def _science_body():
    segs = [_seg("s001", "Sel ada 2 bahagian dan 41.12 cm", expr="P = πr + 2r"),
            _seg("s002", "Nukleus menyimpan DNA")]
    return {"part": 1, "of": 1, "language": "ms", "avatars": {}, "video": {},
            "script": {"script_id": "sc", "book_id": "b", "chapter_num": -1, "episode_num": 1,
                       "episode_title": "Sel", "generated_at": "now", "narrator_persona": "x",
                       "segments": segs, "visual_plan": None, "avatars": None,
                       "total_estimated_duration_seconds": 40, "question_hook_count": 0}}


class TestScienceSegments:
    def test_the_voice_keeps_the_malay_and_the_screen_reads_jawi(self):
        body = _science_body()
        src = body["script"]["segments"]
        dst, report = T.translate_episode(FakeJawi(), body, "ms-arab", [("sel", "سل")], context="")
        assert report["ok"], report["blocking"]
        for s, d in zip(src, dst["segments"]):
            assert _spoken(d) == _spoken(s), "text, elevenlabs_text and every line are the Malay kit's"
            assert [x["who"] for x in d["dialogue"]] == [x["who"] for x in s["dialogue"]]
            assert all(_ARABIC.search(x["display"]) for x in d["dialogue"])
            assert d["display_text"] == " ".join(x["display"] for x in d["dialogue"]), "the bubbles' copy IS the lines'"
            assert _ARABIC.search(d["slide_heading"]) and all(_ARABIC.search(p) for p in d["slide_points"])
            assert d["estimated_duration_seconds"] == s["estimated_duration_seconds"], "timing is the Malay kit's"
        els = dst["segments"][0]["scene"]["elements"]
        assert _ARABIC.search(els[0]["text"]), "the scene's text element is written in Jawi"
        assert els[1]["expr"] == "P = πr + 2r", "a typeset expression is never touched"
        assert "41.12 cm" in dst["segments"][0]["display_text"] and "2" in dst["segments"][0]["display_text"]
        assert _ARABIC.search(dst["episode_title"])
        from agent3_scripts.models import EpisodeScript
        model = EpisodeScript.model_validate(dst)
        assert model.segments[0].display_text == dst["segments"][0]["display_text"]
        assert model.model_dump()["segments"][0]["dialogue"][0]["display"], "the line's display copy survives validation"

    def test_a_display_copy_that_lost_a_number_is_retried_then_refused(self):
        c = FakeJawi(lambda s: _jawify(s.replace("41.12", "41")))
        with pytest.raises(T.TranslateRefused, match="Jawi of part 1"):
            T.translate_episode(c, _science_body(), "ms-arab", [], context="")


# ── the maths path: the board compiled twice ─────────────────────────────


class TestMathsBoard:
    def _body(self):
        body = _maths_body()
        body["language"] = "ms"
        body["script"]["maths"]["language"] = "ms"
        return body

    def test_the_board_is_jawi_the_voice_is_the_rumi_compile(self):
        from maths.lesson import to_episode_script
        body = self._body()
        dst, report = T.translate_episode(FakeJawi(), body, "ms-arab", [], context="")
        assert report["ok"], report["blocking"]
        rumi = T.recompile_maths(body["script"], body["script"]["maths"]["lesson"], "ms", "x")
        assert [s["segment_id"] for s in dst["segments"]] == [s["segment_id"] for s in rumi["segments"]]
        for d, r in zip(dst["segments"], rumi["segments"]):
            assert d["text"] == r["text"] and [x["line"] for x in d["dialogue"]] == [x["line"] for x in r["dialogue"]]
            assert not any(_ARABIC.search(t) for t in _spoken(d)), "nothing the voice reads is in the Arabic script"
            assert all(x["display"] for x in d["dialogue"]) and d["display_text"]
        board = " ".join(e.get("text") or "" for s in dst["segments"]
                         for e in ((s.get("scene") or {}).get("elements") or []) if e.get("type") == "text")
        assert _ARABIC.search(board) and "Try it" not in board and "Cuba" not in board, "the board's own words are maths.i18n's Jawi"
        assert dst["maths"]["language"] == "ms-arab"
        assert dst["maths"]["lesson_rumi"] == body["script"]["maths"]["lesson"], "the voice's record rides along"
        src_lesson = body["script"]["maths"]["lesson"]
        assert dst["maths"]["lesson"]["examples"][0]["steps"][0]["after"] == src_lesson["examples"][0]["steps"][0]["after"]
        assert _ARABIC.search(dst["maths"]["lesson"]["hook"][0]["line"]), "the record the board shows is Jawi"
        assert report["board"]["segments"] == len(dst["segments"])
        del to_episode_script

    def test_the_drawing_is_cued_to_the_rumi_narration(self):
        body = self._body()
        dst, report = T.translate_episode(FakeJawi(), body, "ms-arab", [], context="")
        rumi = T.recompile_maths(body["script"], body["script"]["maths"]["lesson"], "ms", "x")
        r_by = {s["segment_id"]: s for s in rumi["segments"]}
        remapped = left = 0
        for seg in dst["segments"]:
            r_actions = dict(J._keyed((r_by[seg["segment_id"]].get("scene") or {}).get("actions") or []))
            for key, a in J._keyed((seg.get("scene") or {}).get("actions") or []):
                at = a.get("at")
                if not (isinstance(at, dict) and at.get("phrase")):
                    continue
                if key in r_actions:
                    remapped += 1
                    assert at == r_actions[key].get("at"), "a cue names the words the Malay voice says"
                    assert at["phrase"].lower() in seg["text"].lower()
                else:
                    left += 1
                    assert at["phrase"].lower() in seg["display_text"].lower(), "found by proportion in the Jawi"
        assert remapped, "the fixture lesson has spoken cues"
        assert report["board"]["cues_remapped"] >= remapped


# ── the merge and the two-script check, directly ────────────────────────


class TestMergeAndCheck:
    def _compile(self, words, phrase):
        return {"segments": [{"segment_id": "s001", "type": "explore", "text": f"{words} satu {words} dua",
                              "elevenlabs_text": f"{words} satu {words} dua", "hold_secs": 1.5,
                              "estimated_duration_seconds": 9,
                              "dialogue": [{"who": "teacher", "line": f"{words} satu"}, {"who": "student", "line": f"{words} dua"}],
                              "scene": {"elements": [{"id": "q", "type": "text", "text": words}],
                                        "actions": [{"verb": "write", "target": "q", "at": {"phrase": phrase}},
                                                    {"verb": "draw", "target": "q", "at": {"frac": 0.5}}]}}],
                "total_estimated_duration_seconds": 9}

    def test_spoken_from_rumi_shown_from_jawi_cues_from_rumi(self):
        rumi, jaw = self._compile("ini", "ini satu"), self._compile("اين", "اين ساتو")
        jaw["segments"][0]["hold_secs"] = 3.0
        out, rep = J.merge_compiles(rumi, jaw)
        s = out["segments"][0]
        assert s["text"] == "ini satu ini dua" and s["display_text"] == "اين satu اين dua"
        assert [d["line"] for d in s["dialogue"]] == ["ini satu", "ini dua"]
        assert [d["display"] for d in s["dialogue"]] == ["اين satu", "اين dua"]
        assert s["hold_secs"] == 1.5, "the hold is the voice's"
        assert s["scene"]["elements"][0]["text"] == "اين", "the board is the Jawi compile's"
        assert s["scene"]["actions"][0]["at"] == {"phrase": "ini satu"} and s["scene"]["actions"][1]["at"] == {"frac": 0.5}
        assert rep == {"segments": 1, "cues_remapped": 1, "scenes_unpaired": []}

    def test_a_board_that_drew_differently_keeps_its_jawi_cues(self):
        rumi, jaw = self._compile("ini", "ini satu"), self._compile("اين", "اين ساتو")
        jaw["segments"][0]["scene"]["actions"].insert(0, {"verb": "draw", "target": "hl1", "at": {"phrase": "اين"}})
        out, rep = J.merge_compiles(rumi, jaw)
        acts = out["segments"][0]["scene"]["actions"]
        assert acts[0]["at"] == {"phrase": "اين"}, "a step the Rumi board lacks keeps its Jawi cue"
        assert acts[1]["at"] == {"phrase": "ini satu"}, "the shared step still takes the Rumi cue"
        assert rep["scenes_unpaired"] == ["s001"] and rep["cues_remapped"] == 1

    def test_compiles_that_do_not_pair_are_refused(self):
        rumi, jaw = self._compile("ini", "x"), self._compile("اين", "x")
        jaw["segments"][0]["segment_id"] = "s009"
        with pytest.raises(J.Unpaired, match="segment ids"):
            J.merge_compiles(rumi, jaw)
        jaw = self._compile("اين", "x")
        jaw["segments"][0]["dialogue"][1]["who"] = "teacher"
        with pytest.raises(J.Unpaired, match="speaker turns"):
            J.merge_compiles(rumi, jaw)

    def test_jawi_handed_to_the_voice_blocks(self):
        good = {"segments": [{"segment_id": "s1", "text": "ini satu", "elevenlabs_text": "ini satu",
                              "display_text": "اين ساتو", "dialogue": [{"who": "teacher", "line": "ini satu", "display": "اين ساتو"}]}]}
        assert J.check_two_scripts(good)["ok"]
        bad = copy.deepcopy(good)
        bad["segments"][0]["dialogue"][0]["line"] = "اين ساتو"
        rep = J.check_two_scripts(bad)
        assert not rep["ok"] and rep["blocking"][0]["what"] == "spoken text is not Rumi"
        missing = copy.deepcopy(good)
        missing["segments"][0]["display_text"] = ""
        assert "no Jawi display copy of the narration" in [f["what"] for f in J.check_two_scripts(missing)["blocking"]]
        no_line = copy.deepcopy(good)
        no_line["segments"][0]["dialogue"][0]["display"] = ""
        assert not J.check_two_scripts(no_line)["ok"]
        rumi_shown = copy.deepcopy(good)
        rumi_shown["segments"][0]["display_text"] = "ini satu masih dalam rumi"
        rep = J.check_two_scripts(rumi_shown)
        assert rep["ok"] and rep["flags"][0]["what"] == "display text still reads as Rumi"


# ── the job: from the approved Malay kit ─────────────────────────────────


def _malay_sb(**kw):
    """The translate fixture with its source kit turned into the topic's
    APPROVED MALAY translation of an English kit: its items are the Malay
    kit's (translation_kit_id), its glossary pairs are on its provenance."""
    sb = _job_sb(**kw)
    k = sb.tables["topic_kits"][0]
    k.update({"language": "ms", "status": "approved", "translation_status": "approved", "source_kit_id": "kit-en",
              "voice_pair": {"teacher": "g-ms-f", "student": None},
              "translation": {"student_voice_fallback": True, "scripts": [SCRIPT_PATH],
                              "stage": {"glossary": {"pairs": [["cell", "sel"], ["organelle", "organel"]]}}}})
    for q in sb.tables["topic_questions"]:
        q.update({"language": "ms", "translation_kit_id": "kit-1", "source_item_id": "en-" + q["id"]})
    # an approved Malay item of ANOTHER kit: never a source of this Jawi kit
    stray = dict(sb.tables["topic_questions"][0], id="q-stray", translation_kit_id="kit-ms-old",
                 stem="Soalan lama yang lain sama sekali?")
    sb.tables["topic_questions"].append(stray)
    sb.tables["question_sets"][0]["language"] = "ms"
    for g in sb.tables["generations"]:
        if g["id"] == "gen-quiz":
            g["params"]["language"] = "ms"
    sb.tables["jobs"][0]["params"] = {"source_kit_id": "kit-1", "language": "ms-arab", "topic_id": "t-cell"}
    return sb


class TestJawiJob:
    def test_a_jawi_kit_is_derived_from_the_malay_kit(self, monkeypatch, tmp_path):
        sb = _malay_sb()
        uploads = _uploads(monkeypatch)
        monkeypatch.setattr("spike.scene_engine.raster_assets.cache_dir_for", lambda key, cache_dir=None: tmp_path / "nocache" / key)
        c = FakeJawi()

        summary = T.run_translate_job(sb, sb.tables["jobs"][0], client=c)

        assert summary and summary["step"] == "done" and summary["language"] == "ms-arab", sb.tables["jobs"][0].get("error")
        assert summary["precheck_ok"] is True
        k = next(x for x in sb.tables["topic_kits"] if x.get("language") == "ms-arab")
        # lineage: the MALAY kit, so stale and blocked cascade through it
        assert k["source_kit_id"] == "kit-1" and (k["status"], k["translation_status"]) == ("generating", "staged")
        assert k["translation"]["source_language"] == "ms"
        # the Malay kit's own voice: Jawi is spoken Malay
        assert k["voice_pair"] == {"teacher": "g-ms-f", "student": None}
        assert k["translation"]["student_voice_fallback"] is True
        # the glossary: transliterated Malay terms, keyed by the ENGLISH sense
        terms = sb.tables["topic_term_translations"]
        assert {t["term_en"] for t in terms} == {"cell", "organelle"}
        assert all(t["language"] == "ms-arab" and _ARABIC.search(t["term"]) for t in terms)
        assert any("sel →" in p for p in c.calls), "the directive pairs the Malay term with its Jawi"
        # the article: from the Malay one, staged, in Jawi
        arts = [a for a in sb.tables["topic_articles"] if a["language"] == "ms-arab"]
        assert len(arts) == 1 and arts[0]["source_article_id"] == "art-1" and "(ms)" in arts[0]["notes"]
        # the bank: the Malay kit's items only, mapped item for item
        staged = [q for q in sb.tables["topic_questions"] if q.get("translation_kit_id") == k["id"]]
        assert len(staged) == 12 and {q["source_item_id"] for q in staged} == {f"q-{i}" for i in range(1, 13)}
        assert all(q["language"] == "ms-arab" and q["status"] == "staged" for q in staged)
        assert _ARABIC.search(next(q for q in staged if q["source_item_id"] == "q-1")["stem"])
        # the quiz: the Malay set's questions, by id
        sets = [s for s in sb.tables["question_sets"] if s["language"] == "ms-arab"]
        by_src = {q["source_item_id"]: q["id"] for q in staged}
        assert len(sets) == 1 and sets[0]["question_ids"] == [by_src[i] for i in ("q-3", "q-1", "q-8", "q-5")]
        # the presentation: two scripts, the Malay voice
        pres = next(g for g in sb.tables["generations"] if g["kind"] == "presentation"
                    and (g["params"].get("translation") or {}).get("kit_id") == k["id"])
        p = pres["params"]
        assert p["language"] == "ms-arab" and p["tts_voice"] == "g-ms-f"
        assert p["translation"]["source_kit_id"] == "kit-1" and p["translation"]["source_language"] == "ms"
        script = uploads[0][1]["script"]
        assert uploads[0][1]["language"] == "ms-arab" and script["maths"]["language"] == "ms-arab"
        for seg in script["segments"]:
            assert not any(_ARABIC.search(t) for t in _spoken(seg)), seg["segment_id"]
            if seg.get("dialogue"):
                assert all(_ARABIC.search(d["display"]) for d in seg["dialogue"] if d.get("line")), seg["segment_id"]
        # pre-check B read the Jawi back into Rumi, against the Malay
        fid = next(ch for ch in k["precheck"]["checks"] if ch.get("check") == "B")
        assert fid["mean_score"] is not None and fid["mean_score"] > 0.9
        assert any(T.jawi.BACK_RULE in p for p in c.calls)

    def test_the_source_must_be_an_approved_malay_translation(self, monkeypatch, tmp_path):
        monkeypatch.setattr("spike.scene_engine.raster_assets.cache_dir_for", lambda key, cache_dir=None: tmp_path / "nocache" / key)
        # an English kit is not a Jawi source
        sb = _job_sb()
        sb.tables["jobs"][0]["params"]["language"] = "ms-arab"
        assert T.run_translate_job(sb, sb.tables["jobs"][0], client=FakeJawi()) is None
        assert "derived from the topic's approved Malay kit" in sb.tables["jobs"][0]["error"]
        # a stale Malay kit is not either
        sb = _malay_sb()
        sb.tables["topic_kits"][0]["translation_status"] = "stale"
        assert T.run_translate_job(sb, sb.tables["jobs"][0], client=FakeJawi()) is None
        assert "not an approved translation" in sb.tables["jobs"][0]["error"]
        # and a Malay kit is no source for any other language
        sb = _malay_sb()
        sb.tables["jobs"][0]["params"]["language"] = "ar"
        assert T.run_translate_job(sb, sb.tables["jobs"][0], client=FakeJawi()) is None
        assert "translations start from English" in sb.tables["jobs"][0]["error"]
        assert not [k for k in sb.tables["topic_kits"] if k.get("source_kit_id") == "kit-1"], "nothing was created"


# ── the render: the bubbles read the display copy ────────────────────────


class TestTheScreenReadsTheDisplayCopy:
    def test_displayed_segment(self):
        from agent6_animation.video_composer import displayed_segment
        seg = {"segment_id": "s1", "text": "ini satu", "display_text": "اين ساتو",
               "dialogue": [{"who": "teacher", "line": "ini satu", "display": "اين ساتو"}], "_dialogue_starts": [0.0]}
        view, shown = displayed_segment(seg)
        assert shown == "اين ساتو" and view["text"] == "اين ساتو"
        assert view["dialogue"] == [{"who": "teacher", "line": "اين ساتو", "display": "اين ساتو"}]
        assert view["_dialogue_starts"] == [0.0], "the bubbles stay on the voice's clock"
        assert seg["text"] == "ini satu" and seg["dialogue"][0]["line"] == "ini satu", "the voice's copy is untouched"
        plain = {"segment_id": "s2", "text": "hello"}
        assert displayed_segment(plain) == (plain, None)

    def test_the_composer_passes_the_display_copy_to_the_scene_and_the_rumi_to_the_voice(self):
        import inspect
        from agent6_animation import video_composer as VC
        src = inspect.getsource(VC)
        assert "view_seg, view_text = displayed_segment(script_seg)" in src
        assert src.count("view_seg, view_text, audio_path,") == 3, "every scene render reads the display copy"
        assert "build_whiteboard_scene(script_seg" not in src, "the whiteboard fallback reads it too"
        assert "_synth_dialogue(script_seg[\"dialogue\"]" in src, "the voice reads the spoken lines"


class TestJawiOutro:
    def test_the_end_screen_speaks_malay_and_shows_jawi(self):
        from shared import outro as O
        for kind in (O.KIND_CTA, O.KIND_SIGNOFF):
            seg = O.outro_segment(kind, "ms-arab")
            ms = O.outro_segment(kind, "ms")
            assert seg["text"] == ms["text"] and seg["elevenlabs_text"] == ms["text"]
            assert [d["line"] for d in seg["dialogue"]] == [d["line"] for d in ms["dialogue"]]
            assert all(_ARABIC.search(d["display"]) for d in seg["dialogue"]) and _ARABIC.search(seg["display_text"])
            assert _ARABIC.search(seg["slide_heading"])
        assert "display_text" not in O.outro_segment(O.KIND_CTA, "ar")


# ── Latin loanwords left in the Jawi (first live Jawi kit b3ca5484, 2026-10-19) ──


class TestLatinLeftovers:
    def test_malay_words_left_in_rumi_are_caught(self):
        assert J.latin_leftovers("لوݢم Rod دان سينارن inframerah 5 joule") == ["Rod", "inframerah", "joule"]
        assert J.latin_leftovers("ڤماناسن lwn ڤڽيجوقن") == ["lwn"], "an abbreviation is a word too"
        assert J.latin_leftovers("DNA دالم نوکليوس") == ["DNA"]
        assert J.latin_leftovers("Rod rod Rod") == ["Rod", "rod"], "each spelling once, in order"

    def test_variables_units_functions_formulas_and_protected_maths_are_not(self):
        for ok in ("x + 2 = 5", "سوهو 100 °C", "4200 J", "5 kg دان 3 cm", "sin x = 0.5", "log 100", "tan θ",
                   "ݢارم NaCl دان H2O", "HCl", "CaCO3", "101 kPa", "2 mol"):
            assert J.latin_leftovers(ok) == [], ok
        assert J.latin_leftovers("لواس Area = πr²", protected=["Area = πr²"]) == []
        assert J.latin_leftovers("لواس Area = πr²") == ["Area"]

    def test_the_rule_asks_for_loanwords_in_jawi(self):
        assert "loanwords" in J.RULE and "inframerah" in J.RULE and "abbreviations" in J.RULE
        assert "may stay as written" not in J.RULE, "the escape the model used is gone"
        assert "{words}" in J.REPAIR_RULE


class _LeavesLoanwords(FakeJawi):
    """Leaves ``keep`` words in Latin on a first Jawi call; a REPAIR call
    (the prompt names the leftover words) writes them in Jawi too, unless
    ``stubborn``."""

    def __init__(self, keep=("Rod", "inframerah", "joule"), stubborn=False):
        super().__init__()
        self.keep = set(keep)
        self.stubborn = stubborn
        self.payloads: list[list[str]] = []

    def analyze(self, prompt, system="", max_tokens=0, response_schema=None, **kw):
        self.calls.append(prompt)
        self.session_usage["calls"] += 1
        payload = json.loads(prompt[prompt.rindex("INPUT:") + len("INPUT:"):].strip())
        self.payloads.append(payload)
        repair = "A previous Jawi version" in prompt
        keep = set() if (repair and not self.stubborn) else self.keep
        out = [re.sub(r"[A-Za-z]{3,}", lambda m: m.group(0) if m.group(0) in keep else _jawify(m.group(0)), s)
               for s in payload]
        if repair and self.stubborn:
            out = [o + " Rod" for o in out]          # worse than before
        return {"data": {"items": out}, "usage": {}}


class TestRepairPass:
    def test_strings_with_leftovers_are_asked_again_naming_the_words(self):
        c = _LeavesLoanwords()
        got = T.translate_strings(c, ["Logam Rod panas", "Sinaran inframerah", "Air mendidih pada 100 °C"], "ms-arab")
        assert all(J.latin_leftovers(g) == [] for g in got), got
        assert len(c.calls) == 2 and c.payloads[1] == ["Logam Rod panas", "Sinaran inframerah"], "only the two with leftovers"
        assert "Rod" in c.calls[1] and "inframerah" in c.calls[1] and "A previous Jawi version" in c.calls[1]
        assert "100 °C" in got[2]

    def test_a_worse_repair_keeps_the_first_answer(self):
        c = _LeavesLoanwords(keep=("joule",), stubborn=True)
        got = T.translate_strings(c, ["Tenaga 5 joule"], "ms-arab")
        assert got[0].endswith("joule") and "Rod" not in got[0]
        assert len(c.calls) == 2

    def test_protected_maths_and_other_languages_are_never_re_asked(self):
        c = _LeavesLoanwords(keep=("Area",))
        T.translate_strings(c, ["Luas Area = πr²"], "ms-arab", protect=["Area = πr²"])
        assert len(c.calls) == 1, "a protected expression is not a leftover"
        c = _LeavesLoanwords()
        T.translate_strings(c, ["Logam Rod"], "ar")
        assert len(c.calls) == 1, "only Jawi has a repair pass"

    def test_the_science_path_repairs_scene_text_and_bubbles(self):
        c = _LeavesLoanwords(keep=("Nukleus", "menyimpan"))
        dst, report = T.translate_episode(c, _science_body(), "ms-arab", [], context="")
        assert report["ok"], report["blocking"]
        assert not [f for f in report["flags"] if f.get("what") == J.LEFTOVER_FLAG]
        assert any("A previous Jawi version" in p for p in c.calls)
        assert J.written_leftovers(dst["segments"][1]) == {}


class TestLeftoverFlag:
    def test_the_reviewer_sees_every_leftover_word_per_segment_and_field(self):
        script = {"segments": [{
            "segment_id": "s4", "text": "logam rod", "elevenlabs_text": "logam rod",
            "display_text": "لوݢم rod", "slide_heading": "ڤماناسن lwn", "slide_points": ["سينارن inframerah"],
            "dialogue": [{"who": "teacher", "line": "logam rod", "display": "لوݢم rod"}],
            "scene": {"elements": [{"id": "t", "type": "text", "text": "لوݢم Rod"},
                                   {"id": "m", "type": "math", "expr": "Q = mc ΔT"},
                                   {"id": "t2", "type": "text", "text": "Q = mc ΔT, 4200 J, 100 °C, sin x"}]}}]}
        rep = J.check_two_scripts(script)
        assert rep["ok"], "a leftover word is a flag, never a block"
        flag = next(f for f in rep["flags"] if f["what"] == J.LEFTOVER_FLAG)
        assert flag["where"] == "s4" and flag["words"] == ["inframerah", "lwn", "Rod", "rod"]
        assert set(flag["fields"]) == {"display_text", "dialogue[0].display", "slide_heading", "slide_points[0]",
                                       "scene.elements[0].text"}

    def test_a_clean_jawi_segment_has_no_flag(self):
        script = {"segments": [{"segment_id": "s1", "text": "ini", "elevenlabs_text": "ini", "display_text": "اين",
                                "slide_heading": "x = 5 cm", "dialogue": [{"who": "teacher", "line": "ini", "display": "اين"}]}]}
        assert J.check_two_scripts(script)["flags"] == []


def test_point_names_mnemonics_and_inverse_functions_are_not_leftovers():
    """The first Jawi Trig kit (2026-10-19) flagged its triangles' names
    (ABC, PQR, XYZ, DEF), SOH CAH TOA and atan as Malay left in Latin."""
    from catalogue.jawi import latin_leftovers
    assert latin_leftovers("سݢي تيݢ ABC دان PQR، XYZ، DEF") == []
    assert latin_leftovers("ايڠت SOH CAH TOA") == []
    assert latin_leftovers("x = atan(4/3)، asin، acos") == []
    # a word stays a leftover, and so does a long acronym
    assert latin_leftovers("لوݢم Rod") == ["Rod"]
    assert latin_leftovers("ايکوت BODMAS") == ["BODMAS"]
