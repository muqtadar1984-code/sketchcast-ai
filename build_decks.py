"""Build one deck per approved article, from the live rows, offline.

Downloads each figure's artwork once into spike_out/art/, then runs the real
path: article -> LessonModel -> storyboard -> .pptx. No model call anywhere.

    python build_decks.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
for _line in (ROOT / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

from agent5_slides import deck_render                      # noqa: E402
from agent5_slides.deck_storyboard import storyboard, summarise  # noqa: E402
from shared import visual_library as vl                    # noqa: E402
from shared.lesson_model import from_article               # noqa: E402

OUT = ROOT / "spike_out"
ART = OUT / "art"


def main() -> int:
    sb_ = vl._sb()
    ART.mkdir(parents=True, exist_ok=True)
    arts = (sb_.table("topic_articles").select("*").eq("status", "approved").execute()).data

    def artwork(row: dict):
        """Download the figure's PNG and hand back its measured frame."""
        aid = row.get("visual_asset_id")
        if not aid:
            return None
        a = (sb_.table("visual_assets").select("asset_key, storage_path, vision")
             .eq("id", aid).single().execute()).data
        vision = a.get("vision") or {}
        if not (vision.get("regions") and vision.get("w") and vision.get("h")):
            return None
        png = ART / f"{a['asset_key']}.png"
        if not png.exists():
            png.write_bytes(sb_.storage.from_(vl.BUCKET).download(a["storage_path"]))
        return {"png": png, "regions": vision["regions"],
                "w": vision["w"], "h": vision["h"]}

    all_faults = 0
    for a in arts:
        figs = (sb_.table("article_figures").select("*")
                .eq("article_id", a["id"]).order("sort").execute()).data
        model = from_article(a, figs, art=artwork)
        slides = storyboard(model)
        name = "".join(c if c.isalnum() else "_" for c in (model.title or "lesson"))[:44]
        path, faults = deck_render.build(slides, OUT / f"deck_{name}.pptx")
        drawn = sum(1 for f in model.figures.values() if f.annotatable)
        print(f"\n{model.title[:44]:46} {len(slides):>3} slides  "
              f"{path.stat().st_size / 1024:>5.0f} KB  figures {drawn}/{len(model.figures)} annotatable")
        print(f"    {summarise(slides)}")
        for f in faults:
            print("    FAULT:", f)
        all_faults += len(faults)
    print(f"\n{'clean' if not all_faults else str(all_faults) + ' fault(s)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
