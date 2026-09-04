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
import multiprocessing
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import (CancelledError, ProcessPoolExecutor, ThreadPoolExecutor,
                                as_completed)
from concurrent.futures.process import BrokenProcessPool
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

# Rasterization in CHILD PROCESSES. A segment thread does TTS (network) and
# then rasterizes + encodes (pure CPU, held by the GIL); with
# RENDER_PROCESSES > 0 that second half is submitted to a process pool and the
# thread blocks on the result. 0 (default) keeps everything in-process — the
# path every test exercises, and the rollback (delete the variable). The
# number of segments rendering at once is min(threads, processes), so set
# RENDER_WORKERS alongside it. One pool per worker PROCESS, shared by every
# WORKER_CONCURRENCY job thread: total CPU is bounded and lessons queue
# behind each other on it (intended).
try:
    _RENDER_PROCESSES = max(0, int(os.getenv("RENDER_PROCESSES", "0")))
except ValueError:
    _RENDER_PROCESSES = 0

_POOL: Optional[ProcessPoolExecutor] = None
_POOL_LOCK = threading.Lock()


def _cpus() -> int:
    """CPUs this process may use. os.cpu_count() reports the HOST inside a
    container; the affinity mask is the one thing that honours a cpuset."""
    try:
        return len(os.sched_getaffinity(0))  # type: ignore[attr-defined]
    except AttributeError:
        return os.cpu_count() or 2


def _pool() -> ProcessPoolExecutor:
    """The module-global render pool, created on first use. The start method
    is an EXPLICIT spawn on every platform: this process holds worker-loop
    threads, the segment thread pool and the daemon tier thread, so a fork
    would copy locks mid-hold. The child never generates assets (the
    limiters, image budget and spend labels are parent-only)."""
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = ProcessPoolExecutor(
                max_workers=max(1, min(_RENDER_PROCESSES, _cpus())),
                mp_context=multiprocessing.get_context("spawn"),
                max_tasks_per_child=32)
        return _POOL


def _reset_pool(broken: object) -> None:
    """Retire THE executor that broke (an OOM-killed child poisons every
    pending future and the executor is unusable afterwards); the next render
    recreates one. Bound to the instance on purpose: a dead child fails every
    segment thread waiting on that pool, and each of them calls this — the
    first one drops the pool and a later thread's `_pool()` has already built
    a healthy replacement. A reset that cleared *whatever is current* would
    shut that replacement down (cancelling its futures) and fail a segment
    that had nothing wrong with it. Shutting `broken` down again after
    another thread already did is a no-op."""
    global _POOL
    if broken is None:
        return
    with _POOL_LOCK:
        if _POOL is broken:
            _POOL = None
    try:
        broken.shutdown(wait=False, cancel_futures=True)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — a broken executor may refuse
        pass

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
        if _RENDER_PROCESSES > 0:
            # Rasterize in a child process. Generation, annotation and spend
            # attribution happen HERE first, on the thread that owns the
            # segment: warm every illustration the renderer will bind (the
            # hand is warmed once per lesson before the pool), so the child's
            # cache-only resolver finds everything on disk. On a cache hit the
            # child can still ask vision for region names when the meta lacks
            # them — the warm-up with the same prompt writes them; if that
            # write failed (swallowed OSError) it is logged and accepted.
            warm = make_resolver(prompts)
            for e in scene_dict.get("elements") or []:
                if isinstance(e, dict) and e.get("type") == "illustration" and e.get("asset"):
                    warm(str(e["asset"]))
            payload = {"scene": scene_dict, "narration": narration, "prompts": prompts,
                       "words": words, "audio_path": str(audio_path) if audio_path else None,
                       "audio_secs": float(audio_secs), "out_mp4": str(out_mp4),
                       "direction": direction}
            from spike.scene_engine.segment_worker import render_segment_in_child
            ex: Optional[ProcessPoolExecutor] = None
            try:
                ex = _pool()
                try:
                    fut = ex.submit(render_segment_in_child, payload)
                except RuntimeError:
                    # "cannot schedule new futures after shutdown": another
                    # thread retired this executor between _pool() and
                    # submit(); take the replacement it left behind
                    _reset_pool(ex)
                    ex = _pool()
                    fut = ex.submit(render_segment_in_child, payload)
                ok, warnings = fut.result()
            except (BrokenProcessPool, CancelledError):
                # an OOM-killed child breaks the whole pool (every pending
                # future fails; a future a reset cancelled is CancelledError):
                # retire THAT executor and finish THIS segment in-process below
                logger.exception("render pool broken; rendering %s in-process",
                                 script_seg.get("segment_id"))
                _reset_pool(ex)
            else:
                if ok and warnings:
                    script_seg["scene_audit"] = warnings
                return ok
        # allow_generate=False: the hand sprite is warmed ONCE per lesson
        # before the segment pool (compose_episode_videos). Each segment used
        # to generate it on its own thread on a fresh container, so under an
        # image-model 429 the first segment drew with the plain vector pen and
        # the rest with the hand — one lesson, two pens (founder, 2026-09-04).
        r = SceneRenderer(scene, asset_resolver=make_resolver(prompts),
                          hand_loader=lambda k: load_hand(k, allow_generate=False))
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


_PAID_PROVIDERS = ("elevenlabs", "google")


def _fold_stats(into: dict, r: dict) -> None:
    """Sum one synthesize() report into a stats dict.

    `chars` counts ONLY what a PAID provider billed: a segment that fell back
    to Edge is free, and its characters must not be booked to the paid
    provider's ledger (they were — every fallback in a mixed lesson was
    billed as premium). Free characters are kept apart as `free_chars`. The
    provider's own `chars` inside `stats` is the same figure as the report's
    and is skipped — that duplicate was the double count on the dialogue
    path. Booleans are ints in Python and are not summed."""
    for k, v in (r.get("stats") or {}).items():
        if k == "chars" or isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        into[k] = into.get(k, 0) + v
    billed = int(r.get("chars") or 0)
    key = "chars" if r.get("provider") in _PAID_PROVIDERS else "free_chars"
    into[key] = into.get(key, 0) + billed


def _same_prose(markup_copy: str, plain_copy: str) -> bool:
    """Do the two spoken copies carry the same words? The markup copy is what
    a premium provider speaks — and what words.json is timed from — while
    the captions and cue phrases are built from the plain copy. If the model
    paraphrased between them, timing words the captions never show cues
    nothing, so the caller speaks the plain copy instead."""
    from shared.tts.chunks import words_of
    a = [w.lower() for w in words_of(speakable(strip_ssml(markup_copy or "")))]
    b = [w.lower() for w in words_of(speakable(plain_copy or ""))]
    return a == b


def _synth_dialogue(dialogue: list, mp3, vid_dir, seg_id: str,
                    tts_voice, ffmpeg: str, avatars: dict | None = None,
                    lang: str | None = None, allow_premium: bool = False,
                    report: dict | None = None) -> list:
    """Per-line two-voice TTS -> one concatenated MP3 + measured line-start
    offsets (seconds). Teacher lines use the lesson's Edge voice; student
    lines an age-matched Edge voice (non-English falls back to the lesson
    voice — age-matched voices are not guaranteed per locale).

    Dialogue is still Edge-only (Phase 1b routes it through the premium
    provider). Until then a non-Edge pick falls to the free voice for the
    LESSON LANGUAGE — it used to fall to English Aria, so an Arabic teacher
    who picked a premium voice got an English conversation."""
    from shared.tts import resolve_voice
    from shared.tts.providers import edge as edge_tts
    from shared.tts.registry import default_voice, default_voice_id_for, get_voice
    from spike.scene_engine.whiteboard import student_voice_for_avatar

    free = get_voice(default_voice_id_for(lang)) or default_voice()
    # The TEACHER's lines go through synthesize() — gate, cap, report, and the
    # premium provider when the account has one. Conversational is the app's
    # default from grade 10, so leaving dialogue Edge-only would have made
    # premium meaningless for every secondary-school lesson. The STUDENT's
    # lines stay on the free age-matched Edge voice: no provider offers an
    # age-banded child voice, and the student is a foil, not the narrator.
    teacher = resolve_voice(tts_voice, allow_premium, lang=lang)
    stud_ref = student_voice_for_avatar(
        (avatars or {}).get("student", "avatar_student"),
        getattr(free, "lang", "en") or "en") or free.ref
    starts, cursor, parts = [], 0.0, []
    used_voice, downgraded = teacher.voice_id, False
    reasons: set[str] = set()
    for i, d in enumerate(dialogue):
        # belt to the sanitizer's braces: Edge reads SSML tags ALOUD, and it
        # reads worksheet blanks aloud too ("underscore underscore ...")
        line = " ".join(speakable(str(d.get("line") or "")).split())
        if not line:
            continue
        f = vid_dir / f"{seg_id}_dl{i}.mp3"
        if str(d.get("who")) == "student":
            edge_tts.synthesize(line, f, stud_ref)
        else:
            r: dict = {}
            # The REQUESTED id goes in, not the resolved one: resolving here
            # first made a gate downgrade look like a free pick, so the row
            # never said the lesson was downgraded.
            synthesize(line, f, voice_id=tts_voice or teacher.voice_id, allow_premium=allow_premium,
                       report=r, lang=lang)
            used_voice = r.get("used") or used_voice
            downgraded = downgraded or bool(r.get("downgraded"))
            if r.get("reason"):
                reasons.add(str(r["reason"]))
            if report is not None:
                _fold_stats(report, r)
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
    if report is not None:
        report.update({"used": used_voice, "downgraded": downgraded,
                       "reasons": sorted(reasons)})
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
    lang: Optional[str] = None,
) -> VideoManifest:
    """Generate a narrated, object-animated MP4 per segment.

    ``lang`` is the lesson language; every TTS fallback (gate, cap, provider
    failure) lands on that language's free voice rather than English Aria.

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
    _t_compose = time.perf_counter()

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
    _stats: dict = {}
    _preflight_downgrade = False
    _reasons: set[str] = set()

    # PREMIUM PRE-FLIGHT. Segments are synthesized on a thread pool, and each
    # falls back to the free voice on its own — so a provider outage on
    # segment 6 of 12 used to produce a lesson that alternates a premium
    # voice with the free one, and with per-language voices that means an
    # Arabic teacher giving way to English Aria mid-lesson. One short probe
    # before the pool decides for the WHOLE lesson: if premium cannot be
    # rendered now, every segment is free, consistently, and the row says
    # why. Costs one sentence.
    if allow_premium and tts_voice:
        from shared.tts import resolve_voice
        _probe_voice = resolve_voice(tts_voice, allow_premium, lang=lang)
        if _probe_voice.tier == "premium":
            _r: dict = {}
            _probe = vid_dir / "_preflight.mp3"
            try:
                synthesize("Let us begin.", _probe,
                           voice_id=tts_voice, allow_premium=allow_premium,
                           report=_r, lang=lang)
            except Exception as exc:  # noqa: BLE001
                _r = {"downgraded": True, "error": str(exc), "reason": "provider_error"}
            finally:
                try:
                    _probe.unlink(missing_ok=True)  # one sentence, not an orphan per lesson
                except OSError:
                    pass
            if _r.get("downgraded"):
                logger.warning("premium pre-flight failed for %r (%s) — the whole "
                               "lesson renders on the free voice", tts_voice,
                               _r.get("error") or "provider downgrade")
                allow_premium = False
                _preflight_downgrade = True
                _reasons.add(f"preflight:{_r.get('reason') or 'downgrade'}")

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
        seg_stats: dict = {}
        reasons: list[str] = []

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
                dlg_report: dict = {}
                starts = _synth_dialogue(script_seg["dialogue"], mp3, vid_dir,
                                         seg_id, tts_voice, ffmpeg,
                                         avatars=_avatars, lang=lang,
                                         allow_premium=allow_premium, report=dlg_report)
                script_seg["_dialogue_starts"] = starts
                used = dlg_report.get("used") or "dialogue-edge"
                downgraded = bool(dlg_report.get("downgraded"))
                seg_stats = {k: v for k, v in dlg_report.items()
                             if isinstance(v, (int, float)) and not isinstance(v, bool)}
                reasons = list(dlg_report.get("reasons") or [])
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
            if ssml is not text and not _same_prose(ssml, text):
                logger.warning("segment %s: the markup copy paraphrases the caption "
                               "text; speaking the caption copy so words.json matches "
                               "the captions", seg_id)
                ssml = text
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
                synthesize(speakable(text), mp3, voice_id=tts_voice, allow_premium=allow_premium, ssml_text=speakable_ssml(ssml), report=seg_report, boundaries_out=bnd, lang=lang)
                used = seg_report.get("used")
                downgraded = bool(seg_report.get("downgraded"))
                seg_stats = {}
                _fold_stats(seg_stats, seg_report)
                if seg_report.get("reason"):
                    reasons = [str(seg_report["reason"])]
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
            "stats": seg_stats,
            "reasons": reasons,
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
    # The drawing hand is one sprite for the whole lesson: warm it here, once,
    # before any segment renders, so every renderer reads the same cached file
    # (or none — consistently). Per-segment loaders never generate.
    if _scene_flag():
        try:
            from spike.scene_engine.raster_assets import load_hand as _warm_hand
            if _warm_hand() is None:
                logger.warning("hand sprite unavailable for this lesson; every segment draws with the vector pen")
        except Exception as exc:  # noqa: BLE001
            logger.warning("hand sprite warm-up failed: %s", exc)

    # with a process pool the thread count must not cap the pool: a thread
    # blocks on its child's future, so simultaneous renders = min(threads,
    # processes)
    workers = max(1, min(_cpus(), max(_MAX_RENDER_WORKERS, _RENDER_PROCESSES)))
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
        for k, v in (r.get("stats") or {}).items():
            _stats[k] = _stats.get(k, 0) + v
        _reasons.update(r.get("reasons") or [])
        manifest_segments.append(r["segment"])

    if progress_callback:
        progress_callback(total, total, "done")

    vid_count = sum(1 for s in manifest_segments if s.video_path)
    logger.info("compose: %d segments, %d rendered, %.1f s wall (workers=%d, processes=%d)",
                total, vid_count, time.perf_counter() - _t_compose, workers,
                _RENDER_PROCESSES)
    # Report which voice(s) actually rendered, so the caller can persist a silent
    # premium→free downgrade (whole video may have degraded, or only some segments).
    if voice_report is not None:
        # MERGE, never replace: a multi-part chapter calls this once per part
        # with ONE report dict, and the worker bills and records it after the
        # loop — replacing it recorded only the last part's voice and chars.
        used = sorted({v for v in _voices_used if v} | set(voice_report.get("used") or []))
        merged = dict(voice_report.get("stats") or {})
        for k, v in _stats.items():
            merged[k] = merged.get(k, 0) + v
        voice_report.update({
            "requested": tts_voice, "used": used,
            "downgraded": bool(voice_report.get("downgraded")) or _any_downgrade or _preflight_downgrade,
            "preflight_downgrade": bool(voice_report.get("preflight_downgrade")) or _preflight_downgrade,
            "reasons": sorted(set(voice_report.get("reasons") or []) | _reasons),
            "stats": merged,
            "parts": int(voice_report.get("parts") or 0) + 1,
        })

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
