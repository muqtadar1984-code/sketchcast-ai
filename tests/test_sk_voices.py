"""SK second narration track (sk/voices.py) — the pure parts: matching word
marks to narration items, the cut/stretch/place plan that keeps the other
voice on the clip's cues, the remapped word marks, the ffmpeg contracts, and
the packager's graph/segment plumbing around them."""

import json
from pathlib import Path

import pytest

from sk.packager import asset_entry, second_voice_for, voice_gender
from sk.voices import (
    ATEMPO_FIT_MAX,
    ATEMPO_LAST_MAX,
    ATEMPO_MAX,
    ATEMPO_MIN,
    MIN_GAP_MS,
    add_voice_track,
    align_args,
    align_filter,
    default_track,
    mux_args,
    plan_alignment,
    remap_words,
    sentence_spans,
)

ITEMS = ["Look at your hand. It looks like one smooth piece.", "All living organisms are made of cells.", "Magnification = image size ÷ actual size."]


def words_for(items, *, start=100, per_word=300, gap=700):
    """Word marks that spell the items out, `per_word` ms each, `gap` ms between items."""
    ws, t = [], start
    for item in items:
        for w in item.replace("÷", " ÷ ").split():
            ws.append({"i": len(ws), "t0": t, "t1": t + per_word, "text": w.strip(".,")})
            t += per_word + 25
        t += gap
    return ws


BASE = words_for(ITEMS)
BASE_END = BASE[-1]["t1"]
CLIP_MS = BASE_END + 1000


def test_sentence_spans_follow_the_items_through_the_marks():
    spans = sentence_spans(BASE, ITEMS)
    assert spans == [(0, 9), (10, 16), (17, 23)]
    # a symbol the TTS does not emit as a word still matches (÷ → "/" is not a token)
    assert sentence_spans(words_for(ITEMS, per_word=200), ITEMS) == spans


def test_sentence_spans_refuse_marks_that_do_not_spell_the_items():
    assert sentence_spans(BASE[:-3], ITEMS) is None  # too short
    wrong = [dict(w) for w in BASE]
    wrong[5]["text"] = "rough"
    assert sentence_spans(wrong, ITEMS) is None
    assert sentence_spans(BASE, ["", "x"]) is None


def test_a_slower_voice_is_squeezed_per_item_and_lands_on_the_base_cues():
    # the new voice reads 15 % slower and pauses longer between items
    new = words_for(ITEMS, per_word=345, gap=1100)
    al = plan_alignment(BASE, new, ITEMS, clip_ms=CLIP_MS, new_audio_ms=new[-1]["t1"] + 500)
    assert al.fallback is None and al.matched_items == 3 and len(al.pieces) == 3
    assert all(ATEMPO_MIN <= p.atempo <= ATEMPO_MAX for p in al.pieces)
    assert all(1.1 < p.atempo < 1.2 for p in al.pieces)  # ~1.15 each
    # cut points sit in the pause between items of the NEW take
    assert new[9]["t1"] < al.pieces[1].src_start_ms < new[10]["t0"]
    assert al.pieces[0].src_start_ms == 0 and al.pieces[-1].src_end_ms == new[-1]["t1"] + 500
    # every item's first word lands on the base item's first word (±1 ms rounding)
    rw = remap_words(new, al)
    spans_b, spans_n = sentence_spans(BASE, ITEMS), sentence_spans(rw, ITEMS)
    assert spans_n == spans_b
    for (bf, _), (nf, _) in zip(spans_b, spans_n):
        assert abs(rw[nf]["t0"] - BASE[bf]["t0"]) <= 1
    assert al.max_abs_offset_ms <= 1 and al.overflow_ms == 0
    assert rw[-1]["t1"] <= CLIP_MS


def test_a_faster_voice_is_slowed_only_within_bounds_and_padded_otherwise():
    new = words_for(ITEMS, per_word=200, gap=300)  # 33 % faster than the base
    al = plan_alignment(BASE, new, ITEMS, clip_ms=CLIP_MS, new_audio_ms=new[-1]["t1"] + 300)
    assert all(p.atempo == ATEMPO_MIN for p in al.pieces)  # 0.67 wanted, clamped
    rw = remap_words(new, al)
    for (bf, _), (nf, _) in zip(sentence_spans(BASE, ITEMS), sentence_spans(rw, ITEMS)):
        assert abs(rw[nf]["t0"] - BASE[bf]["t0"]) <= 1  # still starts on cue; silence fills the rest
    assert al.overflow_ms == 0


def test_an_item_that_cannot_fit_delays_the_next_instead_of_overlapping():
    # a very slow middle item (2× the base) cannot be squeezed enough: the
    # piece runs past the next item's cue, which then starts late, not on top
    new = words_for(ITEMS, per_word=300, gap=700)
    for w in new[10:17]:
        w["t0"], w["t1"] = w["t0"] + (w["i"] - 10) * 300, w["t1"] + (w["i"] - 9) * 300
    shift = new[16]["t1"] - words_for(ITEMS)[16]["t1"]
    for w in new[17:]:
        w["t0"], w["t1"] = w["t0"] + shift, w["t1"] + shift
    al = plan_alignment(BASE, new, ITEMS, clip_ms=CLIP_MS, new_audio_ms=new[-1]["t1"] + 500)
    assert al.pieces[1].atempo == ATEMPO_FIT_MAX  # squeezed harder than a fitting item, still bounded
    rw = remap_words(new, al)
    (_, l1), (f2, _) = sentence_spans(rw, ITEMS)[1:3]
    assert rw[f2]["t0"] >= rw[l1]["t1"] + MIN_GAP_MS - 1  # item 3 starts after item 2's last word
    assert rw[f2]["t0"] > BASE[17]["t0"]  # …which is LATE against its cue; the plan says so
    assert al.max_abs_offset_ms > 0


def test_unmatched_marks_fall_back_to_one_whole_track_stretch():
    new = words_for(ITEMS, per_word=330)
    new[3]["text"] = "foot"
    al = plan_alignment(BASE, new, ITEMS, clip_ms=CLIP_MS, new_audio_ms=new[-1]["t1"] + 500)
    assert al.fallback and "new marks" in al.fallback
    assert len(al.pieces) == 1 and al.pieces[0].src_start_ms == 0 and al.pieces[0].dst_start_ms == 0
    assert ATEMPO_MIN <= al.pieces[0].atempo <= ATEMPO_LAST_MAX
    assert abs(remap_words(new, al)[-1]["t1"] - BASE_END) < 150


def test_remap_keeps_word_order_and_text():
    new = words_for(ITEMS, per_word=330, gap=900)
    al = plan_alignment(BASE, new, ITEMS, clip_ms=CLIP_MS, new_audio_ms=new[-1]["t1"] + 500)
    rw = remap_words(new, al)
    assert [w["text"] for w in rw] == [w["text"] for w in new]
    assert all(a["t1"] <= b["t0"] + 1 for a, b in zip(rw, rw[1:]))
    assert [w["i"] for w in rw] == list(range(len(rw)))


def test_ffmpeg_contracts():
    new = words_for(ITEMS, per_word=345, gap=1100)
    al = plan_alignment(BASE, new, ITEMS, clip_ms=CLIP_MS, new_audio_ms=new[-1]["t1"] + 500)
    f = align_filter(al)
    assert f.count("atrim=") == 3 and f.count("atempo=") == 3 and f.count("adelay=") == 3
    # the silent anchor is the first mix input, so the output starts at 0 and lasts the clip
    assert f.startswith(f"aevalsrc=0:s=24000:c=mono:d={CLIP_MS / 1000:.3f}[z]")
    assert "[z][p0][p1][p2]amix=inputs=4:normalize=0" in f and f"apad=whole_dur={CLIP_MS / 1000:.3f}[out]" in f
    assert f"adelay={al.pieces[1].dst_start_ms}:all=1" in f
    args = align_args(Path("in.mp3"), Path("out.mp3"), al, "FF")
    assert args[:3] == ["FF", "-y", "-i"] and args[-1] == "out.mp3" and "-filter_complex" in args and "libmp3lame" in args
    # a factor of 1.0 adds no atempo stage
    one = plan_alignment([], [], [], clip_ms=1000, new_audio_ms=1000)
    assert "amix=inputs=2" in align_filter(one) and "atempo" not in align_filter(one)
    m = mux_args(Path("clip.mp4"), Path("audio-male.mp3"), Path("clip-male.mp4"), 56583, "FF")
    assert m[m.index("-c:v") + 1] == "copy" and m[m.index("-c:a") + 1] == "aac" and m[m.index("-t") + 1] == "56.58"
    assert m[m.index("-map") + 1] == "0:v" and m[m.index("-map", m.index("-map") + 1) + 1] == "1:a"


SEG = {
    "segment_id": "s1-explain-main", "voice": "edge-aria", "voice_gender": "female",
    "audio": {"uri": "audio.mp3", "duration_ms": BASE_END + 400}, "clip": {"uri": "clip.mp4", "duration_ms": CLIP_MS}, "hitmap": "hitmap.json",
    "marks": {"words": BASE, "visemes": []}, "narration": " ".join(ITEMS), "narration_sentences": ITEMS,
}


def test_add_voice_track_synthesizes_aligns_and_records_the_track(tmp_path):
    calls = []

    def fake_synth(text, out, voice_id, *, boundaries_out, report):
        calls.append((text, voice_id))
        out.write_bytes(b"ID3fake")
        raw = [{"t": w["t0"] / 1000, "d": (w["t1"] - w["t0"]) / 1000, "w": w["text"]} for w in words_for(ITEMS, per_word=345, gap=1100)]
        Path(boundaries_out).write_text(json.dumps(raw), encoding="utf-8")
        report.update({"requested": voice_id, "used": "en-US-GuyNeural", "provider": "edge"})

    from sk.packager import norm_words

    track = add_voice_track(tmp_path, dict(SEG), "edge-guy", "male", synthesize=fake_synth, norm_words=norm_words, dry=True)
    assert calls == [(SEG["narration"], "edge-guy")]
    assert track["voice"] == "edge-guy" and track["gender"] == "male" and track["voice_ref"] == "en-US-GuyNeural"
    assert track["audio"] == {"uri": "audio-male.mp3", "duration_ms": CLIP_MS}
    assert track["clip"] == {"uri": "clip-male.mp4", "duration_ms": CLIP_MS}
    assert track["alignment"]["matched_items"] == 3 and track["alignment"]["fallback"] is None
    assert len(track["marks"]["words"]) == len(BASE)
    assert json.loads((tmp_path / "audio-male.words.json").read_text(encoding="utf-8")) == track["marks"]["words"]
    with pytest.raises(ValueError):
        add_voice_track(tmp_path, dict(SEG), "edge-guy", "robot", synthesize=fake_synth, norm_words=norm_words, dry=True)


def test_default_track_mirrors_the_rendered_narration():
    t = default_track(SEG)
    assert t["voice"] == "edge-aria" and t["gender"] == "female"
    assert t["clip"] == SEG["clip"] and t["audio"] == SEG["audio"] and t["marks"]["words"] == BASE


def test_second_voice_is_the_other_genders_free_voice_by_default():
    assert voice_gender("edge-aria") == "female" and voice_gender("edge-guy") == "male"
    assert second_voice_for("edge-aria", "auto") == "edge-guy"
    assert second_voice_for("edge-guy", "auto") == "edge-aria"
    assert second_voice_for("edge-aria", "none") is None and second_voice_for("edge-aria", "") is None
    assert second_voice_for("edge-aria", "edge-neerja") == "edge-neerja"


def test_graph_asset_carries_clip_hitmap_and_clips_by_gender():
    seg = {**SEG, "voices": {"female": default_track(SEG), "male": {"voice": "edge-guy", "clip": {"uri": "clip-male.mp4", "duration_ms": CLIP_MS}}}}
    a = asset_entry("s1", seg)
    assert a["segment"] == "s1/explain-main/segment.json"
    assert a["clip"] == "s1/explain-main/clip.mp4" and a["hitmap"] == "s1/explain-main/hitmap.json"
    assert a["clips"] == {"female": "s1/explain-main/clip.mp4", "male": "s1/explain-main/clip-male.mp4"}
    # a unit that failed to build keeps the bare entry (old graph shape)
    assert "clip" not in asset_entry("s2", None) and "clips" not in asset_entry("s2", None)
