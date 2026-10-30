"""Stage `compile_book` — free, no model calls. Compiles the book from its chapters as they are in the source set (the
engine's own publishing.manuscript.book.compile_book, unchanged): cover, title page, imprint, contents, every chapter
with page numbers running on, coverage, glossary, index and credits — and records what went in, for Gate E.

    python -m publishing.runner.compile <slug> <run_rel> <first> <last>

Writes into the run's folder: the book PDF, chapters.json (each chapter's start page, length and text hash),
front-matter.json (the front matter texts as printed, which the app's reader shows and edits) and compile.json (the
summary the app shows; Gate E approves exactly this PDF).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path


FRONT_MATTER_FILE = "front-matter.json"          # in the book folder: the app's edits; in the run folder: as printed
PIECES = ("preface", "imprint", "about")


def front_matter_overrides(edits: list[dict]) -> dict[str, str]:
    """The front matter edited in the app (phase 3c): each piece's latest accepted text. Rejecting an edit in the app
    brings the piece back to the one before it (or to the engine's own text)."""
    out: dict[str, str] = {}
    for e in sorted(edits, key=lambda e: e["id"]):
        if e.get("unit") in PIECES and (e.get("after_text") or "").strip():
            out[e["unit"]] = e["after_text"].strip()
    return out


def main() -> None:
    from publishing.manuscript.book import compile_book
    from publishing.tools.books_import import canonical_sha
    slug, rel, first, last = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
    book = Path(os.environ["PUBLISH_OUT"]) / "manuscripts" / slug
    out = book / rel
    out.mkdir(parents=True, exist_ok=True)
    env_file = out / "empty.env"
    env_file.write_text("", encoding="utf-8")
    present = [n for n in range(first, last + 1) if (book / f"ch{n:02d}-draft.json").exists()]
    if not present:
        raise SystemExit(f"{slug}: no drafted chapters between {first} and {last} — nothing to compile")
    print(f"compiling {slug}: chapters {present}", flush=True)
    pdf = compile_book(slug, first, last, env_file, "Draft manuscript", "")
    env_file.unlink(missing_ok=True)
    target = out / pdf.name
    target.write_bytes(pdf.read_bytes())
    chapters = json.loads((book / "book" / "chapters.json").read_text(encoding="utf-8"))
    for c in chapters:
        doc = json.loads((book / f"ch{c['number']:02d}-draft.json").read_text(encoding="utf-8"))
        c["content_sha256"] = canonical_sha(doc)
    (out / "chapters.json").write_text(json.dumps(chapters, indent=1), encoding="utf-8")
    used = book / "book" / FRONT_MATTER_FILE
    if used.exists():
        (out / FRONT_MATTER_FILE).write_bytes(used.read_bytes())
    import fitz
    pages = len(fitz.open(target))
    summary = {"pdf": target.name, "pdf_sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "pages": pages,
               "chapters": [{"number": c["number"], "title": c["title"], "start": c["start"], "pages": c["pages"],
                             "content_sha256": c["content_sha256"]} for c in chapters],
               "missing": [n for n in range(first, last + 1) if n not in present]}
    (out / "compile.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("pdf", "pages", "missing")}), flush=True)


if __name__ == "__main__":
    main()
