"""Fixes from the adversarial review of Phase 1b (2026-09-03/04). Each class
names the finding it pins so a regression reads as the sentence it breaks."""

from __future__ import annotations

import base64
import json
import threading
from pathlib import Path

import pytest

from shared import tts
from shared.tts import chunks as C
from shared.tts import registry as R
from shared.tts.providers import google as G


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for k in ("TTS_PREMIUM_PROVIDER", "ELEVENLABS_ENABLED", "ELEVENLABS_API_KEY",
              "GOOGLE_TTS_ENABLED", "GOOGLE_APPLICATION_CREDENTIALS",
              "GOOGLE_APPLICATION_CREDENTIALS_JSON", "VERTEX_PROJECT_ID",
              "TTS_PREMIUM_CANARY_OWNERS", "TTS_PREMIUM_CANARY_PROVIDER",
              "TTS_MONTHLY_CHAR_CAP_PER_USER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(tts.cost, "_SPEND_FILE", tmp_path / "spend.json")
    monkeypatch.setattr(G, "_skip_user_project", False)


def _google_on(monkeypatch):
    monkeypatch.setenv("GOOGLE_TTS_ENABLED", "1")
    monkeypatch.setenv("VERTEX_PROJECT_ID", "sketchcast")


# ── gender-aware fallback ────────────────────────────────────────────────────

class TestGenderAwareFallback:
    """A pre-flight downgrade pinned the whole lesson on the FEMALE free voice
    after the teacher avatar had been cast from a MALE premium pick."""

    def test_free_default_can_follow_a_gender(self):
        assert R.default_voice_id_for("ar") == "edge-zariyah"
        assert R.default_voice_id_for("ar", gender="m") == "edge-hamed"
        assert R.default_voice_id_for("ar", gender="f") == "edge-zariyah"
        assert R.default_voice_id_for("en", gender="m") == "edge-guy"
        assert R.default_voice_id_for("zh", gender="m") == "edge-aria", "no entry → the global default"

    def test_a_gate_downgrade_keeps_the_gender(self):
        assert tts.resolve_voice("g-ar-m", False, lang="ar").voice_id == "edge-hamed"
        assert tts.resolve_voice("g-ar-f", False, lang="ar").voice_id == "edge-zariyah"
        assert tts.resolve_voice("el-adam", False, lang="ms-arab").voice_id == "edge-osman"

    def test_a_provider_failure_keeps_the_gender_too(self, monkeypatch, tmp_path):
        _google_on(monkeypatch)
        import shared.tts.providers.edge as edge
        seen = {}

        def boom(*a, **k):
            raise RuntimeError("google down")

        def fake_edge(say, out, ref, boundaries_out=None, **kw):
            seen["ref"] = ref
            Path(out).write_bytes(b"mp3")

        monkeypatch.setattr(G, "synthesize", boom)
        monkeypatch.setattr(edge, "synthesize", fake_edge)
        r: dict = {}
        tts.synthesize("x", tmp_path / "a.mp3", voice_id="g-hi-m", allow_premium=True, report=r, lang="hi")
        assert r["used"] == "edge-madhur" and r["reason"] == "provider_error"
        assert "Madhur" in seen["ref"]


# ── the reason a premium request rendered free ───────────────────────────────

class TestDowngradeReason:
    def test_gate_cap_and_outage_are_told_apart(self, monkeypatch, tmp_path):
        _google_on(monkeypatch)
        import shared.tts.providers.edge as edge
        monkeypatch.setattr(edge, "synthesize", lambda say, out, ref, boundaries_out=None, **k: Path(out).write_bytes(b"m"))
        r: dict = {}
        tts.synthesize("x", tmp_path / "g.mp3", voice_id="g-en-f", allow_premium=False, report=r, lang="en")
        assert r["downgraded"] and r["reason"] == "gate"

        monkeypatch.setitem(tts.cost._CHAR_CAP, "google", 1)
        r = {}
        tts.synthesize("a long sentence", tmp_path / "c.mp3", voice_id="g-en-f", allow_premium=True, report=r, lang="en")
        assert r["downgraded"] and r["reason"] == "cap"

        monkeypatch.setitem(tts.cost._CHAR_CAP, "google", 10**9)
        monkeypatch.setattr(G, "synthesize", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("503")))
        r = {}
        tts.synthesize("x", tmp_path / "o.mp3", voice_id="g-en-f", allow_premium=True, report=r, lang="en")
        assert r["downgraded"] and r["reason"] == "provider_error"

    def test_a_premium_render_has_no_reason(self, monkeypatch, tmp_path):
        _google_on(monkeypatch)
        monkeypatch.setattr(G, "synthesize", lambda text, out, ref, boundaries_out=None: (Path(out).write_bytes(b"m"), {"chars": 5, "family": "chirp"})[1])
        r: dict = {}
        tts.synthesize("hello", tmp_path / "p.mp3", voice_id="g-en-f", allow_premium=True, report=r, lang="en")
        assert r["downgraded"] is False and r["reason"] is None

    def test_provider_disabled_is_its_own_reason(self, monkeypatch, tmp_path):
        """Paid tier, nothing enabled: the gate passed, the worker could not."""
        import shared.tts.providers.edge as edge
        monkeypatch.setattr(edge, "synthesize", lambda say, out, ref, boundaries_out=None, **k: Path(out).write_bytes(b"m"))
        r: dict = {}
        tts.synthesize("x", tmp_path / "d.mp3", voice_id="g-en-f", allow_premium=True, report=r, lang="en")
        assert r["used"] == "edge-aria" and r["reason"] == "provider_disabled"


# ── the runaway cap: reserve, settle, release ────────────────────────────────

class TestReserve:
    def test_reserve_is_atomic_across_threads(self, monkeypatch):
        monkeypatch.setitem(tts.cost._CHAR_CAP, "google", 1000)
        granted = []
        barrier = threading.Barrier(8)

        def go():
            barrier.wait()
            granted.append(tts.cost.reserve(300, "google"))

        ts = [threading.Thread(target=go) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert granted.count(True) == 3, "1000 / 300 → exactly three fit; the check-then-add race let all eight through"

    def test_settle_replaces_the_reservation_with_the_bill(self, monkeypatch):
        monkeypatch.setitem(tts.cost._CHAR_CAP, "google", 1000)
        assert tts.cost.reserve(400, "google")
        tts.cost.settle(400, 250, "google", "chirp")
        assert tts.cost.within_cap(750, "google") is True
        assert tts.cost.within_cap(751, "google") is False

    def test_release_gives_it_back_and_free_is_never_booked(self, monkeypatch):
        monkeypatch.setitem(tts.cost._CHAR_CAP, "google", 100)
        assert tts.cost.reserve(100, "google")
        assert tts.cost.reserve(1, "google") is False
        tts.cost.release(100, "google")
        assert tts.cost.reserve(1, "google") is True
        assert tts.cost.reserve(10**9, "edge") is True


# ── chunks: breaks, scripts, oversize sentences ─────────────────────────────

class TestChunkFixes:
    def test_break_silence_is_not_credited_to_words(self):
        ws = C.interpolate_words('<break time="1s"/> Cells group into tissues.', 10.0, 3.0)
        assert ws[0]["w"] == "Cells" and ws[0]["t"] == 11.0, "the first word starts AFTER the opening pause"
        assert ws[-1]["t"] < 13.0

    def test_an_interior_break_shifts_the_words_after_it(self):
        with_break = C.interpolate_words('One two <break time="500ms"/> three four.', 0.0, 3.0)
        without = C.interpolate_words("One two three four.", 0.0, 2.5)
        assert [w["w"] for w in with_break] == ["One", "two", "three", "four"]
        t_with = {w["w"]: w["t"] for w in with_break}
        t_without = {w["w"]: w["t"] for w in without}
        assert t_with["three"] == pytest.approx(t_without["three"] + 0.5, abs=1e-3)
        assert all(b["t"] >= a["t"] for a, b in zip(with_break, with_break[1:]))

    def test_break_seconds(self):
        assert C.break_seconds('<break time="300ms"/>') == pytest.approx(0.3)
        assert C.break_seconds('<break time="1.5s"/>') == pytest.approx(1.5)
        assert C.break_seconds('<break strength="weak"/>') == pytest.approx(0.25)
        assert C.break_seconds("<break/>") == pytest.approx(0.5)

    def test_devanagari_and_arabic_words_keep_their_vowel_signs(self):
        assert C.words_of("यह कोशिका है।") == ["यह", "कोशिका", "है"]
        assert C.words_of("కణం అనేది జీవ ప్రమాణం.") == ["కణం", "అనేది", "జీవ", "ప్రమాణం"]
        assert C.words_of("ٱلْخَلِيَّةُ هِيَ ٱلْوَحْدَةُ؟") == ["ٱلْخَلِيَّةُ", "هِيَ", "ٱلْوَحْدَةُ"]
        assert C.words_of("it's 'quoted' — fine.") == ["it's", "quoted", "fine"]

    def test_a_break_between_sentences_opens_the_next_and_a_trailing_one_joins_the_last(self):
        # between sentences the pause belongs to the sentence it precedes — the
        # clip then starts with the silence and interpolate_words skips it
        s = C.sentences('Name it. <break time="0.3s"/> Then stop.')
        assert s == ["Name it.", '<break time="0.3s"/> Then stop.']
        # at the very end there is no next sentence: it must not become a
        # word-less request of its own
        assert C.chunks('Done. <break time="0.3s"/>', one_sentence_each=True, marks=False) == ['Done. <break time="0.3s"/>']
        assert C.chunks('<break time="0.3s"/>', one_sentence_each=True, marks=False) == ['<break time="0.3s"/>']

    def test_an_oversize_chirp_sentence_is_cut_not_sent_whole(self, monkeypatch):
        monkeypatch.setattr(C, "MAX_REQUEST_BYTES", 120)
        long = "यह एक बहुत लंबा वाक्य है, जिसमें कई खंड हैं, और यह सीमा से बड़ा है, इसलिए इसे काटना होगा।"
        pieces = C.chunks(long, one_sentence_each=True, marks=False)
        assert len(pieces) > 1
        for p in pieces:
            assert len(C.ssml_for(p, marks=False)[0].encode("utf-8")) <= 120
        assert " ".join(C.words_of(" ".join(pieces))) == " ".join(C.words_of(long)), "no word lost"

    def test_a_sentence_with_no_clause_breaks_is_cut_between_words(self, monkeypatch):
        monkeypatch.setattr(C, "MAX_REQUEST_BYTES", 60)
        long = " ".join(["word"] * 40) + "."
        pieces = C.chunks(long, one_sentence_each=False, marks=False)
        assert len(pieces) > 1 and all(len(C.ssml_for(p, marks=False)[0].encode()) <= 60 for p in pieces)

    def test_tail_fill_never_runs_backwards(self):
        tps = [{"markName": "w0", "timeSeconds": 0.0}, {"markName": "w1", "timeSeconds": 2.0}]
        ws, _ = C.words_from_marks("a b c d", tps, chunk_start=0.0, chunk_duration=0.0)
        ts = [w["t"] for w in ws]
        assert ts == sorted(ts) and ts[2] >= 2.0


# ── the provider: 4xx, Retry-After, the billing header, the limiter ─────────

class _Resp:
    def __init__(self, code, body=None, headers=None):
        self.status_code = code
        self._body = body if body is not None else {"audioContent": base64.b64encode(b"ok").decode()}
        self.headers = headers or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


def _wire(monkeypatch, responses):
    import requests
    sent = []

    def fake_post(url, headers=None, json=None, timeout=None):
        sent.append(headers)
        return responses.pop(0)

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(G, "_token", lambda: ("tok", "proj"))
    slept = []
    monkeypatch.setattr(G.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(G, "_limiter", G._RateLimiter(10**6))
    return sent, slept


class TestPost:
    def test_a_400_raises_at_once_with_googles_reason(self, monkeypatch):
        sent, slept = _wire(monkeypatch, [_Resp(400, {"error": {"status": "INVALID_ARGUMENT", "message": "Voice does not exist"}})])
        with pytest.raises(G.GoogleTTSError) as ei:
            G._post({"input": {"ssml": "<speak>x</speak>"}})
        assert len(sent) == 1 and slept == []
        assert "INVALID_ARGUMENT" in str(ei.value) and "Voice does not exist" in str(ei.value)
        assert ei.value.retryable is False

    def test_a_429_honours_retry_after(self, monkeypatch):
        sent, slept = _wire(monkeypatch, [_Resp(429, {"error": {"status": "RESOURCE_EXHAUSTED", "message": "q"}}, {"Retry-After": "7"}), _Resp(200)])
        out = G._post({"input": {"ssml": "<speak>x</speak>"}})
        assert "audioContent" in out and len(sent) == 2 and slept == [7.0]

    def test_a_500_is_retried_then_given_up_with_detail(self, monkeypatch):
        sent, slept = _wire(monkeypatch, [_Resp(503, {"error": {"status": "UNAVAILABLE", "message": "try later"}}) for _ in range(4)])
        with pytest.raises(G.GoogleTTSError) as ei:
            G._post({"input": {"ssml": "<speak>x</speak>"}})
        assert len(sent) == 4 and len(slept) == 3 and "UNAVAILABLE" in str(ei.value)

    def test_a_403_on_the_billing_header_retries_without_it_and_remembers(self, monkeypatch):
        sent, slept = _wire(monkeypatch, [
            _Resp(403, {"error": {"status": "PERMISSION_DENIED",
                                  "message": "Caller does not have required permission to use project proj. Grant the caller the roles/serviceusage.serviceUsageConsumer role"}}),
            _Resp(200), _Resp(200)])
        G._post({"input": {"ssml": "<speak>x</speak>"}})
        assert "x-goog-user-project" in sent[0] and "x-goog-user-project" not in sent[1]
        G._post({"input": {"ssml": "<speak>y</speak>"}})
        assert "x-goog-user-project" not in sent[2], "remembered for the process"

    def test_the_limiter_paces_a_burst(self):
        clock = [0.0]
        sleeps = []

        def sleep(s):
            sleeps.append(s)
            clock[0] += s

        lim = G._RateLimiter(3, clock=lambda: clock[0], sleep=sleep)
        for _ in range(3):
            assert lim.acquire() == 0.0
        waited = lim.acquire()
        assert waited > 59.0 and len(sleeps) == 1, "the fourth request in a minute waits for the window"


# ── the provider: partial failure, concat quoting, zero durations ───────────

def _stub_ffmpeg(monkeypatch, tmp_path, durations=None):
    monkeypatch.setattr(G, "_ffmpeg", lambda: "ffmpeg")
    durs = list(durations or [])
    monkeypatch.setattr(G, "_duration", lambda p, f: durs.pop(0) if durs else 1.0)
    monkeypatch.setattr(G, "_concat", lambda parts, out, f: out.write_bytes(b"".join(p.read_bytes() for p in parts)))
    monkeypatch.setattr(G, "_limiter", G._RateLimiter(10**6))


class TestProviderFixes:
    def test_chunks_already_billed_are_recorded_when_a_later_one_fails(self, monkeypatch, tmp_path):
        _stub_ffmpeg(monkeypatch, tmp_path)
        monkeypatch.setattr(G, "_PARALLEL", 1)
        recorded = []
        monkeypatch.setattr(tts.cost, "record", lambda n, p, f=None: recorded.append((n, p, f)))

        def post(body):
            if "Second" in body["input"]["ssml"]:
                raise G.GoogleTTSError(400, "INVALID_ARGUMENT", False)
            return {"audioContent": base64.b64encode(b"a").decode(), "timepoints": []}

        monkeypatch.setattr(G, "_post", post)
        with pytest.raises(G.GoogleTTSError):
            G.synthesize("First one. Second two. Third three.", tmp_path / "x.mp3", "en-US-Chirp3-HD-Achernar")
        assert recorded and recorded[0][1] == "google" and recorded[0][2] == "chirp"
        first, third = len("<speak>First one.</speak>"), len("<speak>Third three.</speak>")
        # the failed chunk is never billed; the third may or may not have started
        # before the cancellation landed — if it did, Google billed it and so do we
        assert recorded[0][0] in (first, first + third)

    def test_a_zero_duration_probe_is_estimated_not_collapsed(self, monkeypatch, tmp_path):
        _stub_ffmpeg(monkeypatch, tmp_path, durations=[0.0, 2.0])
        monkeypatch.setattr(G, "_post", lambda body: {"audioContent": base64.b64encode(b"a").decode(), "timepoints": []})
        stats = G.synthesize("One two three four five six. Seven eight.", tmp_path / "z.mp3",
                             "en-US-Chirp3-HD-Achernar", boundaries_out=tmp_path / "z.words.json")
        words = json.loads((tmp_path / "z.words.json").read_text(encoding="utf-8"))
        seven = next(w for w in words if w["w"] == "Seven")
        assert seven["t"] > 1.0, "the second sentence starts after the ESTIMATED length of the first"
        assert stats["duration_estimated"] == 1

    def test_concat_list_escapes_an_apostrophe(self, monkeypatch, tmp_path):
        d = tmp_path / "O'Brien's book"
        d.mkdir()
        a, b = d / "a.mp3", d / "b.mp3"
        a.write_bytes(b"1")
        b.write_bytes(b"2")
        seen = {}

        def run(cmd, capture_output=True):
            lst = Path(cmd[cmd.index("-i") + 1])
            seen["list"] = lst.read_text(encoding="utf-8")
            Path(cmd[-1]).write_bytes(b"12")

        monkeypatch.setattr(G.subprocess, "run", run)
        G._concat([a, b], d / "out.mp3", "ffmpeg")
        assert "O'\\''Brien'\\''s" in seen["list"], seen["list"]


# ── the composer: stats, parts, dialogue, the probe file ────────────────────

import agent6_animation.video_composer as vc  # noqa: E402
from agent6_animation.video_composer import _fold_stats, compose_episode_videos  # noqa: E402


def _composer_stub(monkeypatch, tmp_path):
    def fake_render(spec, audio, out, ffmpeg, **kw):
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(b"mp4")
        return True
    monkeypatch.setattr(vc, "render_native_segment", fake_render)
    # the scene engine path (VIDEO_ENGINE=scene) must not render real frames here
    monkeypatch.setattr(vc, "_render_scene_segment",
                        lambda script_seg, narration, audio_path, audio_secs, out_mp4, direction,
                        scene_dict=None, avatars=None: (Path(out_mp4).write_bytes(b"mp4"), True)[1])
    monkeypatch.setattr("spike.scene_engine.whiteboard.build_whiteboard_scene",
                        lambda seg, avatars=None: {"stub": True})
    monkeypatch.setattr(vc, "_audio_duration", lambda p, f: 2.0)
    monkeypatch.setattr(vc, "_ffmpeg_exe", lambda: "ffmpeg")
    monkeypatch.setattr(vc, "concepts_for_slides", lambda hs: ["c"] * len(hs))
    monkeypatch.setattr(vc, "VIDEO_DIR", tmp_path)
    monkeypatch.setattr(vc, "_MAX_RENDER_WORKERS", 1)


def _inputs(n=2, dialogue=False, part=1):
    segs = []
    for i in range(n):
        s = {"segment_id": f"p{part}s{i}", "text": f"Narration {i}.", "slide_heading": f"H{i}",
             "slide_points": ["p"], "estimated_duration_seconds": 5}
        if dialogue:
            s["dialogue"] = [{"who": "teacher", "line": f"Teacher line {i}."},
                             {"who": "student", "line": "A question?"}]
        segs.append(s)
    script = {"episodes": [{"book_id": "bk", "chapter_num": 1, "episode_num": part,
                            "episode_title": "Ep", "segments": segs}]}
    return script, {"segments": [{"segment_id": f"p{part}s{i}"} for i in range(n)]}


class TestFoldStats:
    def test_paid_chars_once_free_chars_apart_and_no_bools(self):
        into: dict = {}
        _fold_stats(into, {"provider": "google", "chars": 40, "downgraded": False,
                           "stats": {"chars": 40, "requests": 2, "marks_dropped": 0}})
        _fold_stats(into, {"provider": "edge", "chars": 25, "downgraded": True, "stats": {}})
        assert into == {"requests": 2, "marks_dropped": 0, "chars": 40, "free_chars": 25}
        assert "downgraded" not in into


class TestComposerFixes:
    def test_dialogue_chars_are_counted_once_and_a_gate_downgrade_is_visible(self, monkeypatch, tmp_path):
        _google_on(monkeypatch)
        _composer_stub(monkeypatch, tmp_path)
        monkeypatch.setenv("VIDEO_ENGINE", "scene")
        asked = []

        def fake_synth(text, out, *, voice_id=None, allow_premium=False, report=None, **kw):
            asked.append(voice_id)
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(b"mp3")
            if report is not None:
                # the gate refuses: rendered free, and says so
                report.update({"used": "edge-aria", "provider": "edge", "downgraded": True,
                               "reason": "gate", "chars": 7, "stats": {}})
            return Path(out)

        import shared.tts.providers.edge as edge
        monkeypatch.setattr(vc, "synthesize", fake_synth)
        monkeypatch.setattr(edge, "synthesize", lambda line, out, ref, **k: (Path(out).parent.mkdir(parents=True, exist_ok=True), Path(out).write_bytes(b"m")))
        monkeypatch.setattr(vc.subprocess, "run", lambda *a, **k: (Path(a[0][-1]).write_bytes(b"mp3") or None))
        script, slides = _inputs(1, dialogue=True)
        rep: dict = {}
        compose_episode_videos(script, slides, tts_voice="g-en-f", allow_premium=False,
                               voice_report=rep, lang="en")
        assert asked and all(v == "g-en-f" for v in asked), "the REQUESTED id reaches synthesize, so the gate can report"
        assert rep["downgraded"] is True and "gate" in rep["reasons"]
        assert rep["stats"].get("chars", 0) == 0 and rep["stats"]["free_chars"] == 7
        assert "downgraded" not in rep["stats"]

    def test_dialogue_on_a_premium_provider_bills_each_line_once(self, monkeypatch, tmp_path):
        _google_on(monkeypatch)
        _composer_stub(monkeypatch, tmp_path)
        monkeypatch.setenv("VIDEO_ENGINE", "scene")

        def fake_synth(text, out, *, voice_id=None, allow_premium=False, report=None, **kw):
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(b"mp3")
            if report is not None:
                n = 11 if text == "Let us begin." else len(text)
                report.update({"used": "g-en-f", "provider": "google", "downgraded": False,
                               "reason": None, "chars": n, "stats": {"chars": n, "requests": 1, "family": "chirp"}})
            return Path(out)

        import shared.tts.providers.edge as edge
        monkeypatch.setattr(vc, "synthesize", fake_synth)
        monkeypatch.setattr(edge, "synthesize", lambda line, out, ref, **k: (Path(out).parent.mkdir(parents=True, exist_ok=True), Path(out).write_bytes(b"m")))
        monkeypatch.setattr(vc.subprocess, "run", lambda *a, **k: (Path(a[0][-1]).write_bytes(b"mp3") or None))
        script, slides = _inputs(2, dialogue=True)
        rep: dict = {}
        compose_episode_videos(script, slides, tts_voice="g-en-f", allow_premium=True,
                               voice_report=rep, lang="en")
        assert rep["stats"]["chars"] == len("Teacher line 0.") + len("Teacher line 1."), "once per line, not twice"
        assert rep["stats"]["requests"] == 2

    def test_multi_part_chapters_accumulate_into_one_report(self, monkeypatch, tmp_path):
        _google_on(monkeypatch)
        _composer_stub(monkeypatch, tmp_path)

        def fake_synth(text, out, *, voice_id=None, report=None, **kw):
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(b"mp3")
            if report is not None:
                report.update({"used": voice_id, "provider": "google", "downgraded": False,
                               "reason": None, "chars": 10, "stats": {"requests": 1}})
            return Path(out)

        monkeypatch.setattr(vc, "synthesize", fake_synth)
        rep: dict = {}
        for part in (1, 2, 3):
            script, slides = _inputs(2, part=part)
            compose_episode_videos(script, slides, tts_voice="g-en-f", allow_premium=True,
                                   voice_report=rep, lang="en")
        assert rep["parts"] == 3
        assert rep["stats"]["chars"] == 6 * 10, "three parts × two segments; replacing kept only the last part"
        assert rep["stats"]["requests"] == 6 and rep["used"] == ["g-en-f"]

    def test_edge_fallback_segments_are_not_billed_as_premium(self, monkeypatch, tmp_path):
        _google_on(monkeypatch)
        _composer_stub(monkeypatch, tmp_path)
        n = {"i": 0}

        def fake_synth(text, out, *, voice_id=None, report=None, **kw):
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(b"mp3")
            if report is not None:
                if text == "Let us begin.":
                    report.update({"used": "g-en-f", "provider": "google", "downgraded": False, "reason": None, "chars": 11, "stats": {}})
                    return Path(out)
                n["i"] += 1
                if n["i"] == 1:
                    report.update({"used": "g-en-f", "provider": "google", "downgraded": False, "reason": None, "chars": 30, "stats": {}})
                else:  # a mid-lesson outage on the second segment
                    report.update({"used": "edge-aria", "provider": "edge", "downgraded": True, "reason": "provider_error", "chars": 30, "stats": {}})
            return Path(out)

        monkeypatch.setattr(vc, "synthesize", fake_synth)
        script, slides = _inputs(2)
        rep: dict = {}
        compose_episode_videos(script, slides, tts_voice="g-en-f", allow_premium=True, voice_report=rep, lang="en")
        assert rep["stats"]["chars"] == 30 and rep["stats"]["free_chars"] == 30
        assert rep["used"] == ["edge-aria", "g-en-f"] and "provider_error" in rep["reasons"]

    def test_the_preflight_probe_leaves_no_file_behind(self, monkeypatch, tmp_path):
        _google_on(monkeypatch)
        _composer_stub(monkeypatch, tmp_path)

        def fake_synth(text, out, *, voice_id=None, report=None, **kw):
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(b"mp3")
            if report is not None:
                report.update({"used": "g-en-f", "provider": "google", "downgraded": False, "reason": None, "chars": 5, "stats": {}})
            return Path(out)

        monkeypatch.setattr(vc, "synthesize", fake_synth)
        script, slides = _inputs(1)
        compose_episode_videos(script, slides, tts_voice="g-en-f", allow_premium=True, lang="en")
        assert not list(tmp_path.rglob("_preflight.mp3"))

    def test_a_paraphrased_markup_copy_is_not_spoken(self, monkeypatch, tmp_path):
        _composer_stub(monkeypatch, tmp_path)
        spoken = {}

        def fake_synth(text, out, *, ssml_text=None, report=None, **kw):
            spoken["ssml"] = ssml_text
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(b"mp3")
            if report is not None:
                report.update({"used": "edge-aria", "provider": "edge", "downgraded": False, "reason": None, "chars": 1, "stats": {}})
            return Path(out)

        monkeypatch.setattr(vc, "synthesize", fake_synth)
        script, slides = _inputs(1)
        seg = script["episodes"][0]["segments"][0]
        seg["text"] = "Cells group into tissues."
        seg["elevenlabs_text"] = 'Cells <break time="0.3s"/> group into tissues.'
        compose_episode_videos(script, slides, tts_voice="edge-aria", allow_premium=False, lang="en")
        assert "<break" in spoken["ssml"], "same words: the markup copy (with its pause) is spoken"
        seg["elevenlabs_text"] = "Cells <break time=\"0.3s\"/> form organs."   # paraphrased
        compose_episode_videos(script, slides, tts_voice="edge-aria", allow_premium=False, lang="en")
        assert "<break" not in spoken["ssml"] and "tissues" in spoken["ssml"], "different words: the caption copy is spoken"


# ── the ledger and the canary in the worker ─────────────────────────────────

class _Q:
    def __init__(self, sb, table):
        self.sb, self.table = sb, table
        self.filters = {}

    def select(self, *_):
        return self

    def eq(self, k, v):
        self.filters[k] = v
        return self

    def limit(self, *_):
        return self

    def execute(self):
        row = self.sb.rows.get((self.filters.get("user_id"), self.filters.get("period"), self.filters.get("provider")))
        return type("R", (), {"data": [{"chars": row}] if row is not None else []})()


class _SB:
    def __init__(self, fail_rpc=False):
        self.rows: dict = {}
        self.calls: list = []
        self.fail_rpc = fail_rpc

    def rpc(self, name, args):
        self.calls.append((name, args))
        if self.fail_rpc:
            raise RuntimeError("rpc down")
        key = (args["p_user"], args["p_period"], args["p_provider"])
        self.rows[key] = self.rows.get(key, 0) + args["p_chars"]
        return type("R", (), {"execute": lambda s: type("D", (), {"data": True})()})()

    def table(self, name):
        return _Q(self, name)


class TestLedger:
    def test_lessons_book_under_their_own_key_and_never_refuse(self, monkeypatch):
        from worker.process import _record_tts_ledger
        sb = _SB()
        over, key = _record_tts_ledger(sb, "u1", {"used": ["g-en-f", "edge-aria"], "stats": {"chars": 500, "free_chars": 80}}, "gen")
        assert key == "lesson:google" and over is False
        name, args = sb.calls[0]
        assert name == "tutor_tts_reserve" and args["p_provider"] == "lesson:google"
        assert args["p_chars"] == 500, "paid chars only — the Edge fallback's 80 stay out"
        assert args["p_cap"] == 2_147_483_647, "a reservation with the real cap DROPPED over-cap lessons"

    def test_over_allowance_is_reported_from_the_row_not_from_a_refusal(self, monkeypatch):
        from worker.process import _record_tts_ledger
        monkeypatch.setenv("TTS_MONTHLY_CHAR_CAP_PER_USER", "1000")
        sb = _SB()
        assert _record_tts_ledger(sb, "u1", {"used": ["g-en-f"], "stats": {"chars": 600}}, "g1") == (False, "lesson:google")
        over, _ = _record_tts_ledger(sb, "u1", {"used": ["g-en-f"], "stats": {"chars": 600}}, "g2")
        assert over is True
        assert sum(sb.rows.values()) == 1200, "both lessons are in the ledger"

    def test_a_ledger_failure_never_reaches_the_lesson(self):
        from worker.process import _record_tts_ledger
        assert _record_tts_ledger(_SB(fail_rpc=True), "u1", {"used": ["g-en-f"], "stats": {"chars": 5}}, "g") == (False, "lesson:google")

    def test_free_lessons_write_nothing(self):
        from worker.process import _record_tts_ledger
        sb = _SB()
        assert _record_tts_ledger(sb, "u1", {"used": ["edge-aria"], "stats": {"free_chars": 900}}, "g") == (False, None)
        assert sb.calls == []


class TestCanary:
    def test_not_a_canary_without_the_variable_or_when_not_listed(self, monkeypatch):
        assert tts.canary_provider_for("owner-a") is None
        monkeypatch.setenv("TTS_PREMIUM_CANARY_OWNERS", "owner-b, owner-c")
        assert tts.canary_provider_for("owner-a") is None
        assert tts.canary_provider_for(None) is None

    def test_a_listed_owner_gets_google_by_default_or_the_named_family(self, monkeypatch):
        monkeypatch.setenv("TTS_PREMIUM_CANARY_OWNERS", "owner-b, owner-c")
        assert tts.canary_provider_for("owner-b") == "google"
        monkeypatch.setenv("TTS_PREMIUM_CANARY_PROVIDER", "elevenlabs")
        assert tts.canary_provider_for("owner-c") == "elevenlabs"
        monkeypatch.setenv("TTS_PREMIUM_CANARY_PROVIDER", "openai")
        assert tts.canary_provider_for("owner-b") == "google"

    def test_the_canary_provider_overrides_legacy_for_one_pick_only(self):
        assert R.premium_provider() == "legacy"
        assert R.default_premium_voice_id_for("ar") is None
        assert R.default_premium_voice_id_for("ar", provider="google") == "g-ar-f"
        on = frozenset({"edge", "google"})
        assert tts.pick_voice_id("auto", lang="ar", allow_premium=True, enabled=on) == "edge-zariyah"
        assert tts.pick_voice_id("auto", lang="ar", allow_premium=True, enabled=on, provider="google") == "g-ar-f"
        assert tts.pick_voice_id("auto", lang="ar", allow_premium=False, enabled=on, provider="google") == "edge-zariyah"
        assert tts.pick_voice_id("auto", lang="ar", allow_premium=True, enabled=frozenset({"edge"}), provider="google") == "edge-zariyah"
        assert tts.pick_voice_id("edge-hamed", lang="ar", allow_premium=True, enabled=on, provider="google") == "edge-hamed"
