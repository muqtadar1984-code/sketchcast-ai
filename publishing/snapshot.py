"""Read-only snapshot of production catalogue rows into the local ``cat`` schema.

Production is reached ONLY through PostgREST GET requests. The HTTP client
carries a request hook that raises on any other method, so this module cannot
write to production even by mistake. The service key is read from the
worker's .env and never printed.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import httpx
from psycopg.types.json import Jsonb

_log = logging.getLogger(__name__)

PAGE = 1000

# (table, select, extra filter query, local columns). The publication select
# deliberately leaves out ``upload_token``.
TABLES: list[tuple[str, str, dict, list[str]]] = [
    ("curricula", "id,code,name,kind,country,edition,source_url", {},
     ["id", "code", "name", "kind", "country", "edition", "source_url"]),
    ("curriculum_nodes", "id,curriculum_id,code,grade,strand,sub_strand,title,description,kind,parent_id,sort", {},
     ["id", "curriculum_id", "code", "grade", "strand", "sub_strand", "title", "description", "kind", "parent_id", "sort"]),
    ("topics", "id,canonical_key,slug,title,subject,summary,status,bank_maturity,prerequisites", {},
     ["id", "canonical_key", "slug", "title", "subject", "summary", "status", "bank_maturity", "prerequisites"]),
    ("topic_curriculum_map", "id,topic_id,node_id,coverage,notes", {},
     ["id", "topic_id", "node_id", "coverage", "notes"]),
    ("topic_articles",
     "id,topic_id,version,language,source_article_id,title,status,word_count,objectives,sections,glossary,misconceptions,worked_examples,claims,reviewed_at,updated_at",
     {"language": "in.(en,ms)"},
     ["id", "topic_id", "version", "language", "source_article_id", "title", "status", "word_count", "objectives",
      "sections", "glossary", "misconceptions", "worked_examples", "claims", "reviewed_at", "updated_at"]),
    ("article_figures", "id,article_id,figure_key,status,visual_asset_id", {},
     ["id", "article_id", "figure_key", "status", "visual_asset_id"]),
    ("topic_questions", "id,topic_id,language,status,item_type", {"language": "in.(en,ms)"},
     ["id", "topic_id", "language", "status", "item_type"]),
    ("topic_kits", "id,topic_id,article_id,language,source_kit_id,status,translation_status,part_plan,reviewed_at,updated_at,created_at",
     {"language": "in.(en,ms)"},
     ["id", "topic_id", "article_id", "language", "source_kit_id", "status", "translation_status", "part_plan",
      "reviewed_at", "updated_at", "created_at"]),
    ("topic_publications", "id,topic_kit_id,part,channel_language,youtube_video_id,privacy,youtube_upload_status,error,superseded_by,published_at",
     {}, ["id", "topic_kit_id", "part", "channel_language", "youtube_video_id", "privacy", "youtube_upload_status",
          "error", "superseded_by", "published_at"]),
    ("youtube_publish_queue", "id,kit_id,topic_id,language,status,attempts,last_error,updated_at", {},
     ["id", "kit_id", "topic_id", "language", "status", "attempts", "last_error", "updated_at"]),
    ("jobs", "id,type,status,attempts,usage,params,error,created_at,updated_at",
     {"or": "(params->>topic_id.not.is.null,params->>kit_id.not.is.null,params->>article_id.not.is.null)"},
     ["id", "type", "status", "attempts", "usage", "params", "error", "created_at", "updated_at"]),
]

JSON_COLS = {"objectives", "sections", "glossary", "misconceptions", "worked_examples", "claims",
             "part_plan", "usage", "params"}


class WriteAttempt(RuntimeError):
    pass


def _get_only(request: httpx.Request) -> None:
    if request.method != "GET":
        raise WriteAttempt(f"snapshot client refuses {request.method} {request.url.path}")


def load_env(env_file: Path) -> tuple[str, str]:
    url = key = ""
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("SUPABASE_URL="):
            url = line.split("=", 1)[1].strip().strip('"')
        elif line.startswith("SUPABASE_SERVICE_ROLE_KEY="):
            key = line.split("=", 1)[1].strip().strip('"')
    url = os.getenv("SUPABASE_URL", url)
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", key)
    if not url or not key:
        raise RuntimeError(f"SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not found in {env_file}")
    return url.rstrip("/"), key


def client(url: str, key: str) -> httpx.Client:
    return httpx.Client(
        base_url=f"{url}/rest/v1",
        headers={"apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json"},
        event_hooks={"request": [_get_only]},
        timeout=60.0,
    )


def fetch_all(http: httpx.Client, table: str, select: str, filters: dict) -> list[dict]:
    rows: list[dict] = []
    start = 0
    while True:
        params = {"select": select, "order": "id", **filters}
        r = http.get(f"/{table}", params=params,
                     headers={"Range-Unit": "items", "Range": f"{start}-{start + PAGE - 1}"})
        r.raise_for_status()
        batch = r.json()
        rows.extend(batch)
        if len(batch) < PAGE:
            return rows
        start += PAGE


def take_snapshot(conn, env_file: Path) -> dict:
    url, key = load_env(env_file)
    counts: dict[str, int] = {}
    with client(url, key) as http:
        data = {t: fetch_all(http, t, sel, flt) for t, sel, flt, _ in TABLES}
        aliases = fetch_all(http, "topic_aliases", "*", {})
        r = http.get("/rpc/catalogue_current_kits")
        r.raise_for_status()
        current = r.json()

    with conn.transaction():
        sid = conn.execute("insert into cat.snapshots (source_url) values (%s) returning id",
                           (url,)).fetchone()["id"]
        for table, _sel, _flt, cols in TABLES:
            conn.execute(f"truncate cat.{table}")
            rows = data[table]
            with conn.cursor() as cur:
                cur.executemany(
                    f"insert into cat.{table} (snapshot_id,{','.join(cols)}) values (%s,{','.join(['%s'] * len(cols))})",
                    [(sid, *[Jsonb(r.get(c)) if c in JSON_COLS and r.get(c) is not None else r.get(c) for c in cols])
                     for r in rows])
            counts[table] = len(rows)
        conn.execute("truncate cat.topic_aliases")
        with conn.cursor() as cur:
            cur.executemany("insert into cat.topic_aliases (snapshot_id,row) values (%s,%s)",
                            [(sid, Jsonb(a)) for a in aliases])
        counts["topic_aliases"] = len(aliases)
        conn.execute("truncate cat.current_kits_live")
        with conn.cursor() as cur:
            cur.executemany(
                "insert into cat.current_kits_live (snapshot_id,topic_id,language,topic_kit_id,article_id,approved_at) "
                "values (%s,%s,%s,%s,%s,%s)",
                [(sid, c["topic_id"], c["language"], c["topic_kit_id"], c["article_id"], c["approved_at"])
                 for c in current])
        counts["current_kits_live"] = len(current)
        conn.execute("update cat.snapshots set row_counts=%s where id=%s", (Jsonb(counts), sid))
    _log.info("snapshot %s: %s", sid, json.dumps(counts))
    return {"snapshot_id": sid, "counts": counts}


def latest_snapshot_id(conn) -> int:
    row = conn.execute("select max(id) as id from cat.snapshots").fetchone()
    if not row or row["id"] is None:
        raise RuntimeError("no snapshot yet; run `python -m publishing.cli snapshot`")
    return row["id"]
