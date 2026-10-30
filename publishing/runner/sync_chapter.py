"""Stage `sync_chapter` — free, no model calls. Brings a chapter's FILES in line with its text after it was edited in
the book app (founder 2026-10-30, phase 3: highlight → reword / edit, applied as a new chapter revision):

  1. the runner has fetched the chapter's current text (pub_chapter_revisions, the book's current revision) into
     <book>/edit-source/chNN-row.json, and pulled the book's source set (the chapter's full draft and dossier);
  2. the draft's sections and exercises are replaced by the row's — and the result must hash to the row's
     content_sha256, or nothing is written;
  3. the chapter is laid out again (the layout search included) into the run's folder;
  4. the runner then points the book's source set at these files, so the compile and the next release print the
     edited text.

    python -m publishing.runner.sync_chapter <slug> <run_rel> <chapter>
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def merged(draft: dict, body: dict) -> dict:
    """The full draft with the edited text in it (everything else — plan, checks, costs, work — kept)."""
    return {**draft, "sections": body["sections"], "exercises": body["exercises"]}


def main() -> None:
    from publishing.manuscript.render import render
    from publishing.tools.books_import import canonical_sha
    slug, rel, chapter = sys.argv[1], sys.argv[2], int(sys.argv[3])
    book = Path(os.environ["PUBLISH_OUT"]) / "manuscripts" / slug
    out = book / rel
    out.mkdir(parents=True, exist_ok=True)
    row = json.loads((book / "edit-source" / f"ch{chapter:02d}-row.json").read_text(encoding="utf-8"))
    draft = json.loads((book / f"ch{chapter:02d}-draft.json").read_text(encoding="utf-8"))
    doc = merged(draft, row["body"])
    if canonical_sha(doc) != row["content_sha256"]:
        raise SystemExit(f"chapter {chapter}: the merged text does not hash to the edited revision's text — nothing written")
    print(f"chapter {chapter}: laying out the edited text of revision {row['revision_key']}", flush=True)
    for name in ("dossier-full.json", "dossier.json"):
        src = book / f"ch{chapter:02d}-{name}"
        if src.exists():
            (out / src.name).write_bytes(src.read_bytes())
    saved = out / f"ch{chapter:02d}-draft.json"
    saved.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    full = json.loads((out / f"ch{chapter:02d}-dossier-full.json").read_text(encoding="utf-8"))
    env_file = out / "empty.env"
    env_file.write_text("", encoding="utf-8")
    r = render(dict(doc), full, env_file, out)
    env_file.unlink(missing_ok=True)
    kept = json.loads(saved.read_text(encoding="utf-8"))          # only the layout choice joins the saved text
    kept["layout_choice"] = doc.get("layout_choice")
    saved.write_text(json.dumps(kept, ensure_ascii=False, indent=1), encoding="utf-8")
    for f in out.iterdir():
        if f.suffix == ".html" and not f.name.endswith("-review.html"):
            f.unlink()
    summary = {"chapter": chapter, "revision": row["revision_key"], "pages": r["pages"], "hard_defects": r["hard_defects"]}
    (out / "sync.json").write_text(json.dumps(summary), encoding="utf-8")
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
