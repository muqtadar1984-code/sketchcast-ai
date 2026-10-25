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
         "exercises": "card_exercises_v0_1", "review": "card_review_v0_1", "gates": "card_gates_v0_1"}
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
        # The reviewer must be a peer, not the cheapest profile: the shared
        # "analysis" profile resolves to Flash-Lite (ch 1 run, 2026-10-25).
        os.environ.setdefault("PUBLISH_GEMINI_MODEL", "gemini-3.5-flash")
        self.reviewer = author_client(REVIEWER)
        self.costs: list[dict] = []

    def look(self, png: bytes, prompt: str, unit: str) -> dict:
        """The reviewer LOOKS at a placed picture (a text review never sees one)."""
        t0 = time.time()
        reply = self.reviewer.analyze_image(png, prompt, max_tokens=800)
        u = reply.get("usage") or {}
        self.costs.append({"stage": "figure_fit", "unit": unit, "provider": REVIEWER,
                           "model": getattr(self.reviewer, "model", "?"), "in": u.get("input_tokens"),
                           "out": u.get("output_tokens"), "usd": u.get("estimated_cost_usd") or 0,
                           "secs": round(time.time() - t0, 1)})
        return reply.get("data") or {}

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
    toc = "\n".join(f"  {c['sort']}. {c['title']} {c['node_codes']}" for c in conn.execute(
        "select sort, title, node_codes from pub.blueprint_chapters where blueprint_id=(select id from pub.blueprints "
        "where work_uid=(select uid from pub.works where slug=%s) order by revision desc limit 1) order by sort",
        (slug,)).fetchall())
    head = (f"Book: {d['work']['title']} — ages {d['work']['age_band']}, grades {d['work']['grade_band']}, "
            f"{d['work']['curriculum']}.\nChapter {ch['number']}: {ch['title']} (curriculum codes {ch['node_codes']}).\n"
            f"The whole book (teach ONLY this chapter's ideas; anything another chapter teaches is left to it, "
            f"even when a dossier section mentions it):\n{toc}\n"
            f"Curriculum objectives of THIS chapter (exact text — the scope):\n" +
            "".join(f"  {o['code']}: {o['text']}\n" for o in ch.get("objectives") or []) +
            "Skills its exercises may practise:\n" +
            "".join(f"  {o['code']}: {o['text']}\n" for o in ch.get("skills") or []))
    photo_ids = {a["id"] for a in d["library_assets"] if a.get("kind") == "photo"}
    skills = {o["code"] for o in ch.get("skills") or []}

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
    dedupe(sections)
    fit = figure_fit(run, sections, env_file)
    plan.setdefault("visual_gaps", []).extend(fit["gaps"])
    figure_ids -= fit["removed"]          # an exercise may not use a picture the fit check threw out
    for s in plan["sections"]:
        s["figure_refs"] = [r for r in s.get("figure_refs") or [] if r not in fit["removed"]]
    ex = run.call("author", "exercises", head + "Chapter plan:\n" + json.dumps(plan, ensure_ascii=False, indent=1) +
                  "\nSection headings: " + json.dumps([s["heading"] for s in sections]), 9000, "exercises")
    exercises = ex.get("exercises") or []

    def checks():
        return (V.check_sections(sections, valid, figure_ids)
                + V.check_exercises(exercises, valid, figure_ids, nodes, photo_ids, skills)
                + V.check_extension(sections, exercises)
                + V.check_coverage(sections, exercises, plan, nodes))

    def blind():
        return [{k: v for k, v in x.items() if k in ("xid", "tier", "item_type", "stem", "options", "pairs",
                                                      "figure_ref", "figure_refs")} for x in exercises]

    def do_review(unit):
        r = run.call("reviewer", "review", head + "Drafted chapter:\n" + json.dumps(
            {"opening": plan.get("opening"), "sections": sections, "exercises": blind()}, ensure_ascii=False,
            indent=1), 12000, unit)
        # the two compulsory gates (founder 2026-10-25): coverage + assessment validity
        r["gates"] = run.call("reviewer", "gates", head + "Chapter to gate:\n" + json.dumps(
            {"sections": sections, "exercises": exercises}, ensure_ascii=False, indent=1), 9000, f"{unit}:gates")
        return r

    def is_ready(r, fs):
        g = r.get("gates") or {}
        return (g.get("coverage_gate") == "pass" and g.get("assessment_gate") == "pass"
                and not any(f["verdict"] == "fail" for f in fs)
                and not any(f.get("severity") == "blocker" for f in r.get("findings") or []))

    def to_fix(r, fs):
        """Every blocker/major finding, failed gate item and failed check, by unit."""
        serious = [f for f in r.get("findings") or [] if f.get("severity") in ("blocker", "major")]
        g = r.get("gates") or {}
        serious += [{"unit": a["xid"], "kind": "assessment", "severity": "blocker", "detail": a.get("fix"),
                     "failed": a.get("failed")} for a in g.get("assessment") or [] if a.get("verdict") == "fail"]
        serious += [{"unit": b.get("unit"), "kind": "scope", "severity": "major", "quote": b.get("quote"),
                     "detail": "beyond the objectives: cut it, or fold it into the single extension box"}
                    for b in g.get("beyond_scope") or []]
        serious += [{"unit": f["unit"].split(".")[0], "kind": f"check:{f['check']}", "severity": "blocker",
                     "detail": f["detail"]} for f in fs if f["verdict"] == "fail"]
        out: dict[str, list] = {}
        for f in serious:
            out.setdefault(str(f.get("unit")), []).append(f)
        return out

    fold_extension_sections(sections, plan)
    findings = checks()
    review = do_review("review")
    rounds = []
    # REVISE (§4 review → revise), at most twice: the author resolves every
    # blocker and major finding, failed gate item and failed check; the chapter
    # is then checked, reviewed and gated again. What is left goes to Gate D.
    for rnd in (1, 2):
        if is_ready(review, findings):
            break
        by_unit = to_fix(review, findings)
        if not by_unit:
            break
        rounds.append({"round": rnd, "findings": review.get("findings"), "gates": review.get("gates"),
                       "checks": findings})

        def fix(sec, by_unit=by_unit, rnd=rnd):
            fs = by_unit.get(sec["sid"])
            if not fs:
                return sec
            spec = next((p for p in plan["sections"] if p["sid"] == sec["sid"]), {})
            return run.call("author", "section", head + "Revise this section so that every finding below is "
                            "resolved, staying inside the dossier. Keep what is right; change only what the findings "
                            "require; keep every figure block you still need.\nPlan entry:\n" +
                            json.dumps(spec, ensure_ascii=False) + "\nCurrent draft:\n" +
                            json.dumps(sec, ensure_ascii=False) + "\nFindings:\n" + json.dumps(fs, ensure_ascii=False),
                            5000, f"{sec['sid']}:revise{rnd}")
        with ThreadPoolExecutor(max_workers=4) as pool:
            sections = list(pool.map(fix, sections))
        dedupe(sections)
        fold_extension_sections(sections, plan)
        ex_fs = [f for u, fs in by_unit.items() if u.startswith("x") or u in ("exercises", "chapter") for f in fs]
        if ex_fs:
            ex = run.call("author", "exercises", head + "Revise the exercises so every finding below is resolved; "
                          "return the full set. Never assess the optional extension box.\nCurrent exercises:\n" +
                          json.dumps(exercises, ensure_ascii=False) + "\nFindings:\n" +
                          json.dumps(ex_fs, ensure_ascii=False), 9000, f"exercises:revise{rnd}")
            exercises = ex.get("exercises") or exercises
        findings = checks()
        review = do_review(f"review:{rnd + 1}")
    review["rounds"] = rounds

    review.setdefault("findings", []).extend(fit["findings"])
    review["ready_for_gate_d"] = is_ready(review, findings)
    chapter_doc = {"chapter": ch, "work": d["work"], "plan": plan, "sections": sections, "exercises": exercises,
                   "checks": findings, "review": review, "costs": run.costs,
                   "videos": sorted({v for t in d["topics"] for v in t["videos"]}),
                   "prompt_versions": {"constitution": CONSTITUTION, **CARDS}}
    (out_dir / f"ch{chapter:02d}-draft.json").write_text(json.dumps(chapter_doc, ensure_ascii=False, indent=1),
                                                          encoding="utf-8")
    store(conn, d, chapter_doc)
    return chapter_doc


def fold_extension_sections(sections: list[dict], plan: dict) -> None:
    """A section made only of an extension box is folded onto the end of the
    section before it and removed (ch 1, 2026-10-25): extension is a glimpse
    inside a section, never a section of its own."""
    i = 1
    while i < len(sections):
        kinds = {b.get("type") for b in sections[i].get("blocks") or []}
        if kinds and kinds <= {"extension", "figure"}:
            moved = [b for b in sections[i]["blocks"] if b.get("type") == "extension"]
            sections[i - 1]["blocks"] = (sections[i - 1].get("blocks") or []) + moved
            gone = sections.pop(i)["sid"]
            plan["sections"] = [p for p in plan["sections"] if p["sid"] != gone]
            continue
        i += 1


def dedupe(sections: list[dict]) -> None:
    """Sections are drafted in parallel, so two can box the same misconception
    or define the same term (ch 1, 2026-10-25). Keep the first occurrence."""
    seen = set()
    for s in sections:
        keep = []
        for b in s.get("blocks") or []:
            if b.get("type") in ("misconception", "key_term", "figure"):
                k = (b["type"], tuple(sorted(r for r in b.get("source_refs") or [] if r.startswith(("misc:", "gloss:")))),
                     b.get("ref"), str(b.get("term") or b.get("wrong") or "").strip().lower())
                if k in seen:
                    continue
                seen.add(k)
            keep.append(b)
        s["blocks"] = keep


FIT_PROMPT = """You are checking a picture for a children's science study guide (ages {age}).
Section: "{heading}". The author's draft caption: "{caption}".
Judge the PICTURE, not the draft caption. Answer with JSON:
{{"shows": "what the picture actually shows, in one sentence",
  "accurate": true|false,   // scientifically correct, nothing misleading for a child
  "relevant": true|false,   // genuinely helps teach this section
  "caption": "an accurate caption (max 25 words) that says only what the picture shows",
  "not_to_scale": true|false, // things of very different real sizes drawn at similar sizes (e.g. a bacterium beside a person)
  "problem": "why it is inaccurate or irrelevant, if it is"}}"""


def figure_fit(run: Run, sections: list[dict], env_file: Path) -> dict:
    """Vision check of every placed figure (§8 ladder step 5). An unfit picture
    is REMOVED from the draft and becomes a visual gap and a finding — a wrong
    picture is worse than no picture."""
    from publishing.manuscript.render import _download
    from publishing.snapshot import client, load_env

    src = {f["id"]: f["_asset"] for t in run.d["topics"] for f in t["figures"]}
    src |= {a["id"]: a["_asset"] for a in run.d["library_assets"]}
    url, key = load_env(env_file)
    gaps, findings, removed = [], [], set()
    with client(url, key) as http:
        for s in sections:
            keep = []
            for b in s.get("blocks") or []:
                if b.get("type") == "figure" and src.get(b.get("ref")):
                    png = _download(http, url, src[b["ref"]]["storage_path"])
                    v = run.look(png, FIT_PROMPT.format(age=run.d["work"]["age_band"], heading=s.get("heading"),
                                                        caption=b.get("caption")), s["sid"])
                    if not v.get("shows"):
                        # the vision call answered nothing: no evidence against the
                        # picture, so it stays — flagged for the person at Gate D
                        findings.append({"unit": s["sid"], "kind": "figure", "severity": "minor",
                                         "quote": b.get("caption"), "detail": f"{b['ref']}: fit check unanswered"})
                        keep.append(b)
                        continue
                    if v.get("accurate") is True and v.get("relevant") is True and v.get("caption"):
                        b["caption"] = v["caption"]           # say only what the picture shows
                        if v.get("not_to_scale") is True and "not to scale" not in b["caption"].lower():
                            b["caption"] = b["caption"].rstrip(". ") + " (not to scale)."
                    if not (v.get("accurate") is True and v.get("relevant") is True):
                        gaps.append({"need": b.get("caption"), "why": f"{b['ref']} removed: shows {v.get('shows')!r}; "
                                     f"{v.get('problem') or 'did not pass the fit check'}"})
                        findings.append({"unit": s["sid"], "kind": "figure", "severity": "major",
                                         "quote": b.get("caption"), "detail": f"removed {b['ref']}: {v.get('shows')}"})
                        removed.add(b["ref"])
                        continue
                keep.append(b)
            s["blocks"] = keep
    return {"gaps": gaps, "findings": findings, "removed": removed}


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
