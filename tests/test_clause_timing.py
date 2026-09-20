"""Chirp word timing, clause by clause (2026-09-20).

Chirp 3 HD returns no word marks, so a sentence's interior words were a
character-proportion guess over the whole spoken span — a second or more
off inside a long sentence, and every cue on such a word fired against the
wrong picture. The voice does pause at its punctuation, and silencedetect
already runs once per clip for the edge trim: the interior pauses it finds
now anchor each clause's opening word."""

from __future__ import annotations

import pytest

from shared.tts import chunks as C
from shared.tts.providers import google as G


class TestClauseAnchors:
    S = "Plants make food, animals eat plants, and decomposers recycle it all."

    def test_each_clause_opens_where_its_pause_ends(self):
        # spoken span 6.0 s; the voice paused twice, at the two commas
        ws = C.interpolate_words_by_pauses(self.S, 10.0, 6.0, [(1.6, 1.9), (3.7, 4.0)])
        by = {w["w"]: w["t"] for w in ws}
        assert by["Plants"] == 10.0
        assert by["animals"] == pytest.approx(11.9)
        assert by["and"] == pytest.approx(14.0)
        assert [w["w"] for w in ws] == C.words_of(self.S)
        assert all(b["t"] >= a["t"] for a, b in zip(ws, ws[1:]))
        # …and the last clause's words stay inside the span
        assert ws[-1]["t"] < 16.0

    def test_the_longest_pauses_are_the_boundaries(self):
        # a breath (0.13 s) plus two real pauses: the breath is not a comma
        ws = C.interpolate_words_by_pauses(self.S, 0.0, 6.0,
                                           [(0.8, 0.93), (1.6, 1.9), (3.7, 4.0)])
        by = {w["w"]: w["t"] for w in ws}
        assert by["animals"] == pytest.approx(1.9) and by["and"] == pytest.approx(4.0)

    def test_too_few_pauses_falls_back_to_the_plain_proportion(self):
        plain = C.interpolate_words(self.S, 0.0, 6.0)
        assert C.interpolate_words_by_pauses(self.S, 0.0, 6.0, [(1.6, 1.9)]) == plain
        assert C.interpolate_words_by_pauses(self.S, 0.0, 6.0, []) == plain

    def test_a_pause_outside_the_span_is_not_an_anchor(self):
        plain = C.interpolate_words(self.S, 0.0, 6.0)
        assert C.interpolate_words_by_pauses(self.S, 0.0, 6.0, [(1.6, 1.9), (6.5, 6.8)]) == plain

    def test_a_single_clause_sentence_is_unchanged(self):
        s = "Cells group into tissues."
        assert C.interpolate_words_by_pauses(s, 3.0, 2.0, [(0.5, 0.7)]) == C.interpolate_words(s, 3.0, 2.0)

    def test_clauses_split_at_the_same_punctuation_the_request_cap_uses(self):
        assert C.clauses_of(self.S) == ["Plants make food,", "animals eat plants,",
                                        "and decomposers recycle it all."]
        assert C.clauses_of("No clause here.") == ["No clause here."]


class TestInteriorPausesFromSilencedetect:
    ERR = ("[silencedetect @ 0x1] silence_start: 0\n"
           "[silencedetect @ 0x1] silence_end: 0.48 | silence_duration: 0.48\n"
           "[silencedetect @ 0x1] silence_start: 2.1\n"
           "[silencedetect @ 0x1] silence_end: 2.4 | silence_duration: 0.3\n"
           "[silencedetect @ 0x1] silence_start: 4.0\n"
           "[silencedetect @ 0x1] silence_end: 4.2 | silence_duration: 0.2\n"
           "[silencedetect @ 0x1] silence_start: 5.5\n"
           "[silencedetect @ 0x1] silence_end: 6.0 | silence_duration: 0.5\n")

    def test_edges_are_excluded_and_interior_pauses_kept_in_order(self):
        lead, tail = G._silence_edges(self.ERR, 6.0)
        assert (lead, tail) == (0.48, 0.5)
        assert G._interior_pauses(self.ERR, 6.0, lead, tail) == [(2.1, 2.4), (4.0, 4.2)]

    def test_no_output_means_no_pauses(self):
        assert G._interior_pauses("", 6.0, 0.0, 0.0) == []
        assert G._interior_pauses(self.ERR, 0.0, 0.0, 0.0) == []

    def test_the_layout_probe_degrades_to_nothing_on_failure(self, monkeypatch):
        import subprocess

        def boom(*a, **k):
            raise OSError("no ffmpeg")

        monkeypatch.setattr(subprocess, "run", boom)
        from pathlib import Path
        assert G._spoken_layout(Path("x.mp3"), "ffmpeg", 3.0) == (0.0, 0.0, [])
        assert G._spoken_span(Path("x.mp3"), "ffmpeg", 3.0) == (0.0, 0.0)

    def test_the_provider_reports_how_many_sentences_were_anchored(self):
        import inspect
        src = inspect.getsource(G.synthesize)
        assert "interpolate_words_by_pauses(piece, cursor + lead, span, rel)" in src
        assert '"clause_anchored": anchored' in src


class TestTheNoWordsCueStartsWhereThePhraseDoes:
    def test_start_not_midpoint(self):
        from spike.scene_engine.schema import Cue
        from spike.scene_engine.timing import resolve_cue
        n = "the nucleus controls everything in the cell"
        t = resolve_cue(Cue(phrase="controls everything"), n, 10.0)
        assert t == pytest.approx(n.find("controls") / len(n) * 10.0)

    def test_a_single_voice_run_never_reads_another_runs_words(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "agent6_animation"
               / "video_composer.py").read_text(encoding="utf-8")
        i = src.index('bnd = mp3.with_suffix(".words.json") if _scene_flag() else None')
        j = src.index("synthesize(speakable(text), mp3", i)
        assert "bnd.unlink(missing_ok=True)" in src[i:j]
