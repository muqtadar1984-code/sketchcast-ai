"""Draft ONE chapter (Phase 5 vertical slice; the same code drafts every chapter
of every book in Phase 7 — no subject branches).

    dossier → chapter_plan (page target + new pictures) → book illustrations
            → draft_section ×N (parallel, ONE pass at the page target) → exercises
            → deterministic checks (incl. cross-chapter repeats)
            → independent review + gates in ONE call (other model family)
            → ≤2 revise rounds, each re-checking only the units it changed
            → stored as unit revisions with source_refs, costs, findings

Book cost plan (founder 2026-10-26, target $25-30 a book at the same quality):
the separate expand rewrite is gone for new drafts (expand_chapter stays for
books mid-flight), review and gates share one reviewer call, revise rounds
re-check only what changed, and each chapter is told what earlier chapters
already use so the uniqueness pass is a check, not a rewrite.

Roles (D5): publish_author = Claude on Bedrock, publish_reviewer = Gemini on
Vertex. The dossier is the cached prefix of every author call.
"""
from __future__ import annotations

import copy
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
CARDS = {"plan": "card_chapter_plan_v0_2", "section": "card_draft_section_v0_2",
         "exercises": "card_exercises_v0_1", "review": "card_review_v0_1", "gates": "card_gates_v0_1",
         "review_gates": "card_review_gates_v0_1",
         "expand_plan": "card_expand_plan_v0_1", "expand": "card_expand_section_v0_1"}
AUTHOR, REVIEWER = "anthropic", "gemini"
# one-pass drafting (cost plan change 1): the page target a NEW draft is written
# to, the pictures it may commission, and the words that count as "met" — the
# batch driver's old "already near 6 pages" line, so a met draft skips expand
PAGES, PICTURES = "6 to 8", 3
WORDS_PER_PAGE = 330                     # ~330 words fill a teaching page with its pictures
ONE_PASS_MIN_WORDS = 1700
# output caps: a longer one-pass section and the merged review+gates reply get
# room enough never to hit the cap (a capped reply fails the chapter)
SECTION_TOKENS, REVIEW_TOKENS = 9000, 32000
ASSETS = ROOT.parent.parent / "study-guide-sprint1" / "book-assets"


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
        for wait in (20, 45, 90, 0):
            try:
                reply = self.reviewer.analyze_image(png, prompt, max_tokens=800)
                break
            except Exception as e:  # noqa: BLE001 — a shared-pool 429 is capacity, not a verdict; wait it out
                if "429" not in str(e) and "RESOURCE_EXHAUSTED" not in str(e) or not wait:
                    raise
                log.warning("reviewer rate-limited on %s; retrying in %ss", unit, wait)
                time.sleep(wait)
        u = reply.get("usage") or {}
        self.costs.append({"stage": "figure_fit", "unit": unit, "provider": REVIEWER,
                           "model": getattr(self.reviewer, "model", "?"), "in": u.get("input_tokens"),
                           "out": u.get("output_tokens"), "usd": u.get("estimated_cost_usd") or 0,
                           "secs": round(time.time() - t0, 1)})
        data = reply.get("data") or {}
        if isinstance(data, list):        # a verdict wrapped in a list is still one verdict
            data = next((x for x in data if isinstance(x, dict)), {})
        return data

    def call(self, role: str, card: str, task: str, max_tokens: int, unit: str) -> dict:
        cli = self.reviewer if role == "reviewer" else self.author
        t0 = time.time()
        reply = cli.analyze(task, system=_system(card), max_tokens=max_tokens, cache_prefix=self.prefix)
        if reply.get("truncated") and card in ("review", "gates", "review_gates"):
            # a long chapter's review can run on; ask once more for the serious findings only
            reply = cli.analyze(task + "\n\nKeep the reply compact: at most 25 findings, the most serious first, "
                                "each detail in one or two sentences.", system=_system(card),
                                max_tokens=max_tokens, cache_prefix=self.prefix)
        if reply.get("truncated"):
            raise RuntimeError(f"{card} for {unit}: reply hit the output cap ({max_tokens})")
        u = reply.get("usage") or {}
        self.costs.append({"stage": card, "unit": unit, "provider": REVIEWER if role == "reviewer" else AUTHOR,
                           "model": getattr(cli, "model", "?"), "in": u.get("input_tokens"),
                           "out": u.get("output_tokens"), "usd": u.get("estimated_cost_usd") or 0,
                           "secs": round(time.time() - t0, 1)})
        data = reply["data"]
        if isinstance(data, list):        # a reply wrapped in a list is still one reply
            data = next((x for x in data if isinstance(x, dict)), data)
        if not isinstance(data, dict) or data.get("error"):
            raise RuntimeError(f"{card} for {unit}: model returned {str(data)[:300]}")
        return data


def teaching_words(sections: list[dict]) -> int:
    """Words of teaching text as the batch driver has always counted them."""
    return sum(len(str(b.get(k, "")).split()) for s in sections for b in s.get("blocks") or []
               for k in ("text", "wrong", "right", "caption"))


def page_target(pages: str) -> str:
    """'6 to 8' → the words that fill those pages; any other wording is passed on as it is."""
    nums = [int(t) for t in str(pages).replace("-", " ").split() if t.isdigit()]
    if len(nums) == 2:
        return (f"Page target: about {pages} printed pages of teaching, i.e. about {nums[0] * WORDS_PER_PAGE} to "
                f"{nums[1] * WORDS_PER_PAGE} words of prose across the sections.\n")
    return f"Page target: about {pages} printed pages of teaching (~{WORDS_PER_PAGE} words a page).\n"


def earlier_note(earlier: dict, what: str) -> str:
    """What earlier chapters already use (cost plan change 4), for one kind of call:
    'plan' and 'section' get the pictures (a section also the Think questions),
    'exercises' the pictures and every earlier question."""
    if not earlier or not (earlier.get("pictures") or earlier.get("questions")):
        return ""
    out = ""
    if earlier.get("pictures"):
        out += ("Pictures already used in earlier chapters (every chapter has its own: never place these): " +
                json.dumps(sorted(earlier["pictures"])) + "\n")
    qs = earlier.get("questions") or []
    if what == "section":
        thinks = [x.get("text") for m, xid, _, x in qs if str(xid).startswith("think:")]
        if thinks:
            out += "Think questions earlier chapters already asked (ask none of these again): " + \
                json.dumps(thinks, ensure_ascii=False)[:8000] + "\n"
    if what == "exercises" and qs:
        out += ("Questions earlier chapters already asked (repeat none: test this chapter's ideas with a different "
                "context, example, organism, situation or question form):\n" +
                json.dumps([{"chapter": m, "stem": x.get("stem") or x.get("text"), "answer": x.get("answer")}
                            for m, xid, _, x in qs], ensure_ascii=False)[:40000] + "\n")
    return out


def figures_note(d: dict, used: set) -> str:
    """The catalogue figures this chapter's dossier offers, named in the plan request (founder 2026-10-29: a trial
    chapter used none of the two it was offered). Offered, never required: the author places those that teach this
    chapter's ideas and says why in visual_gaps when it uses none."""
    figs = [f for t in d.get("topics") or [] for f in t.get("figures") or [] if f["id"] not in used]
    if not figs:
        return ""
    return ("Catalogue figures offered for this chapter (already drawn and approved; cite the fig: id in a figure "
            "block where one teaches an idea of THIS chapter; if you use none, say why in visual_gaps):\n" +
            "".join(f"  {f['id']}: {f.get('caption') or f.get('key')}\n" for f in figs))


def picture_key(key, chapter: int) -> str:
    """A new illustration's key is the chapter's own: `book:` + name + `_chNN`, so
    it can never overwrite (on conflict) a picture another chapter was approved with."""
    k = str(key or "picture").strip()
    k = k if k.startswith("book:") else "book:" + k
    return k if k.endswith(f"_ch{chapter:02d}") else f"{k}_ch{chapter:02d}"


def make_pictures(conn, slug: str, chapter: int, wanted: list[dict], limit: int, max_busy: int,
                  env_file: Path) -> tuple[list, list]:
    """The plan's new illustrations, through the publisher-grade style system, only
    while production is quiet (at most `max_busy` jobs in flight; the founder's
    standing rule). A picture this draft already made is reused, so a resumed run
    pays nothing twice; a key some other run owns is refused, never overwritten."""
    from publishing.guard import production_queue
    from publishing.manuscript import book_assets
    from publishing.manuscript.theme import for_work
    by = f"founder:draft-ch{chapter:02d}"
    made, refused = [], []
    for pic in (wanted or [])[:max(0, limit)]:
        if not isinstance(pic, dict) or not pic.get("subject"):
            continue
        key = picture_key(pic.get("key"), chapter)
        row = conn.execute("select approved_by from pub.book_assets where key=%s", (key,)).fetchone()
        if row:
            if row["approved_by"] == by:
                made.append({**pic, "key": key, "reused": True})
            else:
                refused.append({**pic, "key": key, "error": f"key already belongs to {row['approved_by']}"})
            continue
        q = production_queue(env_file)
        if q["busy"] > max_busy:
            raise RuntimeError(f"production has {q['busy']} queued/processing job(s) {q['types']}; "
                               "not generating images")
        got = book_assets.generate(conn, key, pic.get("teaches") or pic["subject"], pic["subject"], ASSETS, by,
                                   theme=for_work(slug))
        (refused if got.get("error") else made).append({**pic, **got, "key": key})
    return made, refused


# ── merged review + gates (cost plan change 2) ────────────────────────────────
GATE_KEYS = ("coverage", "beyond_scope", "assessment", "coverage_gate", "assessment_gate")
BLIND_KEYS = ("xid", "tier", "item_type", "stem", "options", "pairs", "categories", "figure_ref", "figure_refs",
              "figure_context")


def blind_view(exercises: list[dict]) -> list[dict]:
    """Everything the learner sees, nothing of the key: a sort task's items and
    categories are printed, so the reviewer must see them too."""
    out = []
    for x in exercises:
        b = {k: v for k, v in x.items() if k in BLIND_KEYS}
        if x.get("items"):
            b["items"] = [i.get("text") if isinstance(i, dict) else i for i in x["items"]]
        out.append(b)
    return out


def answer_key(exercises: list[dict]) -> list[dict]:
    """What the gates need beyond the learner's view: answers, solutions, schemes
    (and a sort task's categories per item)."""
    return [{"xid": x.get("xid"), **{k: v for k, v in x.items() if k not in BLIND_KEYS}} for x in exercises]


def review_gates_task(opening, sections: list[dict], exercises: list[dict], changed: dict | None = None,
                      prior: dict | None = None) -> str:
    """The reviewer's task: the learner's view first, the answer key LAST, so the
    blind re-solve is written before the key is consulted. After a revise round
    (`changed`), the revised units are judged in full and the rest is context."""
    dump = lambda o: json.dumps(o, ensure_ascii=False, indent=1)  # noqa: E731
    if changed is None:
        return ("Drafted chapter (the learner's view; exercises WITHOUT answers):\n" +
                dump({"opening": opening, "sections": sections, "exercises": blind_view(exercises)}) +
                "\n\nANSWER KEY (for Gate 2; write blind_answers before consulting it):\n" + dump(answer_key(exercises)))
    cs, cx = changed["sections"], changed["exercises"]
    rev_x = [x for x in exercises if x.get("xid") in cx]
    return ("RE-CHECK after a revision round. Judge the REVISED units in full and the chapter as a whole for "
            "coverage and chapter-wide problems; the unchanged units were reviewed already and are context only.\n"
            "Revised sections:\n" + dump([s for s in sections if s.get("sid") in cs]) +
            "\nRevised exercises (the learner's view):\n" + dump(blind_view(rev_x)) +
            "\nThe earlier findings these revisions had to resolve (say in a finding if one is still unresolved):\n" +
            dump(prior or {}) +
            "\nUnchanged units (context only):\n" +
            dump({"opening": opening, "sections": [s for s in sections if s.get("sid") not in cs],
                  "exercises": blind_view([x for x in exercises if x.get("xid") not in cx])}) +
            "\n\nANSWER KEY of the revised exercises (for Gate 2; write blind_answers before consulting it):\n" +
            dump(answer_key(rev_x)))


def split_review(data) -> tuple[dict, dict]:
    """One reviewer reply → (review, gates) in the shapes the separate calls had,
    so everything downstream (render, packet, importer) reads them unchanged.
    Takes the gates flat or nested under "gates", and a list-wrapped reply."""
    if isinstance(data, list):
        data = next((x for x in data if isinstance(x, dict)), {})
    data = dict(data or {})
    nested = data.pop("gates", None)
    if isinstance(data.get("review"), dict):
        data = {**data.pop("review"), **data}
    gates = dict(nested) if isinstance(nested, dict) else {}
    for k in GATE_KEYS:
        if k in data:
            gates.setdefault(k, data.pop(k))
    if not isinstance(data.get("findings"), list):
        data["findings"] = []
    return data, gates


def _unit(f: dict) -> str:
    return str(f.get("unit") or f.get("xid") or "").split(".")[0]


def changed_units(old_sections: list[dict], sections: list[dict], old_exercises: list[dict],
                  exercises: list[dict]) -> dict:
    """The sections and exercises a revise round actually changed (by content)."""
    def by(items, key):
        return {str(i.get(key)): json.dumps(i, sort_keys=True, ensure_ascii=False) for i in items}
    os_, ns = by(old_sections, "sid"), by(sections, "sid")
    ox, nx = by(old_exercises, "xid"), by(exercises, "xid")
    return {"sections": {k for k, v in ns.items() if os_.get(k) != v},
            "exercises": {k for k, v in nx.items() if ox.get(k) != v}}


def merge_visual(prev: dict, new: dict, changed: set, alive: set) -> dict:
    """A visual-gate result after a re-check: the new verdicts for the changed
    units, the earlier ones for every unit that did not change."""
    out = {k: [v for v in prev.get(k) or [] if _unit(v) in alive and _unit(v) not in changed] + list(new.get(k) or [])
           for k in ("verdicts", "findings", "human_review")}
    if "removed" in prev or "removed" in new:
        out["removed"] = set(prev.get("removed") or ()) | set(new.get("removed") or ())
        out["gaps"] = list(prev.get("gaps") or []) + [g for g in new.get("gaps") or [] if g not in (prev.get("gaps") or [])]
    return out


def merge_recheck(prev_review: dict, prev_gates: dict, review: dict, gates: dict, changed: dict,
                  xids: list[str], alive: set) -> tuple[dict, dict]:
    """A re-check judged only the changed units (plus chapter-wide coverage):
    findings, blind answers and assessment verdicts of unchanged units carry
    forward; a fresh verdict on any unit wins over an old one."""
    ch = changed["sections"] | changed["exercises"]
    seen, findings = set(), []
    for f in list(review.get("findings") or []) + [f for f in prev_review.get("findings") or []
                                                   if _unit(f) in alive and _unit(f) not in ch]:
        k = (_unit(f), str(f.get("detail")))
        if k not in seen:
            seen.add(k)
            findings.append(f)

    def per_xid(old, new):
        o = {str(a.get("xid")): a for a in old or [] if isinstance(a, dict) and str(a.get("xid")) not in ch}
        n = {str(a.get("xid")): a for a in new or [] if isinstance(a, dict)}
        return [n.get(x) or o[x] for x in xids if x in n or x in o]

    out_r = {**prev_review, **{k: v for k, v in review.items() if k not in ("findings", "blind_answers")},
             "findings": findings, "blind_answers": per_xid(prev_review.get("blind_answers"), review.get("blind_answers")),
             "recheck": sorted(ch)}
    out_g = {**prev_gates, **{k: v for k, v in gates.items() if k != "assessment"},
             "assessment": per_xid(prev_gates.get("assessment"), gates.get("assessment"))}
    out_g["assessment_gate"] = "fail" if gates.get("assessment_gate") == "fail" or any(
        a.get("verdict") == "fail" for a in out_g["assessment"]) else "pass"
    return out_r, out_g


EMPTY_FIT = {"gaps": [], "findings": [], "removed": set(), "verdicts": [], "human_review": []}
EMPTY_VEX = {"findings": [], "human_review": [], "verdicts": []}


def _write_dossier(out_dir: Path, chapter: int, d: dict) -> None:
    (out_dir / f"ch{chapter:02d}-dossier.json").write_text(D.for_prompt(d), encoding="utf-8")
    (out_dir / f"ch{chapter:02d}-dossier-full.json").write_text(json.dumps(d, ensure_ascii=False, default=str),
                                                                encoding="utf-8")


def draft_chapter(conn, slug: str, chapter: int, env_file: Path, out_dir: Path, pages: str | None = PAGES,
                  pictures: int = PICTURES, max_busy: int = 0) -> dict:
    """Draft one chapter at its page target in ONE pass (founder 2026-10-26).
    `pages=None, pictures=0` drafts the short first version the expand stage
    used to grow; the record says which (`one_pass`)."""
    from publishing.tools.uniqueness import earlier_chapters
    _load_model_env(env_file)
    d = D.build(conn, slug, chapter, env_file)
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_dossier(out_dir, chapter, d)
    # what the chapters before this one already use: told to every author call,
    # and a repeat is a failed check the revise rounds fix (cost plan change 4)
    earlier = earlier_chapters(out_dir, chapter)
    used = earlier["pictures"]
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
    skills = {o["code"] for o in ch.get("skills") or []}
    nodes = set(ch["node_codes"])
    # a checkpoint after each costly stage: a busy production queue or a rate
    # limit must not throw away the plan, the pictures or the drafted sections
    work_file = out_dir / f"ch{chapter:02d}-draft-work.json"
    work = json.loads(work_file.read_text(encoding="utf-8")) if work_file.exists() else {}

    def save():
        work_file.write_text(json.dumps(work, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    if "plan" not in work:
        work["plan"] = run.call(
            "author", "plan", head + (page_target(pages) if pages else "") +
            (f"Picture budget: up to {pictures} new colour illustrations.\n" if pictures else "") +
            figures_note(d, used) + earlier_note(earlier, "plan") + "Plan this chapter. Blueprint intent:\n" +
            json.dumps({k: ch[k] for k in ("purpose", "blueprint_sections", "assessment", "illustration_needs")},
                       ensure_ascii=False, indent=1), 9000, "plan")
        save()
    plan = copy.deepcopy(work["plan"])
    if not pictures:
        plan["new_pictures"] = []
    if "made" not in work:
        work["made"], work["refused"] = (make_pictures(conn, slug, chapter, plan.get("new_pictures") or [], pictures,
                                                       max_busy, env_file) if pictures else ([], []))
        save()
    made, refused = work["made"], work["refused"]
    plan.setdefault("visual_gaps", []).extend({"need": p.get("subject"), "why": f"illustration refused: "
                                               f"{str(p.get('error'))[:200]}"} for p in refused)
    if made:                               # the dossier now offers the new pictures
        d = D.build(conn, slug, chapter, env_file)
        run.d, run.prefix = d, "CHAPTER DOSSIER (the complete content boundary):\n" + D.for_prompt(d)
        _write_dossier(out_dir, chapter, d)
    valid = D.ids(d)
    figure_ids = {f["id"] for t in d["topics"] for f in t["figures"]} | {a["id"] for a in d["library_assets"]}
    # an earlier chapter's photographs are not this chapter's to use in an observation task
    photo_ids = {a["id"] for a in d["library_assets"] if a.get("kind") == "photo"} - used

    def one(sec):
        others = [s["heading"] for s in plan["sections"] if s["sid"] != sec["sid"]]
        pics = [{k: p.get(k) for k in ("key", "subject", "teaches", "label")} for p in made if p.get("sid") == sec["sid"]]
        return run.call("author", "section", head + f"Opening of the chapter: {plan.get('opening')}\n"
                        f"Other sections (do not repeat them): {others}\n" +
                        (f"Target for this section: about {sec['target_words']} words of prose.\n"
                         if sec.get("target_words") else "") +
                        ("New pictures made for this section (place each):\n" + json.dumps(pics, ensure_ascii=False) +
                         "\n" if pics else "") +
                        earlier_note(earlier, "section") + "Write this section:\n" +
                        json.dumps(sec, ensure_ascii=False, indent=1), SECTION_TOKENS, sec["sid"])

    if "sections" not in work:
        with ThreadPoolExecutor(max_workers=4) as pool:
            work["sections"] = list(pool.map(one, plan["sections"]))
        save()
    sections = copy.deepcopy(work["sections"])
    dedupe(sections)
    fit = figure_fit(run, sections, env_file)
    plan.setdefault("visual_gaps", []).extend(fit["gaps"])
    figure_ids -= fit["removed"]          # an exercise may not use a picture the fit check threw out
    for s in plan["sections"]:
        s["figure_refs"] = [r for r in s.get("figure_refs") or [] if r not in fit["removed"]]
    ex = run.call("author", "exercises", head + earlier_note(earlier, "exercises") + "Chapter plan:\n" +
                  json.dumps(plan, ensure_ascii=False, indent=1) +
                  "\nSection headings: " + json.dumps([s["heading"] for s in sections]), 9000, "exercises")
    exercises = ex.get("exercises") or []

    def checks():
        return (V.check_sections(sections, valid, figure_ids)
                + V.check_picture_reuse(sections)
                + V.check_exercises(exercises, valid, figure_ids, nodes, photo_ids, skills)
                + V.check_extension(sections, exercises)
                + V.check_coverage(sections, exercises, plan, nodes)
                + V.check_cross_chapter(sections, exercises, used, earlier["questions"]))

    state: dict = {}

    def do_review(unit, fig=None, changed=None, prior=None):
        """Three independent gates (founder 2026-10-25, D1): CONTENT, VISUAL,
        ASSESSMENT. The visual gate looks at the actual pictures, figures and
        question pictures alike, and flags what it cannot judge with confidence.
        Content and assessment come from ONE reviewer call with the review (cost
        plan change 2). After a revise round (`changed`), only the changed units
        are looked at and re-judged (change 3); the rest carry their verdicts."""
        nonlocal figure_ids
        alive = {s["sid"] for s in sections} | {x.get("xid") for x in exercises}
        if changed is None:
            if fig is None:
                fig = figure_fit(run, sections, env_file)
                figure_ids -= fig["removed"]
            vex = visual_exercises(run, exercises, env_file)
            model, gates = split_review(run.call("reviewer", "review_gates", head + review_gates_task(
                plan.get("opening"), sections, exercises), REVIEW_TOKENS, unit))
        else:
            cs, cx = changed["sections"], changed["exercises"]
            new_fig = figure_fit(run, [s for s in sections if s["sid"] in cs], env_file) if cs else EMPTY_FIT
            figure_ids -= new_fig["removed"]
            fig = merge_visual(state["fig"], new_fig, cs, alive)
            vex = merge_visual(state["vex"], visual_exercises(run, [x for x in exercises if x.get("xid") in cx],
                                                              env_file) if cx else EMPTY_VEX, cx, alive)
            if cs or cx:
                model, gates = merge_recheck(state["model"], state["gates"], *split_review(run.call(
                    "reviewer", "review_gates", head + review_gates_task(plan.get("opening"), sections, exercises,
                                                                         changed, prior), REVIEW_TOKENS, unit)),
                    changed, [x.get("xid") for x in exercises], alive)
            else:                          # nothing changed: the last verdicts stand, no model call
                model, gates = copy.deepcopy(state["model"]), copy.deepcopy(state["gates"])
                model["recheck"] = []
        state.update(model=copy.deepcopy(model), gates=copy.deepcopy(gates), fig=fig, vex=vex)
        r = model
        r["gates"] = gates
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
    avoid = earlier_note({"pictures": used}, "plan")
    # REVISE (§4 review → revise), at most twice: the author resolves every
    # blocker and major finding, failed gate item and failed check; what the
    # round changed is then checked, reviewed and gated again. What is left goes to Gate D.
    for rnd in (1, 2):
        if is_ready(review, findings):
            break
        by_unit = to_fix(review, findings)
        if not by_unit:
            break
        rounds.append({"round": rnd, "findings": review.get("findings"), "gates": review.get("gates"),
                       "checks": findings})
        before_s, before_x = copy.deepcopy(sections), copy.deepcopy(exercises)

        def fix(sec, by_unit=by_unit, rnd=rnd):
            fs = by_unit.get(sec["sid"])
            if not fs:
                return sec
            spec = next((p for p in plan["sections"] if p["sid"] == sec["sid"]), {})
            return run.call("author", "section", head + avoid + "Revise this section so that every finding below is "
                            "resolved, staying inside the dossier. Keep what is right; change only what the findings "
                            "require; keep every figure block you still need.\nPlan entry:\n" +
                            json.dumps(spec, ensure_ascii=False) + "\nCurrent draft:\n" +
                            json.dumps(sec, ensure_ascii=False) + "\nFindings:\n" + json.dumps(fs, ensure_ascii=False),
                            SECTION_TOKENS, f"{sec['sid']}:revise{rnd}")
        with ThreadPoolExecutor(max_workers=4) as pool:
            sections = list(pool.map(fix, sections))
        dedupe(sections)
        fold_extension_sections(sections, plan)
        ex_fs = [f for u, fs in by_unit.items() if u.startswith("x") or u in ("exercises", "chapter") for f in fs]
        if ex_fs:
            ex = run.call("author", "exercises", head + avoid + "Revise the exercises so every finding below is "
                          "resolved; return the full set. Never assess the optional extension box.\nCurrent exercises:\n" +
                          json.dumps(exercises, ensure_ascii=False) + "\nFindings:\n" +
                          json.dumps(ex_fs, ensure_ascii=False), 9000, f"exercises:revise{rnd}")
            exercises = ex.get("exercises") or exercises
        changed = changed_units(before_s, sections, before_x, exercises)
        findings = checks()
        review = do_review(f"review:{rnd + 1}", changed=changed,
                           prior={u: fs for u, fs in by_unit.items() if u in changed["sections"] | changed["exercises"]})
    review["rounds"] = rounds
    review["ready_for_gate_d"] = is_ready(review, findings)
    words = teaching_words(sections)
    chapter_doc = {"chapter": ch, "work": d["work"], "plan": plan, "sections": sections, "exercises": exercises,
                   "checks": findings, "review": review, "costs": run.costs,
                   "videos": sorted({v for t in d["topics"] for v in t["videos"]}),
                   "prompt_versions": {"constitution": CONSTITUTION, **CARDS},
                   # what the batch driver reads to skip the expand stage (cost plan change 1)
                   "one_pass": {"pages": pages, "words": words,
                                "target_words": sum(int(s.get("target_words") or 0) for s in plan["sections"]) or None,
                                "met": bool(pages) and words >= ONE_PASS_MIN_WORDS,
                                "pictures_made": made, "pictures_refused": refused,
                                "earlier_pictures_avoided": len(used), "earlier_questions_checked": len(earlier["questions"]),
                                "usd": round(sum(c["usd"] or 0 for c in run.costs), 2)}}
    (out_dir / f"ch{chapter:02d}-draft.json").write_text(json.dumps(chapter_doc, ensure_ascii=False, indent=1),
                                                          encoding="utf-8")
    work_file.unlink(missing_ok=True)     # the draft holds everything now; a redraft starts fresh
    store(conn, d, chapter_doc)
    return chapter_doc


def expand_chapter(conn, slug: str, chapter: int, env_file: Path, out_dir: Path, pages: str = "6 to 8",
                   pictures: int = 3, max_busy: int = 0) -> dict:
    """Teach MORE in an APPROVED chapter (founder 2026-10-25: "expand the theory
    to 6-8 pages, with 2 or 3 more images"). The approved blocks are LOCKED —
    `check_preserved` fails any change to them — and the approved exercises are
    kept as they are. New text and new book illustrations go through the same
    three gates as a first draft, with up to two revise rounds. Pictures are
    made only while production has at most `max_busy` jobs in flight (0 by
    default; the founder may allow the free share of the catalogue slots)."""
    from publishing.guard import production_queue
    from publishing.manuscript import book_assets
    from publishing.manuscript.theme import for_work
    _load_model_env(env_file)
    src = out_dir / f"ch{chapter:02d}-draft.json"
    approved = json.loads(src.read_text(encoding="utf-8"))
    keep = out_dir / f"ch{chapter:02d}-draft.approved-v1.json"
    if not keep.exists():
        keep.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    old_sections, exercises, plan = approved["sections"], approved["exercises"], approved["plan"]
    d = D.build(conn, slug, chapter, env_file)
    run = Run(d)
    ch = d["chapter"]
    head = (f"Book: {d['work']['title']} — ages {d['work']['age_band']}, {d['work']['curriculum']}.\n"
            f"Chapter {ch['number']}: {ch['title']}. Objectives (the scope):\n" +
            "".join(f"  {o['code']}: {o['text']}\n" for o in ch.get("objectives") or []) +
            "The whole book (teach ONLY this chapter's ideas):\n" + "".join(
                f"  {c['sort']}. {c['title']}\n" for c in conn.execute(
                    "select sort, title from pub.blueprint_chapters where blueprint_id=(select id from pub.blueprints "
                    "where work_uid=(select uid from pub.works where slug=%s) order by revision desc limit 1) "
                    "order by sort", (slug,)).fetchall()))
    notes = out_dir / f"ch{chapter:02d}-editorial-notes.md"
    if notes.exists():
        head += "EDITORIAL DIRECTION approved at Gate D (follow it):\n" + notes.read_text(encoding="utf-8") + "\n"
    # a checkpoint after each costly stage: a rate limit late in the run must not
    # throw away the plan, the pictures or the drafted sections
    work_file = out_dir / f"ch{chapter:02d}-expand-work.json"
    work = json.loads(work_file.read_text(encoding="utf-8")) if work_file.exists() else {}

    def save():
        work_file.write_text(json.dumps(work, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    by = f"founder:expand-ch{chapter:02d}"
    ready = [dict(r) for r in conn.execute("select key, concept, description from pub.book_assets where approved_by=%s",
                                           (by,)).fetchall()]
    if "xplan" not in work:
        work["xplan"] = run.call(
            "author", "expand_plan", head + f"Target: about {pages} printed pages of teaching; {pictures} new "
            "pictures.\nPictures ALREADY MADE for this expansion (reuse a key when it fits; it counts toward the "
            "number):\n" + json.dumps(ready, ensure_ascii=False) + "\nApproved chapter:\n" +
            json.dumps({"opening": plan.get("opening"), "sections": old_sections}, ensure_ascii=False),
            6000, "expand_plan")
        save()
    xplan = work["xplan"]
    # new illustrations: through the publisher-grade style system, only while production is quiet
    if "made" not in work:
        made, refused = [], []
        for pic in (xplan.get("new_pictures") or [])[:pictures]:
            key = pic["key"] if str(pic.get("key", "")).startswith("book:") else "book:" + str(pic.get("key"))
            if any(r["key"] == key for r in ready):
                made.append({**pic, "key": key, "reused": True})
                continue
            q = production_queue(env_file)
            if q["busy"] > max_busy:
                raise RuntimeError(f"production has {q['busy']} queued/processing job(s) {q['types']}; "
                                   "not generating images")
            got = book_assets.generate(conn, key, pic.get("teaches") or pic["subject"], pic["subject"],
                                       ROOT.parent.parent / "study-guide-sprint1" / "book-assets", by,
                                       theme=for_work(slug))
            (refused if got.get("error") else made).append({**pic, **got, "key": key})
        work.update(made=made, refused=refused)
        save()
    made, refused = work["made"], work["refused"]
    if made:                               # the dossier now offers the new pictures
        d = D.build(conn, slug, chapter, env_file)
        run.d, run.prefix = d, "CHAPTER DOSSIER (the complete content boundary):\n" + D.for_prompt(d)
    valid = D.ids(d)
    figure_ids = {f["id"] for t in d["topics"] for f in t["figures"]} | {a["id"] for a in d["library_assets"]}
    adds = {s["sid"]: s for s in xplan.get("sections") or []}

    def one(sec):
        pics = [{k: p.get(k) for k in ("key", "subject", "teaches", "label")} for p in made if p.get("sid") == sec["sid"]]
        return run.call("author", "expand", head + "Approved section (every block LOCKED):\n" +
                        json.dumps(sec, ensure_ascii=False) + "\nPlanned additions:\n" +
                        json.dumps(adds.get(sec["sid"]) or {}, ensure_ascii=False) + "\nNew pictures to place:\n" +
                        json.dumps(pics, ensure_ascii=False), 9000, sec["sid"])

    if "sections" not in work:
        with ThreadPoolExecutor(max_workers=4) as pool:
            work["sections"] = list(pool.map(one, old_sections))
        save()
    sections = V.restore_locked(old_sections, work["sections"])
    for sec in sections:                   # a picture block names its picture in `ref`, whatever key the model used
        for blk in sec.get("blocks") or []:
            if blk.get("type") == "figure" and not blk.get("ref"):
                blk["ref"] = next((blk[k] for k in ("asset", "asset_id", "fig", "figure", "id", "key", "src") if str(blk.get(k, "")).startswith(
                    ("book:", "asset:", "fig:"))), None)
    nodes, photo_ids = set(ch["node_codes"]), {a["id"] for a in d["library_assets"] if a.get("kind") == "photo"}
    skills = {o["code"] for o in ch.get("skills") or []}

    def checks():
        return (V.check_preserved(old_sections, sections) + V.check_sections(sections, valid, figure_ids)
                + V.check_picture_reuse(sections)
                + V.check_exercises(exercises, valid, figure_ids, nodes, photo_ids, skills)
                + V.check_extension(sections, exercises) + V.check_coverage(sections, exercises, plan, nodes))

    def review(unit):
        fig = figure_fit(run, sections, env_file)
        vex = visual_exercises(run, exercises, env_file)
        r = run.call("reviewer", "review", head + "Chapter (the publisher approved the earlier text; review the "
                     "WHOLE chapter as a learner will read it):\n" + json.dumps(
                         {"opening": plan.get("opening"), "sections": sections}, ensure_ascii=False), 24000, unit)
        r["gates"] = run.call("reviewer", "gates", head + "Chapter to gate:\n" + json.dumps(
            {"sections": sections, "exercises": exercises}, ensure_ascii=False), 9000, f"{unit}:gates")
        fail = [f for f in fig["findings"] if f.get("severity") == "blocker"] + vex["findings"]
        r["gates"]["visual"] = {"figures": fig["verdicts"], "exercises": vex["verdicts"], "removed": sorted(fig["removed"]),
                                "gaps": fig["gaps"], "failures": fail}
        r["gates"]["visual_gate"] = "fail" if fail else "pass"
        r["gates"]["content_gate"] = r["gates"].get("coverage_gate")
        r["human_review"] = fig["human_review"] + vex["human_review"]
        r.setdefault("findings", []).extend(fig["findings"] + vex["findings"])
        return r

    findings, rv, rounds = checks(), review("review"), []
    for rnd in (1, 2):
        g = rv.get("gates") or {}
        serious = [f for f in rv.get("findings") or [] if f.get("severity") in ("blocker", "major")]
        serious += [{"unit": b.get("unit"), "kind": "scope", "severity": "major", "quote": b.get("quote"),
                     "detail": "beyond the objectives: cut the ADDED text that says it"} for b in g.get("beyond_scope") or []]
        serious += [{"unit": f["unit"].split(".")[0], "kind": f"check:{f['check']}", "severity": "blocker",
                     "detail": f["detail"]} for f in findings if f["verdict"] == "fail"]
        by_unit: dict[str, list] = {}
        for f in serious:
            by_unit.setdefault(str(f.get("unit")).split(".")[0], []).append(f)
        if not any(s["sid"] in by_unit for s in sections):
            break
        rounds.append({"round": rnd, "findings": rv.get("findings"), "gates": g, "checks": findings})

        def fix(pair, by_unit=by_unit, rnd=rnd):
            sec, old = pair
            fs = by_unit.get(sec["sid"])
            if not fs:
                return sec
            return run.call("author", "expand", head + "Revise this EXPANDED section so every finding below is "
                            "resolved. Change only ADDED blocks; the approved blocks stay exactly as in the approved "
                            "section.\nApproved section (LOCKED):\n" + json.dumps(old, ensure_ascii=False) +
                            "\nCurrent expansion:\n" + json.dumps(sec, ensure_ascii=False) + "\nFindings:\n" +
                            json.dumps(fs, ensure_ascii=False), 9000, f"{sec['sid']}:revise{rnd}")
        with ThreadPoolExecutor(max_workers=4) as pool:
            sections = V.restore_locked(old_sections, list(pool.map(fix, zip(sections, old_sections))))
        findings, rv = checks(), review(f"review:{rnd + 1}")
    # the visual gate may tidy a caption; an APPROVED caption is locked text, so it wins
    for new, old in zip(sections, old_sections):
        approved_caps = {(b.get("type"), b.get("ref") or json.dumps(b.get("items"), sort_keys=True)): b.get("caption")
                         for b in old.get("blocks") or [] if b.get("type") in ("figure", "figure_group")}
        for b in new.get("blocks") or []:
            k = (b.get("type"), b.get("ref") or json.dumps(b.get("items"), sort_keys=True))
            if k in approved_caps:
                b["caption"] = approved_caps[k]
    sections = V.restore_locked(old_sections, sections)
    findings = checks()
    work.update(final_sections=sections, review=rv, rounds=rounds, checks=findings)
    save()
    g = rv.get("gates") or {}
    rv["rounds"] = rounds
    rv["ready_for_gate_d"] = (g.get("coverage_gate") == "pass" and g.get("assessment_gate") == "pass"
                              and g.get("visual_gate") == "pass" and not any(f["verdict"] == "fail" for f in findings)
                              and not any(f.get("severity") == "blocker" for f in rv.get("findings") or []))
    for s in plan["sections"]:
        new = next((x for x in sections if x["sid"] == s["sid"]), {})
        s["figure_refs"] = sorted({b["ref"] for b in new.get("blocks") or [] if b.get("ref")} |
                                  {i["ref"] for b in new.get("blocks") or [] if b.get("type") == "figure_group"
                                   for i in b.get("items") or [] if isinstance(i, dict) and i.get("ref")})
    doc = {**approved, "work": d["work"], "chapter": ch, "plan": plan, "sections": sections, "checks": findings,
           "review": rv, "costs": approved.get("costs", []) + run.costs,
           "expansion": {"plan": xplan, "pictures_made": made, "pictures_refused": refused,
                         "usd": round(sum(c["usd"] or 0 for c in run.costs), 2)},
           "prompt_versions": {**approved.get("prompt_versions", {}), "expand_plan": CARDS["expand_plan"],
                               "expand": CARDS["expand"]}}
    (out_dir / f"ch{chapter:02d}-dossier-full.json").write_text(json.dumps(d, ensure_ascii=False, default=str),
                                                                encoding="utf-8")
    src.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    store(conn, d, doc)
    return doc


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
  "caption": "an accurate caption (max 25 words) that says only what the picture shows, using only words the text around it uses (name no structure the text does not teach)",
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

        def near(blocks, j):
            """The text a reader sees beside this picture: two blocks either side,
            not the whole section (a section with two photos must not have one
            judged against the other's sentence)."""
            return " ".join(str(x.get("text") or x.get("term") or "") for x in blocks[max(0, j - 2):j + 3]
                            if x.get("type") not in ("figure", "figure_group"))

        def check_group(s, b, context):
            """A figure group is printed as ONE visual, so it is judged as one: its
            pictures composed with their labels, against its caption."""
            import io

            from PIL import Image, ImageDraw
            items = [i for i in b.get("items") or [] if i.get("ref") in src]
            tiles = [Image.open(io.BytesIO(asset_bytes(http, url, src[i["ref"]][0]))).convert("RGB").resize((300, 300))
                     for i in items]
            sheet = Image.new("RGB", (310 * len(tiles) + 10, 350), "white")
            draw = ImageDraw.Draw(sheet)
            for k, (t, i) in enumerate(zip(tiles, items)):
                sheet.paste(t, (10 + k * 310, 10))
                draw.text((10 + k * 310 + 20, 320), str(i.get("label") or ""), fill="black")
            buf = io.BytesIO()
            sheet.save(buf, "PNG")
            rel = "; ".join(f"{items[r['from']].get('label')} → {items[r['to']].get('label')} ({r.get('kind')})"
                            for r in b.get("relations") or [] if r.get("from", 99) < len(items) and r.get("to", 99) < len(items))
            v = run.look(buf.getvalue(), FIT_PROMPT.format(
                age=run.d["work"]["age_band"], heading=s.get("heading"), context=context[:900],
                caption=f"{b.get('caption')} [This is a composed figure: the pictures above, printed together; "
                        f"relations drawn as arrows: {rel or 'none'}]",
                labels=json.dumps([i.get("label") for i in items]),
                identity=json.dumps({i.get("label"): src[i["ref"]][3] or "illustration" for i in items})), s["sid"])
            v["ref"], v["unit"] = "group:" + ",".join(i["ref"] for i in items), s["sid"]
            verdicts.append(v)
            return v

        for s in sections:
            blocks = s.get("blocks") or []
            keep = []
            for j, b in enumerate(blocks):
                context = near(blocks, j)
                if b.get("type") == "figure_group":
                    v = check_group(s, b, context)
                    if not v.get("shows"):
                        human.append({"unit": s["sid"], "ref": "figure group", "why": "the visual gate gave no answer"})
                    elif any(v.get(k) is not True for k in ("accurate", "relevant", "suitable")):
                        findings.append({"unit": s["sid"], "kind": "figure", "severity": "blocker",
                                         "quote": b.get("caption"), "detail": f"figure group: {v.get('problem')}"})
                    elif v.get("text_consistent") is False or v.get("labels_ok") is False:
                        findings.append({"unit": s["sid"], "kind": "figure", "severity": "blocker",
                                         "quote": b.get("caption"),
                                         "detail": f"figure group caption or labels: {v.get('problem')}"})
                    elif float(v.get("confidence") or 0) < CONFIDENT:
                        human.append({"unit": s["sid"], "ref": "figure group", "why": f"low confidence: {v.get('problem')}"})
                    if v.get("not_to_scale") is True and "not to scale" not in str(b.get("caption")).lower():
                        b["caption"] = str(b.get("caption") or "").rstrip(". ") + ". Not to scale."
                    keep.append(b)
                    continue
                refs = [b.get("ref")] if b.get("type") == "figure" and b.get("ref") in src else []
                if not refs:
                    keep.append(b)
                    continue
                bad = []
                for r in refs:
                    labels = []
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
    known = {r["uid"] for r in conn.execute("select uid from pub.units where edition_id=%s", (ed,)).fetchall()}
    for f in doc["review"].get("findings") or []:
        # a reviewer may name a unit loosely ("s3.b2", "chapter", "x5-x7"): file it on the unit if it
        # exists, else on the chapter, never fail the store over a label
        uid = f"{ch['uid']}-{str(f.get('unit') or '').split('.')[0]}"
        conn.execute("insert into pub.validation_results (unit_uid, check_name, verdict, detail) values (%s,%s,%s,%s)",
                     (uid if uid in known else ch["uid"], f"review:{f.get('kind')}",
                      "fail" if f.get("severity") == "blocker" else "warn", Jsonb(f)))
    for c in doc["costs"]:
        conn.execute("insert into pub.cost_events (work_uid, stage, provider, model, input_tokens, output_tokens, usd, "
                     "edition_id, unit_uid) values ((select work_uid from pub.editions where id=%s),%s,%s,%s,%s,%s,%s,%s,%s)",
                     (ed, f"manuscript:{c['stage']}", c["provider"], c["model"], c["in"], c["out"], c["usd"], ed,
                      c["unit"]))
    conn.commit()
