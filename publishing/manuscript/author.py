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
    # the founder's Gate D direction for THIS chapter (data, not code): every
    # author and gate call reads it beside the dossier
    notes = out_dir / f"ch{chapter:02d}-editorial-notes.md"
    if notes.exists():
        head += "EDITORIAL DIRECTION approved by the publisher at Gate D (follow it):\n" + \
            notes.read_text(encoding="utf-8") + "\n"
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

    def do_review(unit, fig=None):
        """Three independent gates (founder 2026-10-25, D1): CONTENT, VISUAL,
        ASSESSMENT. The visual gate looks at the actual pictures, figures and
        question pictures alike, and flags what it cannot judge with confidence."""
        nonlocal figure_ids
        if fig is None:                    # after a revision: the sections may place new pictures
            fig = figure_fit(run, sections, env_file)
            figure_ids -= fig["removed"]
        vex = visual_exercises(run, exercises, env_file)
        r = run.call("reviewer", "review", head + "Drafted chapter:\n" + json.dumps(
            {"opening": plan.get("opening"), "sections": sections, "exercises": blind()}, ensure_ascii=False,
            indent=1), 12000, unit)
        r["gates"] = run.call("reviewer", "gates", head + "Chapter to gate:\n" + json.dumps(
            {"sections": sections, "exercises": exercises}, ensure_ascii=False, indent=1), 9000, f"{unit}:gates")
        visual_fail = [f for f in fig["findings"] if f.get("severity") == "blocker"] + vex["findings"]
        r["gates"]["visual"] = {"figures": fig["verdicts"], "exercises": vex["verdicts"], "removed": sorted(fig["removed"]),
                                "gaps": fig["gaps"], "failures": visual_fail}
        r["gates"]["visual_gate"] = "fail" if visual_fail else "pass"
        if vex["findings"]:
            r["gates"]["assessment_gate"] = "fail"
        r["gates"]["content_gate"] = r["gates"].get("coverage_gate")
        r["human_review"] = fig["human_review"] + vex["human_review"]
        r.setdefault("findings", []).extend(fig["findings"] + vex["findings"])
        plan.setdefault("visual_gaps", []).extend(g for g in fig["gaps"] if g not in plan.get("visual_gaps", []))
        return r

    def is_ready(r, fs):
        g = r.get("gates") or {}
        return (g.get("coverage_gate") == "pass" and g.get("assessment_gate") == "pass"
                and g.get("visual_gate") == "pass"
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
    review = do_review("review", fig=fit)
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


FIT_PROMPT = """You are the VISUAL GATE of an educational publisher (learners aged {age}).
Section: "{heading}". What the text around this picture says: "{context}".
The author's caption: "{caption}". Labels to be drawn on it: {labels}.
The picture's IDENTITY RECORD (from its source, not from us): {identity}.
Judge the PICTURE as it will be printed. Answer with JSON:
{{"shows": "what the picture actually shows, in one sentence",
  "identity_consistent": true|false, // what you see agrees with the identity record (true when there is none and nothing contradicts)
  "accurate": true|false,     // scientifically correct and not misleading for a learner
  "relevant": true|false,     // it genuinely helps teach what the text says here
  "text_consistent": true|false, // the text and caption say nothing about this picture that it does not show
  "suitable": true|false,     // appropriate for the age (no disease/medical imagery unless the text is about it)
  "labels_ok": true|false,    // every label names something visible in the picture
  "caption": "an accurate caption (max 25 words) that says only what the picture shows",
  "not_to_scale": true|false, // things of very different real sizes drawn at similar sizes
  "confidence": 0.0-1.0,      // how sure you are of these judgements
  "problem": "what is wrong, if anything"}}"""

EXERCISE_PROMPT = """You are the VISUAL GATE for one exercise (learners aged {age}). The image shows the
exercise's pictures side by side, labelled {letters}.
Question: "{stem}"
Information the question gives about each picture: {context}
Identity records (from each picture's source): {identity}
Expected answer: "{answer}"
Marking scheme: {scheme}
Decide whether a learner could reach the expected answer from what each picture visibly shows plus the
information given — and nothing else. Answer with JSON:
{{"per_picture": [{{"label": "A", "shows": str, "consistent_with_identity": true|false,
                   "supports_expected": true|false}}],
  "answer_inferable": true|false, "scheme_matches_pictures": true|false,
  "confidence": 0.0-1.0, "problem": "what is wrong, if anything"}}"""

CONFIDENT = 0.7


def _sources(run: "Run") -> dict:
    src = {f["id"]: (f["_asset"], f.get("caption"), [l["label"] for l in f.get("_labels") or []], None)
           for t in run.d["topics"] for f in t["figures"]}
    src |= {a["id"]: (a["_asset"], a.get("description"), [], a.get("identity")) for a in run.d["library_assets"]}
    return src


def figure_fit(run: "Run", sections: list[dict], env_file: Path) -> dict:
    """VISUAL GATE, figures (D1/D2): every placed picture — alone or inside a
    figure group — is checked as printed against its identity record, its
    caption, its labels and the text around it. A wrong picture is removed (a
    visual gap); an uncertain judgement is kept and flagged for human review,
    never passed as certain."""
    from publishing.manuscript.render import asset_bytes
    from publishing.snapshot import client, load_env

    src = _sources(run)
    url, key = load_env(env_file)
    gaps, findings, removed, verdicts, human = [], [], set(), [], []
    with client(url, key) as http:
        def check(s, ref, caption, labels, context):
            asset, _, base_labels, idn = src[ref]
            v = run.look(asset_bytes(http, url, asset), FIT_PROMPT.format(
                age=run.d["work"]["age_band"], heading=s.get("heading"), context=context[:900],
                caption=caption, labels=json.dumps(labels or base_labels), identity=json.dumps(idn or "none")),
                s["sid"])
            v["ref"], v["unit"] = ref, s["sid"]
            verdicts.append(v)
            return v

        for s in sections:
            context = " ".join(str(b.get("text") or "") for b in s.get("blocks") or [])
            keep = []
            for b in s.get("blocks") or []:
                refs = ([b.get("ref")] if b.get("type") == "figure" else
                        [i.get("ref") for i in b.get("items") or []] if b.get("type") == "figure_group" else [])
                refs = [r for r in refs if r in src]
                if not refs:
                    keep.append(b)
                    continue
                bad = []
                for r in refs:
                    labels = [i.get("label") for i in b.get("items") or [] if i.get("ref") == r]
                    v = check(s, r, b.get("caption"), labels, context)
                    if not v.get("shows"):
                        human.append({"unit": s["sid"], "ref": r, "why": "the visual gate gave no answer"})
                        continue
                    hard = [k for k in ("identity_consistent", "accurate", "relevant", "suitable")
                            if v.get(k) is not True]
                    if hard:
                        bad.append((r, v, hard))
                    elif float(v.get("confidence") or 0) < CONFIDENT:
                        human.append({"unit": s["sid"], "ref": r, "why": f"low confidence {v.get('confidence')}: "
                                      f"{v.get('problem') or v.get('shows')}"})
                    if v.get("text_consistent") is False or v.get("labels_ok") is False:
                        findings.append({"unit": s["sid"], "kind": "figure", "severity": "blocker",
                                         "quote": b.get("caption"),
                                         "detail": f"{r}: the text, caption or labels claim what the picture does not "
                                                   f"show — it shows {v.get('shows')!r}. {v.get('problem') or ''}"})
                    if b.get("type") == "figure" and not hard and v.get("caption"):
                        b["caption"] = v["caption"]
                        if v.get("not_to_scale") is True and "not to scale" not in b["caption"].lower():
                            b["caption"] = b["caption"].rstrip(". ") + " (not to scale)."
                    if b.get("type") == "figure_group" and v.get("not_to_scale") is True \
                            and "not to scale" not in str(b.get("caption")).lower():
                        b["caption"] = str(b.get("caption") or "").rstrip(". ") + " Not to scale."
                for r, v, hard in bad:
                    removed.add(r)
                    gaps.append({"need": b.get("caption"), "why": f"{r} removed ({', '.join(hard)} failed): "
                                 f"shows {v.get('shows')!r}; {v.get('problem') or ''}"})
                    findings.append({"unit": s["sid"], "kind": "figure", "severity": "major",
                                     "quote": b.get("caption"), "detail": f"removed {r}: {v.get('shows')}"})
                if b.get("type") == "figure" and bad:
                    continue
                if b.get("type") == "figure_group":
                    b["items"] = [i for i in b.get("items") or [] if i.get("ref") not in removed]
                    if len(b["items"]) < 2:
                        continue
                keep.append(b)
            s["blocks"] = keep
    return {"gaps": gaps, "findings": findings, "removed": removed, "verdicts": verdicts, "human_review": human}


def visual_exercises(run: "Run", exercises: list[dict], env_file: Path) -> dict:
    """VISUAL GATE, questions (D2): the reviewer sees the question's actual
    pictures together with the stem, what the question says about each
    picture, the identity records, the expected answer and the marking scheme."""
    import io

    from PIL import Image, ImageDraw

    from publishing.manuscript.render import asset_bytes
    from publishing.snapshot import client, load_env

    src = _sources(run)
    url, key = load_env(env_file)
    findings, human, verdicts = [], [], []
    with client(url, key) as http:
        for x in exercises:
            refs = [r for r in ([x["figure_ref"]] if x.get("figure_ref") else []) + list(x.get("figure_refs") or [])
                    if r in src]
            if not refs:
                continue
            tiles = [Image.open(io.BytesIO(asset_bytes(http, url, src[r][0]))).convert("RGB") for r in refs]
            h = 420
            tiles = [t.resize((max(1, int(t.width * h / t.height)), h)) for t in tiles]
            sheet = Image.new("RGB", (sum(t.width for t in tiles) + 20 * (len(tiles) + 1), h + 60), "white")
            xpos, draw = 20, ImageDraw.Draw(sheet)
            letters = "ABCDEFGH"[:len(tiles)]
            for t, letter in zip(tiles, letters):
                sheet.paste(t, (xpos, 10))
                draw.text((xpos + t.width // 2, h + 25), letter, fill="black")
                xpos += t.width + 20
            buf = io.BytesIO()
            sheet.save(buf, "PNG")
            idn = {letters[i]: src[r][3] for i, r in enumerate(refs)}
            v = run.look(buf.getvalue(), EXERCISE_PROMPT.format(
                age=run.d["work"]["age_band"], letters=", ".join(letters), stem=x.get("stem"),
                context=json.dumps(x.get("figure_context") or {}), identity=json.dumps(idn),
                answer=x.get("answer"), scheme=json.dumps(x.get("marking_scheme"))), x["xid"])
            v["xid"] = x["xid"]
            verdicts.append(v)
            if not v.get("per_picture") and v.get("answer_inferable") is None:
                human.append({"unit": x["xid"], "why": "the visual gate gave no answer"})
            elif v.get("answer_inferable") is not True or v.get("scheme_matches_pictures") is not True or \
                    any(p.get("supports_expected") is not True or p.get("consistent_with_identity") is not True
                        for p in v.get("per_picture") or []):
                findings.append({"unit": x["xid"], "kind": "assessment", "severity": "blocker",
                                 "detail": "the pictures do not support the expected answer: "
                                           f"{v.get('problem') or json.dumps(v.get('per_picture'))[:400]}"})
            elif float(v.get("confidence") or 0) < CONFIDENT:
                human.append({"unit": x["xid"], "why": f"low confidence {v.get('confidence')}: {v.get('problem')}"})
    return {"findings": findings, "human_review": human, "verdicts": verdicts}


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
