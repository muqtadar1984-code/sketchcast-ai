"""LOCAL ONLY: rebuild a Director deck from its saved report — same spec, same
pictures, the CURRENT renderer — and QA it again. No design call is made;
only the optional vision review spends anything.

    python scripts/rerender_director_deck.py <fixtures_dir> <sample_dir> <maths|science> [--no-vision]
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from run_director_samples import SAMPLES, art_hook, maths_lesson  # noqa: E402  (loads .env, forces no publish)

from agent5_slides.director import assets as A  # noqa: E402
from agent5_slides.director import context as C  # noqa: E402
from agent5_slides.director import qa as Q  # noqa: E402
from agent5_slides.director import render as R  # noqa: E402
from agent5_slides.director import spec as S  # noqa: E402
from shared.lesson_model import from_article  # noqa: E402


def deck_from(d: dict) -> S.DeckSpec:
    deck = S.DeckSpec(title=d["title"], subtitle=d.get("subtitle", ""), rationale=d.get("rationale", ""),
                      palette=d.get("palette", "indigo"), cover_motif=d.get("cover_motif", ""),
                      repairs=list(d.get("repairs") or []))
    for a in d.get("assets") or []:
        deck.assets[a["id"]] = S.AssetNeed(**a)
    for s in d["slides"]:
        rows = [S.Row(r["weight"], [S.Column(c["weight"], c["elements"]) for c in r["cols"]]) for r in s["rows"]]
        deck.slides.append(S.SlideSpec(id=s["id"], purpose=s.get("purpose", ""), background=s["background"],
                                       kicker=s.get("kicker", ""), title=s.get("title", ""),
                                       subtitle=s.get("subtitle", ""), rows=rows, footer=s.get("footer"),
                                       notes=s.get("notes", ""), evidence=list(s.get("evidence") or [])))
    return deck


def main():
    fx, sample, name = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    vision = "--no-vision" not in sys.argv
    cfg = SAMPLES[name]
    rep = json.loads((sample / "report.json").read_text("utf-8"))
    data = json.loads((fx / name / "article.json").read_text("utf-8"))
    model = from_article(data["article"], data["figures"], art=art_hook)
    ctx = C.build(model, article=data["article"], subject=cfg["subject"],
                  maths_lesson=maths_lesson(json.loads((fx / name / "video_scripts.json").read_text("utf-8"))))
    deck = deck_from(rep["spec_final"])
    pics = {k: A.Picture(**v) for k, v in rep["assets"]["pictures"].items()}
    # Re-resolved from the shelf with a ZERO image budget and no photo
    # search: same pictures, but each carries what the shelf knows about it
    # (its licence and author, for the credits).
    shelf = A.LocalShelf(sample.parent / "shelf")
    again = A.resolve(deck, tmp=sample / "work" / "rerender", shelf=shelf, ctx=ctx,
                      budget=A.Budget(max_new_images=0, photo_lookups=0), spend=A.Spend(),
                      generate=lambda p, a: (None, "", 0.0), photos=lambda q, http=None: [])
    pics.update(again.pictures)
    credits = sorted({p.credit for p in pics.values() if p.credit})
    branding = None
    target = sample / f"{cfg['file']}.pptx"
    for arg in sys.argv:
        if arg.startswith("--branding="):
            # a school's template, logo and accent (worker/process.py `branding`)
            branding = json.loads(Path(arg.split("=", 1)[1]).read_text("utf-8"))
            target = sample / f"{cfg['file']}_branded.pptx"
    path, reports = R.build(deck, pics, ctx.figures, target, credits=credits, branding=branding)
    det = Q.check_deck(deck, reports)
    images, how = Q.render_images(path, sample / ("slides_branded" if branding else "slides"),
                                  pdf=path.with_suffix(".pdf"))
    vis = {}
    client = None
    if vision and images:
        from shared.llm import client_for
        client = client_for("en", kind="deck")
        vis = Q.vision_review(client, images, deck, ctx.age_band)
    issues = Q.merge(det, vis)
    rep["spec_final"] = deck.to_dict()
    rep["qa"] = {"issues": issues, "vision": vis, "summary": Q.summary(issues, vis), "renderer": how}
    rep["rerender"] = {"degraded": {r.id: r.degraded for r in reports if r.degraded},
                       "vision_usd": (getattr(client, "session_usage", {}) or {}).get("cost_usd", 0)}
    rep["usage"]["total_usd"] = round(rep["usage"]["total_usd"] + (rep["rerender"]["vision_usd"] or 0), 4)
    if branding:
        print(json.dumps({"branded": str(path), "qa": Q.summary(issues, vis)}, indent=1))
        return
    (sample / "report.json").write_text(json.dumps(rep, indent=1, ensure_ascii=False, default=str), "utf-8")
    print(json.dumps({"slides": len(images), "renderer": how, "qa": rep["qa"]["summary"],
                      "degraded": rep["rerender"]["degraded"]}, indent=1, ensure_ascii=False))
    for sid, its in issues.items():
        for i in its:
            if i["severity"] == "high":
                print("HIGH", sid, i.get("source", "det"), i["category"], i["detail"][:150])


if __name__ == "__main__":
    os.environ.setdefault("DECK_SHELF_PUBLISH", "0")
    main()
