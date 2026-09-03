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
from shared.text_clean import speakable, speakable_ssml, strip_ssml
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


def _render_scene_segment(script_seg: dict, narration: str, audio_path: str | None,
                          audio_secs: float, out_mp4, direction: str,
                          scene_dict: dict | None = None,
                          avatars: dict | None = None) -> bool:
    """One segment through the scene engine. Imports are lazy and everything is
    caught: with VIDEO_ENGINE unset this function never runs, and when it does,
    False (never an exception) hands the segment to the native renderer.

    NOTE: the engine currently lives under spike/ per the prototype
    convention; promotion to a top-level package is the planned cleanup once
    the A/B against the native renderer settles.
    """
    try:
        from spike.scene_engine.director import parse_scene_response
        from spike.scene_engine.encode import FPS, encode_scene
        from spike.scene_engine.raster_assets import load_hand, make_resolver
        from spike.scene_engine.render import SceneRenderer

        scene_dict = scene_dict or script_seg.get("scene")
        starts = script_seg.get("_dialogue_starts")
        if scene_dict and script_seg.get("dialogue") and starts:
            # conversational captions join HERE, once real per-line audio
            # offsets exist — each line's bubble beside ITS speaker, at its
            # measured second
            from spike.scene_engine.whiteboard import (STUDENT_ID, TEACHER_ID,
                                                       narration_stream,
                                                       student_element,
                                                       teacher_element)
            scene_dict = {**scene_dict,
                          "elements": list(scene_dict.get("elements") or []),
                          "actions": list(scene_dict.get("actions") or [])}
            ids = {e.get("id") for e in scene_dict["elements"]}
            if TEACHER_ID not in ids:
                scene_dict["elements"].append(teacher_element(
                    (avatars or {}).get("teacher", "avatar_teacher")))
            if STUDENT_ID not in ids:
                scene_dict["elements"].append(student_element(
                    (avatars or {}).get("student", "avatar_student")))
            nb_els, nb_acts = narration_stream(
                narration, uid=str(script_seg.get("segment_id", "seg")),
                dialogue=script_seg["dialogue"], line_starts=starts,
                total_secs=audio_secs)
            scene_dict["elements"].extend(nb_els)
            scene_dict["actions"] = scene_dict["actions"] + nb_acts
        elif scene_dict and narration and audio_path and not any(
                str(e.get("id", "")).startswith("__nb_")
                for e in (scene_dict.get("elements") or [])):
            # a conversational scene whose dialogue was rejected or whose
            # two-voice TTS failed still gets the SINGLE-voice caption
            # stream — the caption track must never silently vanish
            from spike.scene_engine.whiteboard import narration_stream
            scene_dict = {**scene_dict,
                          "elements": list(scene_dict.get("elements") or []),
                          "actions": list(scene_dict.get("actions") or [])}
            nb_els, nb_acts = narration_stream(
                narration, uid=str(script_seg.get("segment_id", "seg")))
            scene_dict["elements"].extend(nb_els)
            scene_dict["actions"] = scene_dict["actions"] + nb_acts
        scene = parse_scene_response(scene_dict, narration)
        if scene is None:
            return False
        if direction == "rtl":
            scene.direction = "rtl"
        scene.style.pen_mode = "hand"
        scene.style.hand_scale = 0.8
        prompts = {str(k): str(v)
                   for k, v in (script_seg.get("scene_assets") or {}).items()}
        # a scene may carry its OWN asset prompts (auto-sketched objects the
        # narration named) — they exist only on the scene dict
        for k, v in ((scene_dict or {}).get("scene_assets") or {}).items():
            prompts[str(k)] = str(v)
        # avatars resolve everywhere — whiteboard-fallback segments carry no
        # scene_assets map, but the persistent teacher appears on them too
        from spike.scene_engine.whiteboard import AVATAR_PROMPTS
        for k, v in AVATAR_PROMPTS.items():
            prompts.setdefault(k, v)
        words = None
        if audio_path:
            wjson = Path(str(audio_path)).with_suffix(".words.json")
            if wjson.exists():
                try:
                    words = json.loads(wjson.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001 — timing falls back gracefully
                    words = None
        r = SceneRenderer(scene, asset_resolver=make_resolver(prompts),
                          hand_loader=lambda k: load_hand(k))
        r.compile(audio_secs, words=words)
        ok = encode_scene(r.frames(audio_secs, FPS), r.total_secs(audio_secs),
                          audio_path, Path(str(out_mp4)), FPS)
        if ok:
            audit = r.audit()
            if audit["warnings"]:
                script_seg["scene_audit"] = audit["warnings"]
        return ok
    except Exception:  # noqa: BLE001 — scene failure must never kill a segment
        logger.exception("scene engine failed for %s; native fallback",
                         script_seg.get("segment_id"))
        return False


def _scene_flag() -> bool:
    return os.getenv("VIDEO_ENGINE", "").strip().lower() == "scene"


def _synth_dialogue(dialogue: list, mp3, vid_dir, seg_id: str,
                    tts_voice, ffmpeg: str, avatars: dict | None = None) -> list:
    """Per-line two-voice TTS -> one concatenated MP3 + measured line-start
    offsets (seconds). Teacher lines use the lesson's Edge voice; student
    lines an age-matched Edge voice (non-English falls back to the lesson
    voice — age-matched voices are not guaranteed per locale)."""
    from shared.tts.providers import edge as edge_tts
    from shared.tts.registry import default_voice, get_voice
    from spike.scene_engine.whiteboard import student_voice_for_avatar

    v = get_voice(tts_voice) or default_voice()
    if v.provider != "edge":
        v = default_voice()          # dialogue is Edge-only (two voices)
    teach_ref = v.ref
    stud_ref = student_voice_for_avatar(
        (avatars or {}).get("student", "avatar_student"),
        getattr(v, "lang", "en") or "en") or teach_ref
    starts, cursor, parts = [], 0.0, []
    for i, d in enumerate(dialogue):
        # belt to the sanitizer's braces: Edge reads SSML tags ALOUD, and it
        # reads worksheet blanks aloud too ("underscore underscore ...")
        line = " ".join(speakable(str(d.get("line") or "")).split())
        if not line:
            continue
        f = vid_dir / f"{seg_id}_dl{i}.mp3"
        edge_tts.synthesize(
            line, f,
            stud_ref if str(d.get("who")) == "student" else teach_ref)
        starts.append(cursor)
        cursor += _audio_duration(str(f), ffmpeg)
        parts.append(f)
    if not parts:
        raise RuntimeError("dialogue had no speakable lines")
    lst = vid_dir / f"{seg_id}_dl.txt"
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts),
                   encoding="utf-8")
    subprocess.run([ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i",
                    str(lst), "-c", "copy", str(mp3)], capture_output=True)
    if not Path(str(mp3)).exists() or Path(str(mp3)).stat().st_size == 0:
        raise RuntimeError("dialogue concat produced no audio")
    return starts


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
    direction: str = "ltr",
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
    # avatars live on the EpisodeScript — depending on the caller,
    # script_data is either the episode dump itself or the chapter wrapper
    # whose episodes[0] is (the worker path; reading only the top level once
    # cast every prod lesson with the defaults)
    _avatars = (script_data.get("avatars")
                or ((script_data.get("episodes") or [{}])[0] or {}).get("avatars")
                or {})
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
        # strip_ssml: already-saved scripts may carry <break> tags inside `text`
        # (Gemini, pre-sanitizer) — keep the on-frame fallback in parity with the
        # deck notes, which agent5 strips the same way. TTS is separately guarded
        # at the provider boundary in shared/tts.
        text = strip_ssml((script_seg.get("text") or "").strip())
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
            "direction": direction,  # RTL (Arabic) mirrors the animated slide too
        }

        audio_path: str | None = None
        out_mp4 = vid_dir / f"{seg_id}_video.mp4"
        duration = est
        used: str | None = None
        downgraded = False

        # 1. TTS (provider-agnostic: free Edge default; premium ElevenLabs
        # gated). Conversational segments carry speaker-tagged DIALOGUE: each
        # line is synthesized with its speaker's voice and the clips are
        # concatenated — the measured per-line offsets drive the speech
        # bubbles, so sync is exact by construction.
        if text and _scene_flag() and script_seg.get("dialogue"):
            try:
                mp3 = vid_dir / f"{seg_id}_audio.mp3"
                # vid_dir is reused across generations of the same chapter —
                # a words.json from an earlier SINGLE-voice run would feed
                # stale word timings to this dialogue's captions
                mp3.with_suffix(".words.json").unlink(missing_ok=True)
                starts = _synth_dialogue(script_seg["dialogue"], mp3, vid_dir,
                                         seg_id, tts_voice, ffmpeg,
                                         avatars=_avatars)
                script_seg["_dialogue_starts"] = starts
                used = "dialogue-edge"
                audio_path = str(mp3)
                duration = _audio_duration(audio_path, ffmpeg) or est
            except Exception as exc:  # noqa: BLE001
                logger.error("dialogue TTS failed for %s (%s); single-voice "
                             "fallback", seg_id, exc)
                script_seg["dialogue"] = None
                audio_path = None
        if text and audio_path is None:
            mp3 = vid_dir / f"{seg_id}_audio.mp3"
            ssml = script_seg.get("elevenlabs_text") or text
            try:
                seg_report: dict = {}
                # word boundaries feed frame-accurate cue timing (scene engine)
                bnd = mp3.with_suffix(".words.json") if _scene_flag() else None
                # `text` still feeds the deck and the on-frame fallback, where
                # a printed blank is fine; only the SPOKEN copy drops it.
                # The two spoken copies are NOT the same function: the plain
                # one is for a provider that reads tags aloud (Edge), the
                # markup one for a provider that honours them. Passing
                # speakable() for both deleted every <break> before any
                # provider saw it (e898f49, 2026-09-02) — premium pauses were
                # silently lost for a day. speakable_ssml() keeps the tags.
                synthesize(speakable(text), mp3, voice_id=tts_voice, allow_premium=allow_premium, ssml_text=speakable_ssml(ssml), report=seg_report, boundaries_out=bnd)
                used = seg_report.get("used")
                downgraded = bool(seg_report.get("downgraded"))
                audio_path = str(mp3)
                duration = _audio_duration(audio_path, ffmpeg) or est
            except Exception as exc:  # noqa: BLE001 — a TTS hiccup must not kill the video
                logger.error("TTS failed for %s: %s", seg_id, exc)
                audio_path = None

        # 2a. Scene engine (feature-gated): ONE visual language. A planned
        # scene renders as directed; a segment WITHOUT one gets a
        # whiteboard-NATIVE fallback (handwritten heading/points/quiz) — the
        # engine simplifies within the style, it never switches styles. The
        # legacy renderer below is a crash-guard only; its use is recorded on
        # the manifest and fails visual-language validation.
        ok = False
        renderer = "native"
        if os.getenv("VIDEO_ENGINE", "").strip().lower() == "scene":
            scene_dict = script_seg.get("scene")
            attempt = "scene"
            if not scene_dict:
                try:
                    from spike.scene_engine.whiteboard import build_whiteboard_scene
                    scene_dict = build_whiteboard_scene(script_seg, avatars=_avatars)
                    attempt = "whiteboard"
                except Exception:  # noqa: BLE001
                    logger.exception("whiteboard fallback build failed for %s", seg_id)
                    scene_dict = None
            if scene_dict:
                ok = _render_scene_segment(
                    script_seg, text, audio_path,
                    duration if audio_path else 0.0, out_mp4, direction,
                    scene_dict=scene_dict, avatars=_avatars,
                )
                if not ok and attempt == "scene":
                    # a planned scene that fails (parse or render) must step
                    # DOWN THE SAME visual language — an empty compiled scene
                    # once fell straight to the legacy renderer and failed
                    # lesson validation
                    try:
                        from spike.scene_engine.whiteboard import build_whiteboard_scene
                        ok = _render_scene_segment(
                            script_seg, text, audio_path,
                            duration if audio_path else 0.0, out_mp4,
                            direction,
                            scene_dict=build_whiteboard_scene(script_seg, avatars=_avatars),
                            avatars=_avatars)
                        attempt = "whiteboard"
                    except Exception:  # noqa: BLE001
                        logger.exception("whiteboard fallback failed for %s", seg_id)
                if ok:
                    renderer = attempt

        # 2b. Native object animation (paced to the narration) + audio → MP4
        if not ok:
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
                renderer=renderer,
                scene_audit=list(script_seg.get("scene_audit") or []),
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
    for i, r in enumerate(results):
        if not r:
            # RECORD the failure; do not erase it. Skipping the segment
            # removed it from the manifest entirely, so Agent 8's
            # "refuse a lesson with holes" check could never see the hole it
            # was written for — the concat simply had fewer inputs and
            # reported success. A failed segment now travels as a row with no
            # video_path, which is exactly what that check looks for.
            sid = slide_segments[i].get("segment_id", f"s{i + 1:03d}")
            logger.error("segment %s produced no video; recording the gap", sid)
            manifest_segments.append(VideoSegment(
                segment_id=sid, video_path=None, audio_path=None,
                audio_duration_seconds=0.0, renderer="failed",
            ))
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
