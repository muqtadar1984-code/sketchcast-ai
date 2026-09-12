"""Offline: the deck beside a REAL video, built from the pictures that video drew.

Pulls the Materials · Part 1 lesson's script.json (the scene each segment
drew), fetches the library rows for those assets, and builds the deck the
presentation job now builds after its render. No model call, no generation.

    python demo_video_deck.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
for _l in (ROOT / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        _k, _v = _l.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

from agent5_slides import deck_art, deck_generator as dg          # noqa: E402
from agent5_slides.deck_storyboard import storyboard, summarise   # noqa: E402
from shared import visual_library as vl                           # noqa: E402
from worker.process import _find_segments                         # noqa: E402

OUT = ROOT / "spike_out" / "video_deck"
SCRIPT = "9ad3649c-9776-4e13-b998-6c41f4fa9956/eb119f41-caa5-4581-a66a-05d9e9daee49/script.json"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    sb = vl._sb()
    body = json.loads(sb.storage.from_("artifacts").download(SCRIPT))
    segments = _find_segments(body) or []
    script = {"episodes": [{"episode_title": "Materials and their structure · Part 1 of 6",
                            "segments": segments}]}
    model = dg.model_from_script({}, script, language="en")
    report = deck_art.decorate(model, sb=sb, tmp=OUT / "art", video_segments=segments, exact=True,
                               context=deck_art.book_context({"subject": "Science", "grade": "Year 7"},
                                                             "Materials and their structure", {}),
                               job_id="offline", allow_generate=False)
    print("pictures:", report)
    for sec in model.sections:
        if sec.figure_keys:
            f = model.figures[sec.figure_keys[0]]
            print(f"  {sec.heading[:34]:36} <- {f.key:28} regions={len(f.regions)} caption={f.caption[:50]!r}")
    slides = storyboard(model)
    path = dg.build_lesson_deck(model, OUT / "materials_part1.pptx", direction="ltr")
    print(f"{path.name}: {len(slides)} slides {summarise(slides)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
