"""The avatar is a random roster face, restricted only by the voice's gender,
and one lesson keeps one face (founder decision, 2026-09-04).

Everything here runs offline: the Supabase client is a fake, and no test
reaches a model or the network.
"""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

import shared.visual_library as vl
from spike.scene_engine.raster_assets import canonical_key as renderer_canonical_key
from spike.scene_engine.whiteboard import (AVATAR_PROMPTS, avatar_prompt,
                                           cast_avatars, student_voice_for_avatar,
                                           teacher_avatar_for_voice, voice_gender)


def _row(id_: str, key: str, role: str, created: str, band: str | None = None,
         desc: str = "", storage: str | None = None, **extra) -> dict:
    return {"id": id_, "asset_key": key, "canonical_key": vl.canonical_key(key),
            "description": desc or f"A friendly {role}", "status": "approved",
            "asset_type": "avatar", "role": role, "age_band": band,
            "storage_path": storage or f"generated/{vl.canonical_key(key)}/{id_[:8]}.png",
            "created_at": created, **extra}


# The live roster's shape on 2026-09-04 (13 approved rows): five female
# teacher faces, one male; students by band, the legacy boy included.
FEMALE_TEACHERS = [_row(f"f{i}f{i}f{i}f{i}-0000-4000-8000-00000000000{i}", "avatar_teacher_female",
                        "teacher", f"2026-09-0{i}T00:00:00Z") for i in range(1, 6)]
MALE_TEACHER = _row("5079cb33-2f30-4f50-a0b8-4f027c885d09", "avatar_teacher", "teacher", "2026-09-02T18:36:38Z")
STUDENTS = [
    _row("811421c8-c737-449e-83fd-41ef08264f43", "avatar_student_8_10_f", "student", "2026-09-02T18:36:33Z", "8_10"),
    _row("cbb6f36f-ddc4-4345-ba84-9e0da8cbfc23", "avatar_student_8_10_m", "student", "2026-09-02T18:36:35Z", "8_10"),
    _row("dfd55a7f-599e-4cae-8f25-1ca901538155", "avatar_student_11_12_f", "student", "2026-09-02T18:36:27Z", "11_12"),
    _row("50b56ce8-2496-4b07-aa05-18757e6252bc", "avatar_student_11_12_m", "student", "2026-09-02T18:36:29Z", "11_12"),
    _row("e485144b-4b0f-4dc8-899d-f498fba888ff", "avatar_student_5_7_f", "student", "2026-09-02T18:36:31Z", "5_7"),
    _row("ca2eaed7-2940-420a-a0ad-9db254a730df", "avatar_student_5_7_f", "student", "2026-09-03T12:01:02Z", "5_7"),
    _row("701d181d-7b00-4654-be62-d4bfd130426d", "avatar_student", "student", "2026-09-02T18:36:25Z", None),
]
ROSTER = FEMALE_TEACHERS + [MALE_TEACHER] + STUDENTS


def _png_bytes() -> bytes:
    img = Image.new("RGBA", (64, 64), (255, 255, 255, 0))
    ImageDraw.Draw(img).ellipse([8, 8, 56, 56], outline=(0, 0, 0, 255), width=4)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _fake_sb(rows: list[dict], calls: dict):
    """A Supabase stand-in for the three queries the roster code makes."""
    class Q:
        def __init__(self):
            self.f = {}

        def select(self, *_a, **_k):
            return self

        def eq(self, col, val):
            self.f[f"eq:{col}"] = val
            return self

        def neq(self, col, val):
            self.f[f"neq:{col}"] = val
            return self

        def like(self, col, pat):
            self.f[f"like:{col}"] = pat
            return self

        def order(self, col, **_k):
            self.f["order"] = col
            return self

        def limit(self, _n):
            return self

        def insert(self, row):
            calls.setdefault("insert", []).append(row)
            return self

        def execute(self):
            calls.setdefault("queries", []).append(dict(self.f))
            out = list(rows)
            if "eq:content_hash" in self.f:
                out = []
            if "eq:status" in self.f:
                out = [r for r in out if r.get("status") == self.f["eq:status"]]
            if "eq:canonical_key" in self.f:
                out = [r for r in out if r.get("canonical_key") == self.f["eq:canonical_key"]]
            if "like:asset_key" in self.f:
                pref = self.f["like:asset_key"].rstrip("%")
                out = [r for r in out if str(r.get("asset_key", "")).startswith(pref)]
            if self.f.get("neq:asset_type") == "avatar":
                out = [r for r in out if r.get("asset_type") != "avatar"]
            if self.f.get("order") == "created_at":
                out = sorted(out, key=lambda r: str(r.get("created_at") or ""))
            return type("R", (), {"data": out})()

    class Storage:
        def from_(self, _b):
            return self

        def download(self, path):
            calls.setdefault("download", []).append(path)
            return _png_bytes()

        def upload(self, path, fh, opts):
            calls.setdefault("upload", []).append(path)

    class SB:
        storage = Storage()

        def table(self, _n):
            return Q()

    return SB()


@pytest.fixture(autouse=True)
def _isolated_library(tmp_path, monkeypatch):
    monkeypatch.setattr(vl, "LIBRARY_DIR", tmp_path / "index")
    monkeypatch.setattr(vl, "_sb", lambda: None)          # offline unless a test says otherwise
    vl._CAST_CACHE.clear()
    yield
    vl._CAST_CACHE.clear()


# ── 1. gender: read from the row, restricted by the voice ───────────────────

class TestGenderFromTheRoster:
    @pytest.mark.parametrize("row,gender,role,band", [
        (FEMALE_TEACHERS[0], "f", "teacher", None),
        (MALE_TEACHER, "m", "teacher", None),
        (STUDENTS[0], "f", "student", "8_10"),
        (STUDENTS[1], "m", "student", "8_10"),
        (STUDENTS[3], "m", "student", "11_12"),
        (STUDENTS[4], "f", "student", "5_7"),
        (STUDENTS[6], "m", "student", "5_7"),      # legacy avatar_student is the 5-7 boy
    ])
    def test_every_live_roster_shape_resolves_from_its_key(self, row, gender, role, band):
        assert vl.avatar_gender(row) == gender
        assert vl.avatar_role(row) == role
        assert vl.avatar_age_band(row) == band

    def test_description_is_the_fallback_when_the_key_says_nothing(self):
        assert vl.avatar_gender({"asset_key": "avatar_tutor", "description": "A friendly woman with a scarf"}) == "f"
        assert vl.avatar_gender({"asset_key": "avatar_tutor", "description": "A friendly man with a scarf"}) == "m"
        assert vl.avatar_gender({"asset_key": "avatar_tutor", "description": "A friendly person"}) is None

    def test_a_face_key_carries_the_family_gender_and_band(self):
        k = vl.face_key("avatar_student_8_10_f", "811421c8-c737-449e")
        assert k == "avatar_student_8_10_f__face_811421c8"
        assert vl.base_avatar_key(k) == "avatar_student_8_10_f" and vl.face_id_of(k) == "811421c8"
        assert vl.avatar_gender(k) == "f" and vl.avatar_age_band(k) == "8_10"
        assert vl.avatar_fields(k) == {"asset_type": "avatar", "role": "student", "age_band": "8_10"}
        assert vl.is_avatar_key(k)

    def test_the_teacher_follows_the_voice_gender(self):
        female = cast_avatars("edge-aria", "Grade 9", "gen-a", roster=ROSTER)["teacher"]
        male = cast_avatars("edge-guy", "Grade 9", "gen-a", roster=ROSTER)["teacher"]
        assert vl.avatar_gender(female) == "f" and vl.base_avatar_key(female) == "avatar_teacher_female"
        assert vl.avatar_gender(male) == "m" and vl.base_avatar_key(male) == "avatar_teacher"
        assert vl.face_id_of(male) == MALE_TEACHER["id"][:8]
        assert vl.face_id_of(female) in {r["id"].replace("-", "")[:8] for r in FEMALE_TEACHERS}

    def test_cast_from_the_effective_voice_not_the_requested_one(self):
        """A premium male pick that the tier gate downgrades to Aria must cast
        a female face — the caller passes the RESOLVED voice."""
        from shared.tts import resolve_voice
        for requested in ("el-adam", "el-rachel", "g-en-m"):
            eff = resolve_voice(requested, allow_premium=False, lang="en").voice_id
            assert eff != requested, "the gate downgraded the premium pick"
            cast = cast_avatars(eff, "9", "g", roster=ROSTER)["teacher"]
            assert vl.avatar_gender(cast) == voice_gender(eff), (requested, eff)


# ── 2. one lesson, one face; another lesson may differ ──────────────────────

class TestOneLessonOneFace:
    def test_the_same_generation_id_always_casts_the_same_face(self):
        first = cast_avatars("edge-aria", "Grade 9", "16228b9e", lang="en",
                             style="conversational", roster=ROSTER)
        for _ in range(5):                       # every part, every retry
            assert cast_avatars("edge-aria", "Grade 9", "16228b9e", lang="en",
                                style="conversational", roster=ROSTER) == first
        # the same draw whichever order the roster arrives in
        assert cast_avatars("edge-aria", "Grade 9", "16228b9e", lang="en",
                            style="conversational", roster=list(reversed(ROSTER))) == first

    def test_different_generation_ids_can_cast_different_faces(self):
        faces = {cast_avatars("edge-aria", "9", f"gen-{i}", roster=ROSTER)["teacher"] for i in range(40)}
        assert len(faces) > 1, "five approved female faces; forty lessons should not all pick one"
        assert faces <= {vl.face_key("avatar_teacher_female", r["id"]) for r in FEMALE_TEACHERS}

    def test_the_run_cache_answers_repeat_casts_without_a_second_query(self, monkeypatch):
        calls: list[int] = []

        def listing():
            calls.append(1)
            return list(ROSTER)
        monkeypatch.setattr(vl, "list_avatar_roster", listing)
        a = vl.cast_avatar_key("teacher", "f", "gen-1", "avatar_teacher_female")
        b = vl.cast_avatar_key("teacher", "f", "gen-1", "avatar_teacher_female")
        assert a == b and len(calls) == 1
        assert vl.cast_avatar_key("teacher", "f", "gen-2", "avatar_teacher_female") and len(calls) == 2


# ── 3. fallbacks: any gender, then the generate path; never a failed lesson ─

class TestFallbacks:
    def test_no_face_of_that_gender_falls_back_to_any_face_and_says_so(self, caplog):
        only_female = FEMALE_TEACHERS + STUDENTS
        with caplog.at_level(logging.WARNING, logger="shared.visual_library"):
            key = cast_avatars("edge-guy", "9", "gen-m", roster=only_female)["teacher"]
        assert vl.avatar_gender(key) == "f" and vl.face_id_of(key), "a face beats no face"
        assert any("no approved teacher face of gender 'm'" in r.getMessage() for r in caplog.records)

    def test_no_student_in_the_band_falls_back_to_any_student_of_that_gender(self):
        key = cast_avatars("edge-aria", "Grade 14", "gen-u", roster=ROSTER)["student"]
        assert vl.avatar_role(key) == "student" and vl.face_id_of(key)
        assert vl.avatar_age_band(key) != "undergrad"

    def test_an_empty_roster_is_todays_generate_path(self):
        cast = cast_avatars("edge-aria", "Grade 9", "gen-e", roster=[])
        assert cast == {"teacher": "avatar_teacher_female",
                        "student": cast["student"]} and vl.face_id_of(cast["student"]) is None
        assert cast["student"] in AVATAR_PROMPTS
        # and the renderer wrapper, asked to hydrate that key, gets nothing:
        # the original generator runs exactly as before the roster existed
        assert vl.hydrate_avatar(cast["teacher"], Path("unused")) is None

    def test_offline_the_roster_keys_are_the_cast(self):
        cast = cast_avatars("edge-guy", "9", "gen-off")   # _sb() is None here
        assert cast["teacher"] == "avatar_teacher" and vl.face_id_of(cast["student"]) is None

    def test_a_roster_error_never_fails_the_lesson(self, monkeypatch):
        def boom():
            raise RuntimeError("supabase down")
        monkeypatch.setattr(vl, "list_avatar_roster", boom)
        assert cast_avatars("edge-aria", "9", "gen-x")["teacher"] == "avatar_teacher_female"

    def test_no_generation_when_a_roster_face_exists(self, tmp_path, monkeypatch):
        """The face key hydrates from the library, so the wrapper's
        `existed_before` check finds the file and the generator is never
        given the key."""
        calls: dict = {}
        monkeypatch.setattr(vl, "_sb", lambda: _fake_sb(ROSTER, calls))
        key = cast_avatars("edge-aria", "9", "gen-h")["teacher"]
        cache = tmp_path / "cache"
        assert vl.hydrate_avatar(key, cache)
        assert (cache / renderer_canonical_key(key) / "asset.png").exists(), \
            "cached where spike/scene_engine/raster_assets looks"


# ── 4. the dialogue's student follows the second voice ──────────────────────

class TestStudentFollowsTheSecondVoice:
    def test_english_dialogue_student_is_the_gender_of_the_student_voice(self):
        # English student lines are read by Ana (young) / Emma (older): both female
        for grade, band in (("Grade 6", "5_7"), ("Grade 9", "8_10"), ("Grade 12", "11_12")):
            for seed in ("s1", "s2", "s3"):
                key = cast_avatars("edge-guy", grade, seed, lang="en",
                                   style="conversational", roster=ROSTER)["student"]
                assert vl.avatar_gender(key) == "f", (grade, seed)
                assert vl.avatar_age_band(key) == band
                assert student_voice_for_avatar(key, "en") == (
                    "en-US-AnaNeural" if band in ("5_7", "8_10") else "en-US-EmmaNeural")

    def test_non_english_dialogue_student_is_the_gender_of_the_lesson_language_default(self):
        from shared.tts.registry import default_voice_id_for, get_voice
        for lang in ("ms", "ar", "hi"):
            want = get_voice(default_voice_id_for(lang)).gender
            for seed in ("s1", "s2", "s3"):
                key = cast_avatars("edge-osman", "9", seed, lang=lang,
                                   style="conversational", roster=ROSTER)["student"]
                assert vl.avatar_gender(key) == want, (lang, seed)

    def test_a_single_voice_lesson_keeps_the_seeded_student_gender(self):
        genders = {vl.avatar_gender(cast_avatars("edge-aria", "9", f"s{i}", lang="en",
                                                 style="socratic", roster=ROSTER)["student"])
                   for i in range(20)}
        assert genders == {"f", "m"}, "no second voice constrains a socratic lesson"

    def test_the_student_voice_reads_the_band_through_a_face_key(self):
        assert student_voice_for_avatar("avatar_student_8_10_m__face_cbb6f36f", "en") == "en-US-AnaNeural"
        assert student_voice_for_avatar("avatar_student_11_12_f__face_dfd55a7f", "en") == "en-US-EmmaNeural"
        assert student_voice_for_avatar("avatar_student__face_701d181d", "en") == "en-US-AnaNeural"
        assert student_voice_for_avatar("avatar_student_8_10_m__face_cbb6f36f", "ms") == ""


# ── 5. the library serves the face the key names ────────────────────────────

class TestFaceLookup:
    def test_find_avatar_serves_the_named_face_not_the_oldest(self):
        calls: dict = {}
        vl._sb = lambda: _fake_sb(ROSTER, calls)  # restored by the fixture's monkeypatch
        third = FEMALE_TEACHERS[2]
        hit = vl.find_avatar(vl.face_key("avatar_teacher_female", third["id"]))
        assert hit["id"] == third["id"]
        assert vl.find_avatar("avatar_teacher_female")["id"] == FEMALE_TEACHERS[0]["id"], "a bare key: oldest, as before"

    def test_a_face_that_is_gone_falls_back_to_the_oldest_and_warns(self, caplog):
        vl._sb = lambda: _fake_sb(ROSTER, {})
        with caplog.at_level(logging.WARNING, logger="shared.visual_library"):
            hit = vl.find_avatar("avatar_teacher_female__face_deadbeef")
        assert hit["id"] == FEMALE_TEACHERS[0]["id"]
        assert any("is gone" in r.getMessage() for r in caplog.records)

    def test_two_faces_of_one_teacher_never_share_a_cache_directory(self, tmp_path):
        calls: dict = {}
        vl._sb = lambda: _fake_sb(ROSTER, calls)
        cache = tmp_path / "cache"
        a = vl.face_key("avatar_teacher_female", FEMALE_TEACHERS[0]["id"])
        b = vl.face_key("avatar_teacher_female", FEMALE_TEACHERS[1]["id"])
        assert vl.hydrate_avatar(a, cache) and vl.hydrate_avatar(b, cache)
        da, db = cache / renderer_canonical_key(a), cache / renderer_canonical_key(b)
        assert da != db and (da / "asset.png").exists() and (db / "asset.png").exists()
        assert calls["download"] == [FEMALE_TEACHERS[0]["storage_path"], FEMALE_TEACHERS[1]["storage_path"]]
        assert json.loads((da / "meta.json").read_text(encoding="utf-8"))["library_asset_id"] == FEMALE_TEACHERS[0]["id"]

    def test_the_roster_listing_is_approved_avatars_only(self):
        calls: dict = {}
        stray = {**MALE_TEACHER, "id": "stray", "asset_key": "plant_cell", "canonical_key": "cell_plant",
                 "asset_type": "visual", "role": None}
        demoted = {**MALE_TEACHER, "id": "demoted", "status": "rejected"}
        vl._sb = lambda: _fake_sb(ROSTER + [stray, demoted], calls)
        rows = vl.list_avatar_roster()
        assert {r["id"] for r in rows} == {r["id"] for r in ROSTER}
        q = calls["queries"][0]
        assert q["eq:status"] == "approved" and q["like:asset_key"] == "avatar%"


# ── 6. publish stays coherent: a face key publishes as its family, once ─────

class TestPublishCoherence:
    def test_a_generated_face_is_refused_while_the_roster_has_the_family(self, tmp_path):
        calls: dict = {}
        vl._sb = lambda: _fake_sb(ROSTER, calls)
        png = tmp_path / "face.png"
        png.write_bytes(_png_bytes())
        key = vl.face_key("avatar_teacher_female", FEMALE_TEACHERS[0]["id"])
        assert vl.publish_generated(key, AVATAR_PROMPTS["avatar_teacher_female"], png) is True
        assert "upload" not in calls and "insert" not in calls

    def test_a_family_the_roster_lacks_publishes_under_the_roster_key(self, tmp_path):
        calls: dict = {}
        vl._sb = lambda: _fake_sb(STUDENTS, calls)       # no teacher faces at all
        png = tmp_path / "face.png"
        png.write_bytes(_png_bytes())
        assert vl.publish_generated("avatar_teacher__face_5079cb33", AVATAR_PROMPTS["avatar_teacher"], png) is True
        row = calls["insert"][0]
        assert row["asset_key"] == "avatar_teacher" and row["canonical_key"] == "avatar_teacher"
        assert row["asset_type"] == "avatar" and row["role"] == "teacher"
        assert calls["upload"][0].startswith("generated/avatar_teacher/")
        assert vl._local_candidates()[0]["asset_key"] == "avatar_teacher"


# ── 7. the engine renders a face key like any avatar ────────────────────────

class TestEngineWiring:
    def test_avatar_prompt_is_the_family_prompt(self):
        for base, prompt in AVATAR_PROMPTS.items():
            assert avatar_prompt(vl.face_key(base, "0123abcd-ffff")) == prompt
        assert avatar_prompt("avatar_student_undergrad_m") == AVATAR_PROMPTS["avatar_student_undergrad_m"]
        assert avatar_prompt("avatar_student_never_seen") == AVATAR_PROMPTS["avatar_student"]
        assert avatar_prompt("avatar_teacher_never_seen") == AVATAR_PROMPTS["avatar_teacher"]

    def test_teacher_avatar_for_voice_still_names_the_family(self):
        assert teacher_avatar_for_voice("edge-aria") == "avatar_teacher_female"
        assert teacher_avatar_for_voice("edge-guy") == "avatar_teacher"
        assert voice_gender("en-US-AnaNeural") == "f" and voice_gender("en-US-EmmaNeural") == "f"

    def test_compile_plan_carries_the_face_key_and_its_prompt(self):
        from spike.scene_engine.continuity import compile_plan, parse_visual_plan
        from spike.scene_engine.whiteboard import STUDENT_ID, TEACHER_ID
        plan = parse_visual_plan({"chapters": [
            {"concept": "plant_cell_structure",
             "assets": {"plant_cell": "a cell"},
             "elements": [{"id": "cell", "type": "illustration", "asset": "plant_cell",
                           "at": [600, 380], "scale": 0.9}],
             "steps": [{"segment": 1, "decision": "NEW_VISUAL",
                        "actions": [{"verb": "draw", "target": "cell"}]},
                       {"segment": 2, "decision": "CONTINUE", "actions": []}]}]})
        assert plan is not None
        avatars = cast_avatars("edge-aria", "9", "gen-c", lang="en", style="conversational", roster=ROSTER)
        narr = {"s001": "A cell has a nucleus.", "s002": "The wall protects it."}
        scenes, assets, _ = compile_plan(plan, narr, all_segments=list(narr),
                                         avatars=avatars, style="conversational")
        for sid in narr:                                   # the SAME face on every segment
            els = {e["id"]: e for e in scenes[sid]["elements"]}
            assert els[TEACHER_ID]["asset"] == avatars["teacher"]
            assert els[STUDENT_ID]["asset"] == avatars["student"]
            assert assets[sid][avatars["teacher"]] == AVATAR_PROMPTS["avatar_teacher_female"]
            assert assets[sid][avatars["student"]] == avatar_prompt(avatars["student"])

    def test_the_composer_maps_a_face_key_to_its_family_prompt(self):
        src = Path("agent6_animation/video_composer.py").read_text(encoding="utf-8")
        assert "prompts[str(k)] = avatar_prompt(str(k))" in src
        assert "for k, v in AVATAR_PROMPTS.items():" in src

    def test_the_worker_casts_once_from_the_effective_voice_with_the_generation_id(self):
        src = Path("worker/process.py").read_text(encoding="utf-8")
        i = src.index("avatars = cast_avatars(effective_voice, book.get(\"grade\"), generation_id,")
        assert "style=narration_style" in src[i:i + 300]
        assert i < src.index("for part_idx, episode in enumerate(episodes_plan, start=1):"), \
            "cast before the parts loop: every part shares the face"
        assert "resolve_voice(tts_voice, allow_premium, lang=lesson_lang).voice_id" in src[i - 400:i]
