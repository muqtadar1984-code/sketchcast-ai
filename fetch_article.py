"""Pull the four approved articles whole, as the deck storyboard will see them."""
import json, os, sys
from pathlib import Path
root = Path(__file__).resolve().parent
sys.path.insert(0, str(root))
for line in (root / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
from shared import visual_library as vl
sb = vl._sb()
rows = (sb.table("topic_articles").select("*").eq("status", "approved").execute()).data
out = root / "spike_out"
for r in rows:
    figs = (sb.table("article_figures").select("*").eq("article_id", r["id"]).execute()).data
    r["_figures"] = figs
    print(f"{(r.get('title') or '?')[:34]:36} sections={len(r.get('sections') or [])} "
          f"obj={len(r.get('objectives') or [])} gloss={len(r.get('glossary') or [])} "
          f"misc={len(r.get('misconceptions') or [])} wex={len(r.get('worked_examples') or [])} "
          f"claims={len(r.get('claims') or [])} figs={len(figs)}")
(out / "articles.json").write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
