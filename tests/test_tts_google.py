"""Phase 1b: the Google provider and its pure helpers. No network — the HTTP
call and ffmpeg are stubbed; what is tested is chunking, SSML, timing and the
words.json contract timing.py depends on."""

from __future__ import annotations

import json

import pytest

import threading

from shared.tts import chunks as C


# ── pure helpers ─────────────────────────────────────────────────────────────

class TestSentences:
    def test_splits_on_terminal_punctuation_only(self):
        assert C.sentences("Cells group. Tissues form! Organs? Yes.") == [
            "Cells group.", "Tissues form!", "Organs?", "Yes."]

    def test_arabic_and_devanagari_terminators(self):
        assert len(C.sentences("الخلية هي الوحدة؟ نعم.")) == 2
        assert len(C.sentences("यह कोशिका है। यह ऊतक है।")) == 2

    def test_tags_stay_inside_their_sentence(self):
        s = C.sentences('Trace <break time="0.3s"/> the price. Then stop.')
        assert s[0] == 'Trace <break time="0.3s"/> the price.'


class TestSsml:
    def test_prose_is_escaped_and_break_survives(self):
        ssml, n = C.ssml_for('a < b & c > d <break time="1s"/> done.', marks=False)
        assert "&lt;" in ssml and "&amp;" in ssml and "&gt;" in ssml
        assert '<break time="1s"/>' in ssml
        assert ssml.startswith("<speak>") and ssml.endswith("</speak>")

    def test_only_break_is_let_through(self):
        ssml, _ = C.ssml_for('x <prosody rate="slow">y</prosody> z', marks=False)
        assert "prosody" not in ssml
        assert all(w in ssml for w in ("x", "y", "z")), "the prose around a dropped tag survives"

    def test_marks_number_every_word_from_the_offset(self):
        ssml, n = C.ssml_for("one two three", marks=True, mark_offset=10)
        assert n == 3
        assert '<mark name="w10"/>one' in ssml and '<mark name="w12"/>three' in ssml

    def test_a_mark_is_never_placed_inside_a_tag(self):
        ssml, _ = C.ssml_for('go <break time="0.5s"/> now', marks=True)
        assert '<break time="0.5s"/>' in ssml
        assert "mark" not in ssml[ssml.index("<break"):ssml.index("/>", ssml.index("<break"))]


class TestChunks:
    def test_chirp_gets_one_sentence_per_chunk(self):
        out = C.chunks("A one. B two. C three.", one_sentence_each=True, marks=False)
        assert out == ["A one.", "B two.", "C three."]

    def test_classic_packs_sentences_under_the_byte_cap(self, monkeypatch):
        monkeypatch.setattr(C, "MAX_REQUEST_BYTES", 60)
        out = C.chunks("Short one. Short two. Short three. Short four.",
                       one_sentence_each=False, marks=False)
        assert len(out) >= 2
        assert " ".join(out) == "Short one. Short two. Short three. Short four."
        for piece in out:
            assert piece.endswith(".")                     # never cut mid-sentence

    def test_a_chunk_never_exceeds_the_cap_when_it_can_avoid_it(self):
        text = " ".join(f"Sentence number {i} is here." for i in range(300))
        first = 0
        for piece in C.chunks(text, one_sentence_each=False, marks=True):
            # sized as the provider SENDS it: marks renumbered from a running offset
            ssml, n = C.ssml_for(piece, marks=True, mark_offset=first)
            assert len(ssml.encode()) <= C.MAX_REQUEST_BYTES
            first += n


class TestWordTiming:
    def test_interpolation_starts_exact_and_stays_monotonic(self):
        ws = C.interpolate_words("Cells group into tissues.", 3.0, 2.0)
        assert [w["w"] for w in ws] == ["Cells", "group", "into", "tissues"]
        assert ws[0]["t"] == 3.0
        assert all(b["t"] >= a["t"] for a, b in zip(ws, ws[1:]))
        assert ws[-1]["t"] < 5.0

    def test_marks_map_to_words_and_offset_by_chunk_start(self):
        tps = [{"markName": "w5", "timeSeconds": 0.1}, {"markName": "w6", "timeSeconds": 0.5},
               {"markName": "w7", "timeSeconds": 0.9}]
        ws, missing = C.words_from_marks("one two three", tps, chunk_start=10.0,
                                         chunk_duration=1.2, mark_offset=5)
        assert missing == 0
        assert [w["t"] for w in ws] == [10.1, 10.5, 10.9]

    def test_a_dropped_mark_is_interpolated_between_neighbours(self):
        tps = [{"markName": "w0", "timeSeconds": 0.0}, {"markName": "w2", "timeSeconds": 1.0}]
        ws, missing = C.words_from_marks("a b c", tps, chunk_start=0.0, chunk_duration=1.5)
        assert missing == 1
        assert ws[1]["t"] == 0.5

    def test_no_timepoints_at_all_still_yields_every_word(self):
        ws, missing = C.words_from_marks("a b c d", [], chunk_start=2.0, chunk_duration=2.0)
        assert missing == 4 and len(ws) == 4 and ws[0]["t"] == 2.0

    def test_family_and_language_from_the_voice_name(self):
        assert C.family("ar-XA-Chirp3-HD-Achernar") == "chirp"
        assert C.family("ms-MY-Wavenet-A") == "classic"
        assert C.language_code("ar-XA-Chirp3-HD-Achernar") == "ar-XA"
        assert C.language_code("en-US-Standard-C") == "en-US"


# ── the provider, HTTP and ffmpeg stubbed ────────────────────────────────────

def _stub(monkeypatch, tmp_path, *, tps_per_chunk=None, durations=None):
    from shared.tts.providers import google as G
    calls = []
    lock = threading.Lock()

    def fake_post(body):
        with lock:  # the pool runs chunks in parallel; the marker must not race
            calls.append(body)
            n = len(calls)
        tps = []
        if body.get("enableTimePointing") and tps_per_chunk is not None:
            tps = tps_per_chunk(body["input"]["ssml"])
        import base64
        return {"audioContent": base64.b64encode(f"mp3-{n}".encode()).decode(), "timepoints": tps}

    monkeypatch.setattr(G, "_post", fake_post)
    monkeypatch.setattr(G, "_ffmpeg", lambda: "ffmpeg")
    durs = list(durations or [])

    def fake_duration(path, ffmpeg):
        return durs.pop(0) if durs else 1.0

    monkeypatch.setattr(G, "_duration", fake_duration)

    def fake_concat(parts, out, ffmpeg):
        out.write_bytes(b"".join(p.read_bytes() for p in parts))

    monkeypatch.setattr(G, "_concat", fake_concat)
    return G, calls


class TestGoogleProvider:
    def test_chirp_synthesizes_one_request_per_sentence_and_measures_each(self, tmp_path, monkeypatch):
        G, calls = _stub(monkeypatch, tmp_path, durations=[2.0, 1.5, 3.0])
        out = tmp_path / "a.mp3"
        stats = G.synthesize("First one. Second two. Third three.", out,
                             "en-US-Chirp3-HD-Achernar", boundaries_out=tmp_path / "a.words.json")
        assert len(calls) == 3
        assert all("enableTimePointing" not in b for b in calls), "Chirp ignores marks — do not ask"
        words = json.loads((tmp_path / "a.words.json").read_text(encoding="utf-8"))
        starts = [w["t"] for w in words if w["w"] in ("First", "Second", "Third")]
        assert starts == [0.0, 2.0, 3.5], "sentence starts must be the MEASURED clip boundaries"
        assert stats["requests"] == 3 and stats["family"] == "chirp"
        assert out.read_bytes() == b"mp3-1mp3-2mp3-3"

    def test_classic_uses_marks_and_exact_timepoints(self, tmp_path, monkeypatch):
        import re

        def tps(ssml):
            names = re.findall(r'<mark name="(w\d+)"/>', ssml)
            return [{"markName": n, "timeSeconds": 0.3 * i} for i, n in enumerate(names)]

        G, calls = _stub(monkeypatch, tmp_path, tps_per_chunk=tps, durations=[9.0])
        G.synthesize("One two three. Four five.", tmp_path / "b.mp3",
                     "ar-XA-Wavenet-A", boundaries_out=tmp_path / "b.words.json")
        assert len(calls) == 1, "classic packs sentences into one request"
        assert calls[0]["enableTimePointing"] == ["SSML_MARK"]
        assert calls[0]["voice"]["languageCode"] == "ar-XA"
        words = json.loads((tmp_path / "b.words.json").read_text(encoding="utf-8"))
        assert [w["w"] for w in words] == ["One", "two", "three", "Four", "five"]
        assert [w["t"] for w in words] == [0.0, 0.3, 0.6, 0.9, 1.2]

    def test_no_boundaries_requested_means_no_marks_and_no_words_file(self, tmp_path, monkeypatch):
        G, calls = _stub(monkeypatch, tmp_path)
        G.synthesize("Hello there.", tmp_path / "c.mp3", "en-US-Wavenet-F")
        assert "enableTimePointing" not in calls[0]
        assert not (tmp_path / "c.words.json").exists()

    def test_break_tags_reach_google_and_prose_is_escaped(self, tmp_path, monkeypatch):
        G, calls = _stub(monkeypatch, tmp_path)
        G.synthesize('Now trace <break time="0.3s"/> a < b.', tmp_path / "d.mp3", "en-US-Chirp3-HD-Achernar")
        ssml = calls[0]["input"]["ssml"]
        assert '<break time="0.3s"/>' in ssml and "a &lt; b" in ssml

    def test_billable_chars_exclude_marks(self, tmp_path, monkeypatch):
        G, calls = _stub(monkeypatch, tmp_path)
        text = "One two three four five six."
        with_marks = G.synthesize(text, tmp_path / "e.mp3", "en-US-Wavenet-F",
                                  boundaries_out=tmp_path / "e.words.json")["chars"]
        without = G.synthesize(text, tmp_path / "f.mp3", "en-US-Wavenet-F")["chars"]
        assert with_marks == without, "Google does not bill <mark>; neither do we"

    def test_empty_text_raises(self, tmp_path, monkeypatch):
        G, _ = _stub(monkeypatch, tmp_path)
        with pytest.raises(ValueError):
            G.synthesize("   ", tmp_path / "g.mp3", "en-US-Wavenet-F")

    def test_transient_http_errors_are_retried(self, tmp_path, monkeypatch):
        from shared.tts.providers import google as G
        attempts = []

        class R:
            headers: dict = {}
            text = ""
            def __init__(self, code):
                self.status_code = code
            def json(self):
                import base64
                return {"audioContent": base64.b64encode(b"ok").decode()}

        def fake_request_post(url, headers=None, json=None, timeout=None):
            attempts.append(1)
            return R(429 if len(attempts) < 3 else 200)

        import requests
        monkeypatch.setattr(requests, "post", fake_request_post)
        monkeypatch.setattr(G, "_token", lambda: ("tok", "proj"))
        monkeypatch.setattr(G.time, "sleep", lambda s: None)
        out = G._post({"input": {"ssml": "<speak>x</speak>"}})
        assert len(attempts) == 3 and "audioContent" in out
