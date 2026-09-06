"""Chapter timestamps, teaching clips and the part plan (catalogue.timestamps,
Phase 3 decision 5): pure functions over a part's script and its measured
video manifest. Invariants pinned: the first chapter is at 0, chapters are
monotonic, a part with three or more headings has at least three chapters,
every clip lies inside [0, total] and lasts 120-240 s, and nothing is padded
to a length the video does not have."""

from __future__ import annotations

import random

import pytest

from catalogue.timestamps import (CLIP_MAX_S, CLIP_MIN_S, chapters_for_part, clips_for_part, merge_by_part,
                                  part_plan_entry, segment_duration)


def _script(headings):
    return [{"segment_id": f"s{i:03d}", "slide_heading": h, "text": f"Narration {i}."}
            for i, h in enumerate(headings, start=1)]


def _video(durations):
    return [{"segment_id": f"s{i:03d}", "audio_duration_seconds": d, "video_path": f"/tmp/s{i}.mp4"}
            for i, d in enumerate(durations, start=1)]


class TestChapters:
    def test_grouped_by_heading_first_at_zero(self):
        ch = chapters_for_part(_script(["A", "A", "B", "C", "C", "D"]), _video([30, 40, 50, 60, 70, 80]))
        assert ch == [{"t": 0.0, "label": "A"}, {"t": 70.0, "label": "B"},
                      {"t": 120.0, "label": "C"}, {"t": 250.0, "label": "D"}]

    def test_at_least_three_chapters_when_three_headings(self):
        ch = chapters_for_part(_script(["A", "B", "C"]), _video([10, 10, 10]))
        assert len(ch) >= 3 and ch[0]["t"] == 0.0
        assert [c["t"] for c in ch] == sorted(c["t"] for c in ch)

    def test_a_headingless_opening_is_the_introduction_and_repeats_do_not_reopen(self):
        ch = chapters_for_part(_script(["", "Cells", "", "Cells", "Tissues"]), _video([5, 5, 5, 5, 5]))
        assert [c["label"] for c in ch] == ["Introduction", "Cells", "Tissues"]
        assert [c["t"] for c in ch] == [0.0, 5.0, 20.0]

    def test_section_ids_attach_by_casefolded_heading(self):
        ch = chapters_for_part(_script(["What A Cell Is", "Plant cells"]), _video([9, 9]),
                               section_ids={"what a cell is": "s1"})
        assert ch[0].get("section_id") == "s1" and "section_id" not in ch[1]

    def test_segment_duration_reads_both_manifest_shapes(self):
        assert segment_duration({"audio_duration_seconds": 3.5}) == 3.5
        assert segment_duration({"duration": 4}) == 4.0
        assert segment_duration({"segment_id": "x"}) == 0.0

    def test_unknown_video_segments_still_count_time(self):
        # a video segment the script does not name (a stub, a failed row) keeps
        # the clock honest without opening a chapter of its own
        ch = chapters_for_part(_script(["A", "B"]), _video([10, 10]) + [{"segment_id": "zzz", "audio_duration_seconds": 30}])
        assert ch == [{"t": 0.0, "label": "A"}, {"t": 10.0, "label": "B"}]


class TestClips:
    def _ok(self, clips, total):
        for c in clips:
            assert 0.0 <= c["start"] < c["end"] <= total, c
            assert CLIP_MIN_S <= c["end"] - c["start"] <= CLIP_MAX_S, c
            assert set(c) == {"part", "start", "end", "label", "purpose"}

    def test_aligned_to_chapter_boundaries_and_the_tail_folds_in(self):
        ch = [{"t": 0.0, "label": "A"}, {"t": 70.0, "label": "B"}, {"t": 120.0, "label": "C"}, {"t": 250.0, "label": "D"}]
        clips = clips_for_part(ch, 330.0, part=1)
        assert clips == [{"part": 1, "start": 0.0, "end": 120.0, "label": "A", "purpose": "introduce"},
                         {"part": 1, "start": 120.0, "end": 330.0, "label": "C", "purpose": "consolidate"}]
        self._ok(clips, 330.0)

    def test_a_long_chapter_is_capped_never_padded(self):
        clips = clips_for_part([{"t": 0.0, "label": "Only"}], 600.0, part=2)
        assert clips == [{"part": 2, "start": 0.0, "end": 240.0, "label": "Only", "purpose": "introduce"}]

    def test_a_short_part_yields_no_clip(self):
        assert clips_for_part([{"t": 0.0, "label": "A"}], 90.0, part=1) == []
        assert clips_for_part([], 900.0, part=1) == []

    def test_middle_clips_are_explain(self):
        ch = [{"t": float(t), "label": f"C{t}"} for t in range(0, 900, 150)]
        clips = clips_for_part(ch, 900.0, part=1)
        assert [c["purpose"] for c in clips] == ["introduce", "explain", "explain", "explain", "explain", "consolidate"]
        self._ok(clips, 900.0)

    def test_invariants_hold_on_random_timelines(self):
        rng = random.Random(7)
        for _ in range(300):
            n = rng.randint(1, 25)
            headings = [rng.choice("ABCDEFG") for _ in range(n)]
            durs = [rng.uniform(2, 90) for _ in range(n)]
            ch = chapters_for_part(_script(headings), _video(durs))
            total = sum(durs)
            assert ch[0]["t"] == 0.0
            assert [c["t"] for c in ch] == sorted(c["t"] for c in ch)
            if len(set(headings)) >= 3:
                assert len(ch) >= 3
            clips = clips_for_part(ch, total, part=1)
            assert all(isinstance(c["start"], int) and isinstance(c["end"], int) for c in clips)
            self._ok(clips, int(round(total)))


def test_part_plan_entry():
    assert part_plan_entry(2, ["A", " B ", ""], 1000) == {"part": 2, "sections": ["A", "B"], "minutes": 16.7}


class TestMergeByPart:
    def test_replaces_only_the_incoming_parts(self):
        stored = [{"part": 1, "chapters": ["old1"]}, {"part": 2, "chapters": ["old2"]}]
        out = merge_by_part(stored, [{"part": 2, "chapters": ["new2"]}, {"part": 3, "chapters": ["new3"]}])
        assert out == [{"part": 1, "chapters": ["old1"]}, {"part": 2, "chapters": ["new2"]},
                       {"part": 3, "chapters": ["new3"]}]

    def test_flat_clips_sort_by_part_then_start(self):
        stored = [{"part": 2, "start": 0.0, "end": 130.0}, {"part": 1, "start": 200.0, "end": 330.0}]
        out = merge_by_part(stored, [{"part": 2, "start": 130.0, "end": 260.0}, {"part": 2, "start": 0.0, "end": 120.0}])
        assert out == [{"part": 1, "start": 200.0, "end": 330.0},
                       {"part": 2, "start": 0.0, "end": 120.0}, {"part": 2, "start": 130.0, "end": 260.0}]

    def test_none_and_garbage_are_tolerated(self):
        assert merge_by_part(None, [{"part": 1}]) == [{"part": 1}]
        assert merge_by_part([{"no_part": True}, "junk"], []) == [{"no_part": True}]
