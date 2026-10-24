"""LOCAL ONLY: build the Director's sample decks from real SketchCast lessons.

    python scripts/run_director_samples.py <fixtures_dir> <out_dir> [maths|science ...]

<fixtures_dir>/<name>/article.json        topic_articles row + topic + figure rows (+ local art)
<fixtures_dir>/<name>/video_scripts.json  the sibling video's script_json (verified maths lesson)
<fixtures_dir>/<name>/before_deck.pptx    the production deck today (for the before/after)

What this script will NOT do, by construction:
  * write to the production visual library — the shelf is a LocalShelf in
    <out_dir>/shelf, and DECK_SHELF_PUBLISH is forced off;
  * write to any production table — it reads `jobs` (the never-starve
    probe) and nothing else;
  * run while a user's generation is queued or processing — checked before
    the run and before EVERY image generation.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
ENV = Path(os.getenv("SKETCHCAST_ENV_FILE") or ROOT / ".env")     # a worktree points this at the main checkout's
for line in (ENV.read_text(encoding="utf-8", errors="replace") if ENV.exists() else "").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
os.environ["DECK_SHELF_PUBLISH"] = "0"
os.environ.setdefault("GEMINI_MODEL_DECK", "gemini-3.5-flash")      # prod's deck model

import logging  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

from agent5_slides.director import assets as A  # noqa: E402
from agent5_slides.director import pipeline as P  # noqa: E402
from shared.lesson_model import from_article  # noqa: E402

SAMPLES = {
    "maths": {"file": "SketchCast_Maths_AI_Redesigned", "subject": "mathematics",
              "curriculum": "Cambridge Lower Secondary Mathematics"},
    "science": {"file": "SketchCast_Science_AI_Redesigned", "subject": "biology",
                "curriculum": "Cambridge Lower Secondary Science"},
}


def live_jobs() -> int:
    from shared import visual_library as vl
    sb = vl._sb()
    res = sb.table("jobs").select("id", count="exact").in_("status", ["queued", "processing"]).execute()
    return int(getattr(res, "count", None) or len(getattr(res, "data", None) or []))


def user_live() -> bool:
    try:
        n = live_jobs()
    except Exception:  # noqa: BLE001 — an unreadable queue is a busy one
        return True
    if n:
        print(f"  !! {n} job(s) queued/processing in production — not generating (never-starve rule)")
    return n > 0


def art_hook(row: dict):
    a = row.get("_asset") or {}
    v = a.get("vision") or {}
    if not row.get("_png") or not (v.get("regions") and v.get("w") and v.get("h")):
        return None
    return {"png": row["_png"], "regions": v["regions"], "w": v["w"], "h": v["h"]}


def maths_lesson(scripts: list) -> dict | None:
    for s in scripts:
        sc = s.get("script") if isinstance(s, dict) and isinstance(s.get("script"), dict) else s
        les = ((sc or {}).get("maths") or {}).get("lesson") if isinstance(sc, dict) else None
        if isinstance(les, dict) and les.get("examples"):
            return les
    return None


def main():
    fx, out = Path(sys.argv[1]), Path(sys.argv[2])
    names = sys.argv[3:] or list(SAMPLES)
    n = live_jobs()
    print(f"production jobs queued/processing: {n}")
    if n:
        sys.exit("refusing to start: a user's generation is live (never-starve rule)")
    from shared.llm import client_for
    from spike.scene_engine import raster_assets as ra

    out.mkdir(parents=True, exist_ok=True)
    shelf = A.LocalShelf(out / "shelf")
    summary = {}
    for name in names:
        cfg = SAMPLES[name]
        data = json.loads((fx / name / "article.json").read_text("utf-8"))
        article, figs = data["article"], data["figures"]
        model = from_article(article, figs, art=art_hook)
        lesson = maths_lesson(json.loads((fx / name / "video_scripts.json").read_text("utf-8"))) \
            if (fx / name / "video_scripts.json").exists() else None
        client = client_for("en", kind="deck")
        ra.reset_image_budget(f"director-sample-{name}-{int(time.time())}")
        d = out / name
        d.mkdir(parents=True, exist_ok=True)
        print(f"\n=== {name}: {model.title} — model {getattr(client, 'model', '?')}")
        res = P.build_director_deck(
            model, client=client, vision_client=client, out_path=d / f"{cfg['file']}.pptx", tmp=d / "work",
            shelf=shelf, article=article, subject=cfg["subject"], curriculum=cfg["curriculum"],
            maths_lesson=lesson, yield_to_users=user_live,
            library_context={"subject": cfg["subject"], "topic": model.title, "curriculum": cfg["curriculum"]},
            log=lambda m: print("  " + m))
        P.save_report(res, d / "report.json")
        from agent5_slides.director import qa as Q
        imgs, how = Q.render_images(res.path, d / "slides", pdf=d / f"{cfg['file']}.pdf")
        print(f"  final render: {len(imgs)} slides via {how}")
        summary[name] = {k: res.report[k] for k in ("slides", "timings_s", "usage", "coverage")} | {
            "assets": res.report["assets"]["counts"], "qa": res.report["qa"]["summary"]}
        print(json.dumps(summary[name], indent=1))
    (out / "summary.json").write_text(json.dumps(summary, indent=1), "utf-8")


if __name__ == "__main__":
    main()
