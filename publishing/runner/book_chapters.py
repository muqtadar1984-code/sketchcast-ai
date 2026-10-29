"""A chapter drafted on the books runner becomes part of the BOOK (R1 S3, founder 2026-10-29): the book app shows it
on the Chapters board, where Gate D approves it.

The book app reads a chapter's text from `pub_chapter_revisions` (work, revision, chapter) — immutable rows, every
chapter present in every revision (the importer carries unchanged chapters forward, publishing.tools.books_import).
So a drafted chapter is placed like this:

  - the book's latest revision was made by the app ("d<N>") and does not have this chapter yet → the chapter is added
    to it;
  - otherwise (no revision yet, an imported revision, or this chapter is already in the latest one) → a new revision
    "d<N+1>" is opened, every other chapter of the latest revision is carried forward unchanged, and the new chapter
    is added.

A chapter whose current text carries a Gate D approval is never replaced (approved content is changed only by a
controlled revision, S4): `refusal` stops the run before any model call.

The book's source set (books/<slug>/source/_manifest.json) gains the run's chapter files by REFERENCE — a manifest
entry may name any key — so nothing is copied, and the next stage that pulls the book gets them.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from publishing.runner import files

APP_REVISION = re.compile(r"^d(\d+)$")
# what a drafted chapter adds to the source set (the same files the PC's book folder keeps per chapter)
CHAPTER_FILES = ("draft.json", "dossier-full.json", "dossier.json", "diagnostics.json")


def placement(revisions: list[dict], latest_chapters: set[int], chapter: int) -> tuple[str, dict | None, bool]:
    """(revision key, the new revision row or None, whether to carry the latest revision's other chapters forward)."""
    latest = max(revisions, key=lambda r: r["ordinal"]) if revisions else None
    if latest and APP_REVISION.match(latest["key"]) and chapter not in latest_chapters:
        return latest["key"], None, False
    n = max((int(m.group(1)) for r in revisions if (m := APP_REVISION.match(r["key"]))), default=0) + 1
    row = {"key": f"d{n}", "ordinal": (latest["ordinal"] + 1) if latest else 0,
           "label": f"Draft {n} (from the app)",
           "summary": f"Opened when chapter {chapter} was drafted on the books runner"}
    return row["key"], row, latest is not None


def chapter_row(work_uid: str, revision_key: str, doc: dict, figures: dict, source_path: str) -> dict:
    """The row the importer would write for this chapter (same body, hash and counts)."""
    from publishing.tools.books_import import _figure_refs, _words, canonical_sha, chapter_body
    n = int(doc["chapter"]["number"])
    return {"work_uid": work_uid, "revision_key": revision_key, "chapter": n,
            "title": doc["chapter"].get("title") or f"Chapter {n}", "body": chapter_body(doc, figures),
            "content_sha256": canonical_sha(doc), "figure_refs": _figure_refs(doc), "words": _words(doc),
            "questions": len(doc.get("exercises") or []), "source_path": source_path}


def refusal(store, work_uid: str, chapter: int) -> str | None:
    """Why this chapter may not be drafted into the book, or None. Checked before any model call."""
    revisions = store.book_revisions(work_uid)
    if not revisions:
        return None
    latest = max(revisions, key=lambda r: r["ordinal"])
    current = next((c for c in store.chapter_rows(work_uid, latest["key"]) if c["chapter"] == chapter), None)
    if current and any(a["content_sha256"] == current["content_sha256"] for a in store.gate_d_approvals(work_uid, chapter)):
        return (f"chapter {chapter} is approved at Gate D in {latest['key']}; approved text changes only by a controlled "
                "revision, not a new draft")
    return None


def manifest_with(manifest: dict | None, entries: list[dict]) -> dict:
    """The source manifest with these files added or replaced (by rel); everything else kept as it was."""
    keep = [f for f in (manifest or {}).get("files", []) if f["rel"] not in {e["rel"] for e in entries}]
    return {**(manifest or {}), "files": sorted(keep + entries, key=lambda f: f["rel"])}


def publish(store, bucket, work_uid: str, slug: str, book: Path, run_rel: str, chapter: int) -> dict:
    """Place the drafted chapter in the book (prod rows) and its files in the book's source set."""
    from publishing.tools.books_import import figure_map
    out = book / run_rel
    doc = json.loads((out / f"ch{chapter:02d}-draft.json").read_text(encoding="utf-8"))
    figures = figure_map(out, slug)
    revisions = store.book_revisions(work_uid)
    latest = max(revisions, key=lambda r: r["ordinal"]) if revisions else None
    latest_rows = store.chapter_rows(work_uid, latest["key"]) if latest else []
    key, new_rev, carry = placement(revisions, {c["chapter"] for c in latest_rows}, chapter)
    if new_rev:
        store.insert("pub_book_revisions", [{**new_rev, "work_uid": work_uid}])
    rows = [{k: v for k, v in c.items() if k not in ("id", "created_at")} | {"revision_key": key}
            for c in latest_rows if carry and c["chapter"] != chapter]
    rows.append(chapter_row(work_uid, key, doc, figures, f"{run_rel}/ch{chapter:02d}-draft.json"))
    store.insert("pub_chapter_revisions", rows)

    entries = []
    for name in CHAPTER_FILES:
        p = out / f"ch{chapter:02d}-{name}"
        if p.exists():
            body = p.read_bytes()
            entries.append({"rel": p.name, "key": f"books/{slug}/{run_rel}/{p.name}", "bytes": len(body),
                            "sha256": files.sha256(body)})
    mkey = f"books/{slug}/source/{files.MANIFEST}"
    try:
        raw, etag = bucket.get_with_etag(mkey)
        manifest = json.loads(raw)
    except KeyError:
        manifest, etag = None, None
    bucket.put_if_match(mkey, json.dumps(manifest_with(manifest, entries), indent=1).encode("utf-8"),
                        "application/json", etag)
    return {"revision": key, "new_revision": bool(new_rev), "carried_forward": len(rows) - 1,
            "source_files": len(entries), "content_sha256": rows[-1]["content_sha256"]}
