"""Run one queued book run, start to finish.

    python -m publishing.runner --run-id 123                      # production: book store + books bucket
    python -m publishing.runner --run-id 1 --store file:runs.json --bucket dir:./bucket   # tests / dry run

Order: claim (queued → running) · pull the source set (hash-checked) · run the stage's command with its log
streamed to pub_run_events · upload the run's outputs · record files, costs and the summary · done | failed |
cancelled. A heartbeat every 15 s keeps `heartbeat_at` fresh and honours Cancel from the app.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

from publishing.runner import files, pubdb, stages
from publishing.runner.store import FileStore, RestStore, now

HEARTBEAT_S, FLUSH_S, FLUSH_LINES = 15, 2.0, 25
LEVEL = re.compile(r"\b(DEBUG|INFO|WARNING|ERROR|CRITICAL)\b")


def _task_ref() -> str | None:
    """The Fargate task ARN, from the ECS metadata endpoint (absent off AWS)."""
    uri = os.getenv("ECS_CONTAINER_METADATA_URI_V4")
    if not uri:
        return os.getenv("TASK_REF")
    try:
        import httpx
        return httpx.get(f"{uri}/task", timeout=5).json().get("TaskARN")
    except Exception:  # noqa: BLE001 — a missing ARN only costs the app its Stop button
        return None


class EventLog:
    """Batches the command's output lines into pub_run_events (every FLUSH_S seconds or FLUSH_LINES lines)."""

    def __init__(self, store, run_id: int):
        self.store, self.run_id, self.buf, self.last, self.tail = store, run_id, [], time.monotonic(), []

    def add(self, message: str, level: str | None = None, kind: str = "log", data: dict | None = None) -> None:
        message = message.rstrip()[:4000]
        if not message:
            return
        m = LEVEL.search(message[:80])
        level = level or (m.group(1).lower().replace("critical", "error") if m else "info")
        self.buf.append({"run_id": self.run_id, "at": now(), "level": level, "kind": kind, "message": message,
                         "data": data})
        self.tail = (self.tail + [message])[-20:]
        if len(self.buf) >= FLUSH_LINES or time.monotonic() - self.last >= FLUSH_S:
            self.flush()

    def flush(self) -> None:
        if self.buf:
            rows, self.buf = self.buf, []
            self.store.insert("pub_run_events", rows)
        self.last = time.monotonic()


def execute(store, bucket, run_id: int, work: Path) -> str:
    run = store.claim(run_id, _task_ref())
    if run is None:
        print(f"run {run_id} is not queued; nothing to do")
        return "skipped"
    log = EventLog(store, run_id)
    status, usd, summary, error = "failed", None, {}, None
    pg = None
    try:
        stage, params = stages.validate(run["stage_key"], run.get("params") or {})
        work.mkdir(parents=True, exist_ok=True)
        slug = store.work_slug(run["work_uid"])
        run_rel = f"runs/run-{run_id}"
        book = work / "manuscripts" / slug
        if stage.into_book:
            from publishing.runner import book_chapters
            refused = book_chapters.refusal(store, run["work_uid"], int(params["chapter"]))
            if refused:
                raise RuntimeError(refused)
        if stage.needs_source and stage.source_optional and not files.has_source(bucket, slug):
            book.mkdir(parents=True, exist_ok=True)
            log.add(f"{stage.title}: the book has no drafted chapters in storage yet — starting from none", kind="progress")
        elif stage.needs_source:
            log.add(f"{stage.title}: pulling the book's source set", kind="progress")
            store.update(run_id, progress={"step": "pull", "note": "pulling the book's files"})
            got = files.pull_source(bucket, slug, book)
            log.add(f"pulled {got['files']} files ({got['bytes'] / 1e6:.1f} MB), every one hash-checked", kind="progress")
        extra_env = {}
        if stage.uses_pubdb:
            if stage.writes_pubdb:
                busy = store.others_running(run_id, stages.WRITES_PUBDB)
                if busy:
                    raise RuntimeError(f"run #{busy[0]} is writing the publishing database; start this one when it ends")
            pulled = pubdb.pull(bucket, work / "pubdb.sql.gz")
            pg, pg_url = pubdb.start_server(work / "pgdata")
            pubdb.restore(work / "pubdb.sql.gz", pg_url)
            extra_env["PUBDB_URL"] = pg_url
            waivers = store.waivers(run["work_uid"], slug)
            extra_env["PUBLISH_WAIVERS"] = json.dumps(waivers)
            if waivers:
                log.add(f"{len(waivers)} readiness waiver(s) recorded in the app apply to this book", kind="progress")
            log.add(f"restored the publishing database ({pulled['bytes'] / 1e6:.1f} MB, {pulled['sha256'][:12]}, "
                    f"from {pulled['source']} {pulled['taken_at'][:16]})", kind="progress")
        if stage.prefetch:
            for key, rel in stage.prefetch(slug, params):
                dst = book / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(bucket.get(key))
            log.add(f"{stage.title}: fetched the earlier run's files", kind="progress")
        if stage.needs_assets:
            assets = work / "book-assets"
            got = files.pull_assets(bucket, assets)
            extra_env["PUBLISH_ASSETS"] = str(assets)
            log.add(f"pulled the book pictures: {got['files']} files ({got['bytes'] / 1e6:.1f} MB), hash-checked",
                    kind="progress")
        (book / run_rel).mkdir(parents=True, exist_ok=True)

        empty_env = work / "empty.env"
        empty_env.write_text("", encoding="utf-8")
        argv = [str(empty_env) if a == "__RUNNER_EMPTY_ENV__" else a for a in stage.argv(slug, params, run_rel)]
        if stage.uses_models:
            from shared.claude_client import _ensure_google_credentials
            _ensure_google_credentials()          # GOOGLE_APPLICATION_CREDENTIALS_JSON → a file the command inherits
        env = {**os.environ, "PUBLISH_OUT": str(work), "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", **extra_env,
               **stage.env(slug, params, run_rel)}
        store.upsert_step({"run_id": run_id, "chapter": None, "step": stage.step, "status": "running",
                           "started_at": now()})
        store.update(run_id, progress={"step": stage.step, "note": "running"})
        log.add("$ " + " ".join(Path(a).name if a == str(empty_env) else a for a in argv[1:]), kind="progress")

        repo = Path(__file__).resolve().parents[2]
        proc = subprocess.Popen(argv, cwd=repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", bufsize=1)
        cancelled = threading.Event()

        def heartbeat():
            while proc.poll() is None:
                time.sleep(HEARTBEAT_S)
                try:
                    store.update(run_id, heartbeat_at=now())
                    if store.cancel_requested(run_id) and not cancelled.is_set():
                        cancelled.set()
                        proc.terminate()
                except Exception as e:  # noqa: BLE001 — a missed beat must not kill the run
                    print(f"heartbeat failed: {e}", file=sys.stderr)
        threading.Thread(target=heartbeat, daemon=True).start()
        for line in proc.stdout:
            sys.stdout.write(line)
            log.add(line)
        rc = proc.wait()
        log.flush()

        rows = files.push_outputs(bucket, slug, book, run_rel)
        store.insert("pub_artifacts", [{**r, "work_uid": run["work_uid"], "stage_key": run["stage_key"],
                                        "run_id": run_id} for r in rows])
        log.add(f"uploaded {len(rows)} output file(s)", kind="progress")
        if cancelled.is_set():
            status, error = "cancelled", "cancelled from the app"
        elif rc != 0:
            error = "\n".join(log.tail[-8:]) or f"exit code {rc}"
        else:
            usd, lines, summary = stage.collect(book / run_rel)
            store.insert("pub_cost_lines", [{**ln, "work_uid": run["work_uid"], "stage_key": run["stage_key"],
                                             "run_id": run_id, "provider": None} for ln in lines])
            if stage.writes_pubdb:
                pubdb.dump(pg_url, work / "pubdb-out.sql.gz")
                m = pubdb.push(bucket, work / "pubdb-out.sql.gz", f"run:{run_id}", pulled["sha256"])
                log.add(f"saved the publishing database back ({m['sha256'][:12]})", kind="progress")
            if stage.makes_release:
                from publishing.runner.release import publish_release
                made = publish_release(store, bucket, run, slug, params)
                summary = {**summary, **made}
                log.add(f"released {made['release_kind']} {made['edition_key']}: chapters {made['chapters']}"
                        + (f" (supersedes release #{made['supersedes']})" if made["supersedes"] else ""), kind="progress")
            if stage.into_book:
                placed = book_chapters.publish(store, bucket, run["work_uid"], slug, book, run_rel, int(params["chapter"]))
                summary = {**summary, "book": placed}
                log.add(f"chapter {params['chapter']} is in the book: revision {placed['revision']}"
                        + (" (new; other chapters carried forward)" if placed["new_revision"] else ""), kind="progress")
            status = "done"
        store.upsert_step({"run_id": run_id, "chapter": None, "step": stage.step, "status": status if status != "cancelled" else "failed",
                           "finished_at": now(), "usd": usd, "error": error,
                           "checkpoint_key": f"books/{slug}/{run_rel}/"})
    except Exception as e:  # noqa: BLE001 — every failure is recorded on the run, never lost
        error = f"{type(e).__name__}: {e}"
        log.add(traceback.format_exc(), level="error", kind="error")
    finally:
        if pg is not None:
            try:
                pg.cleanup()                  # stop the run's embedded Postgres
            except Exception:  # noqa: BLE001 — the task ends anyway
                pass
        log.add(f"run {status}" + (f" · ${usd:.2f}" if usd else ""), level="error" if status == "failed" else "info",
                kind="progress")
        log.flush()
        store.update(run_id, status=status, finished_at=now(), usd=usd, error=error,
                     progress={"step": "finished", "note": status},
                     detail={**(run.get("detail") or {}), "summary": summary})
    return status


def main() -> None:
    a = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    a.add_argument("--run-id", type=int, required=True)
    a.add_argument("--store", default="rest", help="rest | file:<path.json>")
    a.add_argument("--bucket", default="s3", help="s3 | dir:<folder>")
    a.add_argument("--work", type=Path, help="working folder (default: a fresh temp folder)")
    x = a.parse_args()
    store = FileStore(Path(x.store[5:])) if x.store.startswith("file:") else RestStore()
    bucket = files.DirBucket(Path(x.bucket[4:])) if x.bucket.startswith("dir:") else files.S3Bucket()
    work = x.work or Path(tempfile.mkdtemp(prefix="book-run-"))
    status = execute(store, bucket, x.run_id, work)
    sys.exit(0 if status in ("done", "skipped", "cancelled") else 1)


if __name__ == "__main__":
    main()
