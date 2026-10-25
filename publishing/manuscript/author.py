"""Draft ONE chapter (Phase 5 vertical slice; the same code drafts every chapter
of every book in Phase 7 — no subject branches).

    dossier → chapter_plan → draft_section ×N (parallel) → exercises
            → deterministic checks → independent review (other model family)
            → stored as unit revisions with source_refs, costs, findings

Roles (D5): publish_author = Claude on Bedrock, publish_reviewer = Gemini on
Vertex. The dossier is the cached prefix of every author call.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from psycopg.types.json import Jsonb

from publishing.blueprint import _load_model_env, author_client
from publishing.manuscript import dossier as D
from publishing.manuscript import validate as V
from publishing.schemas import ROOT

log = logging.getLogger("publishing.manuscript")
PROMPTS = ROOT / "prompts"
CONSTITUTION = "constitution_v0_2"
CARDS = {"plan": "card_chapter_plan_v0_1", "section": "card_draft_section_v0_1",
         "exercises": "card_exercises_v0_1", "review": "card_review_v0_1"}
AUTHOR, REVIEWER = "anthropic", "gemini"


def _ulid() -> str:
    t = int(time.time() * 1000)
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    head = "".join(alphabet[(t >> s) & 31] for s in range(45, -5, -5))
    return head + "".join(secrets.choice(alphabet) for _ in range(16))


def _system(card: str) -> str:
    return (PROMPTS / f"{CONSTITUTION}.md").read_text(encoding="utf-8") + "\n\n" + \
        (PROMPTS / f"{CARDS[card]}.md").read_text(encoding="utf-8")


class Run:
    """One chapter run: the clients, the cached dossier prefix and a cost ledger."""

    def __init__(self, dossier: dict):
        self.d = dossier
        self.prefix = "CHAPTER DOSSIER (the complete content boundary):\n" + D.for_prompt(dossier)
        self.author = author_client(AUTHOR)
        self.reviewer = author_client(REVIEWER)
        self.costs: list[dict] = []

    def call(self, role: str, card: str, task: str, max_tokens: int, unit: str) -> dict:
        cli = self.reviewer if role == "reviewer" else self.author
        t0 = time.time()
        reply = cli.analyze(task, system=_system(card), max_tokens=max_tokens, cache_prefix=self.prefix)
        if reply.get("truncated"):
            raise RuntimeError(f"{card} for {unit}: reply hit the output cap ({max_tokens})")
        u = reply.get("usage") or {}
        self.costs.append({"stage": card, "unit": unit, "provider": REVIEWER if role == "reviewer" else AUTHOR,
                           "model": getattr(cli, "model", "?"), "in": u.get("input_tokens"),
                           "out": u.get("output_tokens"), "usd": u.get("estimated_cost_usd") or 0,
                           "secs": round(time.time() - t0, 1)})
        data = reply["data"]
        if not isinstance(data, dict) or data.get("error"):
            raise RuntimeError(f"{card} for {unit}: model returned {str(data)[:300]}")
        return data


def draft_chapter(conn, slug: str, chapter: int, env_file: Path, out_dir: Path) -> dict:
    _load_model_env(env_file)
    d = D.build(conn, slug, chapter, env_file)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"ch{chapter:02d}-dossier.json").write_text(D.for_prompt(d), encoding="utf-8")
    (out_dir / f"ch{chapter:02d}-dossier-full.json").write_text(json.dumps(d, ensure_ascii=False, default=str),
                                                                encoding="utf-8")
    valid = D.ids(d)
    figure_ids = {f["id"] for t in d["topics"] for f in t["figures"]} | {a["id"] for a in d["library_assets"]}
    nodes = set(d["chapter"]["node_codes"])
    run = Run(d)
    ch = d["chapter"]
    head = (f"Book: {d['work']['title']} — ages {d['work']['age_band']}, grades {d['work']['grade_band']}, "
            f"{d['work']['curriculum']}.\nChapter {ch['number']}: {ch['title']} (curriculum codes {ch['node_codes']}).\n")

    plan = run.call("author", "plan", head + "Plan this chapter. Blueprint intent:\n" +
                    json.dumps({k: ch[k] for k in ("purpose", "blueprint_sections", "assessment", "illustration_needs")},
                               ensure_ascii=False, indent=1), 4000, "plan")

    def one(sec):
        others = [s["heading"] for s in plan["sections"] if s["sid"] != sec["sid"]]
        return run.call("author", "section", head + f"Opening of the chapter: {plan.get('opening')}\n"
                        f"Other sections (do not repeat them): {others}\nWrite this section:\n" +
                        json.dumps(sec, ensure_ascii=False, indent=1), 5000, sec["sid"])

    with ThreadPoolExecutor(max_workers=4) as pool:
        sections = list(pool.map(one, plan["sections"]))
    ex = run.call("author", "exercises", head + "Chapter plan:\n" + json.dumps(plan, ensure_ascii=False, indent=1) +
                  "\nSection headings: " + json.dumps([s["heading"] for s in sections]), 9000, "exercises")
    exercises = ex.get("exercises") or []

    findings = (V.check_sections(sections, valid, figure_ids)
                + V.check_exercises(exercises, valid, figure_ids, nodes)
                + V.check_coverage(sections, exercises, plan, nodes))
    blind = [{k: v for k, v in x.items() if k in ("xid", "tier", "item_type", "stem", "options", "figure_ref")}
             for x in exercises]
    review = run.call("reviewer", "review", head + "Drafted chapter:\n" + json.dumps(
        {"opening": plan.get("opening"), "sections": sections, "exercises": blind}, ensure_ascii=False, indent=1),
        12000, "review")

    chapter_doc = {"chapter": ch, "work": d["work"], "plan": plan, "sections": sections, "exercises": exercises,
                   "checks": findings, "review": review, "costs": run.costs,
                   "videos": sorted({v for t in d["topics"] for v in t["videos"]}),
                   "prompt_versions": {"constitution": CONSTITUTION, **CARDS}}
    (out_dir / f"ch{chapter:02d}-draft.json").write_text(json.dumps(chapter_doc, ensure_ascii=False, indent=1),
                                                          encoding="utf-8")
    store(conn, d, chapter_doc)
    return chapter_doc


def store(conn, d: dict, doc: dict) -> None:
    """Units with stable uids and one immutable revision each (§6.2)."""
    ed, ch = d["edition_id"], d["chapter"]
    conn.execute("insert into pub.units (uid, edition_id, kind, sort) values (%s,%s,'chapter',%s) "
                 "on conflict (uid) do nothing", (ch["uid"], ed, ch["number"]))

    def revise(uid, kind, sort, body, refs, stage, parent=ch["uid"]):
        conn.execute("insert into pub.units (uid, edition_id, parent_uid, kind, sort) values (%s,%s,%s,%s,%s) "
                     "on conflict (uid) do nothing", (uid, ed, parent, kind, sort))
        rev = conn.execute("select coalesce(max(revision),0)+1 r from pub.unit_revisions where unit_uid=%s",
                           (uid,)).fetchone()["r"]
        cost = sum(c["usd"] for c in doc["costs"] if c["unit"] == stage)
        return conn.execute(
            "insert into pub.unit_revisions (unit_uid, revision, body, source_refs, prompt_version, provider, model, usd, "
            "status) values (%s,%s,%s,%s,%s,%s,%s,%s,'draft') returning id",
            (uid, rev, Jsonb(body), sorted(set(refs)), f"{CONSTITUTION}+{CARDS.get(kind, kind)}", AUTHOR,
             os.getenv("PUBLISH_CLAUDE_MODEL", "claude-sonnet-5-5"), cost)).fetchone()["id"]

    revise(ch["uid"], "chapter", ch["number"], {"plan": doc["plan"], "review": doc["review"]},
           [r for s in doc["plan"]["sections"] for r in s.get("source_refs") or []], "plan", parent=None)
    for i, s in enumerate(doc["sections"], 1):
        uid = f"{ch['uid']}-{s['sid']}"
        rid = revise(uid, "section", i, s, [r for b in s.get("blocks") or [] for r in b.get("source_refs") or []],
                     s["sid"])
        for f in [f for f in doc["checks"] if f["unit"].split(".")[0] == s["sid"]]:
            conn.execute("insert into pub.validation_results (unit_uid, revision_id, check_name, verdict, detail) "
                         "values (%s,%s,%s,%s,%s)", (uid, rid, f["check"], f["verdict"], Jsonb(f)))
    for i, x in enumerate(doc["exercises"], 1):
        uid = f"{ch['uid']}-{x['xid']}"
        rid = revise(uid, "exercise", i, x, list(x.get("source_refs") or []) +
                     ([x["source_item_id"]] if x.get("source_item_id") else []), "exercises")
        for f in [f for f in doc["checks"] if f["unit"] == x["xid"]]:
            conn.execute("insert into pub.validation_results (unit_uid, revision_id, check_name, verdict, detail) "
                         "values (%s,%s,%s,%s,%s)", (uid, rid, f["check"], f["verdict"], Jsonb(f)))
    for f in doc["review"].get("findings") or []:
        conn.execute("insert into pub.validation_results (unit_uid, check_name, verdict, detail) values (%s,%s,%s,%s)",
                     (f"{ch['uid']}-{f.get('unit')}" if f.get("unit") else ch["uid"], f"review:{f.get('kind')}",
                      "fail" if f.get("severity") == "blocker" else "warn", Jsonb(f)))
    for c in doc["costs"]:
        conn.execute("insert into pub.cost_events (work_uid, stage, provider, model, input_tokens, output_tokens, usd, "
                     "edition_id, unit_uid) values ((select work_uid from pub.editions where id=%s),%s,%s,%s,%s,%s,%s,%s,%s)",
                     (ed, f"manuscript:{c['stage']}", c["provider"], c["model"], c["in"], c["out"], c["usd"], ed,
                      c["unit"]))
    conn.commit()
