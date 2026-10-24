"""Stage B output: the dependency plan that completes each topic through the
EXISTING catalogue pipelines (D2). Nothing here queues a job.

Cost per step is the MEASURED mean of ``jobs.usage.total_usd`` over finished
production jobs of that type in the snapshot. Where production records no
cost for a type, the step says ``unmeasured``; it never carries a guess.
"""
from __future__ import annotations

from collections import defaultdict
from statistics import mean

from publishing.catalogue_view import CatalogueView
from publishing.schemas import DependencyStep, ReadinessPolicy, TopicAudit

# Human review minutes per step (planning figures; replaced by measured
# reviewer time once the review console records it).
REVIEW_MIN = {
    "create_topic": 10, "map_curriculum": 10, "load_curriculum_node": 15,
    "topic_article": 25, "revise_article": 20, "topic_questions": 30,
    "kit": 25, "figures": 10, "publish": 2, "translate": 20,
}


def measured_costs(cat: CatalogueView) -> dict[str, tuple[float | None, int]]:
    by_type: dict[str, list[float]] = defaultdict(list)
    per_kit: dict[str, float] = defaultdict(float)
    for j in cat.jobs:
        if j["status"] != "done":
            continue
        usd = (j.get("usage") or {}).get("total_usd")
        if usd is None:
            continue
        by_type[j["type"]].append(float(usd))
        p = j.get("params") or {}
        if p.get("kit_id") and j["type"] != "kit_translate":
            per_kit[p["kit_id"]] += float(usd)
    out = {t: (round(mean(v), 4), len(v)) for t, v in by_type.items()}
    # A whole kit = every costed job carrying that kit_id (video, deck,
    # documents, bank render), summed per kit, then averaged across kits.
    # Translation is excluded above, so every kit here is an original build.
    # Many of those kit ids belong to superseded or rejected kits that are not
    # in the en/ms snapshot. They still cost what they cost.
    kits = list(per_kit.values())
    out["kit_total"] = (round(mean(kits), 4), len(kits)) if kits else (None, 0)
    return out


def _cost(costs, key) -> tuple[float | None, str]:
    usd, n = costs.get(key, (None, 0))
    return (usd, f"measured:{n} jobs" if usd is not None else "unmeasured (no usage recorded in production)")


def plan_topic(audit: TopicAudit, costs, policy: ReadinessPolicy) -> list[DependencyStep]:
    failing = {f.split("/")[0] for f in audit.failing}
    sub = {c.code: c.sub_status for c in audit.checks if c.verdict == "fail"}
    steps: list[DependencyStep] = []

    def add(step, job_type, cost_key=None, review=None, lang=None, note="", after=None):
        usd, basis = _cost(costs, cost_key) if cost_key else (0.0, "no model call")
        steps.append(DependencyStep(order=len(steps) + 1, step=step, job_type=job_type, language=lang,
                                    depends_on=after if after is not None else ([len(steps)] if steps else []),
                                    est_cost_usd=usd, cost_basis=basis,
                                    est_review_min=REVIEW_MIN.get(review or "", 0), throughput_note=note))

    rebuild_kit = False
    if "B1" in failing:
        add("Create the topic (Library candidates → created) and map its curriculum nodes",
            "manual:library", review="create_topic")
    if "B2" in failing:
        if sub.get("B2") == "node_not_in_seed":
            add("Load the missing curriculum node(s) into the seed (verify against the official framework)",
                "seed", review="load_curriculum_node")
        add("Map the topic to its curriculum node(s)", "manual:library", review="map_curriculum")
    if "B1" in failing or "B3" in failing:
        add("Generate the English article (topic_article) and approve it", "topic_article",
            "topic_article", "topic_article", "en")
        add("Generate the item bank (topic_questions) and approve items", "topic_questions",
            "topic_questions", "topic_questions", "en")
        rebuild_kit = True
    else:
        b4 = next((c for c in audit.checks if c.code == "B4" and c.verdict == "fail"), None)
        if b4 and b4.sub_status == "content":
            add("Revise the English article (source_article_id + hints) and approve the new version",
                "topic_article", "topic_article", "revise_article", "en",
                note="a new version re-opens the kit, translation and both videos")
            rebuild_kit = True
        if b4 and b4.evidence.get("figures_unrendered"):
            add(f"Render the article's {len(b4.evidence['figures_unrendered'])} unrendered figure(s) "
                "(figure_render) and approve them", "figure_render", "figure_render", "figures", "en",
                note="same article version: no kit, translation or video cascade; uses image quota (off-peak)")
        if "B5" in failing:
            add("Top up the item bank (topic_questions) and approve items", "topic_questions",
                "topic_questions", "topic_questions", "en")
    if rebuild_kit or "B6" in failing:
        add("Generate the English teaching kit and approve it", "presentation+deck+docs", "kit_total", "kit", "en",
            note="off-peak catalogue kit lane (MAX_CATALOGUE_KITS)")
        rebuild_kit = True
    if rebuild_kit or "B7" in failing:
        add("Publish the English video to YouTube", "topic_publish", None, "publish", "en",
            note="EN channel quota")
    for lang in policy.languages[1:]:
        if rebuild_kit or {"B8", "B9"} & failing:
            add(f"Translate the article and kit into {lang} (kit_translate) and approve", "kit_translate",
                "kit_translate", "translate", lang, note="Auto Translate Agent runs one translation at a time")
        if rebuild_kit or {"B8", "B9", "B10"} & failing:
            add(f"Publish the {lang} video to YouTube", "topic_publish", None, "publish", lang,
                note=f"{lang} channel: 1/day today; 3-5/day under D13")
    if "B11" in failing and not steps:
        add("Resolve the failed/blocked job before readiness (person reviews the error)", "manual:review",
            review="publish")
    return steps


def plan_dependencies(cat: CatalogueView, audits: list[TopicAudit], policy: ReadinessPolicy) -> dict:
    costs = measured_costs(cat)
    plans: dict = {}
    for a in audits:
        key = a.topic_slug or f"proposed:{a.title}"
        if key in plans:
            continue
        plans[key] = {"audit": a, "steps": plan_topic(a, costs, policy)}
    totals = {"model_usd": 0.0, "unmeasured_steps": 0, "review_min": 0, "publish": defaultdict(int)}
    for p in plans.values():
        for s in p["steps"]:
            if s.est_cost_usd is None:
                totals["unmeasured_steps"] += 1
            else:
                totals["model_usd"] += s.est_cost_usd
            totals["review_min"] += s.est_review_min
            if s.job_type == "topic_publish":
                totals["publish"][s.language] += 1
    totals["model_usd"] = round(totals["model_usd"], 2)
    totals["publish"] = dict(totals["publish"])
    return {"plans": plans, "totals": totals, "costs": costs}


def publish_days(uploads: int, per_day: int) -> int:
    """Days to clear `uploads` at `per_day` uploads a day (ceiling)."""
    return 0 if uploads <= 0 else -(-uploads // max(1, per_day))


def store_plan(conn, run_id: int, plan: dict, reqs: list[dict]) -> None:
    by_topic: dict = {}
    for r in reqs:
        by_topic.setdefault(str(r["topic_id"]) if r["topic_id"] else f"proposed:{r['proposed_title']}", r["id"])
    for key, p in plan["plans"].items():
        a = p["audit"]
        rid = by_topic.get(a.topic_id or f"proposed:{a.title}") or a.required_index
        for s in p["steps"]:
            conn.execute(
                "insert into pub.dependency_steps (run_id, required_topic_id, step_order, step, job_type, language, "
                "depends_on, est_cost_usd, cost_basis, est_review_min, throughput_note) "
                "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (run_id, rid, s.order, s.step, s.job_type, s.language, s.depends_on, s.est_cost_usd,
                 s.cost_basis, s.est_review_min, s.throughput_note))
