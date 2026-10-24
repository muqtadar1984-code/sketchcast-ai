"""Never starve production: the off-peak window and the live `jobs` queue check.

Any step that calls a paid model must pass both checks first:
  1. the clock is inside the off-peak window (20:00-05:00 UTC), unless the
     founder approved a peak-hours run;
  2. production has no queued or processing job (read with a GET request).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from publishing.snapshot import client, load_env

OFF_PEAK_START, OFF_PEAK_END = 20, 5


class ProductionBusy(RuntimeError):
    pass


def in_off_peak(now: datetime | None = None) -> bool:
    h = (now or datetime.now(timezone.utc)).hour
    return h >= OFF_PEAK_START or h < OFF_PEAK_END


def production_queue(env_file: Path) -> dict:
    url, key = load_env(env_file)
    with client(url, key) as http:
        r = http.get("/jobs", params={"select": "id,type,status", "status": "in.(queued,processing)"})
        r.raise_for_status()
        rows = r.json()
    return {"busy": len(rows), "types": sorted({j["type"] for j in rows})}


def require_quiet(env_file: Path, *, allow_peak: bool = False) -> None:
    if not allow_peak and not in_off_peak():
        raise ProductionBusy("outside the 20:00-05:00 UTC off-peak window")
    q = production_queue(env_file)
    if q["busy"]:
        raise ProductionBusy(f"production has {q['busy']} queued/processing job(s): {q['types']}")
