"""Stage B: the catalogue audit (checks B1-B11) and per-book readiness.

Every verdict is computed from explicit snapshot rows, and the ids of those
rows are kept as evidence. Nothing is inferred from "a row exists". The
required languages come from the readiness policy, never from code.
"""
from __future__ import annotations

import logging

from psycopg.types.json import Jsonb

from publishing.catalogue_view import CatalogueView
from publishing.schemas import BANK_ORDER, CheckResult, ReadinessPolicy, TopicAudit

_log = logging.getLogger(__name__)

THUMBNAIL_ONLY = "thumbnail failed"

# First failing stage decides the topic's primary state.
STATE_ORDER = [
    ("missing_topic", {"B1"}),
    ("missing_mapping", {"B2"}),
    ("missing_article", {"B3"}),
    ("needs_improvement", {"B4", "B5"}),
    ("kit_or_publication_incomplete", {"B6", "B7"}),
    ("missing_translation", {"B8", "B9"}),
    ("missing_video_publication", {"B10"}),
    ("blocked", {"B11"}),
]


def _fail(code, lang=None, detail="", sub=None, **ev) -> CheckResult:
    return CheckResult(code=code, language=lang, verdict="fail", detail=detail, sub_status=sub, evidence=ev)


def _pass(code, lang=None, detail="", **ev) -> CheckResult:
    return CheckResult(code=code, language=lang, verdict="pass", detail=detail, evidence=ev)


def _na(code, lang=None, detail="blocked by an earlier check") -> CheckResult:
    return CheckResult(code=code, language=lang, verdict="n/a", detail=detail)


def check_publication(cat: CatalogueView, kit: dict, channel: str, code: str,
                      policy: ReadinessPolicy) -> CheckResult:
    parts = cat.kit_parts(kit)
    pubs = [p for p in cat.pubs_by_kit.get(kit["id"], [])
            if p["channel_language"] == channel and p["superseded_by"] is None]
    missing, warn = [], []
    for part in parts:
        ok = [p for p in pubs if p["part"] == part and p["youtube_video_id"]
              and p["privacy"] == "public" and p["youtube_upload_status"] == "processed"]
        if not ok:
            states = [f"{p['privacy']}/{p['youtube_upload_status']}" for p in pubs if p["part"] == part]
            missing.append(f"part {part}: {'; '.join(states) or 'no publication'}")
            continue
        err = ok[0].get("error")
        if err and not (policy.thumbnail_warning_ok and err.startswith(THUMBNAIL_ONLY)):
            missing.append(f"part {part}: error {err[:80]}")
        elif err:
            warn.append(f"part {part}: thumbnail not set")
    ev = {"kit_id": kit["id"], "publication_ids": [p["id"] for p in pubs],
          "youtube_video_ids": [p["youtube_video_id"] for p in pubs if p["youtube_video_id"]], "parts": parts}
    if missing:
        queued = [q for q in cat.queue if q["kit_id"] == kit["id"] and q["language"] == channel]
        sub = queued[-1]["status"] if queued else "not_queued"
        return _fail(code, channel, "; ".join(missing), sub=sub, **ev)
    if warn:
        return CheckResult(code=code, language=channel, verdict="warn", detail="; ".join(warn), evidence=ev)
    return _pass(code, channel, f"{len(parts)} part(s) public", **ev)


def audit_topic(cat: CatalogueView, req: dict, policy: ReadinessPolicy, profile: str) -> TopicAudit:
    """`req` is a required-topic dict: topic_slug|None, proposed_title, role,
    node_codes, chapter, index."""
    checks: list[CheckResult] = []
    topic = cat.topic_by_slug.get(req["topic_slug"]) if req.get("topic_slug") else None
    title = (topic or {}).get("title") or req.get("proposed_title") or req.get("topic_slug") or "?"
    other_langs = [l for l in policy.languages if l != "en"]

    # B1 topic exists
    if not topic or topic["status"] == "retired":
        checks.append(_fail("B1", detail="topic must be created" if not topic else "topic is retired",
                            sub="proposed" if not topic else "retired"))
        for c in ["B2", "B3", "B4", "B5", "B6", "B7"]:
            checks.append(_na(c))
        for lang in other_langs:
            checks += [_na("B8", lang), _na("B9", lang), _na("B10", lang)]
        checks.append(_na("B11"))
        return _finish(req, None, title, checks)
    tid = topic["id"]
    checks.append(_pass("B1", detail=f"topic status {topic['status']}", topic_id=tid))

    # B2 curriculum mapping for the nodes the blueprint assigns to this topic
    mapped = {cat.node_by_id[m["node_id"]]["code"]: m["coverage"]
              for m in cat.map_by_topic.get(tid, []) if m["node_id"] in cat.node_by_id}
    want = [c for c in req.get("node_codes") or []]
    not_in_seed = [c for c in want if c not in cat.node_by_code]
    unmapped = [c for c in want if c in cat.node_by_code and c not in mapped]
    if not_in_seed or unmapped:
        sub = "node_not_in_seed" if not_in_seed else "unmapped"
        checks.append(_fail("B2", detail=f"not in seed: {not_in_seed}; unmapped: {unmapped}", sub=sub,
                            mapped=mapped, not_in_seed=not_in_seed, unmapped=unmapped))
    elif not want and not mapped:
        checks.append(_fail("B2", detail="topic has no curriculum mapping at all", sub="unmapped"))
    else:
        checks.append(_pass("B2", detail=f"{len(want)} assigned node(s) mapped", mapped=mapped))

    # B3 approved English article
    en_arts = cat.approved_article(tid, "en")
    if len(en_arts) != 1:
        others = sorted({a["status"] for a in cat.articles_by_topic.get((tid, "en"), [])})
        checks.append(_fail("B3", "en", f"{len(en_arts)} approved EN articles; other statuses {others}",
                            sub=(others[-1] if others else "none")))
        art = None
    else:
        art = en_arts[0]
        checks.append(_pass("B3", "en", f"v{art['version']}", article_id=art["id"], version=art["version"]))

    # B4 depth (D14 balanced policy)
    if art:
        problems = []
        wc = art.get("word_count") or 0
        if wc < policy.min_words:
            problems.append(f"{wc} words < {policy.min_words}")
        wex = len(art.get("worked_examples") or [])
        need = policy.min_worked_examples.get(profile, 1)
        if wex < need:
            problems.append(f"{wex} worked examples < {need}")
        if not (art.get("glossary") or []):
            problems.append("no glossary")
        covers = {c for s in (art.get("sections") or []) for c in (s.get("covers") or [])}
        uncovered = [c for c in want if c in cat.node_by_code and covers and c not in covers]
        if uncovered:
            problems.append(f"sections do not cover {uncovered}")
        referenced = {k for s in (art.get("sections") or []) for k in (s.get("figure_keys") or [])}
        figs = {f["figure_key"]: f for f in cat.figures_by_article.get(art["id"], [])}
        unrendered = sorted(k for k in referenced if k in figs and figs[k]["status"] not in ("rendered", "approved"))
        missing_fig = sorted(k for k in referenced if k not in figs)
        if policy.require_figures_rendered and (unrendered or missing_fig):
            problems.append(f"figures not rendered {unrendered}" + (f", missing {missing_fig}" if missing_fig else ""))
        ev = {"article_id": art["id"], "word_count": wc, "worked_examples": wex,
              "figures_referenced": sorted(referenced), "figures_unrendered": unrendered}
        # Unrendered figures are fixed by a figure_render job on the SAME
        # article version (no new version, no kit/translation cascade). A
        # content shortfall needs a revised article. The planner relies on
        # this distinction.
        only_figures = bool(problems) and all(p.startswith("figures not rendered") for p in problems)
        untagged = bool(art.get("sections")) and not covers
        if problems:
            checks.append(_fail("B4", "en", "; ".join(problems), sub="figures_only" if only_figures else "content", **ev))
        elif untagged:
            # the objectives test above cannot run without tags; a book chapter then draws on the whole article
            # (publishing.manuscript.dossier.scope) — recorded, not blocking (founder 2026-10-29)
            checks.append(CheckResult(code="B4", language="en", verdict="warn", sub_status="sections_untagged",
                                      detail=f"{wc} words, {wex} worked examples; sections carry no curriculum tags, so "
                                             "a chapter uses the whole article", evidence=ev))
        else:
            checks.append(_pass("B4", "en", f"{wc} words, {wex} worked examples", **ev))
    else:
        checks.append(_na("B4", "en"))

    # B5 bank maturity
    bm = topic.get("bank_maturity") or "none"
    items = dict(cat.items_by_topic.get((tid, "en"), {}))
    if BANK_ORDER.index(bm) < BANK_ORDER.index(policy.min_bank_maturity):
        checks.append(_fail("B5", "en", f"bank {bm} < {policy.min_bank_maturity}", sub=bm, items=items))
    else:
        checks.append(_pass("B5", "en", f"bank {bm}", items=items))

    # B6 current EN kit built from the B3 article; B7 its video
    en_kit = cat.current_kit(tid, "en")
    approved_en = [k for k in cat.kits_by_topic.get((tid, "en"), []) if k["status"] == "approved"]
    if not art:
        checks.append(_na("B6", "en"))
        checks.append(_na("B7", "en"))
    elif not en_kit:
        statuses = sorted({k["status"] for k in cat.kits_by_topic.get((tid, "en"), [])})
        sub = "approved_unpublished" if approved_en else (statuses[-1] if statuses else "none")
        checks.append(_fail("B6", "en", f"no current EN kit (kits: {statuses})", sub=sub,
                            approved_kits=[k["id"] for k in approved_en]))
        if approved_en:
            checks.append(check_publication(cat, approved_en[0], "en", "B7", policy))
        else:
            checks.append(_na("B7", "en"))
    elif en_kit["article_id"] != art["id"]:
        checks.append(_fail("B6", "en", "current kit was built from another article version",
                            sub="stale_article", kit_id=en_kit["id"], kit_article_id=en_kit["article_id"]))
        checks.append(check_publication(cat, en_kit, "en", "B7", policy))
    else:
        checks.append(_pass("B6", "en", "current kit from approved article", kit_id=en_kit["id"],
                            approved_en_kits=len(approved_en)))
        checks.append(check_publication(cat, en_kit, "en", "B7", policy))

    # B8-B10 per required translation language
    for lang in other_langs:
        tr_arts = cat.approved_article(tid, lang)
        if not art:
            checks += [_na("B8", lang), _na("B9", lang), _na("B10", lang)]
            continue
        if not tr_arts:
            others = sorted({a["status"] for a in cat.articles_by_topic.get((tid, lang), [])})
            checks.append(_fail("B8", lang, f"no approved {lang} article ({others})", sub=others[-1] if others else "none"))
        elif tr_arts[0]["source_article_id"] != art["id"]:
            checks.append(_fail("B8", lang, "translated from another EN version", sub="stale",
                                article_id=tr_arts[0]["id"], source_article_id=tr_arts[0]["source_article_id"]))
        else:
            checks.append(_pass("B8", lang, f"v{tr_arts[0]['version']}", article_id=tr_arts[0]["id"],
                                version=tr_arts[0]["version"]))
        tr_kit = cat.current_kit(tid, lang)
        all_tr = cat.kits_by_topic.get((tid, lang), [])
        if not tr_kit:
            st = sorted({f"{k['status']}/{k.get('translation_status')}" for k in all_tr})
            checks.append(_fail("B9", lang, f"no current {lang} kit ({st})", sub=st[-1] if st else "none"))
            checks.append(_na("B10", lang))
            continue
        if tr_kit.get("translation_status") not in (None, "approved"):
            checks.append(_fail("B9", lang, f"translation_status {tr_kit['translation_status']}",
                                sub=tr_kit["translation_status"], kit_id=tr_kit["id"]))
        elif en_kit and tr_kit.get("source_kit_id") != en_kit["id"]:
            checks.append(_fail("B9", lang, "translated from a kit that is not the current EN kit",
                                sub="source_kit_mismatch", kit_id=tr_kit["id"],
                                source_kit_id=tr_kit.get("source_kit_id"), current_en_kit=en_kit["id"]))
        else:
            checks.append(_pass("B9", lang, "approved, from current EN kit", kit_id=tr_kit["id"]))
        checks.append(check_publication(cat, tr_kit, lang, "B10", policy))

    # B11 open failures: the LATEST job per (type, language) for this topic
    latest: dict = {}
    for j in sorted(cat.jobs_by_topic.get(tid, []), key=lambda j: str(j["created_at"])):
        lang = (j.get("params") or {}).get("language")
        if lang in (None, *policy.languages):
            latest[(j["type"], lang)] = j
    failing = [f"{t}/{l or '-'}: {j['status']}" for (t, l), j in latest.items() if j["status"] in ("error", "failed")]
    pending = [f"{t}/{l or '-'}: {j['status']}" for (t, l), j in latest.items() if j["status"] in ("queued", "processing")]
    qfail = [f"queue {q['language']}: {q['status']} {(q.get('last_error') or '')[:60]}" for q in cat.queue
             if q["topic_id"] == tid and q["status"] in ("failed", "blocked") and q["language"] in policy.languages]
    if failing or qfail:
        checks.append(_fail("B11", detail="; ".join(failing + qfail), sub="failed",
                            job_ids=[j["id"] for j in latest.values() if j["status"] in ("error", "failed")]))
    elif pending:
        checks.append(_fail("B11", detail="; ".join(pending), sub="pending"))
    else:
        checks.append(_pass("B11", detail=f"{len(latest)} latest jobs clean"))
    return _finish(req, tid, title, checks)


def _finish(req: dict, tid, title, checks: list[CheckResult]) -> TopicAudit:
    failing = [f"{c.code}{'/' + c.language if c.language else ''}" for c in checks if c.verdict == "fail"]
    warnings = [f"{c.code}{'/' + c.language if c.language else ''}: {c.detail}" for c in checks if c.verdict == "warn"]
    codes = {c.code for c in checks if c.verdict == "fail"}
    state = "complete"
    for name, members in STATE_ORDER:
        if codes & members:
            state = name
            break
    return TopicAudit(required_index=req["index"], topic_id=tid, topic_slug=req.get("topic_slug"), title=title,
                      role=req["role"], chapter=req["chapter"], checks=checks, state=state,
                      failing=failing, warnings=warnings)


def apply_waivers(audit: TopicAudit, req: dict, waivers: list) -> TopicAudit:
    """A waived failure becomes a warning that says who waived it, when and why (the check and its evidence stay on
    record); the topic's state is then recomputed. `waivers` are already this book's."""
    if not waivers or not audit.topic_id:
        return audit
    checks, hit = [], False
    for c in audit.checks:
        w = next((w for w in waivers if c.verdict == "fail" and w.topic_id == str(audit.topic_id) and w.check == c.code
                  and w.language == c.language and (w.sub_status is None or w.sub_status == c.sub_status)), None)
        if w:
            hit = True
            c = c.model_copy(update={"verdict": "warn", "detail": f"waived by {w.by} on {w.date}: {w.reason} "
                                                                  f"(was: {c.detail})",
                                     "evidence": {**c.evidence, "waiver": w.model_dump()}})
        checks.append(c)
    return _finish(req, audit.topic_id, audit.title, checks) if hit else audit


def readiness(audits: list[TopicAudit]) -> dict:
    """Readiness over DISTINCT topics (a topic used by two chapters counts once)."""
    seen: dict = {}
    for a in audits:
        seen.setdefault(a.topic_slug or f"proposed:{a.title}", a)
    total = passed = 0
    outstanding = []
    for key, a in seen.items():
        for c in a.checks:
            if c.verdict == "n/a":
                total += 1          # an unreachable check is still an unmet requirement
                continue
            total += 1
            passed += c.verdict in ("pass", "warn")
        if a.state != "complete":
            outstanding.append({"topic": key, "title": a.title, "state": a.state, "failing": a.failing})
    pct = round(100.0 * passed / total, 1) if total else 0.0
    return {"pct": pct, "ready": not outstanding and bool(seen), "distinct_topics": len(seen),
            "outstanding": outstanding}


# -- persistence -----------------------------------------------------------------
def run_audits(conn, slug: str) -> list[int]:
    from publishing.planner import plan_dependencies, store_plan
    policy = ReadinessPolicy.load()
    conn.execute("insert into pub.readiness_policy (id, policy) values (%s,%s) on conflict (id) do nothing",
                 (policy.id, Jsonb(policy.model_dump())))
    cat = CatalogueView.load(conn)
    diffs = cat.parity_with_live()
    if diffs:
        raise RuntimeError(f"current-kit rule disagrees with production on {len(diffs)} rows: {diffs[:3]}")
    works = conn.execute("select * from pub.works" + ("" if slug == "all" else " where slug=%s"),
                         (() if slug == "all" else (slug,))).fetchall()
    run_ids = []
    for w in works:
        bp = conn.execute("select * from pub.blueprints where work_uid=%s order by revision desc limit 1",
                          (w["uid"],)).fetchone()
        if not bp:
            _log.warning("%s has no blueprint; skipped", w["slug"])
            continue
        reqs = conn.execute(
            "select r.*, c.sort as chapter from pub.required_topics r join pub.blueprint_chapters c "
            "on c.chapter_uid=r.chapter_uid where r.blueprint_id=%s order by c.sort, r.id", (bp["id"],)).fetchall()
        slugs = {str(t["id"]): t["slug"] for t in cat.topics}
        run_id = conn.execute("insert into pub.audit_runs (blueprint_id, snapshot_id, policy_id) values (%s,%s,%s) "
                              "returning id", (bp["id"], cat.snapshot_id, policy.id)).fetchone()["id"]
        audits = []
        for r in reqs:
            req = {"index": r["id"], "topic_slug": slugs.get(str(r["topic_id"])) if r["topic_id"] else None,
                   "proposed_title": r["proposed_title"], "role": r["role"], "node_codes": r["node_codes"],
                   "chapter": r["chapter"]}
            a = apply_waivers(audit_topic(cat, req, policy, w["brief"]["profile"]), req,
                              [x for x in policy.waivers if x.work == w["slug"]])
            audits.append(a)
            for c in a.checks:
                conn.execute(
                    "insert into pub.audit_items (run_id, required_topic_id, topic_id, check_code, language, verdict, "
                    "sub_status, evidence) values (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (run_id, r["id"], a.topic_id, c.code, c.language, c.verdict, c.sub_status,
                     Jsonb({"detail": c.detail, **c.evidence})))
            conn.execute("insert into pub.topic_states (run_id, required_topic_id, state, failing_checks) "
                         "values (%s,%s,%s,%s)", (run_id, r["id"], a.state, a.failing))
        rd = readiness(audits)
        conn.execute("insert into pub.readiness_snapshots (run_id, work_uid, pct, ready, outstanding) "
                     "values (%s,%s,%s,%s,%s)", (run_id, w["uid"], rd["pct"], rd["ready"], Jsonb(rd["outstanding"])))
        store_plan(conn, run_id, plan_dependencies(cat, audits, policy), reqs)
        from publishing.pins import dry_run_pins
        existing = sorted({a.topic_id for a in audits if a.topic_id})
        dry_run_pins(conn, cat, run_id, w["uid"], existing, policy.languages)
        conn.commit()
        _log.info("%s: run %s, readiness %.1f%%, ready=%s", w["slug"], run_id, rd["pct"], rd["ready"])
        run_ids.append(run_id)
    return run_ids
