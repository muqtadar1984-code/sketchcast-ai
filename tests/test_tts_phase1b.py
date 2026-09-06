"""Phase 1b of the Google TTS plan: the Google entries in the registry, the
provider wired into synthesize(), provider-keyed cost, the composer's premium
pre-flight, and teacher dialogue lines through the gate. The Google provider
itself is covered in test_tts_google.py; here it is stubbed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shared import tts
from shared.tts import registry as R


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for k in ("TTS_PREMIUM_PROVIDER", "ELEVENLABS_ENABLED", "ELEVENLABS_API_KEY",
              "GOOGLE_TTS_ENABLED", "GOOGLE_APPLICATION_CREDENTIALS",
              "GOOGLE_APPLICATION_CREDENTIALS_JSON", "VERTEX_PROJECT_ID"):
        monkeypatch.delenv(k, raising=False)
    # cost's spend file must never be the repo's real one in tests
    monkeypatch.setattr(tts.cost, "_SPEND_FILE", tmp_path / "spend.json")


def _google_on(monkeypatch):
    # opt-in: the flag AND credentials (credentials alone must never enable it)
    monkeypatch.setenv("GOOGLE_TTS_ENABLED", "1")
    monkeypatch.setenv("VERTEX_PROJECT_ID", "sketchcast")


# ── registry ─────────────────────────────────────────────────────────────────

class TestGoogleEntries:
    # Every ref and gender checked against the live voices.list on 2026-09-04
    # (22/22 present, ssmlGender matches) — the registry must not drift from
    # what the API actually offers.
    VERIFIED = {
        "g-en-f": ("en-US-Chirp3-HD-Achernar", "f", "en"),
        "g-en-m": ("en-US-Chirp3-HD-Achird", "m", "en"),
        "g-en-gb-f": ("en-GB-Chirp3-HD-Achernar", "f", "en"),
        "g-en-gb-m": ("en-GB-Chirp3-HD-Achird", "m", "en"),
        "g-en-in-f": ("en-IN-Chirp3-HD-Achernar", "f", "en"),
        "g-en-in-m": ("en-IN-Chirp3-HD-Achird", "m", "en"),
        "g-ms-f": ("ms-MY-Wavenet-A", "f", "ms"),
        "g-ms-m": ("ms-MY-Wavenet-B", "m", "ms"),
        "g-ar-f": ("ar-XA-Chirp3-HD-Achernar", "f", "ar"),
        "g-ar-m": ("ar-XA-Chirp3-HD-Achird", "m", "ar"),
        "g-fr-f": ("fr-FR-Chirp3-HD-Achernar", "f", "fr"),
        "g-fr-m": ("fr-FR-Chirp3-HD-Achird", "m", "fr"),
        "g-es-f": ("es-ES-Chirp3-HD-Achernar", "f", "es"),
        "g-es-m": ("es-ES-Chirp3-HD-Achird", "m", "es"),
        "g-pt-f": ("pt-BR-Chirp3-HD-Achernar", "f", "pt"),
        "g-pt-m": ("pt-BR-Chirp3-HD-Achird", "m", "pt"),
        "g-te-f": ("te-IN-Chirp3-HD-Achernar", "f", "te"),
        "g-te-m": ("te-IN-Chirp3-HD-Achird", "m", "te"),
        "g-mr-f": ("mr-IN-Chirp3-HD-Achernar", "f", "mr"),
        "g-mr-m": ("mr-IN-Chirp3-HD-Achird", "m", "mr"),
        "g-hi-f": ("hi-IN-Chirp3-HD-Achernar", "f", "hi"),
        "g-hi-m": ("hi-IN-Chirp3-HD-Achird", "m", "hi"),
    }

    def test_every_google_entry_is_pinned(self):
        # Narrator voices only: the catalogue's student voices (role
        # "student", Leda/Puck) are pinned in tests/test_dialogue_student_voice.py
        # and are never offered as a narration voice.
        assert {v.voice_id for v in R.VOICES if v.provider == "google" and v.role == "narrator"} == set(self.VERIFIED)

    def test_verified_names_and_genders(self):
        for vid, (ref, gender, lang) in self.VERIFIED.items():
            v = R.get_voice(vid)
            assert v is not None, vid
            assert (v.ref, v.gender, v.lang, v.provider, v.tier) == (ref, gender, lang, "google", "premium")

    def test_every_lesson_language_has_a_female_and_male_google_voice(self):
        for lang in ("en", "ms", "ar", "fr", "es", "pt", "te", "mr", "hi"):
            genders = {v.gender for v in R.VOICES if v.provider == "google" and v.lang == lang}
            assert genders == {"f", "m"}, lang

    def test_malay_is_wavenet_because_chirp_does_not_exist_for_it(self):
        assert "Wavenet" in R.get_voice("g-ms-f").ref
        assert "Chirp" in R.get_voice("g-ar-f").ref

    def test_auto_under_google_mode_picks_the_language_voice(self, monkeypatch):
        _google_on(monkeypatch)
        monkeypatch.setenv("TTS_PREMIUM_PROVIDER", "google")
        assert tts.pick_voice_id("auto", lang="ar", allow_premium=True) == "g-ar-f"
        assert tts.pick_voice_id("auto", lang="ms-arab", allow_premium=True) == "g-ms-f"
        assert tts.pick_voice_id("auto", lang="en", allow_premium=True) == "g-en-f"

    def test_auto_under_google_mode_without_credentials_stays_free(self, monkeypatch):
        monkeypatch.setenv("TTS_PREMIUM_PROVIDER", "google")
        assert tts.pick_voice_id("auto", lang="ar", allow_premium=True) == "edge-zariyah"

    def test_google_ids_remap_to_elevenlabs_when_google_is_off(self, monkeypatch):
        """The rollback runbook with the real entries, not a stub."""
        monkeypatch.setenv("ELEVENLABS_ENABLED", "true")
        monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
        assert tts.resolve_voice("g-ar-m", True, lang="ar").voice_id == "el-adam"
        assert tts.resolve_voice("g-hi-f", True, lang="hi").voice_id == "el-rachel"

    def test_stored_elevenlabs_ids_land_on_the_lesson_language_google_voice(self, monkeypatch):
        """Forward migration: ElevenLabs switched off, Google on. A stored
        `el-adam` on a Hindi lesson used to become en-US Achird — an English
        reader for Hindi text — because the multilingual source matched the
        first male Google entry in table order."""
        _google_on(monkeypatch)
        monkeypatch.setenv("TTS_PREMIUM_PROVIDER", "google")
        assert tts.resolve_voice("el-adam", True, lang="hi").voice_id == "g-hi-m"
        assert tts.resolve_voice("el-rachel", True, lang="ar").voice_id == "g-ar-f"
        assert tts.resolve_voice("el-rachel", True, lang="ms-arab").voice_id == "g-ms-f"
        assert tts.resolve_voice("el-rachel", True, lang="zh").voice_id == "edge-aria", \
            "a language Google has no entry for falls to the free voice, not to an English Chirp"


class TestEnabledProviders:
    def test_google_is_opt_in_credentials_alone_never_enable_it(self, monkeypatch):
        """Production already carries VERTEX_PROJECT_ID for Gemini. Inferring
        'enabled' from it would have switched Google on the day this
        deployed — stored el-* picks remapped to Chirp, billing started — with
        the variable meant to control the rollout still unset."""
        assert "google" not in tts.enabled_providers()
        monkeypatch.setenv("VERTEX_PROJECT_ID", "sketchcast")
        assert "google" not in tts.enabled_providers(), "ships dark means dark"
        monkeypatch.setenv("GOOGLE_TTS_ENABLED", "1")
        assert "google" in tts.enabled_providers()
        monkeypatch.setenv("GOOGLE_TTS_ENABLED", "0")
        assert "google" not in tts.enabled_providers()

    def test_the_flag_without_credentials_is_not_enough_either(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_TTS_ENABLED", "1")
        assert "google" not in tts.enabled_providers()
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/srv/creds.json")
        assert "google" in tts.enabled_providers()


# ── synthesize() google branch ───────────────────────────────────────────────

def _stub_google(monkeypatch, *, fail=False):
    import shared.tts.providers.edge as edge
    import shared.tts.providers.google as google
    seen: dict = {}

    def fake_google(text, out, ref, boundaries_out=None):
        seen.update({"text": text, "ref": ref, "boundaries_out": boundaries_out})
        if fail:
            raise RuntimeError("google down")
        Path(out).write_bytes(b"mp3")
        if boundaries_out:
            Path(boundaries_out).write_text('[{"t":0.0,"w":"hi"}]', encoding="utf-8")
        return {"provider": "google", "family": "chirp", "requests": 2, "chars": 42,
                "timepoints": 0, "marks_dropped": 0, "audio_secs": 1.0}

    def fake_edge(say, out, ref, boundaries_out=None, **kw):
        seen["edge"] = {"say": say, "ref": ref, "boundaries_out": boundaries_out}
        Path(out).write_bytes(b"mp3")

    monkeypatch.setattr(google, "synthesize", fake_google)
    monkeypatch.setattr(edge, "synthesize", fake_edge)
    return seen


class TestSynthesizeGoogle:
    def test_markup_copy_reaches_google_and_stats_are_reported(self, monkeypatch, tmp_path):
        _google_on(monkeypatch)
        seen = _stub_google(monkeypatch)
        r: dict = {}
        tts.synthesize("plain", tmp_path / "a.mp3", voice_id="g-en-f", allow_premium=True,
                       ssml_text='plain <break time="0.3s"/> copy', report=r,
                       boundaries_out=tmp_path / "a.words.json", lang="en")
        assert seen["text"] == 'plain <break time="0.3s"/> copy'
        assert seen["ref"] == "en-US-Chirp3-HD-Achernar"
        assert r["used"] == "g-en-f" and r["provider"] == "google" and r["downgraded"] is False
        assert r["chars"] == 42 and r["stats"]["requests"] == 2

    def test_google_failure_falls_to_the_language_free_voice_with_boundaries(self, monkeypatch, tmp_path):
        _google_on(monkeypatch)
        seen = _stub_google(monkeypatch, fail=True)
        r: dict = {}
        # the plain copy carries a stray tag on purpose: the fallback must strip it
        tts.synthesize('plain <break time="1s"/> copy', tmp_path / "b.mp3", voice_id="g-ar-f",
                       allow_premium=True,
                       ssml_text='x <break time="1s"/> y', report=r,
                       boundaries_out=tmp_path / "b.words.json", lang="ar")
        assert r["used"] == "edge-zariyah" and r["downgraded"] is True
        assert seen["edge"]["boundaries_out"] == tmp_path / "b.words.json", \
            "an outage must not also lose word timing"
        assert "<break" not in seen["edge"]["say"], "Edge reads tags aloud"

    def test_the_google_cap_refuses_and_falls_free(self, monkeypatch, tmp_path):
        _google_on(monkeypatch)
        seen = _stub_google(monkeypatch)
        monkeypatch.setitem(tts.cost._CHAR_CAP, "google", 10)
        r: dict = {}
        tts.synthesize("a sentence well over ten characters", tmp_path / "c.mp3",
                       voice_id="g-en-f", allow_premium=True, report=r, lang="en")
        assert "text" not in seen, "google must not have been called past the cap"
        assert r["used"] == "edge-aria" and r["downgraded"] is True

    def test_free_requests_never_touch_the_cap(self, monkeypatch, tmp_path):
        seen = _stub_google(monkeypatch)
        monkeypatch.setitem(tts.cost._CHAR_CAP, "google", 0)
        r: dict = {}
        tts.synthesize("hello", tmp_path / "d.mp3", voice_id="edge-aria", report=r)
        assert r["used"] == "edge-aria" and r["downgraded"] is False


# ── cost, provider-keyed ─────────────────────────────────────────────────────

class TestCost:
    def test_caps_are_per_provider(self, monkeypatch):
        monkeypatch.setitem(tts.cost._CHAR_CAP, "google", 100)
        monkeypatch.setitem(tts.cost._CHAR_CAP, "elevenlabs", 100)
        tts.cost.record(90, "google", "chirp")
        assert tts.cost.within_cap(20, "google") is False
        assert tts.cost.within_cap(20, "elevenlabs") is True, "one provider's spend is not another's"
        assert tts.cost.within_cap(10**9, "edge") is True

    def test_google_prices_by_family(self):
        assert tts.cost.estimate_cost_usd(1000, "google", "chirp") == pytest.approx(0.030)
        assert tts.cost.estimate_cost_usd(1000, "google", "classic") == pytest.approx(0.004)
        assert tts.cost.estimate_cost_usd(1000, "edge") == 0.0

    def test_elevenlabs_default_price_is_the_real_turbo_rate(self):
        """The placeholder was $0.30/1K — 3.5x the Pro-plan turbo rate."""
        assert tts.cost.estimate_cost_usd(1000, "elevenlabs") == pytest.approx(0.085)

    def test_legacy_spend_file_shape_is_read(self, tmp_path, monkeypatch):
        f = tmp_path / "spend.json"
        f.write_text(json.dumps({"elevenlabs_chars": 400, "updated_at": 1}))
        monkeypatch.setattr(tts.cost, "_SPEND_FILE", f)
        monkeypatch.setitem(tts.cost._CHAR_CAP, "elevenlabs", 500)
        assert tts.cost.within_cap(100, "elevenlabs") is True
        assert tts.cost.within_cap(101, "elevenlabs") is False


# ── composer: pre-flight and dialogue through the gate ───────────────────────

import agent6_animation.video_composer as vc  # noqa: E402
from agent6_animation.video_composer import compose_episode_videos  # noqa: E402


def _composer_stub(monkeypatch, tmp_path):
    def fake_render(spec, audio, out, ffmpeg, **kw):
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(b"mp4")
        return True
    monkeypatch.setattr(vc, "render_native_segment", fake_render)
    monkeypatch.setattr(vc, "_audio_duration", lambda p, f: 2.0)
    monkeypatch.setattr(vc, "_ffmpeg_exe", lambda: "ffmpeg")
    monkeypatch.setattr(vc, "concepts_for_slides", lambda hs: ["c"] * len(hs))
    monkeypatch.setattr(vc, "VIDEO_DIR", tmp_path)
    monkeypatch.setattr(vc, "_MAX_RENDER_WORKERS", 1)


def _inputs(n=2, dialogue=False):
    segs = []
    for i in range(n):
        s = {"segment_id": f"s{i}", "text": f"Narration {i}.", "slide_heading": f"H{i}",
             "slide_points": ["p"], "estimated_duration_seconds": 5}
        if dialogue:
            s["dialogue"] = [{"who": "teacher", "line": f"Teacher line {i}."},
                             {"who": "student", "line": "A question?"}]
        segs.append(s)
    script = {"episodes": [{"book_id": "bk", "chapter_num": 1, "episode_num": 1,
                            "episode_title": "Ep", "segments": segs}]}
    return script, {"segments": [{"segment_id": f"s{i}"} for i in range(n)]}


class TestPreflight:
    def test_a_failing_premium_provider_pins_the_whole_lesson_free(self, monkeypatch, tmp_path):
        """Segments used to fall back one by one, so a mid-lesson outage
        produced an Arabic teacher giving way to English Aria on segment 6."""
        _google_on(monkeypatch)
        _composer_stub(monkeypatch, tmp_path)
        calls = []

        def fake_synth(text, out, *, voice_id=None, allow_premium=False, ssml_text=None,
                       report=None, boundaries_out=None, lang=None, **kw):
            calls.append({"voice": voice_id, "allow": allow_premium})
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(b"mp3")
            if report is not None:
                # premium is refused every time (provider down)
                report.update({"used": "edge-zariyah", "downgraded": bool(allow_premium)})
            return Path(out)

        monkeypatch.setattr(vc, "synthesize", fake_synth)
        script, slides = _inputs(3)
        rep: dict = {}
        compose_episode_videos(script, slides, tts_voice="g-ar-f", allow_premium=True,
                               voice_report=rep, lang="ar")
        assert calls[0]["allow"] is True, "the pre-flight probe asks for premium"
        assert all(c["allow"] is False for c in calls[1:]), \
            "after a failed pre-flight every segment is rendered free, consistently"
        assert rep["preflight_downgrade"] is True and rep["downgraded"] is True
        assert rep["used"] == ["edge-zariyah"], "one voice, not a mix"

    def test_a_healthy_premium_provider_is_not_pinned(self, monkeypatch, tmp_path):
        _google_on(monkeypatch)
        _composer_stub(monkeypatch, tmp_path)
        calls = []

        def fake_synth(text, out, *, voice_id=None, allow_premium=False, report=None, **kw):
            calls.append(allow_premium)
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(b"mp3")
            if report is not None:
                report.update({"used": voice_id, "provider": "google", "downgraded": False,
                               "chars": 10, "stats": {"requests": 1, "chars": 10}})
            return Path(out)

        monkeypatch.setattr(vc, "synthesize", fake_synth)
        script, slides = _inputs(2)
        rep: dict = {}
        compose_episode_videos(script, slides, tts_voice="g-en-f", allow_premium=True,
                               voice_report=rep, lang="en")
        assert all(calls), "premium stays on when the probe succeeds"
        assert rep["used"] == ["g-en-f"] and rep["preflight_downgrade"] is False
        assert rep["stats"]["requests"] == 2 and rep["stats"]["chars"] == 20, \
            "per-segment stats are summed (the probe is not counted)"

    def test_a_free_voice_needs_no_probe(self, monkeypatch, tmp_path):
        _composer_stub(monkeypatch, tmp_path)
        calls = []

        def fake_synth(text, out, *, report=None, **kw):
            calls.append(text)
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(b"mp3")
            if report is not None:
                report.update({"used": "edge-aria", "downgraded": False})
            return Path(out)

        monkeypatch.setattr(vc, "synthesize", fake_synth)
        script, slides = _inputs(2)
        compose_episode_videos(script, slides, tts_voice="edge-aria", allow_premium=False, lang="en")
        assert len(calls) == 2 and "Let us begin." not in calls


class TestDialogueThroughTheGate:
    def test_teacher_lines_use_synthesize_and_students_stay_on_edge(self, monkeypatch, tmp_path):
        _google_on(monkeypatch)
        _composer_stub(monkeypatch, tmp_path)
        monkeypatch.setenv("VIDEO_ENGINE", "scene")
        teacher_calls, student_calls = [], []

        def fake_synth(text, out, *, voice_id=None, allow_premium=False, report=None, **kw):
            teacher_calls.append((text, voice_id, allow_premium))
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(b"mp3")
            if report is not None:
                report.update({"used": voice_id, "downgraded": False, "chars": 5, "stats": {"chars": 5}})
            return Path(out)

        import shared.tts.providers.edge as edge

        def fake_edge(line, out, ref, **kw):
            student_calls.append((line, ref))
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(b"mp3")

        monkeypatch.setattr(vc, "synthesize", fake_synth)
        monkeypatch.setattr(edge, "synthesize", fake_edge)
        monkeypatch.setattr(vc.subprocess, "run", lambda *a, **k: (Path(a[0][-1]).write_bytes(b"mp3") or None))
        script, slides = _inputs(1, dialogue=True)
        rep: dict = {}
        compose_episode_videos(script, slides, tts_voice="g-en-f", allow_premium=True,
                               voice_report=rep, lang="en")
        teacher_lines = [c for c in teacher_calls if c[0] != "Let us begin."]
        assert teacher_lines and all(c[1] == "g-en-f" and c[2] for c in teacher_lines)
        assert student_calls and all("Neural" in c[1] for c in student_calls), \
            "student lines stay on the free age-matched Edge voice"
        assert rep["used"] == ["g-en-f"], "the segment is recorded under the teacher's voice, not 'dialogue-edge'"
