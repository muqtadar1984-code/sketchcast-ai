"""Offline: the BOOK deck on its new path — the chapter part authored as an
article, the video's pictures on it, rendered. One real authoring call on
the deck's artifact model; no upload, no generation of images.

The chapter text is the Materials · Part 1 lesson's own narration (the
script the video spoke), which is the chapter part's content in the
teacher's words — enough to prove the shape without re-ingesting the PDF.

    python demo_article_deck.py
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
from agent5_slides.deck_article import author_article             # noqa: E402
from agent5_slides.deck_storyboard import storyboard, summarise   # noqa: E402
from shared import visual_library as vl                           # noqa: E402
from shared.llm import client_for                                 # noqa: E402
from worker.process import _find_segments                         # noqa: E402

OUT = ROOT / "spike_out" / "article_deck"
SCRIPT = "9ad3649c-9776-4e13-b998-6c41f4fa9956/eb119f41-caa5-4581-a66a-05d9e9daee49/script.json"
BOOK = {"id": "8fce6e4c-a369-4b61-b677-2dbf9c3089f3", "title": "Cambridge Primary Science Year 7",
        "subject": "Science", "grade": "Year 7"}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    sb = vl._sb()
    body = json.loads(sb.storage.from_("artifacts").download(SCRIPT))
    segments = _find_segments(body) or []
    chapter = {"chapter_num": 2, "title": "Materials and their structure · Part 1 of 6",
               "sections": [{"section_title": str(s.get("slide_heading") or f"Segment {i + 1}"),
                             "content": str(s.get("text") or ""), "subsections": []}
                            for i, s in enumerate(segments)]}
    client = client_for("en", kind="deck")
    article = author_article(BOOK, chapter, {}, client, {}, "en", title=chapter["title"])
    (OUT / "article.json").write_text(json.dumps(article, indent=1, ensure_ascii=False), encoding="utf-8")
    print("article:", article["title"], "| sections", len(article["sections"]), "| claims", len(article["claims"]),
          "| figures", [f["figure_key"] for f in article["figures"]], "| words", article["word_count"])
    for s in article["sections"]:
        print(f"  {s['id']} {s['heading'][:40]:42} figs={s['figure_keys']}")
    model = dg.model_from_book_article(article)
    report = deck_art.decorate(model, sb=sb, tmp=OUT / "art", video_segments=segments, exact=False,
                               context=deck_art.book_context(BOOK, chapter["title"], {}),
                               job_id="offline", allow_generate=False)
    print("pictures:", report)
    for sec in model.sections:
        for f in model.figures_for(sec):
            print(f"  {sec.heading[:34]:36} <- {f.key:28} png={'yes' if f.png else 'no':3} regions={len(f.regions)}")
    slides = storyboard(model)
    path = dg.build_lesson_deck(model, OUT / "materials_part1.pptx", direction="ltr")
    print(f"{path.name}: {len(slides)} slides {summarise(slides)}")
    print("usage:", getattr(client, "session_usage", {}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
