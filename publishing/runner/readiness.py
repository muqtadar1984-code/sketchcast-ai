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


def gaps(conn, slug: str) -> dict:
    """Every topic the latest audit found outstanding, with what to do about it: its state and failing checks, the
    catalogue topic it maps to (none for a topic that does not exist yet), the chapter and objectives it serves, and
    the planner's ordered next steps with measured cost and review time (publishing/planner.py → pub.dependency_steps).
    The book app lists these on the book's Gaps view and links each step to the Library action that carries it out."""
    run = conn.execute(
        "select ar.id from pub.audit_runs ar join pub.blueprints b on b.id = ar.blueprint_id "
        "join pub.works w on w.uid = b.work_uid where w.slug = %s order by ar.id desc limit 1", (slug,)).fetchone()
    cur = conn.execute(
        "select c.id, c.code, c.name from cat.curricula c join pub.works w on w.primary_curriculum_code = c.code "
        "where w.slug = %s", (slug,)).fetchone()
    if not run:
        return {"gaps": [], "curriculum": None}
    rows = conn.execute("""
        select r.id, coalesce(t.title, r.proposed_title) as title, r.topic_id, t.slug as topic_slug, t.status as topic_status,
               r.node_codes, r.role, c.sort as chapter, c.title as chapter_title, ts.state, ts.failing_checks
        from pub.topic_states ts
        join pub.required_topics r on r.id = ts.required_topic_id
        join pub.blueprint_chapters c on c.chapter_uid = r.chapter_uid
        left join cat.topics t on t.id = r.topic_id
        where ts.run_id = %s and exists (select 1 from pub.dependency_steps d where d.run_id = ts.run_id
                                         and d.required_topic_id = r.id)
        order by c.sort, r.id""", (run["id"],)).fetchall()
    steps = collections.defaultdict(list)
    for s in conn.execute("select required_topic_id, step_order, step, job_type, language, est_cost_usd, est_review_min, "
                          "throughput_note from pub.dependency_steps where run_id = %s order by required_topic_id, step_order",
                          (run["id"],)).fetchall():
        steps[s["required_topic_id"]].append({
            "order": s["step_order"], "step": s["step"], "job_type": s["job_type"], "language": s["language"],
            "usd": float(s["est_cost_usd"]) if s["est_cost_usd"] is not None else None,
            "review_min": s["est_review_min"], "note": s["throughput_note"]})
    out = []
    for r in rows:
        out.append({"title": r["title"], "topic_id": str(r["topic_id"]) if r["topic_id"] else None,
                    "topic_slug": r["topic_slug"], "topic_status": r["topic_status"], "chapter": r["chapter"],
                    "chapter_title": r["chapter_title"], "node_codes": r["node_codes"], "role": r["role"],
                    "state": r["state"], "failing": r["failing_checks"], "steps": steps[r["id"]]})
    return {"gaps": out, "curriculum": {"id": str(cur["id"]), "code": cur["code"], "name": cur["name"]} if cur else None}


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
              "outstanding_by_state": dict(states), "outstanding": row["outstanding"], **gaps(conn, slug)}
    (out / "readiness.json").write_text(json.dumps(result, indent=1, default=str, ensure_ascii=False), encoding="utf-8")
    print(f"{slug}: {result['pct']}% ready{' — READY to draft' if row['ready'] else ''}; outstanding {dict(states)}",
          flush=True)
    for o in row["outstanding"] or []:
        print(f"  · {o['state']}: {o['title']} ({', '.join(o.get('failing') or [])})", flush=True)


if __name__ == "__main__":
    main()
