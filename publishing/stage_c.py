"""Stage C: queue EXISTING catalogue jobs for an approved book's gaps.

Two steps, with founder approval between them:

  plan  builds an explicit job list from the latest audit of approved
        blueprints: figure renders for topics whose only gap is figures, and
        topic_article jobs for new topics and content revisions (hints are
        written out). Nothing is sent. The plan is written to JSON + Markdown
        for review.
  run   executes an approved plan, ONE job at a time:
          - only inside the 20:00-05:00 UTC window;
          - only when production has no queued/processing job at all;
          - re-checks each job's preconditions live before inserting it;
          - inserts the job exactly as the Library portal does, plus its
            library_<verb> audit row;
          - waits for that job to finish before the next one.
        The write client can POST only to `jobs` and `platform_audit_log`,
        only job types topic_article / figure_render.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from publishing.guard import in_off_peak
from publishing.snapshot import client as read_client, load_env

_log = logging.getLogger(__name__)

FOUNDER_ACTOR = "9ad3649c-9776-4e13-b998-6c41f4fa9956"   # muqtadar1984@gmail.com; same actor as earlier founder-instructed sets
JOB_TYPES = {"topic_article", "figure_render"}
WRITE_TABLES = {"jobs", "platform_audit_log"}
JOB_TIMEOUT_S = 45 * 60


class NotAllowed(RuntimeError):
    pass


# -- plan -------------------------------------------------------------------------
def _hints_for(audit_detail: dict, node_titles: dict[str, str], profile: str, purpose: str | None) -> str:
    lines = ["Study-guide depth revision approved by the founder at Gate B (2026-10-24). "
             "Keep everything that is already correct and keep the article's structure; improve it so that:"]
    det = audit_detail.get("detail", "")
    wc = audit_detail.get("word_count")
    if wc is not None and "words <" in det:
        lines.append(f"- it reaches at least 1,000 words (now {wc}) through fuller explanation, not padding;")
    need = 3 if profile == "maths" else 1
    if "worked examples <" in det:
        lines.append(f"- it has at least {need} fully worked example(s), every step shown"
                     + (" and mathematically verifiable;" if profile == "maths" else ", each tied to an objective;"))
    if "sections do not cover" in det:
        codes = det.split("sections do not cover", 1)[1].split("]")[0].strip(" [").replace("'", "").split(", ")
        for code in [c for c in codes if c]:
            lines.append(f"- a section explicitly teaches objective {code}: {node_titles.get(code, '')}")
    if purpose:
        lines.append(f"- it serves the study-guide chapter: {purpose}")
    return "\n".join(lines)[:4000]


def build_plan(conn, books: list[str], out_dir: Path, env_file: Path) -> Path:
    """Explicit job list for the approved books, re-verified against LIVE rows."""
    url, key = load_env(env_file)
    node_titles = {r["code"]: r["title"] for r in conn.execute("select code, title from cat.curriculum_nodes")}
    rows = conn.execute("""
      with runs as (select distinct on (b.id) ar.id run_id, b.id bp_id, w.slug, w.brief, b.document
                    from pub.blueprints b join pub.works w on w.uid=b.work_uid
                    join pub.audit_runs ar on ar.blueprint_id=b.id
                    where b.status='approved' and w.slug = any(%s) order by b.id, ar.id desc)
      select runs.slug book, runs.brief->>'profile' profile, runs.document doc, r.id rid, r.topic_id, r.proposed_title,
             r.node_codes, r.rationale, c.sort chapter, ts.state,
             (select ai.evidence || jsonb_build_object('sub', ai.sub_status) from pub.audit_items ai
               where ai.run_id=runs.run_id and ai.required_topic_id=r.id and ai.check_code='B4' and ai.verdict='fail' limit 1) b4,
             (select ai.verdict from pub.audit_items ai where ai.run_id=runs.run_id and ai.required_topic_id=r.id
               and ai.check_code='B3' limit 1) b3
      from runs join pub.required_topics r on r.blueprint_id=runs.bp_id
      join pub.blueprint_chapters c on c.chapter_uid=r.chapter_uid
      left join pub.topic_states ts on ts.run_id=runs.run_id and ts.required_topic_id=r.id
      order by runs.slug, c.sort, r.id""", (books,)).fetchall()

    jobs, seen = [], set()
    with read_client(url, key) as http:
        for r in rows:
            if not r["topic_id"] or str(r["topic_id"]) in seen:
                continue
            seen.add(str(r["topic_id"]))
            tid = str(r["topic_id"])
            topic = http.get("/topics", params={"select": "id,slug,title,status", "id": f"eq.{tid}"}).json()[0]
            arts = http.get("/topic_articles", params={"select": "id,version", "topic_id": f"eq.{tid}",
                                                       "language": "eq.en", "status": "eq.approved"}).json()
            ch = next((c for c in r["doc"]["chapters"] if c["number"] == r["chapter"]), {})
            purpose = f"chapter {r['chapter']} '{ch.get('title', '')}' — {ch.get('purpose', '')}" if ch else None
            if not arts:
                codes = ", ".join(f"{c} ({node_titles.get(c, '')})" for c in r["node_codes"])
                hints = (f"New topic for a SketchCast study guide approved by the founder at Gate B (2026-10-24).\n"
                         f"- Objectives to teach fully: {codes}\n- It serves the study-guide {purpose}\n"
                         f"- Why it is needed: {r['rationale']}")[:4000]
                jobs.append({"type": "topic_article", "topic_id": tid, "slug": topic["slug"], "book": r["book"],
                             "params": {"topic_id": tid, "language": "en", "hints": hints, "source_article_id": None},
                             "audit": {"verb": "article_generate", "detail": {"hints": True, "language": "en",
                                                                              "source_article_id": None}},
                             "precondition": {"topic_status_in": ["approved", "article_approved", "video_approved",
                                                                  "in_review", "published"], "no_approved_article": True}})
                continue
            art = arts[0]
            b4 = r["b4"] or {}
            if b4.get("sub") == "content":
                hints = _hints_for(b4, node_titles, r["profile"], purpose)
                jobs.append({"type": "topic_article", "topic_id": tid, "slug": topic["slug"], "book": r["book"],
                             "params": {"topic_id": tid, "language": "en", "hints": hints,
                                        "source_article_id": art["id"]},
                             "audit": {"verb": "article_generate", "detail": {"hints": True, "language": "en",
                                                                              "source_article_id": art["id"]}},
                             "precondition": {"approved_article_id": art["id"]}})
            elif b4.get("sub") == "figures_only":
                figs = b4.get("figures_unrendered") or []
                jobs.append({"type": "figure_render", "topic_id": tid, "slug": topic["slug"], "book": r["book"],
                             "params": {"article_id": art["id"]},
                             "audit": {"verb": "figures_render", "detail": {"figures": len(figs), "version": art["version"],
                                                                            "article_id": art["id"]}},
                             "precondition": {"approved_article_id": art["id"]}, "figures": figs})
    # Figure renders first (no new version, the cheapest step to clear), then articles.
    jobs.sort(key=lambda j: (j["type"] != "figure_render", j["book"], j["slug"]))
    for i, j in enumerate(jobs, 1):
        j["n"] = i
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    path = out_dir / f"batch-{'+'.join(b.split('-')[0] for b in books)}-{stamp}.plan.json"
    path.write_text(json.dumps({"books": books, "built_at": datetime.now(timezone.utc).isoformat(), "jobs": jobs},
                               indent=1, ensure_ascii=False), encoding="utf-8")
    md = [f"# Stage C job plan: {', '.join(books)}\n", f"{len(jobs)} jobs, run one at a time, 20:00–05:00 UTC, "
          "only when production's queue is empty.\n"]
    for j in jobs:
        what = (f"render {len(j.get('figures', []))} figure(s): {', '.join(j.get('figures', []))}"
                if j["type"] == "figure_render" else
                ("NEW article" if not j["params"]["source_article_id"] else "revised article (new version from the approved one)"))
        md.append(f"\n## {j['n']}. `{j['type']}` — {j['slug']} ({j['book'].split('-')[0]})\n{what}\n")
        if j["type"] == "topic_article":
            md.append("```\n" + j["params"]["hints"] + "\n```\n")
    path.with_suffix(".md").write_text("\n".join(md), encoding="utf-8")
    return path


# -- run --------------------------------------------------------------------------
def _write_client(url: str, key: str) -> httpx.Client:
    def guard(request: httpx.Request) -> None:
        if request.method == "GET":
            return
        table = request.url.path.rsplit("/", 1)[-1]
        if request.method != "POST" or table not in WRITE_TABLES:
            raise NotAllowed(f"stage C client refuses {request.method} {request.url.path}")
        if table == "jobs" and json.loads(request.content).get("type") not in JOB_TYPES:
            raise NotAllowed("stage C may only queue topic_article / figure_render jobs")
    return httpx.Client(base_url=f"{url}/rest/v1",
                        headers={"apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json",
                                 "Prefer": "return=representation"},
                        event_hooks={"request": [guard]}, timeout=60.0)


def _busy(http) -> list[dict]:
    r = http.get("/jobs", params={"select": "id,type,status", "status": "in.(queued,processing)"})
    r.raise_for_status()
    return r.json()


def _precondition_ok(http, job: dict) -> str | None:
    pre = job["precondition"]
    live = http.get("/jobs", params={"select": "id", "status": "in.(queued,processing)", "type": f"eq.{job['type']}",
                                     ("params->>article_id" if job["type"] == "figure_render" else "params->>topic_id"):
                                     f"eq.{job['params'].get('article_id') or job['topic_id']}"}).json()
    if live:
        return "a live job already exists for this target"
    arts = http.get("/topic_articles", params={"select": "id", "topic_id": f"eq.{job['topic_id']}",
                                               "language": "eq.en", "status": "eq.approved"}).json()
    if "approved_article_id" in pre and (not arts or arts[0]["id"] != pre["approved_article_id"]):
        return "the approved article changed since the plan was built"
    if pre.get("no_approved_article") and arts:
        return "an approved article now exists"
    if "topic_status_in" in pre:
        t = http.get("/topics", params={"select": "status", "id": f"eq.{job['topic_id']}"}).json()
        if not t or t[0]["status"] not in pre["topic_status_in"]:
            return f"topic status is {t[0]['status'] if t else 'missing'}"
    return None


def run_plan(plan_path: Path, env_file: Path, log_path: Path, *, allow_peak: bool = False) -> None:
    """`allow_peak` = the founder lifted the off-peak window for this run; the
    empty-queue check still applies before every job."""
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    done_path = plan_path.with_suffix(".done.json")
    done = json.loads(done_path.read_text(encoding="utf-8")) if done_path.exists() else {}
    url, key = load_env(env_file)

    def log(msg: str) -> None:
        line = f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} {msg}"
        _log.info(msg)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    with _write_client(url, key) as http:
        for job in plan["jobs"]:
            k = str(job["n"])
            if k in done:
                continue
            while True:
                if not allow_peak and not in_off_peak():
                    time.sleep(300)
                    continue
                busy = _busy(http)
                if busy:
                    log(f"waiting: production has {len(busy)} live job(s) {sorted({b['type'] for b in busy})}")
                    time.sleep(300)
                    continue
                break
            why = _precondition_ok(http, job)
            if why:
                log(f"SKIP #{k} {job['type']} {job['slug']}: {why}")
                done[k] = {"status": "skipped", "why": why}
                done_path.write_text(json.dumps(done, indent=1), encoding="utf-8")
                continue
            r = http.post("/jobs", json={"type": job["type"], "params": job["params"], "book_id": None,
                                         "generation_id": None, "status": "queued"})
            r.raise_for_status()
            job_id = r.json()[0]["id"]
            detail = {**job["audit"]["detail"], "job_id": job_id,
                      "by": "agent on founder instruction 2026-10-24 (study-guide Stage C)"}
            http.post("/platform_audit_log", json={"actor_id": FOUNDER_ACTOR, "action": f"library_{job['audit']['verb']}",
                                                   "target_kind": "topic", "target_id": job["topic_id"], "detail": detail})
            log(f"QUEUED #{k} {job['type']} {job['slug']} job={job_id}")
            t0 = time.time()
            status = "queued"
            while time.time() - t0 < JOB_TIMEOUT_S:
                time.sleep(30)
                row = http.get("/jobs", params={"select": "status,error", "id": f"eq.{job_id}"}).json()[0]
                status = row["status"]
                if status not in ("queued", "processing"):
                    break
            log(f"FINISHED #{k} {job['slug']} job={job_id} status={status}"
                + (f" error={(row.get('error') or '')[:160]}" if status != "done" else ""))
            done[k] = {"status": status, "job_id": job_id}
            done_path.write_text(json.dumps(done, indent=1), encoding="utf-8")
            if status in ("queued", "processing"):
                log("job still running after the timeout; stopping the run so nothing overlaps")
                return
    log("plan complete")
