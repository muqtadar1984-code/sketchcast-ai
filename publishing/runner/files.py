"""The book's files on the runner: pull the source set from the books bucket, push a run's outputs back.

Bucket layout (books bucket, us-east-1):
    books/<slug>/source/<path>         the source set, same relative paths as the book folder
    books/<slug>/source/_manifest.json  {files: [{rel, key, bytes, sha256}]} — every pull is checked against it
    books/<slug>/runs/run-<id>/<path>   what one run produced

`Bucket` is the S3 surface the runner needs; `DirBucket` is the same over a local folder for tests.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
from pathlib import Path

MANIFEST = "_manifest.json"


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class S3Bucket:
    def __init__(self, name: str | None = None, region: str | None = None):
        import boto3
        self.name = name or os.environ["BOOKS_S3_BUCKET"]
        self.s3 = boto3.client("s3", region_name=region or os.getenv("BOOKS_S3_REGION", "us-east-1"))

    def get(self, key: str) -> bytes:
        return self.s3.get_object(Bucket=self.name, Key=key)["Body"].read()

    def put(self, key: str, body: bytes, content_type: str) -> None:
        self.s3.put_object(Bucket=self.name, Key=key, Body=body, ContentType=content_type,
                           ChecksumAlgorithm="SHA256", Metadata={"sha256": sha256(body)})


class DirBucket:
    def __init__(self, root: Path):
        self.root = Path(root)

    def get(self, key: str) -> bytes:
        return (self.root / key).read_bytes()

    def put(self, key: str, body: bytes, content_type: str) -> None:
        p = self.root / key
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)


SHARED_ASSETS = "books/_shared/book-assets/"


def pull_assets(bucket, dest: Path) -> dict:
    """The shared book-assets/ (the books' own illustrations and opener art), hash-checked like the source set."""
    return _pull(bucket, SHARED_ASSETS, dest)


def pull_source(bucket, slug: str, book: Path) -> dict:
    """Every file in the manifest, written to <book>/<rel> and checked against its sha256. Raises on a mismatch:
    a run must never start from a book that did not come back whole."""
    return _pull(bucket, f"books/{slug}/source/", book)


def _pull(bucket, prefix: str, book: Path) -> dict:
    manifest = json.loads(bucket.get(prefix + MANIFEST))
    n = size = 0
    for f in manifest["files"]:
        if f["rel"].startswith("_notes/"):
            continue
        body = bucket.get(f.get("key") or prefix + f["rel"])
        if sha256(body) != f["sha256"]:
            raise RuntimeError(f"{f['rel']}: the copy in the bucket does not match its manifest hash")
        p = book / f["rel"]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)
        n, size = n + 1, size + len(body)
    return {"files": n, "bytes": size}


def push_outputs(bucket, slug: str, book: Path, run_rel: str) -> list[dict]:
    """Upload everything under <book>/<run_rel>/ to books/<slug>/<run_rel>/; one row per file for pub_artifacts."""
    out, rows = book / run_rel, []
    if not out.exists():
        return rows
    for p in sorted(x for x in out.rglob("*") if x.is_file()):
        rel = p.relative_to(book).as_posix()
        body = p.read_bytes()
        ctype = mimetypes.guess_type(p.name)[0] or ("application/json" if p.suffix == ".json" else "application/octet-stream")
        key = f"books/{slug}/{rel}"
        bucket.put(key, body, ctype)
        rows.append({"rel_path": rel, "bytes": len(body), "sha256": sha256(body), "content_type": ctype,
                     "storage": "s3", "storage_key": key, "title": p.name,
                     "kind": "record" if p.suffix == ".json" else "page" if p.suffix == ".html" else "file"})
    return rows
