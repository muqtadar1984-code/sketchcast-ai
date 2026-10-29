"""The local publishing database (D4) on the books runner, and its one shared copy in the books bucket.

Drafting, the readiness audit and Gate R read and write a local Postgres (`pub.*` drafting state, `cat.*` the catalogue
snapshot; publishing/localdb.py refuses any non-local URL). On the founder's PC it lives in ~/sketchcast-pubdb. The
runner has no PC, so the database travels as one plain-SQL dump:

    books/_shared/pubdb/pubdb.sql.gz      the dump (schemas pub and cat, no owners or privileges)
    books/_shared/pubdb/_manifest.json    {sha256, bytes, taken_at, source, previous_sha}

pull → restore into an embedded Postgres in the run's folder → the stage runs against it → dump → push. A push names
the sha it started from and replaces the manifest only if that is still the current one (S3 conditional write on the
manifest's ETag), so two writers can never silently overwrite each other; the loser fails with a clear message.
The PC side is publishing/tools/pubdb_sync.py (status / push / pull).
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

PREFIX = "books/_shared/pubdb/"
DUMP_KEY, MANIFEST_KEY = PREFIX + "pubdb.sql.gz", PREFIX + "_manifest.json"
SCHEMAS = ("pub", "cat")


class Conflict(RuntimeError):
    """The shared database changed since this copy was pulled."""


def tool(name: str) -> str:
    """A Postgres client binary: the embedded server's own (pgserver ships pg_dump/psql), else the PATH."""
    try:
        import pgserver
        for exe in (name, name + ".exe"):
            p = Path(pgserver.__file__).parent / "pginstall" / "bin" / exe
            if p.exists():
                return str(p)
    except ImportError:
        pass
    found = shutil.which(name)
    if not found:
        raise RuntimeError(f"{name} not found (install pgserver or the Postgres client tools)")
    return found


def dump(url: str, out: Path) -> dict:
    """Schemas pub and cat as plain SQL, gzipped. Plain SQL restores across Postgres builds (Windows 16.2 ↔ Linux)."""
    args = [tool("pg_dump"), "--dbname", url, "--no-owner", "--no-privileges", "--format=plain"]
    for s in SCHEMAS:
        args += ["--schema", s]
    sql = subprocess.run(args, check=True, capture_output=True).stdout
    out.write_bytes(gzip.compress(sql, mtime=0))
    body = out.read_bytes()
    return {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}


def restore(gz: Path, url: str, replace: bool = False) -> None:
    """Load a dump into the database at `url`. With replace=True the schemas are dropped first (the PC's pull);
    a fresh runner database has none."""
    sql = gzip.decompress(gz.read_bytes())
    if replace:
        drop = "".join(f"drop schema if exists {s} cascade;" for s in SCHEMAS).encode()
        sql = drop + b"\n" + sql
    subprocess.run([tool("psql"), "--dbname", url, "-v", "ON_ERROR_STOP=1", "--quiet", "--single-transaction"],
                   input=sql, check=True, capture_output=True)


def start_server(data_dir: Path):
    """An embedded Postgres in the run's folder (the runner image installs pgserver). Returns (server, url)."""
    import pgserver
    data_dir.mkdir(parents=True, exist_ok=True)
    srv = pgserver.get_server(str(data_dir), cleanup_mode="stop")
    return srv, srv.get_uri()


# ── the shared copy ────────────────────────────────────────────────────────────

def read_manifest(bucket) -> tuple[dict | None, str | None]:
    """(manifest, etag) or (None, None) before the first push."""
    try:
        body, etag = bucket.get_with_etag(MANIFEST_KEY)
    except KeyError:
        return None, None
    return json.loads(body), etag


def pull(bucket, dest: Path) -> dict:
    """The current dump, written to `dest` and checked against the manifest's sha256."""
    manifest, _ = read_manifest(bucket)
    if manifest is None:
        raise RuntimeError("no shared publishing database yet: push one from the PC first (pubdb_sync push)")
    body = bucket.get(DUMP_KEY)
    if hashlib.sha256(body).hexdigest() != manifest["sha256"]:
        raise RuntimeError("the shared publishing database does not match its manifest hash")
    dest.write_bytes(body)
    return manifest


def push(bucket, gz: Path, source: str, expect_sha: str | None) -> dict:
    """Upload a dump as the new shared copy. `expect_sha` is the sha this copy was pulled from (None for the very first
    push); if the shared copy moved on meanwhile nothing is replaced and Conflict is raised."""
    current, etag = read_manifest(bucket)
    if (current["sha256"] if current else None) != expect_sha:
        raise Conflict(f"the shared publishing database changed since it was pulled "
                       f"(now {current['sha256'][:12] if current else 'none'}, pulled {str(expect_sha)[:12]}): pull again")
    body = gz.read_bytes()
    sha = hashlib.sha256(body).hexdigest()
    if current and current["sha256"] == sha:
        return current                                    # nothing changed
    bucket.put(DUMP_KEY, body, "application/gzip")
    manifest = {"sha256": sha, "bytes": len(body), "taken_at": datetime.now(timezone.utc).isoformat(),
                "source": source, "previous_sha": expect_sha}
    try:
        bucket.put_if_match(MANIFEST_KEY, json.dumps(manifest, indent=1).encode(), "application/json", etag)
    except PreconditionFailed as e:
        raise Conflict("another writer replaced the shared publishing database at the same moment: pull again") from e
    return manifest


class PreconditionFailed(RuntimeError):
    pass


def local_url() -> str:
    url = os.getenv("PUBDB_URL", "").strip()
    if not url:
        raise RuntimeError("PUBDB_URL is not set (start the local server: publishing/tools/start_localdb.py)")
    return url
