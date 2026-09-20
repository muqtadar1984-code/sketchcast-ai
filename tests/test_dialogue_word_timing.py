"""The drawing must land where the word is said, in a two-voice lesson too.

Heat Transfer (catalogue, 2026-09-13, premium Chirp voices): the founder heard
"a second or so of lag between the narration and the animation". Two causes,
both measured on the finished MP4 and in the code:

1. Dialogue segments never asked their providers for word boundaries, so no
   words.json existed and every drawing cue fell back to character proportion
   across the WHOLE segment — blind to speaker changes and sentence gaps.
   Captions were exact (they use the measured line starts); the ink was not.
2. Chirp returns each sentence with ~0.5 s of silence at its edges (gaps of
   0.48/0.50/0.51/0.54/0.53 s between consecutive sentences in the file), and
   interpolate_words spread the words over that silence as if it were speech.

Every provider and ffmpeg here is a fake. Nothing in this file touches the
network or a real binary.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shared.tts.providers import google as G


# ── 2. the silence at a clip's edges ─────────────────────────────────────────

# ffmpeg v7's silencedetect output for a 3.0 s clip that opens with 0.45 s of
# silence and ends with 0.5 s of it (silence_end logged at end of file)
_V7 = """[silencedetect @ 0x1] silence_start: 0
[silencedetect @ 0x1] silence_end: 0.45 | silence_duration: 0.45
[silencedetect @ 0x1] silence_start: 2.5
[silencedetect @ 0x1] silence_end: 3 | silence_duration: 0.5
"""
# an older build leaves a silence that runs to EOF open
_OLD = """[silencedetect @ 0x1] silence_start: 0
[silencedetect @ 0x1] silence_end: 0.45 | silence_duration: 0.45
[silencedetect @ 0x1] silence_start: 2.5
"""


class TestSilenceEdges:
    def test_reads_lead_and_tail_from_v7_output(self):
        assert G._silence_edges(_V7, 3.0) == (0.45, 0.5)

    def test_an_open_trailing_silence_runs_to_end_of_file(self):
        assert G._silence_edges(_OLD, 3.0) == (0.45, 0.5)

    def test_interior_pauses_are_the_sentences_own(self):
        mid = "[x] silence_start: 1.2\n[x] silence_end: 1.6 | silence_duration: 0.4\n"
        assert G._silence_edges(mid, 3.0) == (0.0, 0.0)

    def test_a_clip_that_is_silence_end_to_end_is_left_alone(self):
        whole = "[x] silence_start: 0\n[x] silence_end: 3 | silence_duration: 3\n"
        assert G._silence_edges(whole, 3.0) == (0.0, 0.0)
        assert G._silence_edges("[x] silence_start: 0\n", 3.0) == (0.0, 0.0)

    def test_never_trims_below_the_speech_floor(self):
        """0.9 s clip, 0.45 lead + 0.4 tail leaves 0.05 s: a wrong guess, so
        behave exactly as before."""
        out = "[x] silence_start: 0\n[x] silence_end: 0.45\n[x] silence_start: 0.5\n"
        assert G._silence_edges(out, 0.9) == (0.0, 0.0)

    def test_no_output_or_no_duration_means_no_trim(self):
        assert G._silence_edges("", 3.0) == (0.0, 0.0)
        assert G._silence_edges(_V7, 0.0) == (0.0, 0.0)

    def test_a_failed_measurement_never_fails_the_lesson(self, monkeypatch):
        def boom(*a, **k):
            raise OSError("no ffmpeg")
        monkeypatch.setattr(G.subprocess, "run", boom)
        assert G._spoken_span(Path("x.mp3"), "ffmpeg", 3.0) == (0.0, 0.0)


def _stub(monkeypatch, tmp_path, durations, spans):
    """A Chirp provider whose network, ffmpeg and silence measurement are fakes."""
    calls = []

    def fake_post(body, *a, **k):
        calls.append(body)
        import base64
        return {"audioContent": base64.b64encode(f"mp3-{len(calls)}".encode()).decode(),
                "timepoints": []}

    monkeypatch.setattr(G, "_post", fake_post)
    monkeypatch.setattr(G, "_ffmpeg", lambda: "ffmpeg")
    durs = list(durations)
    monkeypatch.setattr(G, "_duration", lambda p, f: durs.pop(0) if durs else 1.0)
    sp = list(spans)
    # synthesize reads the layout probe (edges + interior pauses, 2026-09-20);
    # these stubs describe edges only, so no clause is anchored
    monkeypatch.setattr(G, "_spoken_layout",
                        lambda p, f, d: (*(sp.pop(0) if sp else (0.0, 0.0)), []))
    monkeypatch.setattr(G, "_concat", lambda parts, out, f: out.write_bytes(b"x"))
    return calls


class TestChirpWordsCoverTheSpokenSpanOnly:
    def test_words_start_after_the_lead_and_end_before_the_tail(self, tmp_path, monkeypatch):
        _stub(monkeypatch, tmp_path, durations=[3.0], spans=[(0.5, 0.5)])
        stats = G.synthesize("Heat moves from hot to cold.", tmp_path / "a.mp3",
                             "en-US-Chirp3-HD-Achernar", boundaries_out=tmp_path / "a.words.json")
        words = json.loads((tmp_path / "a.words.json").read_text(encoding="utf-8"))
        assert words[0]["t"] == 0.5, "the first word is said after the lead, not at 0"
        assert all(0.5 <= w["t"] < 2.5 for w in words), "no word inside the trailing silence"
        assert stats["silence_trimmed"] == 1.0
        assert stats["audio_secs"] == 3.0, "the clip itself is untouched — only the timing moved"

    def test_the_next_sentence_still_starts_at_the_measured_clip_boundary(self, tmp_path, monkeypatch):
        _stub(monkeypatch, tmp_path, durations=[2.0, 2.0], spans=[(0.4, 0.4), (0.4, 0.4)])
        G.synthesize("First one. Second two.", tmp_path / "b.mp3",
                     "en-US-Chirp3-HD-Achernar", boundaries_out=tmp_path / "b.words.json")
        words = json.loads((tmp_path / "b.words.json").read_text(encoding="utf-8"))
        by = {w["w"]: w["t"] for w in words}
        assert by["First"] == 0.4 and by["Second"] == 2.4
        assert [w["t"] for w in words] == sorted(w["t"] for w in words)

    def test_a_declared_opening_break_is_not_counted_twice(self, tmp_path, monkeypatch):
        """interpolate_words already shifts the first word past a <break> at the
        front; Chirp renders that break as real silence, so the measured lead
        contains it. Subtract the declared part or the word lands a full pause
        late."""
        _stub(monkeypatch, tmp_path, durations=[3.0], spans=[(0.5, 0.0)])
        G.synthesize('<break time="500ms"/> Hello there world.', tmp_path / "c.mp3",
                     "en-US-Chirp3-HD-Achernar", boundaries_out=tmp_path / "c.words.json")
        words = json.loads((tmp_path / "c.words.json").read_text(encoding="utf-8"))
        assert words[0]["t"] == pytest.approx(0.5, abs=1e-3)

    def test_an_estimated_duration_is_not_measured_for_silence(self, tmp_path, monkeypatch):
        """No readable clip, no file worth running silencedetect on."""
        asked = []
        _stub(monkeypatch, tmp_path, durations=[0.0], spans=[])
        monkeypatch.setattr(G, "_spoken_layout", lambda p, f, d: asked.append(p) or (0.0, 0.0, []))
        G.synthesize("Some words here.", tmp_path / "d.mp3",
                     "en-US-Chirp3-HD-Achernar", boundaries_out=tmp_path / "d.words.json")
        assert asked == []

    def test_the_classic_marks_path_is_untouched(self, tmp_path, monkeypatch):
        asked = []
        _stub(monkeypatch, tmp_path, durations=[2.0], spans=[])
        monkeypatch.setattr(G, "_spoken_layout", lambda p, f, d: asked.append(p) or (0.0, 0.0, []))
        G.synthesize("Two words.", tmp_path / "e.mp3", "ms-MY-Wavenet-A",
                     boundaries_out=tmp_path / "e.words.json")
        assert asked == [], "WaveNet has exact per-word marks; nothing to trim"


# ── 1. a dialogue segment gets a words.json ──────────────────────────────────

class TestDialogueWordsAreAssembledFromTheLines:
    def _clip(self, tmp_path, name, words=None):
        p = tmp_path / name
        p.write_bytes(b"mp3")
        if words is not None:
            p.with_suffix(".words.json").write_text(json.dumps(words), encoding="utf-8")
        return p

    def test_each_line_s_boundaries_are_shifted_by_its_measured_start(self, tmp_path):
        from agent6_animation.video_composer import _merge_line_words
        a = self._clip(tmp_path, "s1_dl0.mp3", [{"t": 0.0, "w": "Heat"}, {"t": 0.6, "w": "moves."}])
        b = self._clip(tmp_path, "s1_dl1.mp3", [{"t": 0.1, "w": "Where"}, {"t": 0.5, "w": "to?"}])
        out = _merge_line_words([("Heat moves.", a, 0.0, 2.0), ("Where to?", b, 2.0, 1.5)])
        assert out == [{"t": 0.0, "w": "Heat"}, {"t": 0.6, "w": "moves."},
                       {"t": 2.1, "w": "Where"}, {"t": 2.5, "w": "to?"}]

    def test_a_line_with_no_boundaries_is_spread_inside_its_own_clip(self, tmp_path):
        """The provider could not say; the estimate is still bounded by the
        line's measured start and end, which the old whole-segment estimate
        never was."""
        from agent6_animation.video_composer import _merge_line_words
        a = self._clip(tmp_path, "s2_dl0.mp3", [{"t": 0.0, "w": "Yes."}])
        b = self._clip(tmp_path, "s2_dl1.mp3")          # no words.json
        out = _merge_line_words([("Yes.", a, 0.0, 1.0), ("Because hot air rises.", b, 1.0, 3.0)])
        assert out[0] == {"t": 0.0, "w": "Yes."}
        rest = out[1:]
        # interpolate_words strips trailing punctuation, as the caption track does
        assert [w["w"] for w in rest] == ["Because", "hot", "air", "rises"]
        assert rest[0]["t"] == 1.0 and all(1.0 <= w["t"] <= 4.0 for w in rest)

    def test_a_boundary_past_the_clip_end_is_clamped_not_carried_into_the_next_line(self, tmp_path):
        from agent6_animation.video_composer import _merge_line_words
        a = self._clip(tmp_path, "s3_dl0.mp3", [{"t": 0.0, "w": "Go"}, {"t": 2.3, "w": "on."}])
        out = _merge_line_words([("Go on.", a, 0.0, 2.0)])
        assert out[-1]["t"] == 2.0

    def test_a_corrupt_boundaries_file_falls_back_instead_of_raising(self, tmp_path):
        from agent6_animation.video_composer import _merge_line_words
        p = tmp_path / "s4_dl0.mp3"
        p.write_bytes(b"mp3")
        p.with_suffix(".words.json").write_text("{not json", encoding="utf-8")
        out = _merge_line_words([("Two words.", p, 5.0, 1.0)])
        assert [w["w"] for w in out] == ["Two", "words"] and out[0]["t"] == 5.0

    def test_the_dialogue_path_asks_every_provider_for_boundaries_and_writes_the_segment_file(self):
        import inspect

        from agent6_animation import video_composer as vc
        src = inspect.getsource(vc._synth_dialogue)
        assert src.count("boundaries_out=wjson") == 3, \
            "teacher premium, student premium AND student Edge all say when their words are spoken"
        assert "_merge_line_words(spoken)" in src
        assert 'with_suffix(".words.json").write_text' in src

    def test_the_renderer_reads_the_file_the_dialogue_path_now_writes(self):
        """The two halves must agree on the filename, or the fix is a file
        nobody opens."""
        import inspect

        from agent6_animation import video_composer as vc
        src = inspect.getsource(vc._render_one) if hasattr(vc, "_render_one") else inspect.getsource(vc)
        assert 'with_suffix(".words.json")' in src
