"""SK — a second narration track for a prepared segment (founder, 2026-10-27:
the tutor's default look is the man, so a chapter needs a male track; the
clip stays rendered against the female voice it was built with).

The board in a B3 clip is PIXELS cued to the FEMALE word marks: "write w1 at
5475 ms" is baked into the frames. A second voice reads the same narration at
its own pace (Guy is ~6 % slower than Aria, and not uniformly), so simply
swapping the audio would have the board draw a term seconds before or after
the narrator says it. So the second track is ALIGNED to the first, narration
item by narration item (the plan's sentences, which is also the cue
granularity):

  1. both tracks are matched to the narration items by their word marks;
  2. the new track is cut in the pause between items;
  3. each piece is time-stretched (ffmpeg `atempo`, pitch-preserving, bounded
     to ±ATEMPO_* so it never sounds processed) so its spoken span is as long
     as the female item's, and placed so its first word lands where the
     female item's first word does;
  4. the new word marks are remapped through the same transform (the mouth
     follows them in the browser).

Nothing is re-authored or re-rendered: the clip's VIDEO stream is copied
unchanged into `clip-<gender>.mp4` with the new audio muxed in, so the player
keeps one <video> as its clock (drift and pause behave exactly as before) and
only picks a different file for the other look.

Pure functions (`sentence_spans`, `plan_alignment`, `remap_words`, the ffmpeg
argv builders) are unit-tested; the two `run_*` wrappers call ffmpeg.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("sk.voices")

GENDERS = ("female", "male")
# `atempo` factor bounds. >1 speeds a piece up (the new voice was slower),
# <1 slows it down. Beyond ±25 % the stretch is audible as processing.
ATEMPO_MIN, ATEMPO_MAX = 0.85, 1.25
# A piece that would still run into the NEXT item's cue may be squeezed a
# little harder than that (the board drawing the next term over the tail of
# this one is the worse fault); the LAST piece harder still rather than be cut
# by the clip's end.
ATEMPO_FIT_MAX = 1.3
ATEMPO_LAST_MAX = 1.4
# Breath (ms) a squeezed piece leaves before the next item's cue.
MIN_GAP_MS = 60


def _tokens(s: str) -> list[str]:
    # Edge's WordBoundary text drops the micro sign ("µm" arrives as "m"): strip it on both sides.
    return re.findall(r"[a-z0-9]+", s.lower().replace("×", " x ").replace("÷", " / ").replace("µ", "").replace("μ", ""))


@dataclass(frozen=True)
class Piece:
    """One narration item of the new track, cut and placed on the base timeline."""

    item: int
    src_start_ms: int  # cut window in the NEW track's audio
    src_end_ms: int
    dst_start_ms: int  # where the cut piece starts on the BASE (clip) timeline
    atempo: float  # output = input / atempo


@dataclass
class Alignment:
    pieces: list[Piece]
    total_ms: int
    matched_items: int
    items: int
    fallback: Optional[str]  # None when the item match held on both tracks
    max_abs_offset_ms: int  # worst first-word landing error after placement (0 when every item lands)
    overflow_ms: int  # how far the last WORD would run past the clip (the audio is cut there by -t) — 0 is the goal

    def report(self) -> dict:
        d = asdict(self)
        d["pieces"] = len(self.pieces)
        d["atempo"] = {"min": round(min((p.atempo for p in self.pieces), default=1.0), 3), "max": round(max((p.atempo for p in self.pieces), default=1.0), 3)}
        return d


def sentence_spans(words: list[dict], items: list[str]) -> Optional[list[tuple[int, int]]]:
    """(first word index, last word index) of each narration item in `words`
    (normalised marks: {t0, t1, text}), matched token by token in order.
    None when the marks do not spell the items out exactly — the caller then
    falls back to one whole-track stretch instead of guessing cut points."""
    toks = [_tokens(w.get("text", "")) for w in words]
    spans: list[tuple[int, int]] = []
    wi = 0
    for item in items:
        want = _tokens(item)
        if not want:
            return None
        # skip marks that carry no tokens (punctuation-only events)
        while wi < len(toks) and not toks[wi]:
            wi += 1
        first = wi
        k = 0
        while k < len(want):
            if wi >= len(toks):
                return None
            got = toks[wi]
            if not got:
                wi += 1
                continue
            if want[k:k + len(got)] != got:
                return None
            k += len(got)
            wi += 1
        spans.append((first, wi - 1))
    return spans


def plan_alignment(base_words: list[dict], new_words: list[dict], items: list[str], *, clip_ms: int, new_audio_ms: int) -> Alignment:
    """Where each piece of the new track is cut, stretched and placed so that
    its narration items land on the base track's. Pure."""
    n = len(items)
    base_spans = sentence_spans(base_words, items) if base_words else None
    new_spans = sentence_spans(new_words, items) if new_words else None
    if not base_spans or not new_spans or n == 0:
        why = "no narration items" if n == 0 else ("base marks do not match the narration" if not base_spans else "new marks do not match the narration")
        # One piece, stretched so the new track ends where the base audio ends
        # (its words then land within a few hundred ms of the base's).
        base_end = base_words[-1]["t1"] if base_words else clip_ms
        new_end = new_words[-1]["t1"] if new_words else new_audio_ms
        f = _clamp((new_end / base_end) if base_end > 0 else 1.0, ATEMPO_MIN, ATEMPO_LAST_MAX)
        out_len = new_audio_ms / f
        overflow = max(0, round(out_len - clip_ms))
        return Alignment([Piece(0, 0, new_audio_ms, 0, round(f, 4))], clip_ms, 0, n, why, 0, overflow)

    pieces: list[Piece] = []
    prev_spoken_end = -float(MIN_GAP_MS)
    worst = 0
    for k in range(n):
        bf, bl = base_spans[k]
        nf, nl = new_spans[k]
        # Cut the new track in the pause between items (midpoint of the gap).
        src_start = 0 if k == 0 else round((new_words[new_spans[k - 1][1]]["t1"] + new_words[nf]["t0"]) / 2)
        src_end = new_audio_ms if k == n - 1 else round((new_words[nl]["t1"] + new_words[new_spans[k + 1][0]]["t0"]) / 2)
        # Match the spoken span of this item (first word start → last word end).
        v_new = max(1, new_words[nl]["t1"] - new_words[nf]["t0"])
        v_base = max(1, base_words[bl]["t1"] - base_words[bf]["t0"])
        f = _clamp(v_new / v_base, ATEMPO_MIN, ATEMPO_MAX)
        # The piece may not run into the next item's base start; squeeze a
        # little more if it would, up to the last-piece bound.
        room = (base_words[base_spans[k + 1][0]]["t0"] if k < n - 1 else clip_ms) - base_words[bf]["t0"] - MIN_GAP_MS
        spoken_out = (new_words[nl]["t1"] - new_words[nf]["t0"]) / f
        if room > 0 and spoken_out > room:
            f = _clamp((new_words[nl]["t1"] - new_words[nf]["t0"]) / room, f, ATEMPO_LAST_MAX if k == n - 1 else ATEMPO_FIT_MAX)
        lead = (new_words[nf]["t0"] - src_start) / f  # silence before the first word, after the stretch
        if k == 0 and lead > base_words[bf]["t0"]:
            # The take's opening silence, slowed, would push the first word
            # late: trim the opening instead so it lands on the base's.
            src_start = max(0, round(new_words[nf]["t0"] - base_words[bf]["t0"] * f))
            lead = (new_words[nf]["t0"] - src_start) / f
        want_start = base_words[bf]["t0"] - lead
        # The floor is the previous item's last WORD (plus a breath), not the
        # previous piece's end: the halves of a pause the cut split may overlap
        # (silence over silence in the mix), but an item that ran long starts
        # the next one late, never on top of its words.
        dst_start = max(prev_spoken_end + MIN_GAP_MS - lead, want_start, 0.0)
        worst = max(worst, round(dst_start - want_start))
        pieces.append(Piece(k, src_start, src_end, round(dst_start), round(f, 4)))
        prev_spoken_end = dst_start + (new_words[nl]["t1"] - src_start) / f
    overflow = max(0, round(prev_spoken_end - clip_ms))
    return Alignment(pieces, clip_ms, n, n, None, worst, overflow)


def remap_words(new_words: list[dict], al: Alignment) -> list[dict]:
    """The new track's word marks on the base timeline (each word through its
    piece's cut, stretch and placement). Words outside every piece are dropped."""
    out: list[dict] = []
    for w in new_words:
        p = next((p for p in al.pieces if p.src_start_ms <= w["t0"] < p.src_end_ms), None)
        if p is None:
            continue
        t0 = p.dst_start_ms + (w["t0"] - p.src_start_ms) / p.atempo
        t1 = p.dst_start_ms + (min(w["t1"], p.src_end_ms) - p.src_start_ms) / p.atempo
        out.append({"i": len(out), "t0": round(t0), "t1": max(round(t0) + 1, round(t1)), "text": w["text"]})
    return out


# ── ffmpeg ──────────────────────────────────────────────────────────────────


# Edge speaks 24 kHz mono; every piece and the anchor are brought to that so
# the mix never has to guess a common format.
_FMT = "aformat=sample_rates=24000:channel_layouts=mono"


def align_filter(al: Alignment) -> str:
    """The filter_complex that cuts, stretches, places and pads the pieces.
    A silent anchor the length of the clip is the first mix input: the output
    then always starts at 0 and runs the whole clip (a mix whose first sound
    began a few ms in gave the mp3 muxer a wild first timestamp and no audio)."""
    chains = [f"aevalsrc=0:s=24000:c=mono:d={al.total_ms / 1000:.3f}[z]"]
    for p in al.pieces:
        chain = f"[0:a]atrim=start={p.src_start_ms / 1000:.3f}:end={p.src_end_ms / 1000:.3f},asetpts=PTS-STARTPTS"
        if abs(p.atempo - 1.0) >= 0.0005:
            chain += f",atempo={p.atempo:.4f}"
        chain += f",{_FMT},adelay={p.dst_start_ms}:all=1[p{p.item}]"
        chains.append(chain)
    ins = "[z]" + "".join(f"[p{p.item}]" for p in al.pieces)
    chains.append(f"{ins}amix=inputs={len(al.pieces) + 1}:normalize=0:dropout_transition=0,apad=whole_dur={al.total_ms / 1000:.3f}[out]")
    return ";".join(chains)


def align_args(src_mp3: Path, out_mp3: Path, al: Alignment, ffmpeg: str = "ffmpeg") -> list[str]:
    return [ffmpeg, "-y", "-i", str(src_mp3), "-filter_complex", align_filter(al), "-map", "[out]",
            "-t", f"{al.total_ms / 1000:.3f}", "-c:a", "libmp3lame", "-q:a", "4", "-ar", "24000", "-ac", "1", str(out_mp3)]


def mux_args(clip: Path, audio: Path, out_clip: Path, total_ms: int, ffmpeg: str = "ffmpeg") -> list[str]:
    """The base clip's video stream, copied, with the aligned track muxed in
    (same audio settings as the scene encoder)."""
    return [ffmpeg, "-y", "-i", str(clip), "-i", str(audio), "-map", "0:v", "-map", "1:a", "-c:v", "copy",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2", "-movflags", "+faststart", "-t", f"{total_ms / 1000:.2f}", str(out_clip)]


def _run(cmd: list[str], what: str) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"{what}: ffmpeg exited {r.returncode}: {r.stderr[-1500:]}")


def mp3_duration_ms(path: Path, words: list[dict]) -> int:
    try:
        from mutagen.mp3 import MP3  # type: ignore

        return round(MP3(str(path)).info.length * 1000)
    except Exception:  # noqa: BLE001 — mutagen is optional; ffprobe is not on PATH in the venv
        pass
    try:
        from spike.scene_engine.encode import ffmpeg_exe

        r = subprocess.run([ffmpeg_exe(), "-i", str(path), "-f", "null", "-"], capture_output=True, text=True, errors="replace")
        m = re.findall(r"time=(\d+):(\d+):(\d+)\.(\d+)", r.stderr)
        if m:
            h, mi, s, frac = m[-1]
            return round((int(h) * 3600 + int(mi) * 60 + int(s)) * 1000 + int(frac.ljust(3, "0")[:3]))
    except Exception:  # noqa: BLE001
        pass
    return (words[-1]["t1"] + 600) if words else 1000


def add_voice_track(seg_dir: Path, seg: dict, voice_id: str, gender: str, *, synthesize, norm_words, dry: bool = False) -> dict:
    """Synthesize `seg["narration"]` with `voice_id`, align it to the segment's
    default marks, mux it onto the clip's video, and return the track entry for
    `segment.json["voices"][gender]`. Files written beside the clip:
    audio-<gender>.mp3, audio-<gender>.words.json, audio-<gender>.raw.mp3 (the
    unaligned take, kept for inspection) and clip-<gender>.mp4."""
    if gender not in GENDERS:
        raise ValueError(f"gender must be one of {GENDERS}, not {gender!r}")
    narration = seg["narration"]
    items = list(seg.get("narration_sentences") or [narration])
    base_words = seg["marks"]["words"]
    clip_ms = int(seg["clip"]["duration_ms"])

    raw_mp3 = seg_dir / f"audio-{gender}.raw.mp3"
    raw_words = seg_dir / f"audio-{gender}.raw.words.json"
    rep: dict = {}
    synthesize(narration, raw_mp3, voice_id, boundaries_out=raw_words, report=rep)
    new_words = norm_words(json.loads(raw_words.read_text(encoding="utf-8")))
    new_audio_ms = mp3_duration_ms(raw_mp3, new_words)

    al = plan_alignment(base_words, new_words, items, clip_ms=clip_ms, new_audio_ms=new_audio_ms)
    if al.fallback:
        log.warning("%s: %s voice %s — %s; one whole-track stretch instead", seg.get("segment_id"), gender, voice_id, al.fallback)
    if al.overflow_ms:
        log.warning("%s: %s track runs %d ms past the clip and is cut", seg.get("segment_id"), gender, al.overflow_ms)
    words = remap_words(new_words, al)
    (seg_dir / f"audio-{gender}.words.json").write_text(json.dumps(words, ensure_ascii=False), encoding="utf-8")

    out_mp3 = seg_dir / f"audio-{gender}.mp3"
    out_clip = seg_dir / f"clip-{gender}.mp4"
    if not dry:
        from spike.scene_engine.encode import ffmpeg_exe

        ff = ffmpeg_exe()
        _run(align_args(raw_mp3, out_mp3, al, ff), f"align {gender} track")
        clip = seg_dir / seg["clip"]["uri"]
        if clip.exists():
            _run(mux_args(clip, out_mp3, out_clip, clip_ms, ff), f"mux {gender} clip")
    try:
        from shared.tts.registry import get_voice

        voice_ref = getattr(get_voice(voice_id), "ref", None)  # the provider's name, e.g. en-US-GuyNeural
    except Exception:  # noqa: BLE001
        voice_ref = None
    return {
        "voice": voice_id, "voice_ref": voice_ref, "gender": gender,
        "audio": {"uri": out_mp3.name, "duration_ms": clip_ms},
        "clip": {"uri": out_clip.name, "duration_ms": clip_ms},
        "marks": {"words": words, "visemes": []},
        "alignment": {**al.report(), "raw_audio_ms": new_audio_ms, "raw": raw_mp3.name},
        "tts": rep,
    }


def default_track(seg: dict) -> dict:
    """The segment's own (rendered) narration as a `voices` entry."""
    return {"voice": seg["voice"], "gender": seg.get("voice_gender", "female"), "audio": dict(seg["audio"]), "clip": dict(seg["clip"]), "marks": {"words": list(seg["marks"]["words"]), "visemes": []}}


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))
