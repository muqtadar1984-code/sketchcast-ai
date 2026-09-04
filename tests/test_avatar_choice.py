"""The avatar is a random roster face, restricted only by the voice's gender,
and one lesson keeps one face (founder decision, 2026-09-04).

Everything here runs offline: the Supabase client is a fake, and no test
reaches a model or the network.
"""

from __future__ import annotations

import io
import json
import logging
import re
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

import shared.visual_library as vl
from spike.scene_engine.raster_assets import canonical_key as renderer_canonical_key
from spike.scene_engine.whiteboard import (AVATAR_PROMPTS, avatar_prompt,
                                           cast_avatars, student_avatar_key,
                                           student_voice_for_avatar,
                                           teacher_avatar_for_voice, voice_gender)

REPO = Path(__file__).resolve().parents[1]


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

        def limit(self, n):
            self.f["limit"] = int(n)
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
            if "limit" in self.f:
                out = out[:self.f["limit"]]
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

    @pytest.mark.parametrize("key,band", [
        ("avatar_student_undergrad_f", "undergrad"), ("avatar_student_grad_m", "grad"),
        ("avatar_student_doctorate_f", "doctorate"), ("avatar_student_11_12_m", "11_12"),
        ("avatar_teacher_female", None), ("avatar_student", "5_7"),
    ])
    def test_post_school_bands_read_off_the_key_like_school_bands(self, key, band):
        """AVATAR_PROMPTS keeps undergrad/grad/doctorate faces and a grade-13+
        book asks for them; a band the key parser could not read was a face
        the roster could publish but never pick (age_band NULL, no match)."""
        assert vl.avatar_age_band(key) == band
        assert vl.avatar_age_band(vl.face_key(key, "0123abcd-ffff")) == band
        assert vl.avatar_fields(key)["age_band"] == (None if key == "avatar_student" else band)

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

    def test_no_student_in_the_band_is_the_generate_path_for_that_band(self, caplog):
        """The roster holds school bands only; a university book must not
        cast a schoolchild. The cast is the age-matched ROSTER key (no face
        suffix), which the wrapper generates and publishes — seeding the
        band — exactly as before the roster existed."""
        with caplog.at_level(logging.INFO, logger="shared.visual_library"):
            key = cast_avatars("edge-aria", "Grade 14", "gen-u", roster=ROSTER)["student"]
        assert key.startswith("avatar_student_undergrad_") and key in AVATAR_PROMPTS
        assert vl.face_id_of(key) is None and vl.avatar_age_band(key) == "undergrad"
        assert vl.hydrate_avatar(key, Path("unused")) is None      # nothing to serve: generate
        assert any("no student face in age band undergrad" in r.getMessage() for r in caplog.records)
        # the same holds for a school band the roster happens to lack
        no_8_10 = [r for r in ROSTER if r.get("age_band") != "8_10"]
        key = cast_avatars("edge-aria", "Grade 9", "gen-u", lang="en",
                           style="conversational", roster=no_8_10)["student"]
        assert key == student_avatar_key("8_10", "f") and vl.face_id_of(key) is None
        # while a band the roster holds still casts a roster face of that band
        key = cast_avatars("edge-aria", "Grade 9", "gen-u", roster=ROSTER)["student"]
        assert vl.face_id_of(key) and vl.avatar_age_band(key) == "8_10"

    @pytest.mark.parametrize("grade,band", [("Grade 14", "undergrad"), ("17", "grad"), ("Grade 20", "doctorate")])
    def test_every_post_school_band_names_its_own_generate_key(self, grade, band):
        for seed in ("a", "b", "c"):
            key = cast_avatars("edge-guy", grade, seed, roster=ROSTER)["student"]
            assert vl.avatar_age_band(key) == band and vl.face_id_of(key) is None
            assert avatar_prompt(key) == AVATAR_PROMPTS[key]

    def test_pick_avatar_relaxes_gender_but_never_the_band(self):
        assert vl.pick_avatar("student", "m", "s", age_band="undergrad", roster=ROSTER) is None
        # only female 5_7 faces exist once the legacy boy is gone; a male 5_7
        # ask still gets a 5_7 face, never one from another band
        girls_only = [r for r in ROSTER if r["asset_key"] != "avatar_student"]
        for seed in ("a", "b", "c", "d"):
            row = vl.pick_avatar("student", "m", seed, age_band="5_7", roster=girls_only)
            assert row and vl.avatar_age_band(row) == "5_7"

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

    def test_every_face_the_listing_can_pick_the_lookup_can_serve(self, caplog):
        """The listing that PICKS a face and the family query that SERVES it
        page by the same limit, so a picked face is never past the lookup's
        reach and quietly swapped for the oldest. Once the family lookup
        stopped at 20 while the listing read 200."""
        big = [_row(f"{i:08x}-0000-4000-8000-000000000000", "avatar_teacher_female", "teacher",
                    f"2026-10-{1 + i // 24:02d}T{i % 24:02d}:00:00Z") for i in range(30)]
        assert len(big) > 20
        calls: dict = {}
        vl._sb = lambda: _fake_sb(big + STUDENTS, calls)
        picked = {vl.cast_avatar_key("teacher", "f", f"gen-{i}", "avatar_teacher_female") for i in range(200)}
        assert len(picked) == 30, "every face in the family is reachable by some generation"
        with caplog.at_level(logging.WARNING, logger="shared.visual_library"):
            for key in picked:
                assert vl.find_avatar(key)["id"].replace("-", "")[:8] == vl.face_id_of(key)
        assert not any("is gone" in r.getMessage() for r in caplog.records)
        limits = {q.get("limit") for q in calls["queries"]}
        assert limits == {vl.ROSTER_LIMIT}, "one page size for the listing and the lookup"

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

    def test_a_generated_post_school_face_seeds_its_band_for_the_next_pick(self, tmp_path):
        """The generate path for a grade-13+ book publishes the age-matched
        face with its band on the row, and the next lesson in that band
        picks it from the roster instead of generating again."""
        calls: dict = {}
        vl._sb = lambda: _fake_sb(ROSTER, calls)
        key = cast_avatars("edge-aria", "Grade 15", "gen-p", roster=ROSTER)["student"]
        assert key == student_avatar_key("undergrad", vl.avatar_gender(key))
        png = tmp_path / "face.png"
        png.write_bytes(_png_bytes())
        assert vl.publish_generated(key, AVATAR_PROMPTS[key], png) is True
        row = calls["insert"][0]
        assert row["asset_key"] == key and row["role"] == "student" and row["age_band"] == "undergrad"
        seeded = ROSTER + [_row("0badf00d-0000-4000-8000-000000000000", key, "student",
                                "2026-10-01T00:00:00Z", "undergrad")]
        again = cast_avatars("edge-aria", "Grade 15", "gen-q", roster=seeded)["student"]
        assert vl.face_id_of(again) == "0badf00d" and vl.avatar_age_band(again) == "undergrad"

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

    def test_the_composer_warms_the_cast_faces_and_hands_the_child_their_family_prompts(
            self, tmp_path, monkeypatch):
        """Observed at the child-process seam, not in source: the parent
        warms every illustration — the face-keyed teacher and student
        included — before dispatch, and the payload's prompt map carries
        each FACE key (the child's cache-only resolver binds by key) with
        the family's prompt. A whiteboard-fallback segment carries no
        scene_assets, so this is the only place those prompts can come from."""
        from concurrent.futures import ThreadPoolExecutor

        import agent6_animation.video_composer as vc
        import spike.scene_engine.raster_assets as ra
        import spike.scene_engine.segment_worker as sw
        from spike.scene_engine.whiteboard import student_element, teacher_element

        avatars = cast_avatars("edge-aria", "9", "gen-w", lang="en",
                               style="conversational", roster=ROSTER)
        assert vl.face_id_of(avatars["teacher"]) and vl.face_id_of(avatars["student"])
        warmed: list[str] = []
        monkeypatch.setattr(ra, "make_resolver", lambda prompts: lambda key: warmed.append(key))
        monkeypatch.setattr(vc, "_RENDER_PROCESSES", 2)

        class Pool:
            def submit(self, f, *a, **k):
                return ThreadPoolExecutor(max_workers=1).submit(f, *a, **k)
        monkeypatch.setattr(vc, "_pool", lambda: Pool())
        got: dict = {}

        def child(payload):
            got.update(payload)
            return True, []
        monkeypatch.setattr(sw, "render_segment_in_child", child)
        scene = {"id": "s", "narration": "x",
                 "elements": [teacher_element(avatars["teacher"]), student_element(avatars["student"])],
                 "actions": [{"verb": "draw", "target": "__teach_av"},
                             {"verb": "draw", "target": "__stud_av"}]}
        seg = {"segment_id": "s001", "scene": scene}          # no scene_assets on purpose
        assert vc._render_scene_segment(seg, "some narration", None, 0.0,
                                        tmp_path / "o.mp4", "ltr", avatars=avatars) is True
        prompts = got["prompts"]
        assert prompts[avatars["teacher"]] == AVATAR_PROMPTS["avatar_teacher_female"]
        assert prompts[avatars["student"]] == AVATAR_PROMPTS[vl.base_avatar_key(avatars["student"])]
        assert set(AVATAR_PROMPTS) <= set(prompts)             # the roster keys ride along
        assert sorted(warmed) == sorted([avatars["teacher"], avatars["student"]])

    def test_the_worker_casts_once_before_the_parts_loop(self):
        """The one source check kept: the seed is the generation id and the
        cast happens ABOVE the parts loop, so every part shares the face."""
        src = (REPO / "worker" / "process.py").read_text(encoding="utf-8")
        cast = re.search(r"avatars = cast_avatars\(\s*effective_voice,[\s\S]{0,200}?generation_id", src)
        loop = re.search(r"for part_idx, episode in enumerate\(episodes_plan", src)
        assert cast and loop and cast.start() < loop.start()
