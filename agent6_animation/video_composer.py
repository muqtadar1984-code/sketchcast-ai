"""Agent 6: Video Composer — narration + native object animation → per-segment MP4.

Freemium pipeline (robust, low-memory, free):
  1. Edge TTS turns each segment's narration into an MP3.
  2. The native renderer (``native_render``) animates the slide's objects writing
     on — title, divider, bullets — paced to fit the narration, then freezes the
     finished slide for the remainder and muxes the audio in (libx264 / aac,
     1280x720, 24fps).

This replaces the flat PNG-loop (which itself replaced the OOM-prone cv2
SpeedPaint + moviepy mux): the slide now draws itself on screen, with perfect
text fidelity, deterministically and for free. Every segment ends up h264/aac
and uniform, so Agent 8's ffmpeg concat can stream-copy them with audio intact.

Entry point
-----------
compose_episode_videos()   Streamlit / worker in-process entry point.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .models import VideoManifest, VideoSegment
from .native_render import render_native_segment
from shared.tts import synthesize

from agent5_slides.theme import concepts_for_slides

logger = logging.getLogger(__name__)

# Dev-only on-frame label (segment type + index). OFF in production so no debug
# text is ever burned into shipped video. Set DEBUG_VIDEO=1 to enable locally.
DEBUG_VIDEO = os.getenv("DEBUG_VIDEO", "").strip().lower() in ("1", "true", "yes", "on")

# Segments are independent, so they render in parallel (the old sequential loop
# summed every segment's TTS + encode into a 10-30 min wall-clock). Cap the pool;
# RENDER_WORKERS overrides — set it to 1 to force the old sequential behaviour
# without a redeploy.
try:
    _MAX_RENDER_WORKERS = max(1, int(os.getenv("RENDER_WORKERS", "4")))
except ValueError:
    _MAX_RENDER_WORKERS = 4

STORAGE_DIR = Path(__file__).resolve().parent.parent / "storage"
VIDEO_DIR = STORAGE_DIR / "video_segments"

_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)")


def _ffmpeg_exe() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def _audio_duration(audio_path: str, ffmpeg: str) -> float:
    """Read an audio file's duration (seconds) by parsing ffmpeg output."""
    proc = subprocess.run([ffmpeg, "-i", audio_path], capture_output=True, text=True)
    m = _DUR_RE.search(proc.stderr or "")
    if m:
        h, mnt, s = m.groups()
        return int(h) * 3600 + int(mnt) * 60 + float(s)
    return 0.0


def compose_episode_videos(
    script_data: dict,
    slide_manifest: dict,
    progress_callback: Optional[Callable] = None,
    branding: Optional[dict] = None,
    tts_voice: Optional[str] = None,
    allow_premium: bool = False,
    voice_report: Optional[dict] = None,
) -> VideoManifest:
    """Generate a narrated, object-animated MP4 per segment.

    ``branding`` = {accent_rgb, logo_path} applies the school's colour/logo to the
    animated slide (must match what Agent 5 baked into the deck). Any field may be
    None, in which case the default Scholar style is used.

    ``tts_voice`` is a voice-registry id (default = the free Edge voice);
    ``allow_premium`` is the server-resolved gate for ElevenLabs voices. The TTS
    layer enforces the gate + spend cap and falls back to the free voice.
    """
    _b = branding or {}
    _accent = _b.get("accent_rgb")
    _logo = _b.get("logo_path")

    episodes = script_data.get("episodes", [script_data])
    episode = episodes[0] if isinstance(episodes, list) and episodes else script_data

    book_id = episode.get("book_id", script_data.get("book_id", "unknown"))
    chapter_num = episode.get("chapter_num", script_data.get("chapter_num", 0))
    episode_num = episode.get("episode_num", script_data.get("episode_num", 1))
    script_id = episode.get("script_id", script_data.get("script_id", str(uuid.uuid4())))
    episode_title = episode.get("episode_title") or "SketchCast AI"

    vid_dir = VIDEO_DIR / book_id / f"chapter_{chapter_num}"
    vid_dir.mkdir(parents=True, exist_ok=True)

    slide_segments = slide_manifest.get("segments", [])
    script_segments = {seg["segment_id"]: seg for seg in episode.get("segments", [])}

    # Art-direction: one coherent, non-repeating concept set for the whole episode,
    # from the same heading sequence the deck uses → deck + video illustrations match.
    seg_concepts = concepts_for_slides([
        (script_segments.get(ss["segment_id"], {}).get("slide_heading") or "").strip() or episode_title
        for ss in slide_segments
    ])

    ffmpeg = _ffmpeg_exe()
    manifest_segments: list[VideoSegment] = []
    total = len(slide_segments)
    _voices_used: set[str | None] = set()
    _any_downgrade = False

    def _render_one(i: int, slide_seg: dict) -> Optional[dict]:
        """Build ONE segment (TTS + native animation → MP4). Returns its result
        instead of mutating shared state, so the loop is safe to run across
        threads and the caller aggregates deterministically by index."""
        seg_id = slide_seg["segment_id"]
        script_seg = script_segments.get(seg_id, {})
        text = (script_seg.get("text") or "").strip()
        seg_type = script_seg.get("type", slide_seg.get("type", "explore"))
        # Clean string label (e.g. "hook"), never a SegmentType repr ("SegmentType.hook").
        seg_label = getattr(seg_type, "value", None) or str(seg_type)
        est = float(script_seg.get("estimated_duration_seconds", 8) or 8)

        # Build the slide spec from the script (same inputs Agent 5 lays out), so
        # the animated slide matches the downloadable deck.
        heading = (script_seg.get("slide_heading") or "").strip() or episode_title
        points = [str(p).strip() for p in (script_seg.get("slide_points") or []) if str(p).strip()]
        spec = {
            "heading": heading,
            "points": points,
            # Footer is a dev label only — empty in production (no debug overlay).
            "footer": f"{seg_label} · {i + 1}/{total}" if DEBUG_VIDEO else "",
            "context": episode_title if heading != episode_title else "",
            "fallback": text,
            "visual": script_seg.get("slide_visual"),
            "number": i + 1,
            "concept": seg_concepts[i],
        }

        audio_path: str | None = None
        out_mp4 = vid_dir / f"{seg_id}_video.mp4"
        duration = est
        used: str | None = None
        downgraded = False

        # 1. TTS (provider-agnostic: free Edge default; premium ElevenLabs gated)
        if text:
            mp3 = vid_dir / f"{seg_id}_audio.mp3"
            ssml = script_seg.get("elevenlabs_text") or text
            try:
                seg_report: dict = {}
                synthesize(text, mp3, voice_id=tts_voice, allow_premium=allow_premium, ssml_text=ssml, report=seg_report)
                used = seg_report.get("used")
                downgraded = bool(seg_report.get("downgraded"))
                audio_path = str(mp3)
                duration = _audio_duration(audio_path, ffmpeg) or est
            except Exception as exc:  # noqa: BLE001 — a TTS hiccup must not kill the video
                logger.error("TTS failed for %s: %s", seg_id, exc)
                audio_path = None

        # 2. Native object animation (paced to the narration) + audio → MP4
        ok = render_native_segment(
            spec, audio_path, str(out_mp4), ffmpeg,
            audio_secs=duration if audio_path else 0.0,
            accent=_accent, logo_path=_logo,
        )
        if not ok:
            logger.error("native renderer failed to build segment %s", seg_id)
            return None

        return {
            "index": i,
            "used": used,
            "downgraded": downgraded,
            "duration": duration,
            "segment": VideoSegment(
                segment_id=seg_id,
                type=seg_label,
                audio_path=audio_path,
                video_path=str(out_mp4),
                slide_image_path=slide_seg.get("slide_image_path"),
                audio_duration_seconds=round(duration, 2),
                visual_action=slide_seg.get("visual_action", "GHOST_ONLY"),
            ),
        }

    # Segments are independent (own audio file + own tmp render dir), so build them
    # in parallel — TTS is network I/O and the renderer shells out to ffmpeg, so
    # threads overlap well and the wall-clock drops from sum-of-segments toward
    # slowest-segment. Capped by RENDER_WORKERS (default 4; 1 = old sequential).
    workers = max(1, min(os.cpu_count() or 2, _MAX_RENDER_WORKERS))
    results: list[Optional[dict]] = [None] * total
    if workers > 1 and total > 1:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(_render_one, i, ss) for i, ss in enumerate(slide_segments)]
            done = 0
            for fut in as_completed(futures):
                done += 1
                try:
                    r = fut.result()
                except Exception as exc:  # noqa: BLE001 — one bad segment must not sink the rest
                    logger.error("segment render crashed: %s", exc)
                    r = None
                if r is not None:
                    results[r["index"]] = r
                if progress_callback:
                    progress_callback(done, total, "segment")
    else:
        for i, ss in enumerate(slide_segments):
            if progress_callback:
                progress_callback(i, total, ss["segment_id"])
            results[i] = _render_one(i, ss)

    # Aggregate in segment order — Agent 8's concat needs them ordered.
    total_duration = 0.0
    for r in results:
        if not r:
            continue
        total_duration += r["duration"]
        _voices_used.add(r["used"])
        if r["downgraded"]:
            _any_downgrade = True
        manifest_segments.append(r["segment"])

    if progress_callback:
        progress_callback(total, total, "done")

    vid_count = sum(1 for s in manifest_segments if s.video_path)
    # Report which voice(s) actually rendered, so the caller can persist a silent
    # premium→free downgrade (whole video may have degraded, or only some segments).
    if voice_report is not None:
        used = sorted(v for v in _voices_used if v)
        voice_report.update({"requested": tts_voice, "used": used, "downgraded": _any_downgrade})

    manifest = VideoManifest(
        manifest_id=str(uuid.uuid4()),
        script_id=script_id,
        book_id=book_id,
        chapter_num=chapter_num,
        episode_num=episode_num,
        generated_at=datetime.now(timezone.utc).isoformat(),
        total_segments=total,
        video_segments_count=vid_count,
        total_duration_seconds=round(total_duration, 2),
        segments=manifest_segments,
    )

    manifest_path = vid_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest.model_dump(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    logger.info("Video manifest: %d segments, %d built, %.1fs", total, vid_count, total_duration)
    return manifest


def load_manifest(book_id: str, chapter_num: int) -> Optional[dict]:
    """Load a saved video manifest from disk."""
    path = VIDEO_DIR / book_id / f"chapter_{chapter_num}" / "manifest.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)
