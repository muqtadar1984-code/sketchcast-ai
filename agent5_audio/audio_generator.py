"""
Orchestrator: reads a script, generates all segment audio,
stitches into master MP3, and builds the audio manifest.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from agent5_audio.manifest_builder import build_manifest, save_manifest
from agent5_audio.models import AudioManifest
from agent5_audio.stitcher import stitch_segments
from agent5_audio.tts_client import generate_all_segments, _get_model, _get_voice_id

logger = logging.getLogger(__name__)

STORAGE_DIR = Path(__file__).resolve().parent.parent / "storage"
SCRIPTS_DIR = STORAGE_DIR / "scripts"
AUDIO_DIR = STORAGE_DIR / "audio"


def generate_episode_audio(
    script_id: str | None = None,
    book_id: str = "",
    chapter_num: int = 0,
    episode_num: int = 1,
    script_data: dict | None = None,
    voice_id: str | None = None,
    model: str | None = None,
    progress_callback=None,
) -> AudioManifest:
    """
    Full pipeline: TTS all segments -> stitch master -> build manifest.

    Can accept script_data directly (Streamlit in-process mode)
    or load from disk via book_id/chapter_num.
    """
    # Load script if not provided
    if script_data is None:
        script_path = SCRIPTS_DIR / book_id / f"chapter_{chapter_num}_episode_{episode_num}.json"
        if not script_path.exists():
            raise FileNotFoundError(
                f"Script not found: {script_path}. Run Agent 3 first."
            )
        with open(script_path, encoding="utf-8") as f:
            script_data = json.load(f)

    # Extract fields
    if script_id is None:
        script_id = script_data.get("script_id", "unknown")
    book_id = book_id or script_data.get("book_id", "unknown")
    chapter_num = chapter_num or script_data.get("chapter_num", 0)
    episode_num = episode_num or script_data.get("episode_num", 1)
    segments = script_data.get("segments", [])

    if not segments:
        raise ValueError("Script has no segments.")

    voice_id = voice_id or _get_voice_id()
    model = model or _get_model()

    # Output directory
    output_dir = AUDIO_DIR / book_id / f"chapter_{chapter_num}"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: Generate individual segment MP3s
    logger.info("Generating %d segment audio files...", len(segments))
    segment_results = generate_all_segments(
        segments=segments,
        output_dir=output_dir,
        voice_id=voice_id,
        model=model,
        progress_callback=progress_callback,
    )

    # Step 2: Stitch into master MP3
    logger.info("Stitching master MP3...")
    segment_paths = [r["audio_path"] for r in segment_results]
    master_path = output_dir / "master.mp3"
    total_duration, segment_durations = stitch_segments(segment_paths, master_path)

    # Step 3: Build manifest
    logger.info("Building audio manifest...")
    manifest = build_manifest(
        script_id=script_id,
        book_id=book_id,
        chapter_num=chapter_num,
        episode_num=episode_num,
        voice_id=voice_id,
        model=model,
        segment_results=segment_results,
        segment_durations=segment_durations,
        master_audio_path=str(master_path),
        total_duration=total_duration,
    )

    # Save manifest to disk
    manifest_path = output_dir / "audio_manifest.json"
    save_manifest(manifest, manifest_path)
    logger.info("Audio generation complete: %s", manifest_path)

    return manifest
