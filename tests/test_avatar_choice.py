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
import time
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
    yield


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

    def test_a_face_demoted_between_two_casts_is_replaced_not_re_served(self, monkeypatch):
        """The documented guarantee — "the chosen row is removed (demoted/
        deleted; the next-ranked face takes over)" — has to hold in a RUNNING
        worker, not only across a restart. A per-process memo on
        (seed, role, gender, band) made it false for the life of the process:
        the demoted face was answered from the memo and the console's demotion
        was ignored until the worker was restarted. So every cast reads the
        roster, and the seed alone keeps repeats stable."""
        roster = list(FEMALE_TEACHERS)
        calls: list[int] = []

        def listing():
            calls.append(1)
            return list(roster)
        monkeypatch.setattr(vl, "list_avatar_roster", listing)

        first = vl.cast_avatar_key("teacher", "f", "gen-1", "avatar_teacher_female")
        # same seed, unchanged roster: the same face, no memo needed
        assert vl.cast_avatar_key("teacher", "f", "gen-1", "avatar_teacher_female") == first

        chosen = next(r for r in roster if vl.face_key(r["asset_key"], r["id"]) == first)
        roster.remove(chosen)                      # demoted in the console mid-run
        after = vl.cast_avatar_key("teacher", "f", "gen-1", "avatar_teacher_female")
        assert after != first, "a demoted face must never be cast again"
        assert after == vl.face_key(
            "avatar_teacher_female",
            vl._stable_pick(roster, "teacher:gen-1")["id"],
        ), "and the replacement is the next-ranked face, not an arbitrary one"
        assert len(calls) == 3, "the roster is read on every cast; nothing is memoised"

        # an APPROVAL lands the same way: a new row that outranks the current
        # pick for this seed is cast at once, not after a restart
        standing = vl._pick_rank("teacher:gen-1", vl._stable_pick(roster, "teacher:gen-1"))
        winner = next(r for r in (_row(f"{i:08x}-0000-4000-8000-000000000000", "avatar_teacher_female",
                                       "teacher", "2026-12-01T00:00:00Z") for i in range(200))
                      if vl._pick_rank("teacher:gen-1", r) < standing)
        roster.append(winner)
        assert (vl.cast_avatar_key("teacher", "f", "gen-1", "avatar_teacher_female")
                == vl.face_key("avatar_teacher_female", winner["id"]))


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
        """The one source check kept: the cast happens ABOVE the parts loop so
        every part of THIS generation shares the face, and it is seeded on the
        chapter so the separately-generated parts of one chapter agree too.
        The seed was the generation id until 2026-09-05, which silently broke
        the second half of that promise."""
        src = (REPO / "worker" / "process.py").read_text(encoding="utf-8")
        cast = re.search(r"avatars = cast_avatars\(\s*effective_voice,[\s\S]{0,200}?cast_seed", src)
        loop = re.search(r"for part_idx, episode in enumerate\(episodes_plan", src)
        assert cast and loop and cast.start() < loop.start()
        assert 'cast_seed = f"{book_id}:{chapter_num}"' in src


# ── 8. second adversarial pass ──────────────────────────────────────────────

class TestTheFaceFollowsTheDialogueNotTheLabel:
    """The student face must match the second voice exactly when a second
    voice READS the student's lines — a property of the script, decided by
    script_generator, not of a style label re-tested at the cast site."""

    @staticmethod
    def _script(style: str):
        from agent3_scripts.script_generator import generate_episode_script

        def seg():
            # `text` is filled so the LEGACY path (which drops the dialogue
            # array for the four single-narrator styles) is not silent
            return {"type": "explore", "text": "What is a cell? The smallest unit of life?",
                    "elevenlabs_text": "",
                    "dialogue": [{"who": "teacher", "line": "What is a cell?"},
                                 {"who": "student", "line": "The smallest unit of life?"}],
                    "slide_heading": "H", "slide_points": []}

        class _Stub:
            def analyze(self, **kw):
                return {"data": {"segments": [seg(), seg(), seg()]}, "usage": {},
                        "truncated": False}
        return generate_episode_script({"episode_num": 1, "title": "T", "sections": []},
                                       {"chapter_title": "T"}, 1, _Stub(),
                                       narration_style=style)

    def test_an_explicit_dialogue_flag_beats_the_style_label(self):
        # a socratic lesson that DOES carry two-voice dialogue: the student
        # is the voice's gender (Ana/Emma, female, in English) on every seed
        for seed in ("a", "b", "c", "d", "e"):
            key = cast_avatars("edge-guy", "Grade 9", seed, lang="en", style="socratic",
                               dialogue=True, roster=ROSTER)["student"]
            assert vl.avatar_gender(key) == "f", seed
        # a conversational label WITHOUT dialogue keeps the seeded pick
        genders = {vl.avatar_gender(cast_avatars("edge-guy", "Grade 9", f"s{i}", lang="en",
                                                 style="conversational", dialogue=False,
                                                 roster=ROSTER)["student"]) for i in range(20)}
        assert genders == {"f", "m"}

    @pytest.mark.parametrize("semantic", ["", "1"])
    def test_the_default_is_the_script_generators_own_decision(self, monkeypatch, semantic):
        """Behavioural, on BOTH script paths: for every style, the script is
        generated from a reply whose every segment carries a two-line
        dialogue; the face is constrained to the second voice's gender iff
        the generator turned that dialogue into two-voice playback. On the
        SEMANTIC path the prompt asks every style for dialogue, and the
        generator still narrates four of them singly — so their student
        face stays free, and a socratic lesson never casts a girl for a
        voice that never speaks."""
        from agent3_scripts.prompts import NARRATION_STYLES
        from spike.scene_engine.whiteboard import two_voice_dialogue
        monkeypatch.setenv("SEMANTIC_PLAN", semantic)
        two_voice_styles = set()
        for style in NARRATION_STYLES:
            script = self._script(style)
            two_voice = any(s.dialogue for s in script.segments)
            assert two_voice_dialogue(style) is two_voice, (style, semantic)
            genders = {vl.avatar_gender(cast_avatars("edge-guy", "Grade 9", f"g{i}", lang="en",
                                                     style=style, roster=ROSTER)["student"])
                       for i in range(20)}
            if two_voice:
                two_voice_styles.add(style)
                assert genders == {"f"}, (style, semantic)
            else:
                assert genders == {"f", "m"}, (style, semantic)
        assert two_voice_styles == {"conversational"}, "the only two-voice style today"

    def test_the_board_seats_the_student_by_the_same_predicate(self):
        from spike.scene_engine.continuity import compile_plan, parse_visual_plan
        from spike.scene_engine.whiteboard import STUDENT_ID
        plan = parse_visual_plan({"chapters": [
            {"concept": "c", "assets": {"cell": "a cell"},
             "elements": [{"id": "cell", "type": "illustration", "asset": "cell",
                           "at": [600, 380], "scale": 0.9}],
             "steps": [{"segment": 1, "decision": "NEW_VISUAL",
                        "actions": [{"verb": "draw", "target": "cell"}]}]}]})
        narr = {"s001": "A cell has a nucleus."}
        seated = {}
        for style in ("socratic", "conversational"):
            avatars = cast_avatars("edge-aria", "9", "gen-b", lang="en", style=style, roster=ROSTER)
            scenes, _, _ = compile_plan(plan, narr, all_segments=list(narr),
                                        avatars=avatars, style=style)
            seated[style] = any(e.get("id") == STUDENT_ID for e in scenes["s001"]["elements"])
        assert seated == {"socratic": False, "conversational": True}

    def test_the_worker_passes_the_decision_explicitly(self):
        src = (REPO / "worker" / "process.py").read_text(encoding="utf-8")
        assert re.search(r"cast_avatars\([\s\S]{0,300}?dialogue=two_voice_dialogue\(narration_style\)", src)


class TestFacesWarmOnceBeforeThePool:
    def _compose(self, tmp_path, monkeypatch, avatars: dict, events: list):
        import agent6_animation.video_composer as vc
        from spike.scene_engine import raster_assets as ra

        def fake_make_resolver(prompts, *a, **k):
            events.append(("resolver", dict(prompts)))
            events.append(("resolver_kwargs", {"positional": a, **k}))
            return lambda key: events.append(("warm", key)) or object()
        monkeypatch.setattr(ra, "make_resolver", fake_make_resolver)
        monkeypatch.setattr(ra, "load_hand", lambda *a, **k: events.append(("hand", None)) or None)
        monkeypatch.setenv("VIDEO_ENGINE", "scene")
        monkeypatch.setattr(vc, "VIDEO_DIR", tmp_path)
        monkeypatch.setattr(vc, "_MAX_RENDER_WORKERS", 2)
        monkeypatch.setattr(vc, "_ffmpeg_exe", lambda: "ffmpeg")
        monkeypatch.setattr(vc, "_audio_duration", lambda p, f: 2.0)
        monkeypatch.setattr(vc, "concepts_for_slides", lambda hs: ["c"] * len(hs))

        def render(seg, narration, audio, secs, out, direction, scene_dict=None, avatars=None):
            events.append(("segment", seg["segment_id"]))
            Path(out).write_bytes(b"mp4")
            return True
        monkeypatch.setattr(vc, "_render_scene_segment", render)
        monkeypatch.setattr(vc, "render_native_segment", lambda *a, **k: True)
        monkeypatch.setattr("spike.scene_engine.whiteboard.build_whiteboard_scene",
                            lambda seg, avatars=None: {"stub": True})

        def fake_synth(text, out, *, report=None, **kw):
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(b"mp3")
            if report is not None:
                report.update({"used": "edge-aria", "provider": "edge", "downgraded": False,
                               "reason": None, "chars": 1, "stats": {}})
            return Path(out)
        monkeypatch.setattr(vc, "synthesize", fake_synth)
        segs = [{"segment_id": f"s{i}", "text": f"Narration {i}.", "slide_heading": "H",
                 "slide_points": ["p"], "estimated_duration_seconds": 3} for i in range(3)]
        script = {"episodes": [{"book_id": "bk", "chapter_num": 1, "episode_num": 1,
                                "episode_title": "Ep", "segments": segs, "avatars": avatars}]}
        vc.compose_episode_videos(script, {"segments": [{"segment_id": s["segment_id"]} for s in segs]},
                                  tts_voice="edge-aria", allow_premium=False, lang="en")

    def test_the_lesson_warms_both_cast_faces_once_before_any_segment(self, tmp_path, monkeypatch):
        avatars = cast_avatars("edge-aria", "9", "gen-warm", lang="en",
                               style="conversational", roster=ROSTER)
        assert vl.face_id_of(avatars["teacher"]) and vl.face_id_of(avatars["student"])
        events: list = []
        self._compose(tmp_path, monkeypatch, avatars, events)
        kinds = [k for k, _ in events]
        assert kinds.count("resolver") == 1, "one resolver, built once for the lesson"
        first_segment = kinds.index("segment")
        assert kinds.index("resolver") < first_segment and kinds.index("hand") < first_segment
        warmed = [v for k, v in events[:first_segment] if k == "warm"]
        assert sorted(warmed) == sorted(avatars.values()), "both faces, each once, before the pool"
        prompts = next(v for k, v in events if k == "resolver")
        assert prompts == {avatars["teacher"]: AVATAR_PROMPTS["avatar_teacher_female"],
                           avatars["student"]: AVATAR_PROMPTS[vl.base_avatar_key(avatars["student"])]}

    def test_a_lesson_without_a_cast_warms_no_face(self, tmp_path, monkeypatch):
        events: list = []
        self._compose(tmp_path, monkeypatch, {}, events)
        assert not [e for e in events if e[0] in ("resolver", "warm")]
        assert [k for k, _ in events].count("segment") == 3

    def test_the_warm_up_hydrates_but_never_pays_to_generate(self, tmp_path, monkeypatch):
        """The warm-up resolves with allow_generate=False.

        The cast names a student face for EVERY lesson, but only a lesson
        whose script gives a segment two-voice dialogue ever seats the
        student on the board. Warming with allow_generate=True therefore made
        every socratic/narrative lesson pay for — and block the whole render
        on — an image it would never draw. The flag stops the generation and
        nothing else: hydration runs before it is consulted (pinned below).
        """
        avatars = cast_avatars("edge-aria", "9", "gen-warm", lang="en",
                               style="conversational", roster=ROSTER)
        events: list = []
        self._compose(tmp_path, monkeypatch, avatars, events)
        kwargs = next(v for k, v in events if k == "resolver_kwargs")
        assert kwargs.get("allow_generate") is False, kwargs

    def test_a_library_face_still_lands_on_disk_before_the_pool(self, tmp_path, monkeypatch):
        """allow_generate=False must not cost the warm-up its point. The
        visual-library wrapper hydrates the roster face BEFORE it calls the
        renderer, and the renderer consults the flag only after the cache
        misses — so a face the library already holds is on disk before the
        first segment either way, which is what the warm-up is for."""
        import shared.visual_library_integration  # noqa: F401 — installs the wrapper
        from spike.scene_engine import raster_assets as ra
        calls: dict = {}
        monkeypatch.setattr(vl, "_sb", lambda: _fake_sb(ROSTER, calls))
        monkeypatch.setattr(vl, "DECISION_LOG", tmp_path / "decisions.log")
        generated: list = []
        monkeypatch.setattr(ra, "_vertex_call",
                            lambda *a, **k: generated.append("vertex") or None)
        monkeypatch.setattr(ra, "_aistudio_call",
                            lambda *a, **k: generated.append("aistudio") or None)
        key = cast_avatars("edge-aria", "9", "gen-warm2", lang="en", roster=ROSTER)["teacher"]
        cache = tmp_path / "cache"
        png = cache / renderer_canonical_key(key) / "asset.png"
        assert not png.exists()
        resolve = ra.make_resolver({key: avatar_prompt(key)}, cache_dir=cache,
                                   allow_generate=False)
        resolve(key)
        assert png.exists(), "hydration is not gated on allow_generate"
        assert calls.get("download"), "and the face came from the library"
        assert generated == [], "nothing was generated at warm-up"

    def test_a_face_the_lesson_really_draws_is_still_generated_by_its_segment(self):
        """The other half of allow_generate=False: the warm-up refuses to pay,
        so the SEGMENT has to. Both segment paths reach generation through a
        resolver over `prompts` (which carries the cast face keys), and the
        process path pre-warms every `illustration` element on the segment's
        own thread before handing the child a cache-only resolver — so this
        only holds because a seated avatar IS an illustration element."""
        from spike.scene_engine.whiteboard import (STUDENT_ID, TEACHER_ID,
                                                   build_whiteboard_scene)
        avatars = cast_avatars("edge-aria", "9", "gen-draw", lang="en",
                               style="conversational", roster=ROSTER)
        scene = build_whiteboard_scene(
            {"segment_id": "s1", "slide_heading": "H", "slide_points": ["p"],
             "dialogue": [{"who": "teacher", "line": "What is a cell?"},
                          {"who": "student", "line": "The smallest unit."}]},
            avatars=avatars)
        seated = [e for e in scene["elements"]
                  if e.get("id") in (TEACHER_ID, STUDENT_ID)]
        assert {e["id"] for e in seated} == {TEACHER_ID, STUDENT_ID}
        assert all(e.get("type") == "illustration" for e in seated), seated
        assert {e.get("asset") for e in seated} == set(avatars.values())

        src = (REPO / "agent6_animation" / "video_composer.py").read_text(encoding="utf-8")
        seg = src[:src.index("def compose_episode_videos")]
        # the process path's per-segment warm, and the in-process resolver:
        # neither may be gated, or a drawn face would never be generated
        assert "warm = make_resolver(prompts)" in seg
        assert 'e.get("type") == "illustration"' in seg
        assert "asset_resolver=make_resolver(prompts)," in seg


class TestHydrationHoldsThePerKeyLock:
    """Two segment threads, one cast face, an empty cache: the second thread
    must wait for the first's hydration to finish rather than open the file
    it is still writing."""

    @staticmethod
    def _blocking_sb(started, release, calls):
        sb = _fake_sb(ROSTER, calls)

        class Storage:
            def from_(self, _b):
                return self

            def download(self, path):
                calls.setdefault("download", []).append(path)
                started.set()
                assert release.wait(10), "test released the download"
                return _png_bytes()
        sb.storage = Storage()
        return sb

    def test_a_second_thread_waits_for_the_first_hydration(self, tmp_path, monkeypatch):
        import threading

        import shared.visual_library_integration  # noqa: F401 — installs the wrapper
        from spike.scene_engine import raster_assets as ra

        started, release, calls = threading.Event(), threading.Event(), {}
        monkeypatch.setattr(vl, "_sb", lambda: self._blocking_sb(started, release, calls))
        monkeypatch.setattr(vl, "DECISION_LOG", tmp_path / "decisions.log")
        key = cast_avatars("edge-aria", "9", "gen-race", roster=ROSTER)["teacher"]
        assert vl.face_id_of(key)
        cache = tmp_path / "cache"
        png = cache / renderer_canonical_key(key) / "asset.png"
        seen: list = []

        def inner(k, prompt, cache_dir=None, allow_generate=True):
            # inside the lock: record what the cache holds at that moment
            complete = png.exists() and Image.open(png).size == (64, 64)
            seen.append((threading.current_thread().name, complete))
            return None
        monkeypatch.setattr(ra, "_get_raster_asset", inner)

        def run():
            ra.get_raster_asset(key, avatar_prompt(key), cache)
        a = threading.Thread(target=run, name="A")
        b = threading.Thread(target=run, name="B")
        a.start()
        assert started.wait(10), "thread A reached the download"
        # A is inside the download, holding the key's lock: the lock is
        # NOT available, and B cannot get past it
        assert ra.asset_lock(key).acquire(blocking=False) is False, \
            "hydration must run under the per-key lock"
        b.start()
        b.join(0.5)
        assert b.is_alive() and seen == [] and len(calls["download"]) == 1
        release.set()
        a.join(10)
        b.join(10)
        assert not a.is_alive() and not b.is_alive()
        assert seen == [("A", True), ("B", True)], "both saw a COMPLETE face, in lock order"
        assert len(calls["download"]) == 1, "B found A's file; it never hydrated again"
        assert not list(png.parent.glob("*.part"))

    def test_the_lock_is_re_entrant_for_the_wrapper(self):
        from spike.scene_engine import raster_assets as ra
        lock = ra.asset_lock("avatar_teacher__face_test")
        assert lock is ra.asset_lock("avatar_teacher__face_test")
        assert lock.acquire(blocking=False) and lock.acquire(blocking=False)
        lock.release()
        lock.release()

    def test_a_failed_write_leaves_no_half_face_behind(self, tmp_path, monkeypatch, caplog):
        import os
        calls: dict = {}
        monkeypatch.setattr(vl, "_sb", lambda: _fake_sb(ROSTER, calls))
        key = vl.face_key("avatar_teacher_female", FEMALE_TEACHERS[1]["id"])
        cache = tmp_path / "cache"

        def boom(src, dst):
            raise OSError("disk full")
        monkeypatch.setattr(os, "replace", boom)
        with caplog.at_level(logging.WARNING, logger="shared.visual_library"):
            assert vl.hydrate_avatar(key, cache) is None
        d = cache / renderer_canonical_key(key)
        assert not (d / "asset.png").exists() and not list(d.glob("*.part"))
        assert any("hydration failed" in r.getMessage() for r in caplog.records)
        monkeypatch.undo()
        monkeypatch.setattr(vl, "_sb", lambda: _fake_sb(ROSTER, calls))
        assert vl.hydrate_avatar(key, cache) and (d / "asset.png").exists()
        assert not list(d.glob("*.part"))


class TestTheScratchFileIsPrivateToItsWriter:
    """`_write_atomic` writes beside the target and renames it in, so a reader
    never opens a half-written asset.png. One FIXED `asset.png.part` for every
    writer defeated exactly the case it exists for: two writers of the same
    key — the parent and a segment subprocess, or two workers sharing a cache
    volume — opened the SAME scratch path, so one could overwrite the other's
    bytes and the unconditional finally-unlink could delete a scratch file
    still on its way in. The name now carries the writer's pid and a uuid4,
    and the unlink runs only on failure."""

    def test_two_writers_never_share_a_scratch_path(self, tmp_path, monkeypatch):
        import os
        png = tmp_path / "asset.png"
        scratch: list[Path] = []
        real_write = Path.write_bytes

        def spy(self, data):
            scratch.append(Path(self))
            return real_write(self, data)
        monkeypatch.setattr(Path, "write_bytes", spy)

        vl._write_atomic(png, b"first")
        vl._write_atomic(png, b"second")
        assert png.read_bytes() == b"second"
        assert (len(scratch) == 2 and scratch[0] != scratch[1]), (
            "each writer needs its own scratch file")
        assert all(q.name.endswith(".part") and f".{os.getpid()}." in q.name
                   for q in scratch), scratch
        assert not list(tmp_path.glob("*.part")), "renamed away, never left behind"

    def test_a_second_writer_cannot_clobber_a_flight_in_progress(self, tmp_path, monkeypatch):
        """Writer A is interrupted between its write and its rename; writer B
        runs to completion inside that window. With one shared name B
        overwrote A's bytes AND its finally-unlink removed the file A was
        about to rename, so A raised FileNotFoundError — which hydrate_avatar
        turns into a lost face and a regenerated, different one."""
        png = tmp_path / "asset.png"
        real_write = Path.write_bytes

        def spy(self, data):
            real_write(self, data)
            if data == b"A":            # A's window: B runs start to finish
                vl._write_atomic(png, b"B")
        monkeypatch.setattr(Path, "write_bytes", spy)

        vl._write_atomic(png, b"A")     # must not raise
        assert png.read_bytes() == b"A", "A renamed in ITS OWN bytes, last"
        assert not list(tmp_path.glob("*.part"))

    # a day is long past any plausible age floor, so these say "abandoned"
    # without pinning the constant; the floor itself is pinned below.
    DAY = 24 * 3600.0

    def _age(self, path: Path, seconds: float) -> Path:
        import os
        t = time.time() - seconds
        os.utime(path, (t, t))
        return path

    def test_an_abandoned_scratch_file_is_swept_and_a_live_one_is_not(self, tmp_path):
        """A writer killed between write_bytes and os.replace leaves a
        uniquely named, full-size PNG that nothing will ever name again — one
        per crash, forever, on a persistent cache volume. Each writer sweeps
        its own directory on the way in. The age floor is the safety: a young
        .part may belong to a writer still in flight, and taking it would be
        the shared-name bug again with a stopwatch on."""
        import os
        png = tmp_path / "asset.png"
        orphan = tmp_path / f"asset.png.{os.getpid() - 1}.deadbeef.part"
        orphan.write_bytes(b"x" * 4096)
        self._age(orphan, self.DAY)
        inflight = tmp_path / "asset.png.99999.cafe.part"
        inflight.write_bytes(b"still being written")
        other = tmp_path / "keep.png"
        other.write_bytes(b"not a scratch file")

        vl._write_atomic(png, b"new")

        assert png.read_bytes() == b"new"
        assert not orphan.exists(), "a day-old orphan belongs to no live writer"
        assert inflight.read_bytes() == b"still being written", \
            "a young .part belongs to a writer that may still rename it in"
        assert other.exists(), "the sweep only takes .part files"

    def test_the_age_floor_outlives_any_write_this_module_makes(self, tmp_path):
        """Where the line sits. Below the floor a scratch file is another
        writer's property; above it, it is litter. The floor has to be far
        longer than a few hundred KB take to hit the disk."""
        assert vl._SCRATCH_TTL_S >= 600, "an in-flight write must never look abandoned"
        png = tmp_path / "asset.png"
        inside, outside = tmp_path / "asset.png.1.aaa.part", tmp_path / "asset.png.1.bbb.part"
        for q, age in ((inside, vl._SCRATCH_TTL_S - 120), (outside, vl._SCRATCH_TTL_S + 120)):
            q.write_bytes(b"scratch")
            self._age(q, age)
        vl._write_atomic(png, b"new")
        assert inside.exists() and not outside.exists()

    def test_the_sweep_only_takes_the_directory_it_writes_to(self, tmp_path):
        here, elsewhere = tmp_path / "a", tmp_path / "b"
        here.mkdir()
        elsewhere.mkdir()
        stale = []
        for d in (here, elsewhere):
            q = d / "asset.png.1.abc.part"
            q.write_bytes(b"orphan")
            stale.append(self._age(q, self.DAY))
        vl._write_atomic(here / "asset.png", b"new")
        assert not stale[0].exists()
        assert stale[1].exists(), "a sibling key's directory is not this writer's to tidy"

    def test_a_sweep_that_cannot_run_still_lets_the_write_through(self, tmp_path, monkeypatch):
        """Tidy-up, never a precondition: a listing or an unlink that raises
        must not cost the caller its asset."""
        png = tmp_path / "asset.png"
        orphan = tmp_path / "asset.png.1.abc.part"
        orphan.write_bytes(b"orphan")
        self._age(orphan, self.DAY)
        real_unlink = Path.unlink

        def refuse(self, *a, **k):
            if self.name.endswith(".part") and self == orphan:
                raise PermissionError("in use")
            return real_unlink(self, *a, **k)
        monkeypatch.setattr(Path, "unlink", refuse)
        vl._write_atomic(png, b"new")
        assert png.read_bytes() == b"new" and orphan.exists()
        monkeypatch.undo()

        def blind(self, pattern):
            raise OSError("cannot list")
        monkeypatch.setattr(Path, "glob", blind)
        vl._write_atomic(png, b"newer")
        assert png.read_bytes() == b"newer"


class TestMetaJsonGoesInTheSameWayTheAssetDoes:
    """Making asset.png atomic moved the torn read one file over: meta.json
    was still a truncating write_text. A reader (another process — the
    per-key lock is in-process only) that catches it mid-write parses
    nothing, falls back to `md = {}`, finds no `annotated_for` and re-runs
    annotate_regions. That is a PAID vision call for a file that was good a
    moment before and is good again a moment after."""

    def test_both_files_are_renamed_in_from_a_private_scratch(self, tmp_path, monkeypatch):
        import os
        calls: dict = {}
        monkeypatch.setattr(vl, "_sb", lambda: _fake_sb(ROSTER, calls))
        key = vl.face_key("avatar_teacher_female", FEMALE_TEACHERS[1]["id"])
        cache = tmp_path / "cache"
        renamed: list[tuple[str, str]] = []
        real_replace = os.replace

        def spy(src, dst, **kw):
            renamed.append((str(src), str(dst)))
            return real_replace(src, dst, **kw)
        monkeypatch.setattr(os, "replace", spy)

        assert vl.hydrate_avatar(key, cache)
        d = cache / renderer_canonical_key(key)
        md = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        assert md["provenance"] == "visual_library"
        assert {Path(dst).name for _, dst in renamed} == {"asset.png", "meta.json"}, renamed
        for src, dst in renamed:
            assert Path(src).name.startswith(Path(dst).name + "."), (src, dst)
            assert src.endswith(".part") and f".{os.getpid()}." in src, src
        assert not list(d.glob("*.part"))

    def test_the_educational_path_writes_its_meta_the_same_way(self, tmp_path, monkeypatch):
        import os
        calls: dict = {}
        monkeypatch.setattr(vl, "_sb", lambda: _fake_sb(ROSTER, calls))
        monkeypatch.setattr(vl, "find", lambda *a, **k: dict(MALE_TEACHER, match_score=1.0))
        renamed: list[str] = []
        real_replace = os.replace

        def spy(src, dst, **kw):
            renamed.append(Path(dst).name)
            return real_replace(src, dst, **kw)
        monkeypatch.setattr(os, "replace", spy)

        assert vl.hydrate("water_cycle", "the water cycle", tmp_path / "cache")
        assert sorted(renamed) == ["asset.png", "meta.json"], renamed

    def test_a_reader_mid_write_gets_the_previous_meta_whole(self, tmp_path, monkeypatch):
        """The consequence, stated as a reader sees it. The scratch write is
        the one instant a concurrent reader can be scheduled into, so that is
        where the reader looks: it must get the OLD document, complete and
        parseable — never a truncated one."""
        meta = tmp_path / "meta.json"
        before = {"provenance": "generated", "annotated_for": ["stem", "leaf"]}
        meta.write_text(json.dumps(before), encoding="utf-8")
        seen: list[dict] = []
        real_write = Path.write_bytes

        def spy(self, data):
            out = real_write(self, data)
            seen.append(json.loads(meta.read_text(encoding="utf-8")))
            return out
        monkeypatch.setattr(Path, "write_bytes", spy)

        vl._write_json_atomic(meta, {"provenance": "visual_library", "library_asset_id": "x"})

        assert seen == [before], (
            "meta.json must be written to a scratch sibling and renamed in; a "
            "truncating write_text never reaches this spy and leaves a window "
            "in which a reader parses nothing")
        assert json.loads(meta.read_text(encoding="utf-8"))["provenance"] == "visual_library"


class TestThisModuleStaysReadable:
    """A wrapped assert whose backslash continuation is lost still passes: the
    message glues onto the end of the expression behind a run of spaces, the
    assert still runs and still reports the right thing, and nothing at all
    complains. Two arrived that way in section 2 (164 and 149 characters) and
    a THIRD slipped into section 9 while those were being fixed — at 115
    characters, under any width floor worth setting. So width is the second
    net, not the test.

    The signature is the run of spaces itself: within ONE physical line, code
    does not put three of them between two tokens. Deliberate alignment lives
    on continuation lines (where the gap is leading indentation this cannot
    see) or before a trailing comment (dropped below), which is why the whole
    module has exactly zero hits once the collapses are wrapped."""

    LIMIT = 125        # the widest line this module was actually written to

    def _lines(self) -> list[str]:
        return Path(__file__).read_text(encoding="utf-8").splitlines()

    def test_no_assert_lost_its_line_continuation(self):
        import token as tk
        import tokenize
        skip = {tk.NL, tk.NEWLINE, tk.INDENT, tk.DEDENT, tk.ENDMARKER, tk.COMMENT}
        with open(__file__, "rb") as fh:
            toks = [t for t in tokenize.tokenize(fh.readline) if t.type not in skip]
        glued = [(b.start[0], b.start[1] - a.end[1]) for a, b in zip(toks, toks[1:])
                 if a.end[0] == b.start[0] and b.start[1] - a.end[1] >= 3]
        lines = self._lines()
        assert not glued, ("collapsed continuation(s) (line, spaces) "
                           + repr(glued) + " in "
                           + repr([lines[n - 1].strip()[:70] for n, _ in glued]))

    def test_no_line_runs_past_the_width_this_module_was_written_to(self):
        wide = [(i, len(line)) for i, line in enumerate(self._lines(), 1)
                if len(line) > self.LIMIT]
        assert not wide, "re-wrap these lines (number, width): " + repr(wide)


class TestTheDrawIsInsertionStable:
    """The draw is rendezvous hashing: each row's rank for a seed is
    sha256(f"{seed}:{row_id}"), the smallest wins. A roster row approved
    between a lesson's first run and its retry therefore cannot move the
    draw from one existing face to another."""

    SEEDS = [f"gen-{i}" for i in range(300)]

    def test_the_rank_is_the_documented_hash(self):
        import hashlib
        for seed in self.SEEDS[:50]:
            want = min(FEMALE_TEACHERS,
                       key=lambda r: hashlib.sha256(f"{seed}:{r['id']}".encode()).hexdigest())
            assert vl._stable_pick(FEMALE_TEACHERS, seed)["id"] == want["id"]

    def test_a_row_approved_later_never_shifts_a_draw_between_existing_faces(self):
        new = _row("0badcafe-0000-4000-8000-00000000cafe", "avatar_teacher_female", "teacher",
                   "2026-12-01T00:00:00Z")
        moved_to_new, unchanged = 0, 0
        for seed in self.SEEDS:
            before = vl._stable_pick(FEMALE_TEACHERS, seed)["id"]
            after = vl._stable_pick(FEMALE_TEACHERS + [new], seed)["id"]
            if after == before:
                unchanged += 1
            else:
                assert after == new["id"], (seed, before, after)
                moved_to_new += 1
        # ~1/6 of seeds may now draw the NEW face (it is a fair candidate);
        # the rest keep theirs. random.choice reshuffled nearly all of them.
        assert unchanged >= 200 and moved_to_new > 0
        # and the same holds end to end, through the cast and its face key
        for seed in self.SEEDS[:60]:
            before = cast_avatars("edge-aria", "9", seed, roster=ROSTER)["teacher"]
            after = cast_avatars("edge-aria", "9", seed, roster=ROSTER + [new])["teacher"]
            assert after == before or vl.face_id_of(after) == "0badcafe", seed

    def test_removing_an_unchosen_row_never_changes_the_draw(self):
        for seed in self.SEEDS[:100]:
            chosen = vl._stable_pick(FEMALE_TEACHERS, seed)["id"]
            for r in FEMALE_TEACHERS:
                rest = [x for x in FEMALE_TEACHERS if x["id"] != r["id"]]
                if r["id"] == chosen:
                    # only the chosen row's removal changes the face — to the
                    # next-ranked one, deterministically
                    assert vl._stable_pick(rest, seed)["id"] != chosen
                else:
                    assert vl._stable_pick(rest, seed)["id"] == chosen, (seed, r["id"])

    def test_the_draw_ignores_row_order_and_created_at(self):
        import random
        for seed in self.SEEDS[:30]:
            want = vl._stable_pick(FEMALE_TEACHERS, seed)["id"]
            rows = list(FEMALE_TEACHERS)
            random.Random(seed).shuffle(rows)
            assert vl._stable_pick(rows, seed)["id"] == want
            redated = [{**r, "created_at": "2030-01-01T00:00:00Z"} for r in FEMALE_TEACHERS]
            assert vl._stable_pick(redated, seed)["id"] == want

    def test_a_draw_still_spreads_across_the_roster(self):
        picks = {vl._stable_pick(FEMALE_TEACHERS, s)["id"] for s in self.SEEDS}
        assert picks == {r["id"] for r in FEMALE_TEACHERS}


# ── the cast seed is the CHAPTER, not the generation ────────────────────────
class TestFaceHoldsAcrossParts:
    """Founder saw Part 1..4 of one chapter as one lesson series; a per-
    generation seed put a different teacher in each, because every part is its
    own generation."""

    def test_the_worker_seeds_the_cast_on_book_and_chapter(self):
        import inspect
        from worker import process
        # The cast lives in the shared build half of process_generation
        # (_build_from_analysis, split out 2026-09-06 for the catalogue path).
        src = inspect.getsource(process._build_from_analysis)
        assert 'cast_seed = f"{book_id}:{chapter_num}"' in src
        assert "cast_avatars(effective_voice, book.get(\"grade\"), cast_seed" in src
        assert "cast_avatars(effective_voice, book.get(\"grade\"), generation_id" not in src

    def test_every_part_of_a_chapter_casts_the_same_face(self):
        from spike.scene_engine.whiteboard import cast_avatars
        roster = [
            {"id": "a1", "asset_key": "avatar_teacher_female", "canonical_key": "avatar_female_teacher",
             "asset_type": "avatar", "status": "approved", "description": "female teacher"},
            {"id": "b2", "asset_key": "avatar_teacher_female", "canonical_key": "avatar_female_teacher",
             "asset_type": "avatar", "status": "approved", "description": "female teacher"},
            {"id": "c3", "asset_key": "avatar_teacher_female", "canonical_key": "avatar_female_teacher",
             "asset_type": "avatar", "status": "approved", "description": "female teacher"},
        ]
        seed = "book-8fce:0"
        casts = [cast_avatars("edge-aria", 7, seed, roster=roster) for _ in range(4)]
        assert len({c["teacher"] for c in casts}) == 1, casts

    def test_another_chapter_may_cast_another_face(self):
        from spike.scene_engine.whiteboard import cast_avatars
        roster = [
            {"id": f"id{i}", "asset_key": "avatar_teacher_female",
             "canonical_key": "avatar_female_teacher", "asset_type": "avatar",
             "status": "approved", "description": "female teacher"} for i in range(8)
        ]
        seen = {cast_avatars("edge-aria", 7, f"book-8fce:{ch}", roster=roster)["teacher"]
                for ch in range(12)}
        assert len(seen) > 1, "every chapter drew the same face; the seed is not varying"


class TestScriptGeneratorDegradesWithoutSpike:
    def test_the_dialogue_predicate_import_is_guarded(self):
        import inspect
        from agent3_scripts import script_generator
        src = inspect.getsource(script_generator)
        i = src.index("from spike.scene_engine.whiteboard import two_voice_dialogue")
        before = src[max(0, i - 400):i]
        assert "try:" in before, "the spike import must be guarded like the adapter import"
