"""Stage `draft_trial` — PAID (model calls). Drafts one chapter on the books runner, exactly as `cli chapter` does on the
PC (publishing.manuscript.author.draft_chapter, then render), but into the RUN's folder only:

  - the book's own chapter files are never touched: the draft, its dossier, the PDF and the review page land in
    <book>/<run_rel>/, and the run's files are what the app shows;
  - the earlier chapters' drafts are copied in first, so the uniqueness rules (no repeated question or picture) see
    them, then removed again before upload;
  - no new pictures (pictures = 0): commissioning illustrations creates production image jobs, which the founder's
    standing rule keeps to quiet hours; the draft uses the catalogue's figures and the book's existing assets;
  - the publishing database is restored for the dossier and Gate R pins but NOT pushed back, so the trial's unit
    revisions never reach the shared record.

    python -m publishing.runner.draft <slug> <run_rel> <chapter>

Needs Gate R pins (an English edition): a book that has not passed Gate R stops before any model call, at $0.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def earlier_drafts(book: Path, out: Path, chapter: int) -> list[Path]:
    """Copy chapters 1..chapter-1's drafts into the run folder (uniqueness reads them there); returns the copies."""
    copied = []
    for m in range(1, chapter):
        src = book / f"ch{m:02d}-draft.json"
        if src.exists():
            dst = out / src.name
            dst.write_bytes(src.read_bytes())
            copied.append(dst)
    notes = book / f"ch{chapter:02d}-editorial-notes.md"        # the founder's Gate D direction, if any, is followed
    if notes.exists():
        (out / notes.name).write_bytes(notes.read_bytes())
    return copied


def main() -> None:
    from publishing import localdb
    from publishing.manuscript.author import draft_chapter
    from publishing.manuscript.render import render
    slug, rel, chapter = sys.argv[1], sys.argv[2], int(sys.argv[3])
    book = Path(os.environ["PUBLISH_OUT"]) / "manuscripts" / slug
    out = book / rel
    out.mkdir(parents=True, exist_ok=True)
    env_file = out / "empty.env"
    env_file.write_text("", encoding="utf-8")            # models and Supabase from the environment
    copied = earlier_drafts(book, out, chapter)
    print(f"drafting chapter {chapter} of {slug} into {rel} ({len(copied)} earlier chapter(s) checked for repeats)",
          flush=True)
    conn = localdb.connect()
    try:
        doc = draft_chapter(conn, slug, chapter, env_file, out, pictures=0)
        conn.commit()
        full = json.loads((out / f"ch{chapter:02d}-dossier-full.json").read_text(encoding="utf-8"))
        r = render(doc, full, env_file, out)
    finally:
        for f in copied:
            f.unlink(missing_ok=True)
        env_file.unlink(missing_ok=True)
    for f in out.iterdir():                              # the HTML pages embed every picture (~17 MB each)
        if f.suffix == ".html" and not f.name.endswith("-review.html"):
            f.unlink()
    summary = {"chapter": chapter, "ready_for_gate_d": doc["review"].get("ready_for_gate_d"),
               "words": doc["one_pass"]["words"], "target_words": doc["one_pass"]["target_words"],
               "met": doc["one_pass"]["met"], "sections": len(doc["sections"]), "exercises": len(doc["exercises"]),
               "pages": r.get("pages"), "hard_defects": r.get("hard_defects"), "usd": doc["one_pass"]["usd"],
               "review_findings": len(doc["review"].get("findings") or []),
               "failed_checks": sum(1 for f in doc["checks"] if f.get("verdict") == "fail")}
    (out / "draft.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
