"""Stitch individual segment MP3s into a single master MP3."""

from __future__ import annotations

import logging
from pathlib import Path

from pydub import AudioSegment

logger = logging.getLogger(__name__)

GAP_MS = 300  # milliseconds of silence between segments
EXPORT_BITRATE = "128k"


def get_duration_seconds(mp3_path: str | Path) -> float:
    """Return actual duration of an MP3 file in seconds."""
    audio = AudioSegment.from_mp3(str(mp3_path))
    return len(audio) / 1000.0


def stitch_segments(
    segment_paths: list[str | Path],
    master_output_path: str | Path,
) -> tuple[float, list[float]]:
    """
    Concatenate segment MP3s with 300ms silence gaps.

    Returns:
        (total_duration_seconds, list_of_individual_durations_seconds)
    """
    master_output_path = Path(master_output_path)
    master_output_path.parent.mkdir(parents=True, exist_ok=True)

    gap = AudioSegment.silent(duration=GAP_MS)
    master = AudioSegment.empty()
    durations: list[float] = []

    for i, path in enumerate(segment_paths):
        segment = AudioSegment.from_mp3(str(path))
        dur = len(segment) / 1000.0
        durations.append(dur)

        master += segment
        # Add gap after every segment except the last
        if i < len(segment_paths) - 1:
            master += gap

    total_duration = len(master) / 1000.0

    master.export(str(master_output_path), format="mp3", bitrate=EXPORT_BITRATE)
    logger.info(
        "Stitched %d segments -> %s (%.1fs)",
        len(segment_paths),
        master_output_path.name,
        total_duration,
    )

    return total_duration, durations
