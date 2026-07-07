"""Render one AI-Tutor "sketch" clip (Phase 2).

The Coach draws a short animated explainer: the app authored a ONE-slide spec
(heading / points / optional diagram) + a couple of sentences of narration and
queued it in tutor_sketch. Here we turn that into a single narrated MP4 by reusing
the shipped native lesson renderer — PIL mask-reveal + pen, free Edge TTS, ffmpeg
(~$0 compute) — upload it to the private tutor-sketch bucket, and flip the row to
done. Identical specs are cached (unique book+chapter+spec_hash) so the clip is
rendered ONCE and replayed for every student for $0.

Self-contained + best-effort: any failure marks the row 'error' (the panel then
gently suggests asking a question instead) and never crashes the poller.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from . import client as db

logger = logging.getLogger("worker")


def render_sketch(sb, sketch: dict) -> None:
    """Render a claimed tutor_sketch row to an MP4 and mark it done/error."""
    sketch_id = sketch["id"]
    try:
        spec_in = sketch.get("spec") or {}
        narration = (sketch.get("narration") or "").strip()
        if not narration and not spec_in.get("heading"):
            raise RuntimeError("empty sketch spec")

        with tempfile.TemporaryDirectory(prefix="sketch_") as tmpdir:
            tmp = Path(tmpdir)

            # 1) Narration → MP3. Free Edge by default; a premium voice would need
            #    allow_premium + a key, which sketches don't use yet ($0).
            from shared.tts import synthesize

            mp3 = tmp / "narration.mp3"
            synthesize(narration or spec_in.get("heading", ""), mp3, voice_id=sketch.get("voice_id"), allow_premium=False)

            # 2) Measure narration length to pace the write-on animation.
            from agent6_animation.video_composer import _audio_duration, _ffmpeg_exe

            ffmpeg = _ffmpeg_exe()
            duration = _audio_duration(str(mp3), ffmpeg)

            # 3) Render ONE object-animated segment (same renderer as lessons). A
            #    single segment is already a self-contained MP4 — no concat needed.
            from agent6_animation.native_render import render_native_segment

            spec = {
                "heading": spec_in.get("heading", ""),
                "points": spec_in.get("points") or [],
                "visual": spec_in.get("visual"),
                "fallback": narration,  # shown if there are no bullets/visual
                "number": 1,
            }
            out_mp4 = tmp / "sketch.mp4"
            ok = render_native_segment(spec, str(mp3), str(out_mp4), ffmpeg, audio_secs=duration)
            if not ok or not out_mp4.exists():
                raise RuntimeError("native sketch render failed")

            # 4) Upload to the private bucket, keyed by the cache hash so an
            #    identical spec always resolves to the same object.
            dest = f"{sketch['book_id']}/{sketch['spec_hash']}.mp4"
            db.upload_sketch(sb, out_mp4, dest)
            db.set_sketch_done(sb, sketch_id, dest)
            logger.info("tutor sketch %s rendered (%.1fs) -> %s", sketch_id, duration, dest)
    except Exception as exc:  # noqa: BLE001 — a sketch must never crash the poller
        logger.error("tutor sketch %s failed: %s", sketch_id, exc)
        try:
            db.set_sketch_error(sb, sketch_id, str(exc))
        except Exception:  # noqa: BLE001
            pass
