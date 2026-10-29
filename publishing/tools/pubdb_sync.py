"""Keep this PC's local publishing database and the books runner's shared copy in step (R1 S3).

    python -m publishing.tools.pubdb_sync status
    python -m publishing.tools.pubdb_sync push            # this PC's database becomes the shared copy
    python -m publishing.tools.pubdb_sync pull --yes      # the shared copy replaces this PC's database

One copy is the truth at a time. A push is refused when the shared copy changed since this PC last pulled or pushed
(a run on the server wrote to it): pull first. A pull first saves this PC's database to a timestamped backup dump
next to the sync state, then replaces the pub and cat schemas. Uses PUBDB_URL (start_localdb.py) and the books
bucket (BOOKS_S3_BUCKET, default sketchcast-books-172042957848).
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import tempfile
import time
from pathlib import Path

from publishing.runner import pubdb
from publishing.runner.files import S3Bucket

STATE = Path(os.getenv("PUBDB_SYNC_STATE", Path.home() / ".sketchcast-pubdb-sync.json"))


def _state() -> dict:
    return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}


def _save(d: dict) -> None:
    STATE.write_text(json.dumps(d, indent=1), encoding="utf-8")


def main() -> None:
    a = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    a.add_argument("cmd", choices=["status", "push", "pull"])
    a.add_argument("--yes", action="store_true", help="pull: really replace this PC's database")
    a.add_argument("--force", action="store_true", help="push: replace the shared copy even if it moved on")
    x = a.parse_args()
    bucket = S3Bucket(os.getenv("BOOKS_S3_BUCKET", "sketchcast-books-172042957848"))
    shared, _ = pubdb.read_manifest(bucket)
    st = _state()
    if x.cmd == "status":
        print(json.dumps({"shared": shared, "this_pc_last_synced": st}, indent=1))
        return
    url = pubdb.local_url()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        if x.cmd == "push":
            if shared and st.get("sha256") != shared["sha256"] and not x.force:
                raise SystemExit(f"refused: the shared copy ({shared['sha256'][:12]}, {shared['source']}, "
                                 f"{shared['taken_at']}) changed since this PC last synced — pull first")
            got = pubdb.dump(url, tmp / "pubdb.sql.gz")
            m = pubdb.push(bucket, tmp / "pubdb.sql.gz", f"pc:{socket.gethostname()}",
                           shared["sha256"] if shared else None)
            _save({"sha256": m["sha256"], "at": m["taken_at"], "how": "push"})
            print(f"pushed {got['bytes'] / 1e6:.1f} MB as {m['sha256'][:12]}")
        else:
            if not x.yes:
                raise SystemExit("pull replaces this PC's pub and cat schemas: run again with --yes")
            backup = STATE.with_name(f"sketchcast-pubdb-backup-{time.strftime('%Y%m%d-%H%M%S')}.sql.gz")
            pubdb.dump(url, backup)
            m = pubdb.pull(bucket, tmp / "pubdb.sql.gz")
            pubdb.restore(tmp / "pubdb.sql.gz", url, replace=True)
            _save({"sha256": m["sha256"], "at": m["taken_at"], "how": "pull"})
            print(f"pulled {m['sha256'][:12]} ({m['source']}, {m['taken_at']}); this PC's previous database: {backup}")


if __name__ == "__main__":
    main()
