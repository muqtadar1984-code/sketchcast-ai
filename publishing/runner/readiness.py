"""Stage `readiness` — free, no model calls. Re-checks how ready a book is to draft, against the catalogue as it is NOW:

  1. a fresh snapshot of the production catalogue into the local database (publishing.snapshot, GET-only),
  2. the readiness audit for the book (publishing.audit.run_audits — the same command as `cli audit <slug>`),
  3. readiness.json in the run's folder: the percentage, ready or not, and what is outstanding, topic by topic.

    python -m publishing.runner.readiness <slug> <run_rel>

The runner restores the shared publishing database first and pushes it back afterwards (the audit and the snapshot are
recorded there, as on the PC).
"""
from __future__ import annotations

import collections
import json
import os
import sys
from pathlib import Path


def main() -> None:
    from publishing import localdb
    from publishing.audit import run_audits
    from publishing.snapshot import take_snapshot
    slug, rel = sys.argv[1], sys.argv[2]
    out = Path(os.environ["PUBLISH_OUT"]) / "manuscripts" / slug / rel
    out.mkdir(parents=True, exist_ok=True)
    env_file = out / "empty.env"
    env_file.write_text("", encoding="utf-8")            # Supabase from the environment (load_env falls back to it)
    conn = localdb.connect()
    print("taking a fresh catalogue snapshot (production, read-only)…", flush=True)
    snap = take_snapshot(conn, env_file)
    conn.commit()
    print(f"  snapshot: {json.dumps(snap.get('row_counts', snap), default=str)[:300]}", flush=True)
    print(f"auditing {slug}…", flush=True)
    run_audits(conn, slug)
    conn.commit()
    row = conn.execute(
        "select r.pct, r.ready, r.outstanding, r.evaluated_at from pub.readiness_snapshots r join pub.works w "
        "on w.uid = r.work_uid where w.slug = %s order by r.id desc limit 1", (slug,)).fetchone()
    env_file.unlink()
    if not row:
        raise SystemExit(f"{slug}: no readiness was recorded (does the book have a blueprint?)")
    states = collections.Counter(o["state"] for o in row["outstanding"] or [])
    result = {"slug": slug, "pct": float(row["pct"]), "ready": row["ready"], "evaluated_at": str(row["evaluated_at"]),
              "outstanding_by_state": dict(states), "outstanding": row["outstanding"]}
    (out / "readiness.json").write_text(json.dumps(result, indent=1, default=str, ensure_ascii=False), encoding="utf-8")
    print(f"{slug}: {result['pct']}% ready{' — READY to draft' if row['ready'] else ''}; outstanding {dict(states)}",
          flush=True)
    for o in row["outstanding"] or []:
        print(f"  · {o['state']}: {o['title']} ({', '.join(o.get('failing') or [])})", flush=True)


if __name__ == "__main__":
    main()
