"""The STUDENT's voice in a two-voice dialogue (catalogue Phase 3, decision 2).

The registry gains a ``role`` and eight premium student voices (Chirp 3 HD
Leda/Puck in en, ar, fr, es) that no picker offers and no default resolves
to; the composer speaks the student's lines through synthesize() with that
voice when the account may render premium and the provider is enabled, and
otherwise keeps today's free age-matched Edge student — recording either way
under ``report["student"]``; the avatar cast follows the student voice's
gender. Providers are stubbed; nothing here reaches Google or Edge."""

from __future__ import annotations

from pathlib import Path

import pytest

import agent6_animation.video_composer as vc
from agent6_animation.video_composer import compose_episode_videos
from shared import tts
from shared.tts import registry as R
from spike.scene_engine.whiteboard import cast_avatars, student_avatar_key, student_band_for_grade
from tests.test_tts_phase1b import _composer_stub, _google_on, _inputs

STUDENTS = {
    "g-en-student-f": ("en-US-Chirp3-HD-Leda", "f", "en"),
    "g-en-student-m": ("en-US-Chirp3-HD-Puck", "m", "en"),
    "g-ar-student-f": ("ar-XA-Chirp3-HD-Leda", "f", "ar"),
    "g-ar-student-m": ("ar-XA-Chirp3-HD-Puck", "m", "ar"),
    "g-fr-student-f": ("fr-FR-Chirp3-HD-Leda", "f", "fr"),
    "g-fr-student-m": ("fr-FR-Chirp3-HD-Puck", "m", "fr"),
    "g-es-student-f": ("es-ES-Chirp3-HD-Leda", "f", "es"),
    "g-es-student-m": ("es-ES-Chirp3-HD-Puck", "m", "es"),
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for k in ("TTS_PREMIUM_PROVIDER", "ELEVENLABS_ENABLED", "ELEVENLABS_API_KEY",
              "GOOGLE_TTS_ENABLED", "GOOGLE_APPLICATION_CREDENTIALS",
              "GOOGLE_APPLICATION_CREDENTIALS_JSON", "VERTEX_PROJECT_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(tts.cost, "_SPEND_FILE", tmp_path / "spend.json")


# ── registry ────────────────────────────────────────────────────────────────


class TestRegistry:
    def test_every_voice_has_a_role_and_narrator_is_the_default(self):
        assert R.TTSVoice("x", "X", "edge", "free", "ref").role == "narrator"
        assert all(v.role in ("narrator", "student") for v in R.VOICES)

    def test_the_student_voices_are_pinned(self):
        assert {v.voice_id for v in R.VOICES if v.role == "student"} == set(STUDENTS)
        for vid, (ref, gender, lang) in STUDENTS.items():
            v = R.get_voice(vid)
            assert (v.ref, v.gender, v.lang, v.provider, v.tier, v.role) == (ref, gender, lang, "google", "premium", "student")

    def test_students_are_hidden_from_every_picker(self):
        for include_premium in (False, True):
            offered = {v.voice_id for v in R.list_voices(include_premium)}
            assert not (offered & set(STUDENTS)), "a youthful student voice must never be a narration option"
        assert "g-en-f" in {v.voice_id for v in R.list_voices(True)}, "narrators are still offered"

    @pytest.mark.parametrize("student", sorted(STUDENTS))
    def test_a_student_voice_is_not_a_narration_pick_either(self, student, monkeypatch):
        """A crafted params.tts_voice naming a student voice used to come back
        as-is from pick_voice_id and narrate the whole lesson in Leda's
        voice. It is now read as `auto`: the account's default for the
        language — premium when the tier and the provider allow, free
        otherwise — never the student."""
        lang = R.get_voice(student).lang
        free = tts.pick_voice_id(student, lang=lang, allow_premium=True, explicit_language=True, enabled=frozenset({"edge"}))
        assert free == R.default_voice_id_for(lang) and R.get_voice(free).role == "narrator"
        monkeypatch.setenv("TTS_PREMIUM_PROVIDER", "google")
        prem = tts.pick_voice_id(student, lang=lang, allow_premium=True, explicit_language=True,
                                 enabled=frozenset({"edge", "google"}))
        assert prem == tts.pick_voice_id(None, lang=lang, allow_premium=True, enabled=frozenset({"edge", "google"}))
        assert R.get_voice(prem).role == "narrator" and R.get_voice(prem).tier == "premium"
        # the composer's own path still renders the student's LINES on it
        assert tts.resolve_voice(student, allow_premium=True, lang=lang, enabled=frozenset({"edge", "google"})).voice_id == student

    @pytest.mark.parametrize("lang", ["en", "ar", "fr", "es", "hi"])
    @pytest.mark.parametrize("gender", ["f", "m"])
    def test_a_premium_default_is_never_a_student(self, lang, gender):
        vid = R.default_premium_voice_id_for(lang, gender=gender, provider="google")
        assert vid is not None and R.get_voice(vid).role == "narrator", vid

    def test_student_voice_id_for(self):
        assert R.student_voice_id_for("en", "m") == "g-en-student-m"
        assert R.student_voice_id_for("ar", "f") == "g-ar-student-f"
        assert R.student_voice_id_for("hi", "f") is None, "no student entry → the caller keeps Edge"
        assert R.student_voice_id_for("ms-arab", "f") is None

    def test_remaps_stay_within_the_role(self):
        # a student id never remaps onto a narrator of another family, nor
        # does a narrator remap onto a student
        assert R.equivalent_voice_id("g-en-student-m", "elevenlabs") is None
        assert R.equivalent_voice_id("g-en-m", "google", lang="en") == "g-en-m"


# ── the composer: student lines through synthesize() ────────────────────────


def _record(monkeypatch):
    calls = {"synth": [], "edge": []}

    def fake_synth(text, out, *, voice_id=None, allow_premium=False, report=None, **kw):
        calls["synth"].append((text, voice_id, allow_premium))
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(b"mp3")
        if report is not None:
            report.update({"used": voice_id, "provider": "google", "downgraded": False, "reason": None,
                           "chars": len(text), "stats": {"chars": len(text), "requests": 1}})
        return Path(out)

    import shared.tts.providers.edge as edge

    def fake_edge(line, out, ref, **kw):
        calls["edge"].append((line, ref))
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(b"mp3")

    monkeypatch.setattr(vc, "synthesize", fake_synth)
    monkeypatch.setattr(edge, "synthesize", fake_edge)
    monkeypatch.setattr(vc.subprocess, "run", lambda *a, **k: (Path(a[0][-1]).write_bytes(b"mp3") or None))
    return calls


class TestComposerStudentVoice:
    def test_student_lines_use_the_premium_student_voice_when_allowed(self, monkeypatch, tmp_path):
        _google_on(monkeypatch)
        _composer_stub(monkeypatch, tmp_path)
        monkeypatch.setenv("VIDEO_ENGINE", "scene")
        calls = _record(monkeypatch)
        script, slides = _inputs(2, dialogue=True)
        rep: dict = {}
        compose_episode_videos(script, slides, tts_voice="g-en-f", allow_premium=True, voice_report=rep,
                               lang="en", student_voice="g-en-student-m")
        student = [c for c in calls["synth"] if c[0] == "A question?"]
        assert len(student) == 2 and all(c[1] == "g-en-student-m" and c[2] for c in student)
        teacher = [c for c in calls["synth"] if c[0].startswith("Teacher line")]
        assert teacher and all(c[1] == "g-en-f" for c in teacher)
        assert calls["edge"] == [], "no student line fell to Edge"
        assert rep["student"] == {"requested": "g-en-student-m", "used": ["g-en-student-m"],
                                  "downgraded": False, "reasons": []}
        assert rep["used"] == ["g-en-f"], "the segment is still recorded under the teacher's voice"
        # the student's premium characters are billed like the teacher's
        assert rep["stats"]["chars"] == 2 * (len("A question?") + len("Teacher line 0."))

    def test_without_premium_the_student_stays_on_edge_and_the_fallback_is_recorded(self, monkeypatch, tmp_path):
        _google_on(monkeypatch)
        _composer_stub(monkeypatch, tmp_path)
        monkeypatch.setenv("VIDEO_ENGINE", "scene")
        calls = _record(monkeypatch)
        script, slides = _inputs(1, dialogue=True)
        rep: dict = {}
        compose_episode_videos(script, slides, tts_voice="g-en-f", allow_premium=False, voice_report=rep,
                               lang="en", student_voice="g-en-student-m")
        assert calls["edge"] and all("Neural" in ref for _, ref in calls["edge"]), "today's Edge student"
        assert not [c for c in calls["synth"] if c[1] == "g-en-student-m"]
        assert rep["student"]["requested"] == "g-en-student-m"
        assert rep["student"]["downgraded"] is True and rep["student"]["reasons"] == ["gate"]
        assert "Neural" in rep["student"]["used"][0]

    def test_a_disabled_provider_is_a_recorded_downgrade_too(self, monkeypatch, tmp_path):
        # allow_premium, but Google is not enabled on this worker
        _composer_stub(monkeypatch, tmp_path)
        monkeypatch.setenv("VIDEO_ENGINE", "scene")
        calls = _record(monkeypatch)
        script, slides = _inputs(1, dialogue=True)
        rep: dict = {}
        compose_episode_videos(script, slides, tts_voice="edge-aria", allow_premium=True, voice_report=rep,
                               lang="en", student_voice="g-en-student-f")
        assert calls["edge"], "the student rendered on Edge"
        assert rep["student"]["downgraded"] is True and rep["student"]["reasons"] == ["provider_disabled"]

    def test_no_student_voice_means_todays_behaviour_and_no_report_key(self, monkeypatch, tmp_path):
        _google_on(monkeypatch)
        _composer_stub(monkeypatch, tmp_path)
        monkeypatch.setenv("VIDEO_ENGINE", "scene")
        calls = _record(monkeypatch)
        script, slides = _inputs(1, dialogue=True)
        rep: dict = {}
        compose_episode_videos(script, slides, tts_voice="g-en-f", allow_premium=True, voice_report=rep, lang="en")
        assert calls["edge"] and "student" not in rep

    def test_the_student_report_merges_across_parts(self, monkeypatch, tmp_path):
        _google_on(monkeypatch)
        _composer_stub(monkeypatch, tmp_path)
        monkeypatch.setenv("VIDEO_ENGINE", "scene")
        _record(monkeypatch)
        rep: dict = {}
        for _ in (1, 2):
            script, slides = _inputs(1, dialogue=True)
            compose_episode_videos(script, slides, tts_voice="g-en-f", allow_premium=True, voice_report=rep,
                                   lang="en", student_voice="g-en-student-m")
        assert rep["parts"] == 2 and rep["student"]["used"] == ["g-en-student-m"]


# ── the cast follows the student voice ──────────────────────────────────────


class TestCastFollowsTheStudentVoice:
    def test_the_student_face_takes_the_student_voices_gender(self):
        band = student_band_for_grade("7")
        male = cast_avatars("g-en-f", "7", "seed", style="conversational", dialogue=True,
                            student_voice="g-en-student-m")
        female = cast_avatars("g-en-f", "7", "seed", style="conversational", dialogue=True,
                              student_voice="g-en-student-f")
        assert male["student"] == student_avatar_key(band, "m")
        assert female["student"] == student_avatar_key(band, "f")
        assert male["teacher"] == female["teacher"] == "avatar_teacher_female"

    def test_without_a_student_voice_the_edge_voice_still_decides(self):
        # today's rule: Ana/Emma read the English student → a girl
        band = student_band_for_grade("7")
        assert cast_avatars("g-en-f", "7", "seed", style="conversational", dialogue=True)["student"] == \
            student_avatar_key(band, "f")

    def test_a_student_voice_is_ignored_when_the_lesson_is_not_two_voice(self):
        # the seeded per-lesson pick, exactly as before
        a = cast_avatars("g-en-f", "7", "seed", style="socratic", dialogue=False, student_voice="g-en-student-m")
        b = cast_avatars("g-en-f", "7", "seed", style="socratic", dialogue=False)
        assert a == b
