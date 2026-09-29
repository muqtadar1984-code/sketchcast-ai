"""Ship a sample of the ink visual library to the scratch table
``colour_calibration_assets`` — the relay for the board-colour library
calibration (founder, 2026-09-29).

The library's pictures live in the private ``visual-assets`` bucket, which
the engineering session cannot reach; the worker can. Once per boot, when
COLOUR_CALIBRATION_SAMPLE is set to a count, this pass downloads that many
approved board pictures (the most-annotated ones, plus a few margin
sketches) and upserts each as base64 PNG with its description and vision
regions. Nothing else changes: no library row is touched, no picture is
generated. Dark without the variable; never raises into the reaper. The
table is dropped when the calibration is done.
"""

from __future__ import annotations

import base64
import logging
import os
import threading
from typing import Optional

log = logging.getLogger("worker")

ENV = "COLOUR_CALIBRATION_SAMPLE"
TABLE = "colour_calibration_assets"
SKETCHES = 3  # of the sample, this many are margin sketches (sk_*)

_lock = threading.Lock()
_done = False


def sample_size() -> int:
    raw = os.getenv(ENV, "").strip()
    try:
        return max(0, int(raw)) if raw else 0
    except ValueError:
        return 0


def pick_rows(sb, n: int) -> list[dict]:
    """The most-annotated approved board pictures, and a few margin
    sketches, as library rows."""
    cols = "asset_key, storage_path, description, vision, group_count"
    board = (sb.table("visual_assets").select(cols).eq("status", "approved").is_("role", "null")
             .eq("asset_format", "png").not_.like("asset_key", "sk\\_%").not_.like("asset_key", "avatar%")
             .order("group_count", desc=True).limit(max(0, n - SKETCHES)).execute().data or [])
    sk = (sb.table("visual_assets").select(cols).eq("status", "approved").is_("role", "null")
          .eq("asset_format", "png").like("asset_key", "sk\\_%")
          .order("created_at", desc=True).limit(SKETCHES).execute().data or [])
    return list(board) + list(sk)


def ship(sb, n: int, *, bucket: Optional[str] = None) -> list[str]:
    """Download and upsert ``n`` rows; returns the keys shipped."""
    import io

    from PIL import Image

    from shared.visual_library import BUCKET

    shipped: list[str] = []
    for row in pick_rows(sb, n):
        key = str(row.get("asset_key") or "")
        path = str(row.get("storage_path") or "")
        if not key or not path:
            continue
        try:
            data = sb.storage.from_(bucket or BUCKET).download(path)
            im = Image.open(io.BytesIO(data))
            w, h = im.size
            vision = row.get("vision") if isinstance(row.get("vision"), dict) else {}
            sb.table(TABLE).upsert({
                "asset_key": key, "storage_path": path,
                "description": row.get("description"),
                "regions": vision.get("regions") or vision or {},
                "width": w, "height": h,
                "png_b64": base64.b64encode(data).decode("ascii"),
            }).execute()
            shipped.append(key)
        except Exception as exc:  # noqa: BLE001 — one bad row never stops the sample
            log.warning("colour calibration sample: %r not shipped: %s", key, exc)
    log.info("colour calibration sample: %d of %d asset(s) shipped to %s", len(shipped), n, TABLE)
    return shipped


def maybe_ship(sb) -> Optional[list[str]]:
    """Called from the reaper tick: once per process, dark without the
    variable, never raises."""
    global _done
    n = sample_size()
    if not n:
        return None
    with _lock:
        if _done:
            return None
        _done = True
    try:
        return ship(sb, n)
    except Exception as exc:  # noqa: BLE001
        log.error("colour calibration sample failed: %s", exc)
        return None


__all__ = ["ENV", "TABLE", "sample_size", "pick_rows", "ship", "maybe_ship"]
