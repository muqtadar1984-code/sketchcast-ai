"""Stage `check_files` — free, no model calls. Proves the book's source set is whole on the runner and that every
Gate-D-approved chapter's live text still matches its latest stamp (the stamp's hash IS the approval).

    python -m publishing.runner.check <slug> runs/run-<id>

Writes <book>/<run_rel>/check.json. Exits 1 when a chapter is missing or an approved chapter no longer matches.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from publishing.tools.books_import import canonical_sha, whole_file_sha

NEEDED = ["annotations.json", "approvals.json", "kdp.json", "rev2/audit/objectives-0893.json"]
PER_CHAPTER = ["draft", "dossier", "dossier-full", "diagnostics"]


def check(book: Path) -> dict:
    drafts = sorted(book.glob("ch[0-9][0-9]-draft.json"))
    chapters, problems = [], []
    for f in drafts:
        n = int(f.name[2:4])
        doc = json.loads(f.read_text(encoding="utf-8"))
        missing = [k for k in PER_CHAPTER if not (book / f"ch{n:02d}-{k}.json").exists()]
        g = doc.get("gate_d") or {}
        stamped = g.get("status") == "approved"
        matches = None
        if stamped:
            matches = g.get("content_sha256") in (canonical_sha(doc), whole_file_sha(doc))
            if not matches:
                problems.append(f"ch{n:02d}: live text no longer matches its Gate D stamp ({g.get('version')})")
        if missing:
            problems.append(f"ch{n:02d}: missing {', '.join(missing)}")
        chapters.append({"chapter": n, "title": (doc.get("chapter") or {}).get("title"), "gate_d": stamped,
                         "stamp_matches": matches, "missing": missing,
                         "sections": len(doc.get("sections") or []), "exercises": len(doc.get("exercises") or [])})
    absent = [p for p in NEEDED if not (book / p).exists()]
    problems += [f"missing {p}" for p in absent]
    if not drafts:
        problems.append("no chapter drafts found")
    summary = {"chapters": len(chapters), "approved": sum(c["gate_d"] for c in chapters),
               "approved_matching": sum(1 for c in chapters if c["stamp_matches"]), "problems": len(problems)}
    return {"summary": summary, "chapters": chapters, "problems": problems}


def main() -> None:
    slug, rel = sys.argv[1], sys.argv[2]
    book = Path(os.environ["PUBLISH_OUT"]) / "manuscripts" / slug
    result = check(book)
    out = book / rel
    out.mkdir(parents=True, exist_ok=True)
    (out / "check.json").write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    for line in result["problems"]:
        print(f"PROBLEM {line}", flush=True)
    s = result["summary"]
    print(f"checked {s['chapters']} chapters: {s['approved_matching']} of {s['approved']} approved chapters match "
          f"their stamp; {s['problems']} problem(s)", flush=True)
    sys.exit(1 if result["problems"] else 0)


if __name__ == "__main__":
    main()
