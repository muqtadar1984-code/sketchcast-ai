"""Stage `gate_r` — free, no model calls. Gate R for one book, against the catalogue as it is NOW:

  1. a fresh snapshot of the production catalogue into the local database (publishing.snapshot, GET-only),
  2. Gate R itself (publishing.pins.gate_r — the same as `cli gate-r <slug>`): re-audit, and ONLY when every topic the
     book needs is ready, turn the audit's pins into real ones, open English edition 1 and set the book to drafting,
  3. gate_r.json in the run's folder: passed or not, the percentage and what is still outstanding.

    python -m publishing.runner.gate_r <slug> <run_rel>

A book that is not 100% ready changes nothing but the audit record. The runner restores the shared publishing database
first and pushes it back afterwards, so the pins are what the drafting stage reads next.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> None:
    from publishing import localdb
    from publishing.pins import gate_r
    from publishing.runner.readiness import gaps
    from publishing.snapshot import take_snapshot
    slug, rel = sys.argv[1], sys.argv[2]
    out = Path(os.environ["PUBLISH_OUT"]) / "manuscripts" / slug / rel
    out.mkdir(parents=True, exist_ok=True)
    env_file = out / "empty.env"
    env_file.write_text("", encoding="utf-8")            # Supabase from the environment (load_env falls back to it)
    conn = localdb.connect()
    print("taking a fresh catalogue snapshot (production, read-only)…", flush=True)
    take_snapshot(conn, env_file)
    conn.commit()
    env_file.unlink()
    print(f"Gate R for {slug}…", flush=True)
    r = gate_r(conn, slug)
    conn.commit()
    result = {"slug": slug, "passed": bool(r["ready"]), **r}
    if r["ready"]:
        print(f"{slug}: PASSED — {r['pins']} sources pinned, edition {r['edition_id']} open for drafting", flush=True)
    else:
        result.update(gaps(conn, slug))
        print(f"{slug}: not ready ({r['pct']}%) — nothing pinned; {len(r['outstanding'] or [])} topic(s) outstanding",
              flush=True)
    (out / "gate_r.json").write_text(json.dumps(result, indent=1, default=str, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
