"""Stage C: rebuild the English kit of topics whose article was revised.

Mirrors the Library portal exactly (app src/app/api/library/topics/[id]/kit/
route.ts), founder-approved 2026-10-25 ("portal way, accept the gap"):

  1. reject every APPROVED English kit of the topic with reject_topic_kit()
     (reason 'other', a note naming the revised article version). The RPC
     pulls the approval, moves the topic video_approved -> in_review and
     audits library_kit_reject itself;
  2. Regenerate: insert the new topic_kits row (source_kit_id = the newest
     rejected kit, its teacher avatar and voices), move the topic in_review ->
     generating (guarded; a lost race removes the row), insert the five
     generations owned by the catalogue account with kitGenerationParams,
     point the kit at them, enqueue the topic_questions job unless one is
     live, and write the library_kit_regenerate audit row.

One topic at a time. Each starts only when production has no queued or
processing job, and the next waits until this kit has left 'generating'.
The write client may only call reject_topic_kit and write the tables above.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from publishing.snapshot import load_env

ACTOR = "122fb511-42c1-43cc-9bce-302b4e11eec6"        # founder's Library account (muqtadar.quraishi@sketchcast.app)
CATALOGUE_OWNER = "9d41e12e-e9c9-4b82-b3a9-3421c2c57726"
KINDS = ["presentation", "activity", "case_study", "worksheet", "deck"]
BY = "agent on founder instruction 2026-10-25 (study-guide Stage C batch 1: kit rebuilds)"
KIT_TIMEOUT_S = 3 * 60 * 60

POST_OK = {"topic_kits", "generations", "jobs", "platform_audit_log", "reject_topic_kit"}
PATCH_OK = {"topic_kits", "topics"}
DELETE_OK = {"topic_kits"}


class NotAllowed(RuntimeError):
    pass


def _client(url: str, key: str) -> httpx.Client:
    def guard(req: httpx.Request) -> None:
        name = req.url.path.rsplit("/", 1)[-1]
        allowed = {"GET": None, "POST": POST_OK, "PATCH": PATCH_OK, "DELETE": DELETE_OK}.get(req.method, set())
        if allowed is None:
            return
        if name not in allowed:
            raise NotAllowed(f"kit rebuild refuses {req.method} {req.url.path}")
        if name == "jobs" and json.loads(req.content).get("type") != "topic_questions":
            raise NotAllowed("kit rebuild may only enqueue topic_questions jobs")
    return httpx.Client(base_url=f"{url}/rest/v1", headers={"apikey": key, "Authorization": f"Bearer {key}",
                        "Accept": "application/json", "Prefer": "return=representation"},
                        event_hooks={"request": [guard]}, timeout=60.0)


def voice_pair(avatar: str, lang: str = "en") -> dict:
    t, s = ("m", "f") if avatar == "male" else ("f", "m")
    return {"teacher": f"g-{lang}-{t}", "student": f"g-{lang}-student-{s}"}


def _natural(code: str):
    import re
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", code)]


def header_lines(mappings: list[dict]) -> list[str]:
    """curriculumHeaderLines() from app src/utils/catalogue/kit.ts."""
    by = {}
    for m in mappings:
        n = m.get("curriculum_nodes") or {}
        c = n.get("curricula") or {}
        if isinstance(c, list):
            c = c[0] if c else {}
        if not n or not c:
            continue
        e = by.setdefault(c["id"], {"c": c, "nodes": []})
        if not any(x["code"] == n["code"] for x in e["nodes"]):
            e["nodes"].append(n)
    out = []
    for e in sorted(by.values(), key=lambda e: (e["c"]["name"], e["c"]["code"])):
        nodes = sorted(e["nodes"], key=lambda n: _natural(n["code"]))
        use_codes = all(len(n["code"]) <= 10 and not any(ch.isspace() for ch in n["code"]) for n in nodes)
        grades = {(n.get("grade") or "").strip() for n in nodes} - {""}
        items = list(dict.fromkeys(n["code"] if use_codes else ((n.get("title") or "").strip() or n["code"]) for n in nodes))
        head = e["c"]["name"].strip() + (f" {e['c']['code'].strip()}" if e["c"]["code"].strip() else "")
        grade = (f" · {next(iter(grades))}" if not use_codes and len(grades) == 1
                 and all((n.get("grade") or "").strip() for n in nodes) else "")
        out.append(f"{head}{grade} · {', '.join(items)}")
    return out


def _one(rows):
    return rows[0] if rows else None


def rebuild(http: httpx.Client, slug: str, expected_article: str, log) -> str:
    t = _one(http.get("/topics", params={"select": "id,status", "slug": f"eq.{slug}"}).json())
    if not t:
        return "SKIP: topic missing"
    tid = t["id"]
    art = _one(http.get("/topic_articles", params={"select": "id,version", "topic_id": f"eq.{tid}", "language": "eq.en",
                                                   "status": "eq.approved"}).json())
    if not art or art["id"] != expected_article:
        return f"SKIP: approved article is {art and art['id']}, expected {expected_article}"
    kits = http.get("/topic_kits", params={"select": "id,status,teacher_avatar,article_id,created_at", "topic_id": f"eq.{tid}",
                                           "language": "eq.en", "order": "created_at.desc"}).json()
    if any(k["status"] in ("generating", "in_review") for k in kits):
        return "SKIP: a kit is already generating or in review"
    approved = [k for k in kits if k["status"] == "approved"]
    if t["status"] == "video_approved":
        if not approved:
            return "SKIP: video_approved topic without an approved kit"
        for k in approved:
            if k["article_id"] == art["id"]:
                return "SKIP: an approved kit already uses the approved article"
            r = http.post("/rpc/reject_topic_kit", json={
                "p_kit": k["id"], "p_reviewer": ACTOR, "p_reason": "other",
                "p_notes": f"Superseded by the revised article v{art['version']} (study-guide Stage C); "
                           f"a new kit is regenerated from it. [{BY}]"})
            r.raise_for_status()
            log(f"  rejected kit {k['id']}")
    elif t["status"] != "in_review":
        return f"SKIP: topic is {t['status']}"
    source = approved[0] if approved else next((k for k in kits if k["status"] == "rejected"), None)
    if not source:
        return "SKIP: no source kit to regenerate"
    avatar = source.get("teacher_avatar") if source.get("teacher_avatar") in ("male", "female") else "female"

    maps = http.get("/topic_curriculum_map", params={
        "select": "curriculum_nodes(code,title,grade,curricula(id,code,name))", "topic_id": f"eq.{tid}"}).json()
    header = header_lines(maps)
    kit = http.post("/topic_kits", json={"topic_id": tid, "article_id": art["id"], "language": "en",
                                         "source_kit_id": source["id"], "teacher_avatar": avatar,
                                         "voice_pair": voice_pair(avatar), "status": "generating"}).json()[0]
    moved = http.patch("/topics", params={"id": f"eq.{tid}", "status": "eq.in_review"},
                       json={"status": "generating"}).json()
    if not moved:
        http.delete("/topic_kits", params={"id": f"eq.{kit['id']}", "status": "eq.generating"})
        return "SKIP: topic moved under us; kit row removed"
    vp = voice_pair(avatar)
    params = {"catalogue": True, "topic_id": tid, "kit_id": kit["id"], "article_id": art["id"], "language": "en",
              "narration_style": "dialogue", "teacher_avatar": avatar, "tts_voice": vp["teacher"],
              "student_voice": vp["student"], "curriculum_header": header}
    gens = http.post("/generations", json=[{"kind": k, "owner_id": CATALOGUE_OWNER, "book_id": None, "chapter_ref": None,
                                            "school_id": None, "status": "queued", "params": params} for k in KINDS]).json()
    pres = next(g["id"] for g in gens if g["kind"] == "presentation")
    docs = {g["kind"]: g["id"] for g in gens if g["kind"] != "presentation"}
    http.patch("/topic_kits", params={"id": f"eq.{kit['id']}"},
               json={"presentation_generation_id": pres, "doc_generation_ids": docs}).raise_for_status()
    live_q = http.get("/jobs", params={"select": "id", "type": "eq.topic_questions", "params->>topic_id": f"eq.{tid}",
                                       "params->>language": "eq.en", "status": "in.(queued,processing)"}).json()
    if live_q:
        qid, existing = live_q[0]["id"], True
    else:
        qid = http.post("/jobs", json={"type": "topic_questions", "params": {"topic_id": tid, "article_id": art["id"],
                                       "language": "en"}, "book_id": None, "generation_id": None,
                                       "status": "queued"}).json()[0]["id"]
        existing = False
    http.post("/platform_audit_log", json={"actor_id": ACTOR, "action": "library_kit_regenerate", "target_kind": "topic",
              "target_id": tid, "detail": {"kit_id": kit["id"], "article_id": art["id"],
              "source_article_id": source["article_id"], "article_changed": source["article_id"] != art["id"],
              "language": "en", "teacher_avatar": avatar, "source_kit_id": source["id"],
              "generation_ids": {"presentation": pres, **docs}, "curriculum_header": header,
              "questions_job": qid, "questions_job_existing": existing, "questions_job_error": None,
              "pointer_error": None, "from": "in_review", "to": "generating", "by": BY}}).raise_for_status()
    log(f"  kit {kit['id']} queued from article v{art['version']} ({avatar}); header {header}")
    return f"QUEUED kit={kit['id']}"


def run(plan_path: Path, env_file: Path) -> None:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    done_path, log_path = plan_path.with_suffix(".done.json"), plan_path.with_suffix(".log")
    done = json.loads(done_path.read_text(encoding="utf-8")) if done_path.exists() else {}
    url, key = load_env(env_file)

    def log(msg):
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} {msg}\n")

    with _client(url, key) as http:
        for item in plan["topics"]:
            slug = item["slug"]
            if slug in done:
                continue
            while True:
                busy = http.get("/jobs", params={"select": "type", "status": "in.(queued,processing)"}).json()
                if not busy:
                    break
                log(f"waiting: production has {len(busy)} live job(s) {sorted({b['type'] for b in busy})}")
                time.sleep(300)
            log(f"START {slug}")
            result = rebuild(http, slug, item["approved_article_id"], log)
            log(f"{slug}: {result}")
            done[slug] = {"result": result}
            done_path.write_text(json.dumps(done, indent=1), encoding="utf-8")
            if not result.startswith("QUEUED"):
                continue
            kit_id = result.split("kit=")[1]
            t0 = time.time()
            while time.time() - t0 < KIT_TIMEOUT_S:
                time.sleep(60)
                k = _one(http.get("/topic_kits", params={"select": "status", "id": f"eq.{kit_id}"}).json())
                if k and k["status"] != "generating":
                    break
            log(f"FINISHED {slug}: kit {kit_id} is {k and k['status']}")
            done[slug]["kit_status"] = k and k["status"]
            done_path.write_text(json.dumps(done, indent=1), encoding="utf-8")
            if k and k["status"] == "generating":
                log("kit still generating after the timeout; stopping so kits never overlap")
                return
    log("plan complete")
