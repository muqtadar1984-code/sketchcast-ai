"""Connection to the LOCAL isolated publishing database (D4).

The URL comes from ``PUBDB_URL``. As a guard, any host other than
localhost/127.0.0.1 is refused, so a stray production URL can never be used
here.
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import psycopg
from psycopg.rows import dict_row

MIGRATIONS = Path(__file__).resolve().parent / "migrations"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class NotLocal(RuntimeError):
    pass


def db_url() -> str:
    url = os.getenv("PUBDB_URL", "").strip()
    if not url:
        raise NotLocal("PUBDB_URL is not set; start the local server with "
                       "`python -m publishing.cli db-start`")
    parts = urlparse(url)
    host = parts.hostname or ""
    # a Unix-domain socket (the embedded server on Linux — the books runner — gives
    # postgresql://user:@/db?host=/tmp/...) is local by construction: it has no network address at all
    socket_dir = parse_qs(parts.query).get("host", [""])[0]
    if not host and socket_dir.startswith("/"):
        return url
    if host not in LOCAL_HOSTS:
        raise NotLocal(f"PUBDB_URL host {host!r} is not local; refusing")
    return url


def connect() -> psycopg.Connection:
    return psycopg.connect(db_url(), row_factory=dict_row, autocommit=False)


def migrate(conn: psycopg.Connection) -> list[str]:
    applied = []
    for f in sorted(MIGRATIONS.glob("*.sql")):
        conn.execute(f.read_text(encoding="utf-8"))
        applied.append(f.name)
    conn.commit()
    return applied
