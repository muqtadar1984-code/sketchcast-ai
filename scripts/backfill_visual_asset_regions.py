"""Annotate the library assets that predate the vision column.

WHY. `visual_assets.vision` records where each named part of a picture is, and
`row_has_parts` answers "does this stored asset carry the parts this lesson
wants to label?" from the row, with no download. That is the mechanism by
which the library serves a picture instead of generating one.

It cannot fire today. The approved non-avatar PNGs predate the column and
carry `group_count = 0`, so a parts-aware lookup matches nothing and every run
regenerates artwork the library already holds. Image capacity is ~1 per minute
and shared, so a needless generation is not merely wasteful — it is the thing
that hands a concurrent lesson a blank board.

WHAT IT COSTS. One VISION call per asset: no image quota at all, so this
cannot starve a lesson of pictures. Vision is limited separately and far more
generously. Measured basis: ~217 rows at roughly $0.001 each.

SAFETY. Refuses to start while production has queued or processing work, for
the standing never-starve rule — vision and image budgets are separate, but a
few hundred concurrent HTTP calls against the same project is not something to
do underneath a live lesson. `--dry-run` prints what it would ask for and
spends nothing. Nothing is deleted or overwritten destructively: a row that
already has regions is skipped, and `record_vision` only ever adds.

    python scripts/backfill_visual_asset_regions.py --dry-run
    python scripts/backfill_visual_asset_regions.py --limit 20
    python scripts/backfill_visual_asset_regions.py            # all of them
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger("backfill_regions")


def _load_env() -> None:
    """The worker reads .env at import; a standalone script must too."""
    root = Path(__file__).resolve().parents[1]
    env = root / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _live_work(sb) -> int:
    rows = (sb.table("jobs").select("id")
            .in_("status", ["queued", "processing"]).limit(50).execute())
    return len(getattr(rows, "data", None) or [])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be asked for; spend nothing")
    ap.add_argument("--limit", type=int, default=0, help="stop after N assets")
    ap.add_argument("--key", help="only this asset_key")
    ap.add_argument("--force", action="store_true",
                    help="run even if production has live jobs")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    _load_env()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    from PIL import Image

    from shared import visual_library as vl
    from spike.scene_engine.raster_assets import (annotate_regions,
                                                  part_names_from_description,
                                                  part_names_from_prompt)

    sb = vl._sb()
    if sb is None:
        logger.error("no Supabase admin client — SUPABASE_URL and "
                     "SUPABASE_SERVICE_ROLE_KEY must be set")
        return 2
    if not args.dry_run and not args.force:
        live = _live_work(sb)
        if live:
            logger.error("REFUSING: %d job(s) queued or processing. This is a "
                         "batch; run it when production is idle, or --force.", live)
            return 2

    q = (sb.table("visual_assets")
         .select("id, asset_key, description, storage_path, asset_format, "
                 "status, group_count, vision")
         .eq("status", "approved").eq("asset_format", "png")
         .order("created_at", desc=False))
    if args.key:
        q = q.eq("asset_key", args.key)
    rows = getattr(q.execute(), "data", None) or []

    todo = []
    for r in rows:
        key = str(r.get("asset_key") or "")
        if key.startswith("avatar_"):
            continue                       # a face has no labelled parts
        if (vl.row_vision(r) or {}).get("regions"):
            continue                       # already annotated; never redo
        todo.append(r)
    if args.limit:
        todo = todo[:args.limit]

    # A TAIL SHARED BY TWO ASSETS IS NOT EITHER ASSET'S PARTS. Measured on the
    # live library: `cells_to_tissue` and `ciliated_epithelium` carry the same
    # 12-name tail (brain, heart, stomach, intestine, lungs, ...) and
    # `cotton_bud` carries `cell membrane, cytoplasm, nucleus, mitochondrion`
    # — a cotton bud has no cytoplasm. The compiler pasted a CHAPTER's part
    # list onto every asset of that chapter, so the tail is authoritative in
    # form and wrong in fact.
    #
    # Asking vision for a part the picture cannot contain is not merely a
    # wasted call: a model handed a name and an image will sometimes find
    # something, and a stored region is a permanent instruction to point an
    # arrow there. This file already carries scars about confident arrows on
    # the wrong structure. Uniqueness is the one signal available without
    # judging content, and it keeps `human_body_organs` (brain, heart, lungs —
    # correct for a body outline) while refusing the copy of the same list
    # that landed on a tissue diagram.
    import collections
    tails = collections.Counter(tuple(part_names_from_prompt(
        str(r.get("description") or "")) or ()) for r in todo)
    shared = {t for t, c in tails.items() if t and c > 1}
    if shared:
        before = len(todo)
        todo = [r for r in todo
                if tuple(part_names_from_prompt(str(r.get("description") or ""))
                         or ()) not in shared]
        logger.info("refused %d row(s) whose part list is shared with another "
                    "asset — a chapter list, not this picture's parts",
                    before - len(todo))

    logger.info("%d approved PNG row(s); %d need annotation", len(rows), len(todo))
    asked = found = skipped = failed = 0

    for i, r in enumerate(todo, 1):
        key = str(r.get("asset_key") or "?")
        desc = str(r.get("description") or "")
        # ONLY the explicit "Name the layer groups exactly:" tail. The
        # enumerated-parts reader is the render path's second tier and it is
        # right to have one — but it INFERS parts from prose, and on this
        # data it infers from the wrong prose. Measured on the live library:
        #
        #   cotton_bud           -> cell membrane, cytoplasm, nucleus, ...
        #   cells_to_tissue      -> brain, heart, stomach, intestine, lungs
        #   ciliated_epithelium  -> brain, heart, stomach, intestine, lungs
        #
        # A cotton bud has no cytoplasm. Annotating on those names would spend
        # a call and then write them into `annotated_for`, which records what
        # was ASKED — so the miss latches and the asset is permanently marked
        # as having been checked for parts it was never going to have. Doing
        # nothing leaves it repairable; guessing does not.
        names = part_names_from_prompt(desc)
        if not names:
            skipped += 1
            inferred = part_names_from_description(desc)
            logger.info("[%d/%d] %-34s SKIP — %s", i, len(todo), key[:34],
                        "names only INFERRED from prose, not authoritative"
                        if inferred else "its description names no parts")
            continue
        if args.dry_run:
            logger.info("[%d/%d] %-34s would ask for: %s",
                        i, len(todo), key[:34], ", ".join(names[:6]))
            continue
        try:
            blob = sb.storage.from_(vl.BUCKET).download(r["storage_path"])
            ink = Image.open(io.BytesIO(blob)).convert("RGBA")
            ann = annotate_regions(ink, names)
            regions = ann.get("regions") or {}
            payload = vl.vision_payload(regions, list(names),
                                        bool(ann.get("has_text")),
                                        ink.width, ink.height)
            vl.record_vision(r["id"], payload)
            asked += 1
            found += len(regions)
            logger.info("[%d/%d] %-34s %d/%d parts located%s", i, len(todo),
                        key[:34], len(regions), len(names),
                        "" if regions else "  <- vision saw none")
        except Exception as exc:            # noqa: BLE001 — one bad row must not stop the run
            failed += 1
            logger.warning("[%d/%d] %-34s FAILED: %s", i, len(todo), key[:34], exc)

    logger.info("done — %d annotated, %d region(s) located, %d skipped, %d failed",
                asked, found, skipped, failed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
