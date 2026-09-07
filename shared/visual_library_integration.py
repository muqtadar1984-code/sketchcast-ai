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


def library_over_generated_cache_enabled() -> bool:
    """Whether an approved LIBRARY asset outranks a cache entry an earlier
    AI generation wrote for the same key.

    Default ON. ``LIBRARY_OVER_GENERATED_CACHE=0`` restores the older
    cache-first order without a deploy: the check costs one library lookup per
    generated cache entry, and a lookup is a paged read of the approved table.
    A cache entry the library already supplied is never re-checked, so the
    cost falls away as the cache fills with library assets.
    """
    return os.getenv("LIBRARY_OVER_GENERATED_CACHE", "1").strip().lower() \
        not in ("0", "false", "no", "off")


def _cache_provenance(asset_dir: Path) -> str:
    """What a cache entry says it IS — "generated", "visual_library", or ""
    when nothing readable is there. raster_assets writes this on every save,
    including when it replaces a hydrated asset in place."""
    try:
        md = json.loads((asset_dir / "meta.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return ""
    return str(md.get("provenance") or "") if isinstance(md, dict) else ""


def _refresh_from_library(key: str, prompt: str, cache: Path) -> dict[str, Any] | None:
    """Replace a GENERATED cache entry with the library's approved asset for
    the same key. Returns the hit when the cache now holds it, else None.

    A cache entry is one model's first attempt, kept because it was first; an
    approved library row has been reviewed and published for reuse. Measured
    on the live Cells kit (2026-09-07): five diagrams resolved `local_cache`
    with `asset_provenance: generated` and `library_asset_id: null` while
    `visual_assets` held an approved row for every one of them — the avatars,
    which have no cache entry on a fresh container, came from the library on
    the same run, so the library path itself was working.

    Only a DURABLE library row is taken. `find` also sees this worker's own
    local index, whose rows ARE the generated cache entries (the bootstrap
    registers every one of them) and which carry no `id`; refreshing a file
    from itself would relabel a generated picture as a library hit and change
    nothing else.
    """
    try:
        from shared.visual_library import find, hydrate
        hit = find(key, prompt, context(), asset_format="png")
        if not hit or not hit.get("id") or not hit.get("storage_path"):
            return None
        # The scan above is the expensive part; hand it to hydrate rather than
        # paying for the identical read again.
        if hydrate(key, prompt, cache, context(), asset_format="png",
                   replace=True, hit=hit) is None:
            return None
        logger.info("visual library: %s replaced a generated cache entry with "
                    "%s (score %.2f)", key, hit.get("asset_key"),
                    hit.get("match_score", 0))
        return hit
    except Exception as exc:  # noqa: BLE001 — reuse is an optimisation, never a failure
        logger.debug("visual library refresh failed for %s: %s", key, exc)
        return None


def _library_outcome(hydrated: bool, provenance: str,
                     usable: bool) -> tuple[bool, str | None]:
    """What actually became of a file the library put into the cache.

    Hydration is not the end of the story. BOTH renderers re-validate what
    they find in the cache and replace it IN PLACE when it does not hold up —
    svg_assets when the markup no longer parses, raster_assets when the image
    is corrupt or carries baked text — and the meta.json they rewrite then
    says "generated". Deciding reuse from "a file appeared where none was"
    therefore logged one request as a library hit AND ai_generated=true at the
    same time: two mutually exclusive facts on one row, in the only record
    anyone judges the threshold and the reuse rate by. A hit that had to be
    redrawn is not a hit; counting it as one overstates the library's value
    exactly where the evidence is supposed to be read.

    Returns (served_by_library, discarded), where `discarded` names what
    happened instead: 'regenerated' when the renderer replaced the hydrated
    asset, 'unusable' when it could not be replaced either and no asset was
    bound at all.
    """
    if not hydrated:
        return False, None
    if provenance == "generated":
        return False, "regenerated"
    if not usable:
        return False, "unusable"
    return True, None


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
        # `original` may take it again). It closes two races that were found
        # separately: taken inside `original` only, two render threads could
        # both read existed_before=False for one key and both log
        # generated+published for a single image; and hydration ran OUTSIDE it,
        # so one thread could open the half-written asset.png another was
        # writing, call it corrupt, and pay for a second, different face
        # mid-lesson.
        with ra.asset_lock(key):
            return _decide(key, prompt, cache_dir, allow_generate)

    def _decide(key: str, prompt: str, cache_dir: Path | None,
                allow_generate: bool):
        cache = cache_dir or ra.CACHE_DIR
        cache.mkdir(parents=True, exist_ok=True)
        # Asked of the renderer, which also answers with the directory of the
        # SAME WORD spelled the other way — so the two halves of one decision
        # can never disagree about which file is this key's cache entry.
        asset_dir = ra.cache_dir_for(key, cache)
        png = asset_dir / "asset.png"
        existed_before = png.exists()
        avatar = is_avatar_key(key)

        # A cache entry an earlier GENERATION wrote does not outrank the
        # library's reviewed asset for the same key: refresh it. An entry the
        # library already supplied keeps today's fast path and costs no
        # lookup. See _refresh_from_library.
        refreshed = None
        if existed_before and not avatar and library_over_generated_cache_enabled() \
                and _cache_provenance(asset_dir) == "generated":
            refreshed = _refresh_from_library(key, prompt, cache)
            if refreshed is not None:
                # hydrate files under the REQUESTED key, which is not
                # `asset_dir` when the entry came from the other spelling's
                # directory. Re-ask rather than assume: everything below —
                # the provenance read, `asset_used`, the publish guard — must
                # look at the file the renderer is about to bind.
                asset_dir = ra.cache_dir_for(key, cache)
                png = asset_dir / "asset.png"

        # Scored BEFORE any lookup mutates the cache, and recorded whether or
        # not it clears the threshold — a near miss is the evidence that says
        # whether the threshold is set right. Avatars are not scored: they are
        # an identity, not a meaning, and the nearest educational visual (an
        # onion epidermis, measured) is noise in the decision log.
        match, score, source = (None, 0.0, "none")
        if refreshed is not None:
            match, score = refreshed, float(refreshed.get("match_score") or 0.0)
            source = str(refreshed.get("match_source") or "remote")
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

        hydrated = refreshed is not None or ((not existed_before) and png.exists())
        published = False
        result = original(key, prompt, cache, allow_generate)

        # A newly generated, validated asset is promoted into the reusable
        # library. We use the metadata written by raster_assets as the source
        # of truth and never re-publish a library-hydrated file. A refreshed
        # entry is `existed_before` and so is never re-published: it came FROM
        # the library.
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
        # Read from the provenance the renderer wrote, not from the fact that
        # a file arrived: raster_assets replaces a corrupt or text-baked
        # hydrated asset in place and stamps it "generated".
        served_by_library, library_discarded = _library_outcome(
            hydrated, provenance, result is not None)
        # `library_over_generated_cache` is its own outcome, not a "library
        # hit": this line is the single source of truth for what happened to a
        # request, and an operator counting reuse has to be able to see how
        # much of it came from overruling a cache entry.
        if existed_before and refreshed is None:
            outcome = "local_cache"
        elif served_by_library:
            outcome = ("library_over_generated_cache" if refreshed is not None
                       else "library_hit")
        elif library_discarded:
            outcome = f"library_asset_{library_discarded}"
        elif provenance == "generated":
            outcome = "generated"
        elif not png.exists():
            outcome = "failed"
        else:
            outcome = provenance
        log_decision({
            "tier": "raster",
            "requested_key": key,
            "canonical_key": ra.canonical_key(key),
            "requested_prompt": prompt[:300],
            "outcome": outcome,
            "library_hit": bool(served_by_library),
            # Set only when the library DID hand over an asset and it did not
            # survive: 'regenerated' or 'unusable'. A reader counting reuse
            # wants these separated from a request the library never answered.
            "library_discarded": library_discarded,
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

        hydrated = (not existed_before) and svg_file.exists()
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
        # A hydrated SVG that no longer parses is regenerated IN PLACE by
        # get_svg_asset, which rewrites meta.json as "generated". The row says
        # so rather than claiming a hit it did not get.
        served_by_library, library_discarded = _library_outcome(
            hydrated, provenance, result is not None)

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
                        else f"library_asset_{library_discarded}"
                        if library_discarded
                        else "generated" if provenance == "generated"
                        else "failed" if not svg_file.exists() else provenance),
            "library_hit": bool(served_by_library),
            "library_discarded": library_discarded,
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
