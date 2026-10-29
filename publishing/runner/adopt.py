"""Stage `adopt_trial` — free, no model calls. Puts an earlier TRIAL draft into the book instead of paying to draft the
chapter again (founder 2026-10-29: adopt trial run #27, Linear Equations chapter 1):

  1. the trial's chNN-draft.json and dossier come from its run folder (the runner has fetched them into
     <book>/trial-source/);
  2. the trial must have been written from the book's CURRENT Gate R sources — its dossier's edition and pin run are
     the edition the publishing database has open now — or nothing is adopted;
  3. the chapter is laid out again with the current renderer (the text is the trial's, unchanged);
  4. its units are recorded in the publishing database, exactly as a draft records them;
  5. the runner then places it in the book (publishing.runner.book_chapters.publish), as for `draft_chapter`.

    python -m publishing.runner.adopt <slug> <run_rel> <chapter> <source_run>
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def stale(dossier: dict, edition: dict | None) -> str | None:
    """Why the trial's sources are not the book's current ones, or None."""
    if not edition:
        return "the book has no open English edition — run Gate R first"
    if dossier.get("edition_id") != edition["id"] or dossier.get("pin_run_id") != edition["pin_run_id"]:
        return (f"the trial was written from edition {dossier.get('edition_id')} / pins of run "
                f"{dossier.get('pin_run_id')}, but the book now has edition {edition['id']} / pins of run "
                f"{edition['pin_run_id']} — draft the chapter again")
    return None


def main() -> None:
    from publishing import localdb
    from publishing.manuscript.author import store
    from publishing.manuscript.render import render
    slug, rel, chapter, source = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
    book = Path(os.environ["PUBLISH_OUT"]) / "manuscripts" / slug
    out = book / rel
    out.mkdir(parents=True, exist_ok=True)
    src = book / "trial-source"
    doc = json.loads((src / f"ch{chapter:02d}-draft.json").read_text(encoding="utf-8"))
    full = json.loads((src / f"ch{chapter:02d}-dossier-full.json").read_text(encoding="utf-8"))
    conn = localdb.connect()
    edition = conn.execute(
        "select e.id, e.pin_run_id from pub.editions e join pub.works w on w.uid = e.work_uid where w.slug = %s "
        "and e.language = 'en' order by e.edition_no desc limit 1", (slug,)).fetchone()
    why = stale(full, edition)
    if why:
        raise SystemExit(f"not adopted: {why}")
    if not doc.get("review", {}).get("ready_for_gate_d"):
        raise SystemExit(f"not adopted: the trial in run #{source} was not ready for Gate D")
    print(f"adopting chapter {chapter} of {slug} from trial run #{source} (text unchanged, laid out again)", flush=True)
    for name in ("draft.json", "dossier-full.json", "dossier.json"):
        if (src / f"ch{chapter:02d}-{name}").exists():
            (out / f"ch{chapter:02d}-{name}").write_bytes((src / f"ch{chapter:02d}-{name}").read_bytes())
    env_file = out / "empty.env"
    env_file.write_text("", encoding="utf-8")
    r = render(doc, full, env_file, out)
    env_file.unlink(missing_ok=True)
    for f in out.iterdir():                              # the HTML pages embed every picture
        if f.suffix == ".html" and not f.name.endswith("-review.html"):
            f.unlink()
    store(conn, full, doc)
    conn.commit()
    summary = {"chapter": chapter, "from_run": source, "ready_for_gate_d": True,
               "words": doc["one_pass"]["words"], "target_words": doc["one_pass"]["target_words"],
               "met": doc["one_pass"]["met"], "sections": len(doc["sections"]), "exercises": len(doc["exercises"]),
               "pages": r["pages"], "hard_defects": r["hard_defects"], "usd": 0.0, "drafted_for_usd": doc["one_pass"]["usd"],
               "layout": doc.get("layout_choice")}
    (out / "draft.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
