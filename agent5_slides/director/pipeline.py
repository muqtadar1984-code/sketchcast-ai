"""The Director route end to end: lesson in, checked editable deck out.

    context → design → ground (steps, numbers, answers) → correct
            → pictures → render → QA (deterministic + rendered + vision)
            → correct the failing slides only → re-render … (bounded)

Everything a reviewer needs to judge the run is in the returned report:
the spec, every repair, coverage, the asset ledger (reused / licensed /
generated / missing), QA per slide, timings per phase and the money.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from shared.lesson_model import LessonModel

from . import assets as A
from . import context as C
from . import director as D
from . import qa as Q
from . import render as R

logger = logging.getLogger(__name__)


@dataclass
class Result:
    path: Path
    report: dict = field(default_factory=dict)
    images: list[Path] = field(default_factory=list)
    pdf: Optional[Path] = None


class _Clock:
    def __init__(self):
        self.t = {}

    def __call__(self, name):
        clock = self

        class _P:
            def __enter__(self):
                self.s = time.time()

            def __exit__(self, *a):
                clock.t[name] = round(clock.t.get(name, 0) + time.time() - self.s, 2)
        return _P()


def ground(deck, ctx) -> dict:
    """Steps to their sources, missing article steps restored, problems
    de-duplicated and long workings split — then resolved once more so every
    steps element carries its final range."""
    shown = D.resolve_steps(deck, ctx)
    D.ensure_examples(deck, ctx, shown)
    D.tidy(deck, ctx)
    return D.resolve_steps(deck, ctx)


def _usage(client) -> dict:
    u = dict(getattr(client, "session_usage", {}) or {})
    return {k: u.get(k) for k in ("calls", "input_tokens", "output_tokens", "cost_usd")}


def build_director_deck(model: LessonModel, *, client, out_path: Path, tmp: Path,
                        vision_client=None, shelf=None, article: Optional[dict] = None,
                        subject: str = "", curriculum: str = "", age_band: str = "",
                        maths_lesson: Optional[dict] = None, shelf_pictures: Optional[list[dict]] = None,
                        branding: Optional[dict] = None, direction: str = "ltr",
                        budget: Optional[A.Budget] = None,
                        yield_to_users: Callable[[], bool] = lambda: False,
                        generate: Callable = A.default_generate, photos: Callable = A.default_photos,
                        render_preview: bool = True, http=None, library_context: Optional[dict] = None,
                        log: Callable[[str], None] = lambda m: None) -> Result:
    budget = budget or A.Budget.from_env()
    spend = A.Spend()
    clock = _Clock()
    tmp = Path(tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    out_path = Path(out_path)
    rounds: list[dict] = []

    with clock("context"):
        ctx = C.build(model, article=article, subject=subject, maths_lesson=maths_lesson,
                      curriculum=curriculum, age_band=age_band, shelf=shelf_pictures, branding=branding)
    log(f"context: {len(ctx.sources)} worked examples, {len(ctx.figures)} figures, age {ctx.age_band or '?'}")

    with clock("design"):
        deck = D.design(ctx, client)
    log(f"design: {len(deck.slides)} slides, {len(deck.assets)} asset needs — {deck.rationale[:140]}")

    with clock("grounding"):
        shown = ground(deck, ctx)
        problems = D.check(deck, ctx)
        n = 0
        while problems and n < budget.corrections:
            n += 1
            log(f"grounding round {n}: {sum(len(v) for v in problems.values())} problems on {len(problems)} slides")
            rounds.append({"stage": "grounding", "round": n, "problems": problems})
            D.correct(deck, ctx, client, problems)
            shown = ground(deck, ctx)
            problems = D.check(deck, ctx)
        if problems:
            D.drop_unfixable(deck, problems, ctx)
            shown = D.resolve_steps(deck, ctx)
        spec_first = deck.to_dict()

    def pictures_and_render(tag: str):
        with clock("assets"):
            rep = A.resolve(deck, tmp=tmp / "assets", shelf=shelf, ctx=ctx, budget=budget, spend=spend,
                            vision_client=vision_client, generate=generate, photos=photos,
                            yield_to_users=yield_to_users, context=library_context or {
                                "subject": ctx.subject, "topic": ctx.title}, http=http)
        log(f"assets ({tag}): {rep.counts()}")
        credits = sorted({p.credit for p in rep.pictures.values() if p.credit})
        with clock("render"):
            path, reports = R.build(deck, rep.pictures, ctx.figures, out_path, branding=branding,
                                    direction=direction, credits=credits)
        return rep, path, reports

    arep, path, reports = pictures_and_render("first")
    images: list[Path] = []
    renderer = "not rendered"
    vis: dict = {}
    issues: dict = {}
    for qa_round in range(budget.corrections + 1):
        with clock("qa"):
            det = Q.check_deck(deck, reports)
            if render_preview:
                images, renderer = Q.render_images(path, tmp / f"qa_{qa_round}")
                if images and vision_client is not None:
                    vis = Q.vision_review(vision_client, images, deck, ctx.age_band, budget.vision_qa_slides)
            issues = Q.merge(det, vis)
            block = Q.blocking(issues)
        log(f"qa round {qa_round}: {Q.summary(issues, vis)}; blocking on {len(block)} slides")
        rounds.append({"stage": "qa", "round": qa_round, "renderer": renderer,
                       "summary": Q.summary(issues, vis), "blocking": block})
        if not block or qa_round >= budget.corrections or spend.elapsed() > budget.max_seconds:
            break
        with clock("correction"):
            D.correct(deck, ctx, client, block)
            shown = ground(deck, ctx)
            leftover = D.check(deck, ctx)
            D.drop_unfixable(deck, {k: v for k, v in leftover.items() if k in block}, ctx)
            shown = D.resolve_steps(deck, ctx)
        arep, path, reports = pictures_and_render(f"round {qa_round + 1}")

    text_usage = _usage(client)
    vision_usage = _usage(vision_client) if vision_client is not None and vision_client is not client else {}
    text_usd = float(text_usage.get("cost_usd") or 0)
    vision_usd = float(vision_usage.get("cost_usd") or 0)
    report = {
        "title": deck.title,
        "slides": len(deck.slides) + (1 if any(p.credit for p in arep.pictures.values()) else 0),
        "rationale": deck.rationale,
        "palette": deck.palette,
        "spec_first": spec_first,
        "spec_final": deck.to_dict(),
        "repairs": deck.repairs,
        "coverage": D.coverage(deck, ctx, shown),
        "assets": arep.to_dict(),
        "qa": {"issues": issues, "vision": vis, "summary": Q.summary(issues, vis), "renderer": renderer},
        "rounds": rounds,
        "timings_s": clock.t | {"total": round(spend.elapsed(), 2)},
        "usage": {"text": text_usage, "vision": vision_usage,
                  "images": {"generated": spend.images_generated, "attempts": spend.image_attempts,
                             "usd": round(spend.image_usd, 4), "photo_lookups": spend.photo_lookups,
                             "vision_checks": spend.vision_calls},
                  "total_usd": round(text_usd + vision_usd + spend.image_usd, 4)},
        "budget": budget.__dict__,
        "renderer_faults": {r.id: r.faults for r in reports if r.faults},
    }
    return Result(path=path, report=report, images=images)


def save_report(result: Result, path: Path) -> None:
    Path(path).write_text(json.dumps(result.report, indent=1, ensure_ascii=False, default=str), "utf-8")
