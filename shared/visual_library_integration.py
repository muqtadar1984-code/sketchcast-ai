"""Wire the persistent visual library into the existing scene asset cache.

This module is imported by ``spike.scene_engine`` at package initialisation,
so callers that already import ``raster_assets.get_raster_asset`` or
``svg_assets.get_svg_asset`` require no call-site changes. Each wrapper
hydrates an approved library hit before the existing generator runs and
publishes a newly generated asset afterwards.

BOTH tiers are wrapped, and each only ever sees its own format. The renderer
contract is unchanged — ``make_resolver`` still returns ("vector", …) or
("raster", …), and a library SVG arrives as a VectorAsset like any other,
because format is a property of the STORED asset, not of what the renderer
draws. There is no ("svg", …) resolver tag and there must not be one.

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

    Both formats: ``<canonical>/asset.png`` from the raster tier and
    ``svg_<canonical>/asset.svg`` from the SVG tier. Indexing only the PNGs
    would leave the SVG cache invisible to the very reuse layer this module
    exists to provide.
    """
    try:
        from shared.visual_library import avatar_fields, register_local
        root = Path(ra.CACHE_DIR)

        def index(meta_path: Path, asset: Path, fmt: str) -> None:
            if not asset.exists():
                return
            try:
                md = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                return
            if md.get("provenance") != "generated" or md.get("baked_text"):
                return
            key = str(md.get("key") or meta_path.parent.name)
            group_ids = list(md.get("group_ids") or [])
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
                "local_cache_path": str(asset),
                "asset_format": fmt,
                "group_ids": group_ids,
                "group_count": len(group_ids),
                # This bootstrap re-indexes the WHOLE cache on every worker
                # start, avatars included. find() would filter them by key
                # anyway, but an index row that says what it is beats one that
                # relies on a downstream guard.
                **avatar_fields(key),
            })

        for meta_path in root.glob("*/meta.json"):
            if meta_path.parent.name.startswith("svg_"):
                index(meta_path, meta_path.parent / "asset.svg", "svg")
            else:
                index(meta_path, meta_path.parent / "asset.png", "png")
    except Exception as exc:  # noqa: BLE001
        logger.debug("existing visual cache bootstrap skipped: %s", exc)


def _hydrate_local_library(key: str, prompt: str, cache: Path,
                           asset_format: str = "png") -> bool:
    """Copy a known local library asset into the renderer cache.

    The target path is asked of the library rather than rebuilt here: a second
    copy of that fold is what once filed every downloaded *_cell picture where
    the renderer never looks.
    """
    try:
        from shared.visual_library import _local_asset_path, find
        hit = find(key, prompt, context(), asset_format=asset_format)
        source = Path(str(hit.get("local_cache_path") or "")) if hit else None
        if not source or not source.exists():
            return False
        target = _local_asset_path(cache, key, asset_format)
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
            md.update({"provenance": "visual_library",
                       "library_asset_id": hit.get("id"),
                       "asset_format": asset_format})
            (target.parent / "meta.json").write_text(json.dumps(md, indent=2), encoding="utf-8")
        logger.info("visual library local hit: %s <- %s (%s, score %.2f)",
                    key, hit.get("asset_key"), asset_format,
                    hit.get("match_score", 0))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("local visual library lookup failed for %s: %s", key, exc)
        return False


def _patch() -> None:
    global _PATCHED
    if _PATCHED:
        return
    from spike.scene_engine import raster_assets as ra
    from spike.scene_engine import svg_assets as sa
    from shared.visual_library import (best_match, hydrate, hydrate_avatar,
                                       is_avatar_key, key_guard_ok,
                                       log_decision, publish_generated,
                                       threshold_now)

    _bootstrap_existing_cache(ra)
    original = ra.get_raster_asset
    original_svg = sa.get_svg_asset

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
                match, score, source = best_match(key, prompt, context(),
                                                  asset_format="png")
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
            elif not _hydrate_local_library(key, prompt, cache, "png"):
                try:
                    hydrate(key, prompt, cache, context(), asset_format="png")
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
            "tier": "raster",
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
            # Whether the best-scoring row was even ABOUT the requested key.
            # cleared_threshold=true with library_hit=false used to mean a
            # canonical-key path mismatch; now it can also mean this guard
            # refused a confident wrong picture, and the log says which.
            "key_guard_passed": bool(match is not None and key_guard_ok(key, match)),
            "ai_generated": provenance == "generated" and not existed_before,
            "published": published,
            "asset_used": str(png) if png.exists() else None,
            "asset_provenance": provenance,
            # WHAT was bound, not just where it came from. A row now says
            # "visual_library, svg, 7 groups" or "generated, png" — enough to
            # tell the two tiers apart in one log stream without joining
            # anything.
            "asset_format": "png" if png.exists() else None,
            "library_asset_id": final.get("library_asset_id"),
            # a raster asset has vision-annotated regions, not groups
            "group_count": None,
        })
        return result

    def wrapped_get_svg_asset(key: str, prompt: str, cache_dir: Path | None = None,
                              allow_generate: bool = True):
        with ra.asset_lock(key):
            return _decide_svg(key, prompt, cache_dir, allow_generate)

    def _decide_svg(key: str, prompt: str, cache_dir: Path | None,
                    allow_generate: bool):
        """The SVG tier's half of the same decision.

        Deliberately NOT a copy of the raster path with a different suffix.
        Two things differ, and both are the point:

        * There is no `annotate_regions` here, and there must never be one.
          That call is a paid VISION request whose whole job is to guess where
          the named parts of a flat image are. An SVG has no guessing to do —
          the groups ARE the regions, named by the model that drew them. The
          zero-vision property is the largest single saving of this tier and
          it is pinned by a test.
        * The library is not consulted at all for an avatar key. Educational
          retrieval is already avatar-blind, but a persistent character is an
          identity rather than a meaning, and the roster lives on the raster
          tier where the colour path is. Nothing here may hand an avatar
          request an educational diagram.
        """
        cache = cache_dir or sa.CACHE_DIR
        svg_dir = sa.svg_cache_dir(cache, key)
        svg_file, svg_meta = svg_dir / "asset.svg", svg_dir / "meta.json"
        existed_before = svg_file.exists()
        avatar = is_avatar_key(key)

        match, score, source = (None, 0.0, "none")
        if not existed_before and not avatar:
            try:
                match, score, source = best_match(key, prompt, context(),
                                                  asset_format="svg")
            except Exception as exc:  # noqa: BLE001
                logger.debug("visual library scoring failed for %s: %s", key, exc)
            if not _hydrate_local_library(key, prompt, cache, "svg"):
                try:
                    hydrate(key, prompt, cache, context(), asset_format="svg")
                except Exception as exc:  # noqa: BLE001
                    logger.debug("visual library lookup failed for %s: %s", key, exc)

        served_by_library = (not existed_before) and svg_file.exists()
        published = False
        result = original_svg(key, prompt, cache, allow_generate)

        final: dict[str, Any] = {}
        if svg_file.exists():
            try:
                final = json.loads(svg_meta.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                final = {}
        provenance = str(final.get("provenance")
                         or ("absent" if not svg_file.exists() else "unknown"))

        if (not existed_before and not avatar and result is not None
                and svg_file.exists() and provenance == "generated"):
            try:
                # publish_generated is the STRICT gate: markup that breaks the
                # asset contract returns False and never enters the library,
                # while `result` — already parsed by the forgiving runtime —
                # still draws this board.
                published = bool(publish_generated(key, prompt, svg_file, final,
                                                   context(), asset_format="svg"))
            except Exception as exc:  # noqa: BLE001
                logger.debug("visual library publish failed for %s: %s", key, exc)

        group_ids = list(final.get("group_ids") or [])
        if not group_ids and result is not None:
            group_ids = list(result.layer_ids())
        log_decision({
            "tier": "svg",
            "requested_key": key,
            "canonical_key": ra.canonical_key(key),
            "requested_prompt": prompt[:300],
            "outcome": ("local_cache" if existed_before
                        else "library_hit" if served_by_library
                        else "generated" if provenance == "generated"
                        else "failed" if not svg_file.exists() else provenance),
            "library_hit": bool(served_by_library),
            "matched_key": (match or {}).get("asset_key"),
            "matched_id": (match or {}).get("id"),
            "match_score": round(score, 4),
            "match_source": source,
            "threshold": threshold_now(),
            "cleared_threshold": bool(match is not None and score >= threshold_now()),
            "key_guard_passed": bool(match is not None and key_guard_ok(key, match)),
            "ai_generated": provenance == "generated" and not existed_before,
            "published": published,
            "asset_used": str(svg_file) if svg_file.exists() else None,
            "asset_provenance": provenance,
            "asset_format": "svg" if svg_file.exists() else None,
            "library_asset_id": final.get("library_asset_id"),
            "group_count": len(group_ids) if svg_file.exists() else None,
        })
        return result

    ra.get_raster_asset = wrapped_get_raster_asset
    sa.get_svg_asset = wrapped_get_svg_asset
    _PATCHED = True


_patch()
