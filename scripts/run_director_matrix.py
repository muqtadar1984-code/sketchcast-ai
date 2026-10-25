"""LOCAL ONLY: the Director's regression matrix on real approved lessons, with
PRODUCTION's model routing (read from Railway once and mirrored here).

    python scripts/run_director_matrix.py <out_dir> [case ...]

Each case fetches its lesson read-only (topic_articles, article_figures,
visual_assets, the sibling video's script), runs the full pipeline exactly as
the worker would for that language, renders and QAs it, and writes
<out_dir>/<case>/{deck.pptx, deck.pdf, slides/, report.json}. The shelf is a
LocalShelf shared across cases (so a repeated / translated topic shows reuse);
DECK_SHELF_PUBLISH is forced off. The production `jobs` queue is checked
before every case and before every image generation (never-starve).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

# Production routing (Railway, 2026-10-24) — set BEFORE the clients are built.
PROD_ROUTING = {
    "LLM_BACKEND": "bedrock", "BEDROCK_REGION": "us-east-1",
    "ARTIFACT_MODEL": "claude-sonnet-5-5", "GEMINI_MODEL_DECK": "gemini-3.5-flash",
    "CATALOGUE_TEXT_PROVIDER": "anthropic", "CATALOGUE_CLAUDE_MODEL_TEXT": "claude-sonnet-5-5",
    "TEXT_MODEL_PROFILE": "supported",
    # the configuration being validated for release (matrix round 3: Sonnet
    # designed both control decks better than Haiku, the role's default)
    "CATALOGUE_CLAUDE_MODEL_DECK": os.getenv("MATRIX_DECK_MODEL", "claude-sonnet-5-5"),
}
for k, v in PROD_ROUTING.items():
    os.environ[k] = v
from run_director_samples import art_hook, live_jobs, maths_lesson, user_live  # noqa: E402  (loads .env)

from agent5_slides import director  # noqa: E402
from agent5_slides.director import assets as A  # noqa: E402
from agent5_slides.director import pipeline as P  # noqa: E402
from agent5_slides.director import qa as Q  # noqa: E402
from shared.lesson_model import from_article  # noqa: E402

CASES = {
    "ar_photosynthesis": {"article": "eb4b31af-ca3f-4ab9-9cdf-a050284bcbce", "covers": "Arabic, RTL, science process"},
    "jawi_plant_animal_cells": {"article": "ab964f01-1198-4392-a34e-f05eb33518d2",
                                "covers": "Jawi, RTL, translated repeat of the approved science deck"},
    "en_types_of_angles": {"article": "5bb68173-3142-4563-9f16-4e272e1d0675",
                           "covers": "geometry with engine figures and verified examples"},
    "en_algebraic_fractions": {"article": "f32bde40-030a-4252-afc1-dfaea517e3df", "covers": "maths, no diagrams"},
    "en_food_chains_no_images": {"article": "a4325c8b-109e-41a4-b74a-5ed41ae9b131",
                                 "covers": "image generation and photo search unavailable (failure path)",
                                 "budget": {"max_new_images": 0, "photo_lookups": 0}},
    "en_acids_alkalis_cbse": {"article": "c908e256-f4f7-4fac-81e6-621c14b6a9f5", "covers": "CBSE curriculum"},
    "en_factorising_long": {"article": "923e85e9-226a-4720-9f93-0123da1ba00a",
                            "covers": "long lesson (1,959 words), large notes"},
    "jawi_forces_short": {"article": "fa14bb7c-ee0b-444e-be18-c3d1b376865d", "covers": "short lesson (588 words), Jawi"},
    # controls: the same lessons designed by Sonnet instead of production's Haiku default
    "en_types_of_angles_sonnet": {"article": "5bb68173-3142-4563-9f16-4e272e1d0675",
                                  "covers": "control: Types of Angles designed by Sonnet 5.5",
                                  "env": {"CATALOGUE_CLAUDE_MODEL_DECK": "claude-sonnet-5-5"}},
    "en_acids_alkalis_sonnet": {"article": "c908e256-f4f7-4fac-81e6-621c14b6a9f5",
                                "covers": "control: Acids & Alkalis designed by Sonnet 5.5",
                                "env": {"CATALOGUE_CLAUDE_MODEL_DECK": "claude-sonnet-5-5"}},
}


def fetch(sb, article_id: str, d: Path) -> dict:
    """Read-only: the article, its figure rows + art, and its kit's video script."""
    from shared import visual_library as vl
    (d / "art").mkdir(parents=True, exist_ok=True)
    cache = d / "article.json"
    if cache.exists():
        return json.loads(cache.read_text("utf-8"))
    art = sb.table("topic_articles").select("*").eq("id", article_id).execute().data[0]
    topic = (sb.table("topics").select("*").eq("id", art["topic_id"]).execute().data or [{}])[0]
    figs = sb.table("article_figures").select("*").eq("article_id", article_id).order("sort").execute().data
    for f in figs:
        if not f.get("visual_asset_id"):
            continue
        a = (sb.table("visual_assets").select("id, asset_key, storage_path, vision, description")
             .eq("id", f["visual_asset_id"]).execute().data or [None])[0]
        if not a or not a.get("storage_path"):
            continue
        f["_asset"] = a
        png = d / "art" / f"{a['asset_key']}.png"
        if not png.exists():
            png.write_bytes(sb.storage.from_(vl.BUCKET).download(a["storage_path"]))
        f["_png"] = str(png)
    kit = (sb.table("topic_kits").select("presentation_generation_id").eq("article_id", article_id)
           .order("created_at", desc=True).limit(1).execute().data or [{}])[0]
    scripts = []
    if kit.get("presentation_generation_id"):
        rows = (sb.table("artifacts").select("storage_path").eq("generation_id", kit["presentation_generation_id"])
                .eq("kind", "script_json").execute().data)
        for r in sorted(x["storage_path"] for x in rows):
            scripts.append(json.loads(sb.storage.from_("artifacts").download(r)))
    data = {"article": art, "topic": topic, "figures": figs, "scripts": scripts}
    cache.write_text(json.dumps(data, indent=1, default=str, ensure_ascii=False), "utf-8")
    return data


def main():
    out = Path(sys.argv[1])
    names = sys.argv[2:] or list(CASES)
    from shared import llm
    from shared import visual_library as vl
    from spike.scene_engine import raster_assets as ra

    sb = vl._sb()
    shelf = A.LocalShelf(out / "shelf")
    summary = json.loads((out / "summary.json").read_text("utf-8")) if (out / "summary.json").exists() else {}
    for name in names:
        case = CASES[name]
        n = live_jobs()
        if n:
            print(f"!! {n} production job(s) live — stopping before {name} (never-starve)")
            break
        d = out / name
        data = fetch(sb, case["article"], d)
        art = data["article"]
        lang = (art.get("language") or "en").lower()
        rtl = lang in ("ar", "ms-arab")
        model = from_article(art, data["figures"], art=art_hook)
        lesson = maths_lesson(data["scripts"])
        # exactly the worker's choice for a catalogue deck (worker/process.py)
        llm.set_catalogue_scope(True)
        saved_env = {k: os.environ.get(k) for k in (case.get("env") or {})}
        os.environ.update(case.get("env") or {})
        client = llm.client_for(lang, kind=None if lang == "ms-arab" else "deck")
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        ra.reset_image_budget(f"director-matrix-{name}-{int(time.time())}")
        budget = A.Budget.from_env()
        for k, v in (case.get("budget") or {}).items():
            setattr(budget, k, v)
        subject = str(data["topic"].get("subject") or "")
        print(f"\n=== {name} [{lang}] {art['title'][:60]} — {type(client).__name__} {getattr(client, 'model', '?')}")
        t0 = time.time()
        try:
            res = P.build_director_deck(
                model, client=client, vision_client=director.vision_client(client), out_path=d / "deck.pptx", tmp=d / "work",
                shelf=shelf, article=art, subject=subject, maths_lesson=lesson, budget=budget,
                direction="rtl" if rtl else "ltr", yield_to_users=user_live,
                library_context={"subject": subject, "topic": model.title}, log=lambda m: print("  " + m))
            P.save_report(res, d / "report.json")
            imgs, how = Q.render_images(res.path, d / "slides", pdf=d / "deck.pdf")
            r = res.report
            summary[name] = {"covers": case["covers"], "language": lang, "model": getattr(client, "model", ""),
                             "slides": r["slides"], "qa": r["qa"]["summary"], "usd": r["usage"]["total_usd"],
                             "seconds": r["timings_s"].get("total"), "assets": r["assets"]["counts"],
                             "coverage": r["coverage"], "budget_stops": r.get("budget_stops"),
                             "repairs": len(r["repairs"]), "ok": True}
        except Exception as exc:  # noqa: BLE001 — a failed case is a result, not a crashed matrix
            import traceback
            traceback.print_exc()
            summary[name] = {"covers": case["covers"], "language": lang, "ok": False,
                             "error": f"{type(exc).__name__}: {exc}"[:400], "seconds": round(time.time() - t0, 1)}
        finally:
            llm.set_catalogue_scope(False)
        print(json.dumps(summary[name], indent=1, ensure_ascii=False))
        (out / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False), "utf-8")


if __name__ == "__main__":
    os.environ["DECK_SHELF_PUBLISH"] = "0"
    main()
