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

WRITE = {"PATCH": {"pub_runs", "pub_run_steps",
                   "pub_works"},       # only set_current_revision below: the revision a drafted chapter went into
         "POST": {"pub_run_events", "pub_run_steps", "pub_artifacts", "pub_cost_lines",
                  # a chapter drafted into the book (publishing/runner/book_chapters.py): insert-only, immutable rows
                  "pub_book_revisions", "pub_chapter_revisions",
                  # a book release (publishing/runner/release.py): the publish function inserts the row and marks
                  # the previous live release superseded in one transaction (app migration 0172)
                  "pub_release_publish"}}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


PLANNING = ("scoping", "scope_approved")


def _waiver(w: dict, slug: str) -> dict:
    return {"work": slug, "topic_id": str(w["topic_id"]), "check": w["check_code"], "language": w.get("language"),
            "sub_status": w.get("sub_status"), "by": w["waived_by"], "date": str(w["waived_at"])[:10],
            "reason": w["reason"]}


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

    def _get(self, table: str, **params) -> list[dict]:
        with self.lock:
            return self._ok(self.http.get(f"/{table}", params=params)).json()

    def book_revisions(self, uid: str) -> list[dict]:
        return self._get("pub_book_revisions", work_uid=f"eq.{uid}", select="key,ordinal,label")

    def set_current_revision(self, uid: str, key: str) -> None:
        """The book's current revision is the one a chapter was just drafted into (the app approves Gate D only in
        the current revision); a book still being planned moves to drafting. Nothing else on the work changes."""
        [w] = self._get("pub_works", uid=f"eq.{uid}", select="status")
        fields = {"current_revision": key, **({"status": "drafting"} if w["status"] in PLANNING else {})}
        with self.lock:
            self._ok(self.http.patch("/pub_works", params={"uid": f"eq.{uid}"}, json=fields))

    def chapter_rows(self, uid: str, revision_key: str) -> list[dict]:
        return self._get("pub_chapter_revisions", work_uid=f"eq.{uid}", revision_key=f"eq.{revision_key}",
                         select="work_uid,revision_key,chapter,title,body,content_sha256,figure_refs,words,questions,"
                                "source_path")

    def gate_d_approvals(self, uid: str, chapter: int) -> list[dict]:
        return self._get("pub_approvals", work_uid=f"eq.{uid}", kind="eq.gate_d", chapter=f"eq.{chapter}",
                         select="content_sha256,revision_key")

    def work(self, uid: str) -> dict:
        return self._get("pub_works", uid=f"eq.{uid}", select="*")[0]

    def approvals(self, uid: str) -> list[dict]:
        return self._get("pub_approvals", work_uid=f"eq.{uid}",
                         select="id,kind,scope,chapter,revision_key,content_sha256,approved_by,approved_on")

    def releases(self, uid: str) -> list[dict]:
        return self._get("pub_releases", work_uid=f"eq.{uid}", order="id",
                         select="id,release_kind,edition_key,language,chapters,gate_e_approval_id,superseded_by,withdrawn_at")

    def compile_runs(self, uid: str) -> list[dict]:
        return self._get("pub_runs", work_uid=f"eq.{uid}", stage_key="eq.compile_book", status="eq.done",
                         order="id.desc", select="id,detail")

    def publish_release(self, row: dict) -> int:
        with self.lock:
            return int(self._ok(self.http.post("/rpc/pub_release_publish", json={"release": row})).json())

    def waivers(self, uid: str, slug: str) -> list[dict]:
        """The book's active readiness waivers (editors record them on the Gaps tab, migration 0168), in the shape
        publishing.schemas.Waiver reads. Before 0168 is applied there are none."""
        with self.lock:
            r = self.http.get("/pub_readiness_waivers", params={"work_uid": f"eq.{uid}", "revoked_at": "is.null",
                                                                "select": "topic_id,check_code,language,sub_status,"
                                                                          "reason,waived_by,waived_at"})
        if r.status_code == 404 or (r.status_code >= 400 and "pub_readiness_waivers" in r.text):
            return []
        return [_waiver(w, slug) for w in self._ok(r).json()]

    def others_running(self, run_id: int, stage_keys: list[str]) -> list[int]:
        """Ids of OTHER runs that are running one of these stages (the publishing database has one writer at a time)."""
        if not stage_keys:
            return []
        with self.lock:
            r = self._ok(self.http.get("/pub_runs", params={"status": "eq.running", "id": f"neq.{run_id}",
                                                            "stage_key": f"in.({','.join(stage_keys)})", "select": "id"}))
        return [row["id"] for row in r.json()]

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

    def book_revisions(self, uid: str) -> list[dict]:
        return [r for r in self._load().get("pub_book_revisions", []) if r["work_uid"] == uid]

    def set_current_revision(self, uid: str, key: str) -> None:
        with self.lock:
            d = self._load()
            w = next(w for w in d["pub_works"] if w["uid"] == uid)
            w["current_revision"] = key
            if w.get("status") in PLANNING:
                w["status"] = "drafting"
            self._save(d)

    def chapter_rows(self, uid: str, revision_key: str) -> list[dict]:
        return [c for c in self._load().get("pub_chapter_revisions", [])
                if c["work_uid"] == uid and c["revision_key"] == revision_key]

    def gate_d_approvals(self, uid: str, chapter: int) -> list[dict]:
        return [a for a in self._load().get("pub_approvals", [])
                if a["work_uid"] == uid and a.get("kind") == "gate_d" and a.get("chapter") == chapter]

    def work(self, uid: str) -> dict:
        return next(w for w in self._load()["pub_works"] if w["uid"] == uid)

    def approvals(self, uid: str) -> list[dict]:
        return [a for a in self._load().get("pub_approvals", []) if a["work_uid"] == uid]

    def releases(self, uid: str) -> list[dict]:
        return [r for r in self._load().get("pub_releases", []) if r["work_uid"] == uid]

    def compile_runs(self, uid: str) -> list[dict]:
        return sorted((r for r in self._load()["pub_runs"] if r["work_uid"] == uid and r["stage_key"] == "compile_book"
                       and r["status"] == "done"), key=lambda r: -r["id"])

    def publish_release(self, row: dict) -> int:
        """What pub_release_publish() does, for tests: insert, supersede the previous live one."""
        with self.lock:
            d = self._load()
            rows = d.setdefault("pub_releases", [])
            new_id = 1 + max((r["id"] for r in rows), default=0)
            for r in rows:
                if r["work_uid"] == row["work_uid"] and r["release_kind"] == row["release_kind"] and                         r["language"] == row["language"] and not r.get("superseded_by") and not r.get("withdrawn_at"):
                    r["superseded_by"] = new_id
            rows.append({**row, "id": new_id, "superseded_by": None, "withdrawn_at": None})
            self._save(d)
            return new_id

    def waivers(self, uid: str, slug: str) -> list[dict]:
        return [_waiver(w, slug) for w in self._load().get("pub_readiness_waivers", [])
                if w["work_uid"] == uid and not w.get("revoked_at")]

    def others_running(self, run_id: int, stage_keys: list[str]) -> list[int]:
        with self.lock:
            return [r["id"] for r in self._load()["pub_runs"]
                    if r["id"] != run_id and r.get("status") == "running" and r.get("stage_key") in stage_keys]

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
