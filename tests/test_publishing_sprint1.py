"""Sprint 1 publishing engine: audit, planner, blueprint checks and guards.

Every catalogue here is a hand-built fixture. No test touches production or
the local database.
"""
import copy
import re
from pathlib import Path

import httpx
import pytest

from publishing.audit import audit_topic, readiness
from publishing.blueprint import SUPPLY_REASON, _has_cycle, check_blueprint
from publishing.catalogue_view import CatalogueView
from publishing.localdb import NotLocal, db_url
from publishing.pins import drift, pin_rows
from publishing.planner import measured_costs, plan_topic, publish_days
from publishing.schemas import Blueprint, BookBrief, ReadinessPolicy
from publishing.snapshot import WriteAttempt, client

POLICY = ReadinessPolicy.load()


def base_catalogue() -> dict:
    """One topic that passes every check in EN and MS."""
    return dict(
        snapshot_id=1,
        curricula=[{"id": "c1", "code": "cur", "name": "Cur"}],
        nodes=[{"id": "n1", "curriculum_id": "c1", "code": "7Xx.01", "grade": "7", "title": "Obj"}],
        topics=[{"id": "t1", "slug": "topic-one", "title": "Topic One", "subject": "S", "status": "video_approved",
                 "bank_maturity": "strong", "prerequisites": []}],
        tcm=[{"id": "m1", "topic_id": "t1", "node_id": "n1", "coverage": "full"}],
        articles=[
            {"id": "a1", "topic_id": "t1", "language": "en", "version": 2, "status": "approved", "word_count": 1200,
             "source_article_id": None, "worked_examples": [{}, {}, {}], "glossary": [{"term": "x"}],
             "sections": [{"covers": ["7Xx.01"], "figure_keys": ["f1"]}]},
            {"id": "a2", "topic_id": "t1", "language": "ms", "version": 1, "status": "approved", "word_count": 1200,
             "source_article_id": "a1", "worked_examples": [], "glossary": [], "sections": []},
        ],
        figures=[{"id": "fg1", "article_id": "a1", "figure_key": "f1", "status": "rendered"}],
        questions=[{"id": f"q{i}", "topic_id": "t1", "language": "en", "status": "approved", "item_type": "mcq"}
                   for i in range(30)],
        kits=[
            {"id": "k1", "topic_id": "t1", "article_id": "a1", "language": "en", "status": "approved",
             "translation_status": None, "source_kit_id": None, "part_plan": [{"part": 1}],
             "reviewed_at": "2026-10-01", "created_at": "2026-10-01"},
            {"id": "k2", "topic_id": "t1", "article_id": "a2", "language": "ms", "status": "approved",
             "translation_status": "approved", "source_kit_id": "k1", "part_plan": [{"part": 1}],
             "reviewed_at": "2026-10-02", "created_at": "2026-10-02"},
        ],
        publications=[
            {"id": "p1", "topic_kit_id": "k1", "part": 1, "channel_language": "en", "youtube_video_id": "yt1",
             "privacy": "public", "youtube_upload_status": "processed", "error": None, "superseded_by": None},
            {"id": "p2", "topic_kit_id": "k2", "part": 1, "channel_language": "ms", "youtube_video_id": "yt2",
             "privacy": "public", "youtube_upload_status": "processed", "error": None, "superseded_by": None},
        ],
        queue=[],
        jobs=[],
    )


def run(cat_kw: dict, slug="topic-one", profile="maths", codes=("7Xx.01",)):
    cat = CatalogueView(**cat_kw)
    req = {"index": 1, "topic_slug": slug, "proposed_title": None if slug else "New Topic", "role": "core",
           "node_codes": list(codes), "chapter": 1}
    return cat, audit_topic(cat, req, POLICY, profile)


def verdict(a, code, lang=None):
    return next(c for c in a.checks if c.code == code and (lang is None or c.language == lang))


# -- audit states ------------------------------------------------------------------
def test_complete_topic_passes_every_check():
    _, a = run(base_catalogue())
    assert a.state == "complete", a.failing
    assert {c.verdict for c in a.checks} == {"pass"}


def test_thumbnail_only_error_is_a_warning_not_a_failure():   # D12
    kw = base_catalogue()
    kw["publications"][1]["error"] = "thumbnail failed (HttpError 403 ...)"
    _, a = run(kw)
    assert a.state == "complete"
    assert verdict(a, "B10").verdict == "warn"


def test_other_publication_error_blocks():
    kw = base_catalogue()
    kw["publications"][1]["error"] = "upload failed: quotaExceeded"
    _, a = run(kw)
    assert a.state == "missing_video_publication"


def test_missing_malay_video_is_reported_with_queue_status():
    kw = base_catalogue()
    kw["publications"].pop()
    kw["queue"] = [{"kit_id": "k2", "topic_id": "t1", "language": "ms", "status": "failed", "last_error": "403"}]
    _, a = run(kw)
    assert a.state == "missing_video_publication"
    assert verdict(a, "B10").sub_status == "failed"
    assert verdict(a, "B11").verdict == "fail"          # a failed queue item is never ignored


def test_malay_kit_from_a_superseded_english_kit_is_stale():
    kw = base_catalogue()
    kw["kits"].append({"id": "k3", "topic_id": "t1", "article_id": "a1", "language": "en", "status": "approved",
                       "translation_status": None, "source_kit_id": None, "part_plan": [{"part": 1}],
                       "reviewed_at": "2026-10-05", "created_at": "2026-10-05"})
    kw["publications"].append({"id": "p3", "topic_kit_id": "k3", "part": 1, "channel_language": "en",
                               "youtube_video_id": "yt3", "privacy": "public", "youtube_upload_status": "processed",
                               "error": None, "superseded_by": None})
    cat, a = run(kw)
    assert cat.current_kit("t1", "en")["id"] == "k3"     # newest approved + public wins
    assert verdict(a, "B9").sub_status == "source_kit_mismatch"
    assert a.state == "missing_translation"


def test_every_part_must_be_published():
    kw = base_catalogue()
    kw["kits"][1]["part_plan"] = [{"part": 1}, {"part": 2}]
    _, a = run(kw)
    assert verdict(a, "B10").verdict == "fail"
    assert "part 2" in verdict(a, "B10").detail


def test_unrendered_figures_are_figures_only_and_do_not_cascade():
    kw = base_catalogue()
    kw["figures"][0]["status"] = "draft"
    cat, a = run(kw)
    assert a.state == "needs_improvement"
    assert verdict(a, "B4").sub_status == "figures_only"
    steps = plan_topic(a, measured_costs(cat), POLICY)
    assert [s.job_type for s in steps] == ["figure_render"]   # no kit, translation or video rebuild


def test_thin_article_needs_revision_and_cascades():
    kw = base_catalogue()
    kw["articles"][0]["word_count"] = 640
    cat, a = run(kw)
    assert verdict(a, "B4").sub_status == "content"
    jobs = [s.job_type for s in plan_topic(a, measured_costs(cat), POLICY)]
    assert jobs[0] == "topic_article" and "kit_translate" in jobs and jobs.count("topic_publish") == 2


def test_proposed_topic_plans_the_full_lifecycle():
    cat, a = run(base_catalogue(), slug=None)
    assert a.state == "missing_topic"
    jobs = [s.job_type for s in plan_topic(a, measured_costs(cat), POLICY)]
    assert jobs == ["manual:library", "topic_article", "topic_questions", "presentation+deck+docs",
                    "topic_publish", "kit_translate", "topic_publish"]


def test_node_missing_from_seed_fails_mapping():
    _, a = run(base_catalogue(), codes=("7Xx.01", "7Xx.99"))
    assert verdict(a, "B2").sub_status == "node_not_in_seed"
    assert a.state == "missing_mapping"


def test_latest_job_decides_b11():
    kw = base_catalogue()
    kw["jobs"] = [
        {"id": "j1", "type": "kit_translate", "status": "error", "params": {"topic_id": "t1", "language": "ms"},
         "created_at": "2026-10-01"},
        {"id": "j2", "type": "kit_translate", "status": "done", "params": {"topic_id": "t1", "language": "ms"},
         "created_at": "2026-10-02"},
    ]
    _, a = run(kw)
    assert verdict(a, "B11").verdict == "pass"            # a retried-and-succeeded job is not a blocker


def test_readiness_pct_is_informational():
    _, ok = run(base_catalogue())
    kw = base_catalogue()
    kw["publications"].pop()
    _, bad = run(kw)
    bad = bad.model_copy(update={"topic_slug": "other"})
    r = readiness([ok, bad])
    assert 0 < r["pct"] < 100 and r["ready"] is False
    assert readiness([ok])["ready"] is True


def test_pins_and_drift():
    kw = base_catalogue()
    cat = CatalogueView(**kw)
    pin = pin_rows(cat, "t1", ["en"])[0]
    assert pin["article_version"] == 2 and pin["youtube_video_ids"] == ["yt1"]
    assert drift(cat, pin) == []
    kw2 = copy.deepcopy(kw)
    kw2["publications"][0]["superseded_by"] = "p9"
    assert any("video" in d for d in drift(CatalogueView(**kw2), pin))


# -- blueprint checks --------------------------------------------------------------
def _bp(**over) -> Blueprint:
    doc = dict(title="T", subtitle="S", audience="A", curriculum_coverage="C", consolidation_rationale="R",
               prerequisite_knowledge=[], assessment_plan="P", illustration_requirements=[], estimated_pages=120,
               estimated_words=40000, complexity="medium", learning_progression="L", publishing_considerations=[],
               localization_considerations=[], interactive_units=[],
               chapters=[dict(number=i, title=f"Ch {i}", purpose="p", sections=["s"], node_codes=["7Xx.01"],
                              required_topics=[dict(topic_slug="topic-one", role="core", node_codes=["7Xx.01"],
                                                    rationale="r")],
                              prerequisite_chapters=[i - 1] if i > 1 else [], illustration_needs=["table"],
                              assessment="a") for i in range(1, 7)])
    doc.update(over)
    return Blueprint.model_validate(doc)


BRIEF = BookBrief(slug="b", working_title="B", intent="i", subject_strand="x/y", profile="maths",
                  primary_curriculum="cur", grades=["7"], age_band="11-12", strand_prefixes=["Xx"],
                  catalogue_subjects=["S"])


def test_blueprint_checks_pass_on_a_sound_plan():
    cat = CatalogueView(**base_catalogue())
    assert check_blueprint(_bp(), cat, BRIEF, ["7Xx.01"])["all_ok"]


def test_blueprint_flags_unassigned_and_supply_exclusions():
    cat = CatalogueView(**base_catalogue())
    cat.nodes.append({"id": "n2", "curriculum_id": "c1", "code": "7Xx.02", "grade": "7", "title": "O2"})
    cat.__post_init__()
    res = check_blueprint(_bp(), cat, BRIEF, ["7Xx.01", "7Xx.02"])
    assert res["coverage_complete"]["detail"] == ["7Xx.02"]
    res = check_blueprint(_bp(excluded_nodes=[{"code": "7Xx.02", "reason": "No article exists in the catalogue"}]),
                          cat, BRIEF, ["7Xx.01", "7Xx.02"])
    assert res["coverage_complete"]["ok"] and not res["no_supply_exclusions"]["ok"]


def test_v02_checks_catch_scope_hygiene_problems():
    cat = CatalogueView(**base_catalogue())
    chapters = _bp().model_dump()["chapters"]
    chapters[5]["title"] = "Review and Exam Practice"
    chapters[5]["required_topics"] = [dict(topic_slug=None, proposed_title="Topic Review and Practice",
                                           role="core", node_codes=[], rationale="r")]
    chapters[4]["required_topics"] = [dict(topic_slug=None, proposed_title="Density: Mass and Volume", role="core",
                                           node_codes=["7Xx.01"], rationale="r"),
                                      dict(topic_slug=None, proposed_title="Mass and Volume: Density",
                                           role="core", node_codes=["7Xx.01"], rationale="r"),
                                      dict(topic_slug=None, proposed_title="Topic One", role="core",
                                           node_codes=["7Xx.01"], rationale="r")]
    bp = _bp(chapters=chapters, proposed_curriculum_nodes=[{"code": "proposed:F.01", "title": "t", "reason": "r"},
                                                           {"code": "7Xx.05", "title": "t", "reason": "r"}])
    res = check_blueprint(bp, cat, BRIEF, ["7Xx.01"])
    assert res["proposed_codes_official_format"]["detail"] == ["proposed:F.01"]
    assert res["review_chapters_reuse_topics"]["detail"] == [6]
    dup = res["no_duplicate_proposals"]["detail"]
    assert ("Density: Mass and Volume", "Mass and Volume: Density") in dup["within_book"]
    assert ("Topic One", "Topic One") in dup["vs_existing"]


def test_founder_directives_are_enforced():
    from publishing.schemas import Directives
    cat = CatalogueView(**base_catalogue())
    cat.nodes.append({"id": "n2", "curriculum_id": "c1", "code": "7Xx.02", "grade": "7", "title": "O2"})
    cat.__post_init__()
    chapters = _bp().model_dump()["chapters"]
    chapters[3]["required_topics"].append(dict(topic_slug=None, proposed_title="Turning Forces", role="core",
                                               node_codes=["7Xx.01"], rationale="r"))
    bp = _bp(chapters=chapters)
    brief = BRIEF.model_copy(update={"directives": Directives(
        decided="2026-10-24", must_include=["7Xx.02"], must_exclude=["7Xx.01"],
        required_new_topics=["Shared Vacuum Topic"], forbidden_new_topics=["Turning Forces and Moments"])})
    d = check_blueprint(bp, cat, brief, ["7Xx.01", "7Xx.02"])["founder_directives"]
    assert not d["ok"]
    assert d["detail"]["must_include_missing"] == ["7Xx.02"]
    assert d["detail"]["must_exclude_used"] == ["7Xx.01"]
    assert d["detail"]["required_titles_absent"] == ["Shared Vacuum Topic"]
    assert d["detail"]["forbidden_proposals"]


def test_blocked_brief_is_skipped(monkeypatch):
    from publishing import blueprint as bpmod
    from publishing.schemas import Directives
    blocked = BRIEF.model_copy(update={"directives": Directives(decided="d", blocked_until="seed loaded")})
    monkeypatch.setattr(bpmod, "load_briefs", lambda: [blocked])
    monkeypatch.setattr(bpmod, "_load_model_env", lambda p: None)
    monkeypatch.setattr(bpmod.CatalogueView, "load", classmethod(lambda cls, conn, sid=None: None))
    called = []
    monkeypatch.setattr(bpmod, "generate", lambda *a, **k: called.append(1))
    import publishing.guard as guard
    monkeypatch.setattr(guard, "require_quiet", lambda *a, **k: called.append("guard"))
    assert bpmod.run_blueprints(None, "all", provider="x", env_file=Path(".")) == []
    assert called == []


def test_supply_reason_pattern():
    assert SUPPLY_REASON.search("not yet in the catalogue")
    assert not SUPPLY_REASON.search("Belongs to the statistics book; outside this book's theme")


def test_capability_kind_reads_the_leading_kind():
    from publishing.blueprint import capability_kind
    assert capability_kind("chart") == "chart"
    assert capability_kind("Apparatus illustration of a friction experiment") == "apparatus_illustration"
    assert capability_kind("geometry_figure for exterior angles") == "geometry_figure"
    assert capability_kind("chem_equation: word equations") == "chem_equation"
    assert capability_kind("hologram of an atom") is None
    assert capability_kind("Force diagrams showing friction opposing motion") == "force_diagram"
    assert capability_kind("Photographs of tools") == "photo"
    assert capability_kind("Summary table of formulas") == "table"
    assert capability_kind("Concept-map style process diagram") == "process_diagram"


def test_cycle_detection():
    assert _has_cycle({1: set(), 2: {1}, 3: {2}}) == []
    assert _has_cycle({1: {3}, 2: {1}, 3: {2}})


# -- guards ------------------------------------------------------------------------
def test_snapshot_client_refuses_writes():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=[]))
    c = client("https://example.supabase.co", "k")
    c._transport = transport
    assert c.get("/topics").status_code == 200
    for method in ("post", "patch", "delete", "put"):
        with pytest.raises(WriteAttempt):
            getattr(c, method)("/topics")


def test_local_db_refuses_remote_hosts(monkeypatch):
    monkeypatch.setenv("PUBDB_URL", "postgresql://postgres@db.pblaxsqhjwlqbrwbraxy.supabase.co:5432/postgres")
    with pytest.raises(NotLocal):
        db_url()
    monkeypatch.setenv("PUBDB_URL", "postgresql://postgres:@127.0.0.1:5555/postgres")
    assert db_url().startswith("postgresql://")


def test_publish_days():
    assert publish_days(50, 1) == 50 and publish_days(50, 3) == 17 and publish_days(0, 5) == 0


def test_engine_code_has_no_subject_branches():
    """One engine for every subject: subject words may appear only in the
    briefs (data), the prompt files and docstrings, never in code."""
    words = re.compile(r"\b(algebra|geometry|physics|chemistry|biology|linear equation|cells?)\b", re.I)
    root = Path(__file__).resolve().parents[1] / "publishing"
    offenders = []
    for f in root.rglob("*.py"):
        src = re.sub(r'"""[\s\S]*?"""', "", f.read_text(encoding="utf-8"))
        for n, line in enumerate(src.splitlines(), 1):
            code = line.split("#", 1)[0]
            if words.search(code):
                offenders.append(f"{f.name}:{n}: {line.strip()}")
    assert offenders == []
