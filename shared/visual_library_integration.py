"""Wire the persistent visual library into the existing scene raster cache.

This module is imported by ``spike.scene_engine`` at package initialisation,
so callers that already import ``raster_assets.get_raster_asset`` require no
call-site changes. The wrapper hydrates an approved library hit before the
existing generator runs and publishes a newly generated asset afterwards.

The renderer therefore keeps its existing fallback ladder and semantics:
asset lookup is an optimization, never a reason for a lesson to fail.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_PATCHED = False
_CONTEXT: dict[str, Any] = {}


def set_context(*, curriculum: str | None = None, subject: str | None = None,
                grade: str | None = None, topic: str | None = None,
                concepts: list[str] | None = None) -> None:
    """Set optional per-generation metadata for subsequent visual lookups."""
    global _CONTEXT
    _CONTEXT = {
        "curriculum": curriculum or os.getenv("SKETCHCAST_CURRICULUM", "generic"),
        "subject": subject or os.getenv("SKETCHCAST_SUBJECT", "general"),
        "grade": grade or os.getenv("SKETCHCAST_GRADE", "k12"),
        "topic": topic or "",
        "concepts": concepts or [],
    }


def context() -> dict[str, Any]:
    return dict(_CONTEXT)


def _patch() -> None:
    global _PATCHED
    if _PATCHED:
        return
    from spike.scene_engine import raster_assets as ra
    from shared.visual_library import hydrate, publish_generated

    original = ra.get_raster_asset

    def wrapped_get_raster_asset(key: str, prompt: str, cache_dir: Path | None = None,
                                 allow_generate: bool = True):
        cache = cache_dir or ra.CACHE_DIR
        cache.mkdir(parents=True, exist_ok=True)
        asset_dir = cache / ra.canonical_key(key)
        png = asset_dir / "asset.png"
        existed_before = png.exists()

        # Hydrate first. If the library has no confident approved match, the
        # original resolver proceeds exactly as before and may call the image
        # model. A failed library lookup is intentionally invisible to the
        # rendering contract.
        if not existed_before:
            try:
                hydrate(key, prompt, cache, context())
            except Exception as exc:  # noqa: BLE001
                logger.debug("visual library lookup failed for %s: %s", key, exc)

        result = original(key, prompt, cache, allow_generate)

        # A newly generated, validated asset is promoted into the reusable
        # library. We use the metadata written by raster_assets as the source
        # of truth and never re-publish a library-hydrated file.
        if not existed_before and result is not None and png.exists():
            try:
                md = json.loads((asset_dir / "meta.json").read_text(encoding="utf-8"))
            except Exception:
                md = {}
            if md.get("provenance") == "generated" and not md.get("baked_text"):
                try:
                    publish_generated(key, prompt, png, md, context())
                except Exception as exc:  # noqa: BLE001
                    logger.debug("visual library publish failed for %s: %s", key, exc)
        return result

    ra.get_raster_asset = wrapped_get_raster_asset
    _PATCHED = True


_patch()
