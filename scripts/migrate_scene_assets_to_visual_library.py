"""Publish existing validated scene assets into the persistent visual library.

Run once after the ``visual_assets`` table and ``visual-assets`` bucket exist:

    python scripts/migrate_scene_assets_to_visual_library.py

The script is intentionally explicit rather than running on every worker boot.
It does not regenerate anything and therefore does not spend image credits.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from shared.visual_library import publish_generated


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path("storage/scene_assets"),
        help="Existing Scene Engine raster cache",
    )
    args = parser.parse_args()

    metas = sorted(args.cache_dir.glob("*/meta.json"))
    published = 0
    skipped = 0
    for meta_path in metas:
        png = meta_path.parent / "asset.png"
        if not png.exists():
            skipped += 1
            continue
        try:
            md = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            skipped += 1
            continue
        if md.get("provenance") != "generated" or md.get("baked_text"):
            skipped += 1
            continue
        key = str(md.get("key") or meta_path.parent.name)
        if publish_generated(key, str(md.get("prompt") or key), png, md):
            published += 1
    print(f"visual library migration: published={published} skipped={skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
