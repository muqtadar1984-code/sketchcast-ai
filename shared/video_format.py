"""The video FORMAT VERSION: which generation of the rendering pipeline a
video was made by, so the library can tell which published videos predate a
significant upgrade.

A YouTube video's file cannot be replaced once uploaded (only its words,
thumbnail, captions and privacy can change), so a rendering upgrade never
reaches the videos already on the channel. Whether an old video is worth
re-rendering and superseding is the founder's call, video by video, by age
and by how visible the change is (2026-09-26: "leave it for me to decide").
This module gives that decision its facts and nothing else: no email, no
automatic job.

Bump VIDEO_FORMAT_VERSION — and describe the change in FORMAT_CHANGES —
when a change alters what a viewer SEES in a finished video (the crayon
colouring, a new end screen, a labelling overhaul). Not for a bug fix that
only some lessons hit, and never for a worker-internal change. The number
is stamped on every presentation generation as it renders
(params.format_version) and copied onto its topic_publications row when
it is posted; the current value is written to platform_settings on every
worker boot, which is where the portal reads it from.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

VIDEO_FORMAT_VERSION = 2

# Versions that exist only behind a flag: the constant above is the base;
# current() reports the highest whose flag is on. Board colour phase 1
# (spike/scene_engine/colour.py) is viewer-visible — coral relation arrows
# and two-tone equations — so a video drawn with it must not read as
# format 2, and a worker that turns the flag off reads as 2 again.
FLAGGED_VERSIONS: dict[int, str] = {3: "FEATURE_BOARD_COLOUR"}

FORMAT_CHANGES: dict[int, str] = {
    1: "The original scene engine.",
    2: ("Labels anchored to the parts they name, with a description-aware vision pass; "
        "process labels point at the arrow between their states; a zoom keeps a picture's "
        "labels in the frame; the end screen with the call to action."),
    3: ("Board colour, phase 1: relationship arrows in a second accent, leader arrows "
        "in the first, equations coloured side by side; pictures unchanged."),
}

SETTINGS_KEY = "video_format"


def _flag_on(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def current() -> int:
    on = [v for v, flag in FLAGGED_VERSIONS.items() if _flag_on(flag)]
    return max([int(VIDEO_FORMAT_VERSION), *on])


def settings_value() -> dict:
    """What the worker writes to platform_settings: the version and the
    changelog the portal shows beside an outdated video."""
    return {
        "version": current(),
        "changes": {str(k): v for k, v in sorted(FORMAT_CHANGES.items())},
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }


def record_current(sb) -> bool:
    """Write the current version to platform_settings (upsert by key). Called
    once per worker boot; never raises — the portal reading a stale version
    is a display issue, not a reason to refuse work."""
    try:
        sb.table("platform_settings").upsert(
            {"key": SETTINGS_KEY, "value": settings_value(),
             "updated_at": datetime.now(timezone.utc).isoformat()},
            on_conflict="key").execute()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not record the video format version: %s", exc)
        return False


__all__ = ["VIDEO_FORMAT_VERSION", "FLAGGED_VERSIONS", "FORMAT_CHANGES", "SETTINGS_KEY", "current",
           "settings_value", "record_current"]
