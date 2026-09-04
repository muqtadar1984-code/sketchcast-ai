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
import shutil
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


def _bootstrap_existing_cache(ra) -> None:
    """Index existing generated scene assets without spending another AI call.

    This is deliberately local-only. A separate one-shot migration script can
    publish these assets to Supabase. Indexing them here immediately makes the
    current worker cache searchable for subsequent renders on the same host.
    """
    try:
        from shared.visual_library import avatar_fields, register_local
        root = Path(ra.CACHE_DIR)
        for meta_path in root.glob("*/meta.json"):
            png = meta_path.parent / "asset.png"
            if not png.exists():
                continue
            try:
                md = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if md.get("provenance") != "generated" or md.get("baked_text"):
                continue
            key = str(md.get("key") or meta_path.parent.name)
            register_local({
                "asset_key": key,
                "canonical_key": ra.canonical_key(key),
                "description": str(md.get("prompt") or key),
                "curriculum": "generic",
                "subject": "general",
                "grade": "k12",
                "topic": key,
                "concepts": [],
                "status": "approved",
                "provenance": "generated",
                "local_cache_path": str(png),
                # This bootstrap re-indexes the WHOLE cache on every worker
                # start, avatars included. find() would filter them by key
                # anyway, but an index row that says what it is beats one that
                # relies on a downstream guard.
                **avatar_fields(key),
            })
    except Exception as exc:  # noqa: BLE001
        logger.debug("existing visual cache bootstrap skipped: %s", exc)


def _hydrate_local_library(key: str, prompt: str, cache: Path) -> bool:
    """Copy a known local library asset into the renderer cache."""
    try:
        from shared.visual_library import find
        hit = find(key, prompt, context())
        source = Path(str(hit.get("local_cache_path") or "")) if hit else None
        if not source or not source.exists():
            return False
        target = cache / __import__("spike.scene_engine.raster_assets", fromlist=["canonical_key"]).canonical_key(key) / "asset.png"
        if target.exists():
            return True
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        meta = source.parent / "meta.json"
        if meta.exists():
            try:
                md = json.loads(meta.read_text(encoding="utf-8"))
            except Exception:
                md = {}
            md.update({"provenance": "visual_library", "library_asset_id": hit.get("id")})
            (target.parent / "meta.json").write_text(json.dumps(md, indent=2), encoding="utf-8")
        logger.info("visual library local hit: %s <- %s (score %.2f)",
                    key, hit.get("asset_key"), hit.get("match_score", 0))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("local visual library lookup failed for %s: %s", key, exc)
        return False


def _patch() -> None:
    global _PATCHED
    if _PATCHED:
        return
    from spike.scene_engine import raster_assets as ra
    from shared.visual_library import (best_match, hydrate, hydrate_avatar,
                                       is_avatar_key, log_decision,
                                       publish_generated, threshold_now)

    _bootstrap_existing_cache(ra)
    original = ra.get_raster_asset

    def wrapped_get_raster_asset(key: str, prompt: str, cache_dir: Path | None = None,
                                 allow_generate: bool = True):
        # The whole decision — did it exist, hydrate, generate, publish, log —
        # runs under the SAME per-key lock the generator uses (re-entrant, so
        # `original` may take it again). It used to be taken inside `original`
        # only, so two render threads could both read existed_before=False for
        # one key and both log generated+published for a single image.
        with ra.asset_lock(key):
            return _decide(key, prompt, cache_dir, allow_generate)

    def _decide(key: str, prompt: str, cache_dir: Path | None,
                allow_generate: bool):
        cache = cache_dir or ra.CACHE_DIR
        cache.mkdir(parents=True, exist_ok=True)
        asset_dir = cache / ra.canonical_key(key)
        png = asset_dir / "asset.png"
        existed_before = png.exists()
        avatar = is_avatar_key(key)

        # Scored BEFORE any lookup mutates the cache, and recorded whether or
        # not it clears the threshold — a near miss is the evidence that says
        # whether the threshold is set right. Avatars are not scored: they are
        # an identity, not a meaning, and the nearest educational visual (an
        # onion epidermis, measured) is noise in the decision log.
        match, score, source = (None, 0.0, "none")
        if not existed_before and not avatar:
            try:
                match, score, source = best_match(key, prompt, context())
            except Exception as exc:  # noqa: BLE001
                logger.debug("visual library scoring failed for %s: %s", key, exc)

        if not existed_before:
            if avatar:
                # The roster: the approved avatar for this key, by key. Every
                # fresh container used to generate a new teacher because the
                # semantic lookups below are avatar-blind by design.
                try:
                    if hydrate_avatar(key, cache):
                        source = "avatar"
                except Exception as exc:  # noqa: BLE001
                    logger.debug("visual library avatar lookup failed for %s: %s", key, exc)
            # First reuse a previously generated asset already present on the
            # worker. Then try the durable Supabase library. Only after both
            # fail does the original function get permission to call Gemini.
            elif not _hydrate_local_library(key, prompt, cache):
                try:
                    hydrate(key, prompt, cache, context())
                except Exception as exc:  # noqa: BLE001
                    logger.debug("visual library lookup failed for %s: %s", key, exc)

        served_by_library = (not existed_before) and png.exists()
        published = False
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
                    published = True
                except Exception as exc:  # noqa: BLE001
                    logger.debug("visual library publish failed for %s: %s", key, exc)

        # One row per visual request. `ai_generated` is derived from the
        # provenance raster_assets itself wrote, not guessed from timing: a
        # file it produced says "generated", one the library supplied says
        # "visual_library". That keeps the log honest without reaching into
        # the generation path to instrument it.
        final = {}
        if png.exists():
            try:
                final = json.loads((asset_dir / "meta.json").read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                final = {}
        provenance = str(final.get("provenance") or ("absent" if not png.exists() else "unknown"))
        log_decision({
            "requested_key": key,
            "canonical_key": ra.canonical_key(key),
            "requested_prompt": prompt[:300],
            "outcome": ("local_cache" if existed_before
                        else "library_hit" if served_by_library
                        else "generated" if provenance == "generated"
                        else "failed" if not png.exists() else provenance),
            "library_hit": bool(served_by_library),
            "matched_key": (match or {}).get("asset_key"),
            "matched_id": (match or {}).get("id"),
            "match_score": round(score, 4),
            "match_source": source,
            "threshold": threshold_now(),
            "cleared_threshold": bool(match is not None and score >= threshold_now()),
            "ai_generated": provenance == "generated" and not existed_before,
            "published": published,
            "asset_used": str(png) if png.exists() else None,
            "asset_provenance": provenance,
        })
        return result

    ra.get_raster_asset = wrapped_get_raster_asset
    _PATCHED = True


_patch()
