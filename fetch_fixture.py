"""Pull the two rendered Cells figures + their annotated assets to disk."""
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
out = root / "spike_out"
rows = (sb.table("article_figures")
        .select("figure_key, caption, spec, labels, visual_asset_id")
        .in_("figure_key", ["animal_cell", "plant_cell"]).execute()).data
fix = []
for r in rows:
    a = (sb.table("visual_assets").select("asset_key, storage_path, vision")
         .eq("id", r["visual_asset_id"]).single().execute()).data
    blob = sb.storage.from_(vl.BUCKET).download(a["storage_path"])
    png = out / f"{r['figure_key']}.png"
    png.write_bytes(blob)
    fix.append({"figure": r, "asset": a, "png": png.name, "bytes": len(blob)})
    print(f"{r['figure_key']:12} {len(blob):>8} bytes  {a['storage_path']}")
(out / "fixture.json").write_text(json.dumps(fix, indent=2, default=str), encoding="utf-8")
