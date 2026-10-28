"""Where a run is read and recorded: the book store over PostgREST (production), or a JSON file (tests, dry runs).

The REST client refuses every request outside the run tables it needs, by method: GET on pub_*, PATCH on
pub_runs / pub_run_steps only, POST on the append-only run tables. It can never write a revision, an approval or
anything outside pub_* — the 0161 immutability trigger would refuse the first two anyway.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

WRITE = {"PATCH": {"pub_runs", "pub_run_steps"},
         "POST": {"pub_run_events", "pub_run_steps", "pub_artifacts", "pub_cost_lines"}}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RestStore:
    def __init__(self, url: str | None = None, key: str | None = None):
        import httpx
        url = url or os.environ["SUPABASE_URL"]
        key = key or os.environ["SUPABASE_SERVICE_ROLE_KEY"]

        def guard(request: httpx.Request) -> None:
            table = request.url.path.rsplit("/", 1)[-1]
            if request.method == "GET" and table.startswith("pub_"):
                return
            if table not in WRITE.get(request.method, ()):
                raise RuntimeError(f"the books runner refuses {request.method} {request.url.path}")
        self.http = httpx.Client(base_url=f"{url}/rest/v1",
                                 headers={"apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json"},
                                 event_hooks={"request": [guard]}, timeout=60.0)
        self.lock = threading.Lock()     # the heartbeat thread shares the client

    def _ok(self, r):
        if r.status_code >= 300:
            raise RuntimeError(f"{r.request.method} {r.request.url.path}: {r.status_code} {r.text[:300]}")
        return r

    def claim(self, run_id: int, task_ref: str | None) -> dict | None:
        """Moves a QUEUED run to running; None when it is not queued (claimed elsewhere, cancelled, unknown)."""
        with self.lock:
            r = self._ok(self.http.patch("/pub_runs", params={"id": f"eq.{run_id}", "status": "eq.queued"},
                                         headers={"Prefer": "return=representation"},
                                         json={"status": "running", "claimed_at": now(), "heartbeat_at": now(),
                                               "started_at": now(), "task_ref": task_ref}))
        rows = r.json()
        return rows[0] if rows else None

    def work_slug(self, uid: str) -> str:
        with self.lock:
            r = self._ok(self.http.get("/pub_works", params={"uid": f"eq.{uid}", "select": "slug"}))
        rows = r.json()
        if not rows:
            raise RuntimeError(f"no book {uid!r} in the store")
        return rows[0]["slug"]

    def update(self, run_id: int, **fields) -> None:
        with self.lock:
            self._ok(self.http.patch("/pub_runs", params={"id": f"eq.{run_id}"},
                                     headers={"Prefer": "return=minimal"}, json=fields))

    def cancel_requested(self, run_id: int) -> bool:
        with self.lock:
            r = self._ok(self.http.get("/pub_runs", params={"id": f"eq.{run_id}", "select": "cancel_requested_at"}))
        rows = r.json()
        return bool(rows and rows[0].get("cancel_requested_at"))

    def insert(self, table: str, rows: list[dict]) -> None:
        if not rows:
            return
        with self.lock:
            self._ok(self.http.post(f"/{table}", headers={"Prefer": "return=minimal"}, json=rows))

    def upsert_step(self, row: dict) -> None:
        with self.lock:
            self._ok(self.http.post("/pub_run_steps", params={"on_conflict": "run_id,chapter,step"},
                                    headers={"Prefer": "resolution=merge-duplicates,return=minimal"}, json=[row]))


class FileStore:
    """The same surface over one JSON file — for tests and for a dry run on this PC without production."""

    def __init__(self, path: Path):
        self.path, self.lock = Path(path), threading.Lock()

    def _load(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, d: dict) -> None:
        self.path.write_text(json.dumps(d, indent=1, ensure_ascii=False, default=str), encoding="utf-8")

    def claim(self, run_id: int, task_ref: str | None) -> dict | None:
        with self.lock:
            d = self._load()
            run = next((r for r in d["pub_runs"] if r["id"] == run_id), None)
            if not run or run.get("status") != "queued":
                return None
            run.update(status="running", claimed_at=now(), heartbeat_at=now(), started_at=now(), task_ref=task_ref)
            self._save(d)
            return dict(run)

    def work_slug(self, uid: str) -> str:
        works = {w["uid"]: w["slug"] for w in self._load().get("pub_works", [])}
        return works.get(uid, uid)

    def update(self, run_id: int, **fields) -> None:
        with self.lock:
            d = self._load()
            next(r for r in d["pub_runs"] if r["id"] == run_id).update(fields)
            self._save(d)

    def cancel_requested(self, run_id: int) -> bool:
        with self.lock:
            return bool(next(r for r in self._load()["pub_runs"] if r["id"] == run_id).get("cancel_requested_at"))

    def insert(self, table: str, rows: list[dict]) -> None:
        with self.lock:
            d = self._load()
            d.setdefault(table, []).extend(rows)
            self._save(d)

    def upsert_step(self, row: dict) -> None:
        with self.lock:
            d = self._load()
            steps = d.setdefault("pub_run_steps", [])
            key = (row["run_id"], row.get("chapter"), row["step"])
            for s in steps:
                if (s["run_id"], s.get("chapter"), s["step"]) == key:
                    s.update(row)
                    break
            else:
                steps.append(row)
            self._save(d)
