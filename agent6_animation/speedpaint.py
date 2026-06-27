"""SpeedPaint animation engine — progressive pixel reveal using OpenCV.

Creates a video that gradually reveals an image from left to right,
simulating a hand-drawing / speed-paint effect.
"""

from __future__ import annotations

import logging
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger(__name__)

_WIDTH = 1280
_HEIGHT = 720
_FPS_DEFAULT = 24


def create_speedpaint_video(
    image_path: str,
    duration_seconds: float,
    output_path: str,
    fps: int = _FPS_DEFAULT,
) -> str:
    """Create a progressive-reveal SpeedPaint video from a static image.

    Parameters
    ----------
    image_path : str
        Path to the source slide PNG (1280x720).
    duration_seconds : float
        Target video duration in seconds (matches audio).
    output_path : str
        Where to write the output MP4.
    fps : int
        Frames per second (default 24).

    Returns
    -------
    str
        The output path (same as input for convenience).
    """
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    # Load the target image
    img = cv2.imread(image_path)
    if img is None:
        logger.warning("Could not read image %s — creating blank video", image_path)
        img = np.full((_HEIGHT, _WIDTH, 3), 250, dtype=np.uint8)  # #FAFAFA approx

    # Resize to 1280x720 if needed
    if img.shape[:2] != (_HEIGHT, _WIDTH):
        img = cv2.resize(img, (_WIDTH, _HEIGHT), interpolation=cv2.INTER_LANCZOS4)

    # Create white canvas (background)
    white = np.full_like(img, 250, dtype=np.uint8)  # #FAFAFA

    total_frames = max(int(duration_seconds * fps), 1)

    # Use mp4v codec (widely compatible)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(output), fourcc, fps, (_WIDTH, _HEIGHT))

    if not writer.isOpened():
        raise RuntimeError(f"Failed to open VideoWriter for {output}")

    try:
        for frame_idx in range(total_frames):
            progress = frame_idx / max(total_frames - 1, 1)

            # Left-to-right wipe reveal
            reveal_x = int(progress * _WIDTH)

            # Build frame: white background with revealed portion of image
            frame = white.copy()
            if reveal_x > 0:
                frame[:, :reveal_x] = img[:, :reveal_x]

            # Add a subtle vertical line at the reveal edge (pen cursor)
            if 0 < reveal_x < _WIDTH:
                cv2.line(frame, (reveal_x, 0), (reveal_x, _HEIGHT), (44, 62, 80), 2)

            writer.write(frame)

        # Hold the final complete image for a few extra frames
        for _ in range(max(fps // 4, 1)):  # ~0.25 seconds hold
            writer.write(img)

    finally:
        writer.release()

    logger.info(
        "SpeedPaint video: %d frames @ %dfps (%.1fs) -> %s",
        total_frames, fps, duration_seconds, output.name,
    )
    return str(output)


def create_static_video(
    image_path: str,
    duration_seconds: float,
    output_path: str,
    fps: int = _FPS_DEFAULT,
) -> str:
    """Create a static video (no animation) that just shows the image.

    Used for DRAW_CONTINUE and GHOST_ONLY segments where we don't
    need a reveal animation — just hold the image for the duration.
    """
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    img = cv2.imread(image_path)
    if img is None:
        img = np.full((_HEIGHT, _WIDTH, 3), 250, dtype=np.uint8)

    if img.shape[:2] != (_HEIGHT, _WIDTH):
        img = cv2.resize(img, (_WIDTH, _HEIGHT), interpolation=cv2.INTER_LANCZOS4)

    total_frames = max(int(duration_seconds * fps), 1)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(output), fourcc, fps, (_WIDTH, _HEIGHT))

    if not writer.isOpened():
        raise RuntimeError(f"Failed to open VideoWriter for {output}")

    try:
        for _ in range(total_frames):
            writer.write(img)
    finally:
        writer.release()

    logger.info(
        "Static video: %d frames @ %dfps (%.1fs) -> %s",
        total_frames, fps, duration_seconds, output.name,
    )
    return str(output)
