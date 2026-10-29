"""Stage `render_trial` — free, no model calls. Lays out an earlier trial draft again with the current renderer
(layout search, spacing variants, overlap check), so a layout fix can be seen without paying for a new draft:

    python -m publishing.runner.render_trial <slug> <run_rel> <chapter> <source_run>

The runner has fetched the earlier run's chNN-draft.json and chNN-dossier-full.json into <book>/trial-source/. The
text is that run's, unchanged; the PDF, print edition, Gate D review page and diagnostics land in this run's folder.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> None:
    from publishing.manuscript.render import render
    slug, rel, chapter, source = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
    book = Path(os.environ["PUBLISH_OUT"]) / "manuscripts" / slug
    out = book / rel
    out.mkdir(parents=True, exist_ok=True)
    env_file = out / "empty.env"
    env_file.write_text("", encoding="utf-8")
    src = book / "trial-source"
    doc = json.loads((src / f"ch{chapter:02d}-draft.json").read_text(encoding="utf-8"))
    full = json.loads((src / f"ch{chapter:02d}-dossier-full.json").read_text(encoding="utf-8"))
    print(f"re-rendering chapter {chapter} of {slug} from run #{source} (text unchanged)", flush=True)
    r = render(doc, full, env_file, out)
    # keep the layout the search chose with the saved draft (the compile prints the chapter in it) — only
    # that key: render() labels figure blocks in memory, and the saved text must stay exactly as drafted
    saved = out / f"ch{chapter:02d}-draft.json"
    kept = json.loads((src / f"ch{chapter:02d}-draft.json").read_text(encoding="utf-8"))
    kept["layout_choice"] = doc.get("layout_choice")
    saved.write_text(json.dumps(kept, ensure_ascii=False, indent=1), encoding="utf-8")
    env_file.unlink(missing_ok=True)
    for f in out.iterdir():                              # the HTML pages embed every picture
        if f.suffix == ".html" and not f.name.endswith("-review.html"):
            f.unlink()
    summary = {"chapter": chapter, "from_run": source, "pages": r["pages"], "hard_defects": r["hard_defects"],
               "layout": doc.get("layout_choice")}
    (out / "render.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
