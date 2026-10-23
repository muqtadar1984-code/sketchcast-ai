"""The Fix Agent, Phase 1 (catalogue/fix_agent.py) and the translator's two
inputs from it (catalogue/translate.py: evidence on failure, fix_note,
waivers) — plan "SketchCast Feature Plan - Translation Fix Agent.md" Rev 3 §15.

The database is tests/catalogue_fakes.FakeSB with the two functions the agent
calls (auto_translate_candidates, fix_case_enqueue) and the range filter it
uses; the model is a stub. No network, no live Supabase."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import pytest

from catalogue import auto_translate as at
from catalogue import fix_agent as fa
from catalogue import translate as T
from tests.catalogue_fakes import FakeSB, _Query, _Res
from tests.test_catalogue_translate import SRC, FakeTranslator, _arabize, _translated

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
SRC_KIT, TOPIC = "en-kit-1", "topic-1"
AT_ON = {"enabled": True, "paused": False, "languages": ["ms"], "breaker_cleared_at": "2026-10-20T17:19:00+00:00"}


# ── the translator: waivers ────────────────────────────────────────────────


def _report(*blocking):
    return {"ok": not blocking, "blocking": list(blocking), "flags": [], "checks": []}


ADDED_1 = {"where": "s006", "what": "numbers differ", "extra": ["1", "1"]}


class TestWaivers:
    def test_an_exact_waiver_turns_the_finding_into_a_visible_flag(self):
        r = T.apply_waivers(_report(dict(ADDED_1)), [dict(ADDED_1)], "case-1")
        assert r["ok"] and r["blocking"] == []
        assert r["flags"][0]["waived_by"] == "case-1" and r["flags"][0]["extra"] == ["1", "1"]

    def test_fewer_of_the_waived_numbers_is_covered(self):
        r = T.apply_waivers(_report({**ADDED_1, "extra": ["1"]}), [dict(ADDED_1)], "case-1")
        assert r["ok"]

    def test_more_or_different_numbers_stay_blocking(self):
        for extra in (["1", "1", "1"], ["2"], ["1", "5"]):
            r = T.apply_waivers(_report({**ADDED_1, "extra": extra}), [dict(ADDED_1)], "case-1")
            assert not r["ok"], extra

    def test_a_dropped_number_is_not_covered_by_a_waiver_for_an_added_one(self):
        r = T.apply_waivers(_report({**ADDED_1, "missing": ["100"]}), [dict(ADDED_1)], "case-1")
        assert not r["ok"]

    def test_another_field_is_not_covered(self):
        r = T.apply_waivers(_report({**ADDED_1, "where": "s007"}), [dict(ADDED_1)], "case-1")
        assert not r["ok"]

    def test_forbidden_kinds_are_never_waived(self):
        forbidden = [
            {"where": "examples[0].steps[1].after", "what": "protected field altered"},
            {"where": "examples[0].problem", "what": "maths in the question altered"},
            {"where": "lesson", "what": "structure differs"},
            {"where": "examples[0].final_answer[0]", "what": "numbers differ", "extra": ["1"]},
            {"where": "try_it.problem", "what": "numbers differ", "extra": ["1"]},
        ]
        for f in forbidden:
            r = T.apply_waivers(_report(dict(f)), [dict(f)], "case-1")
            assert not r["ok"], f

    def test_no_waivers_changes_nothing(self):
        rep = _report(dict(ADDED_1))
        assert T.apply_waivers(rep, [], "case-1") is rep


# ── the translator: evidence and the instruction ───────────────────────────


def _science_body():
    return {"part": 1, "of": 1, "language": "en", "avatars": {}, "video": {},
            "script": {"script_id": "sc", "book_id": "b", "chapter_num": -1, "episode_num": 1,
                       "episode_title": "Cells", "generated_at": "now", "narrator_persona": "x",
                       "segments": copy.deepcopy(SRC["segments"]), "visual_plan": None, "avatars": None,
                       "total_estimated_duration_seconds": 40, "question_hook_count": 0}}


def _adds_a_one(s: str) -> str:
    # the Malay "1 meter" for "a metre": a number the English never wrote as a digit
    return _arabize(s.replace("Nucleus holds", "1 nucleus holds"))


class TestEvidenceAndNote:
    def test_a_refusal_carries_the_english_beside_the_translation(self):
        with pytest.raises(T.TranslateRefused) as e:
            T.translate_episode(FakeTranslator(_adds_a_one), _science_body(), "ar", [], context="")
        ev = e.value.report["evidence"]
        assert ev and ev[0]["where"] == "s002" and ev[0]["extra"] == ["1", "1"]  # the line is spoken twice
        assert "Nucleus holds DNA" in ev[0]["source"] and "١ nucleus" in ev[0]["target"]

    def test_the_approved_waiver_lets_that_finding_through_as_a_flag(self):
        waiver = {"where": "s002", "what": "numbers differ", "extra": ["1", "1"]}
        dst, report = T.translate_episode(FakeTranslator(_adds_a_one), _science_body(), "ar", [], context="",
                                          waivers=[waiver], waiver_case="case-9")
        assert report["ok"]
        assert [f for f in report["flags"] if f.get("waived_by") == "case-9"][0]["where"] == "s002"

    def test_the_fix_note_reaches_every_ask(self):
        c = FakeTranslator()
        T.translate_episode(c, _science_body(), "ar", [], context="Lesson context. FIX-NOTE: write 'a' as a word.")
        assert c.calls and all("FIX-NOTE" in p for p in c.calls)

    def test_failure_record_is_compact(self):
        rep = {"part": 1, "blocking": [dict(ADDED_1)] * 20, "flags": [{}] * 3, "checks": [{"check": "A"}],
               "evidence": [{"where": "s006", "source": "x" * 5000}] * 20}
        out = T.failure_record(rep)
        assert len(out["blocking"]) == T.EVIDENCE_FINDINGS and out["flags"] == 3
        assert len(out["evidence"]) == T.EVIDENCE_FINDINGS

    def test_the_failed_job_keeps_its_findings(self, monkeypatch):
        sb = FakeSB()
        sb.tables["jobs"].append({"id": "j1", "status": "processing", "stage": {"step": "script", "kit_id": "k"}})

        def refuse(*_a, **_k):
            raise T.TranslateRefused("translation of part 1 failed pre-check A: s006: numbers differ",
                                     report={"part": 1, **_report(dict(ADDED_1)),
                                             "evidence": [{**ADDED_1, "source": "a metre", "target": "1 meter"}]})

        monkeypatch.setattr(T, "translate_kit", refuse)
        monkeypatch.setattr(T, "find_translation_kit", lambda *a, **k: None)
        T.run_translate_job(sb, {"id": "j1", "params": {"source_kit_id": SRC_KIT, "language": "ms"}})
        job = sb.tables["jobs"][0]
        assert job["status"] == "error" and "pre-check A" in job["error"]
        assert job["stage"]["kit_id"] == "k", "the stage the job reached is kept"
        assert job["stage"]["failure"]["evidence"][0]["target"] == "1 meter"


# ── the agent: pure parts ──────────────────────────────────────────────────

FINDINGS = [{"where": "s006", "what": "numbers differ", "extra": ["1", "1"],
             "source": "because a metre has one hundred centimetres", "target": "kerana 1 meter ada 100 sentimeter"},
            {"where": "examples[0].final_answer[0]", "what": "numbers differ", "extra": ["1"],
             "source": "x = 3", "target": "x = 31"}]


class TestProposal:
    def test_a_false_positive_gets_a_note_and_a_waiver_with_the_exact_values(self):
        reply = {"classification": "check_false_positive", "explanation": "the article 'a' became '1'",
                 "verdicts": [{"where": "s006", "verdict": "faithful", "why": "1 meter = a metre"}],
                 "note": "Write the English article 'a' as a Malay word, never the digit 1.", "waive": ["s006"],
                 "check_rule": {"change": "let an article absorb an added 1"}}
        diag, prop = fa.validate_diagnosis(reply, FINDINGS)
        assert diag["classification"] == "check_false_positive"
        assert prop["actions"] == ["rerun_with_note", "rerun_with_waiver", "check_rule"]
        assert prop["waivers"] == [{"where": "s006", "what": "numbers differ", "extra": ["1", "1"], "missing": [],
                                    "justification": "1 meter = a metre"}]

    def test_a_waiver_needs_a_faithful_verdict_and_a_waivable_finding(self):
        reply = {"classification": "check_false_positive",
                 "verdicts": [{"where": "s006", "verdict": "wrong", "why": "?"},
                              {"where": "examples[0].final_answer[0]", "verdict": "faithful", "why": "?"}],
                 "waive": ["s006", "examples[0].final_answer[0]", "nowhere"]}
        _, prop = fa.validate_diagnosis(reply, FINDINGS)
        assert prop["waivers"] == [] and "rerun_with_waiver" not in prop["actions"]

    def test_a_translation_error_never_gets_a_waiver_or_a_rule(self):
        reply = {"classification": "translation_error", "verdicts": [{"where": "s006", "verdict": "faithful"}],
                 "waive": ["s006"], "check_rule": {"change": "x"}, "note": "keep every number"}
        _, prop = fa.validate_diagnosis(reply, FINDINGS)
        assert prop["actions"] == ["rerun_with_note"] and prop["waivers"] == [] and prop["check_rule"] is None

    def test_an_unreadable_reply_is_unknown_and_a_plain_rerun(self):
        diag, prop = fa.validate_diagnosis("not json", FINDINGS)
        assert diag["classification"] == "unknown" and prop["actions"] == ["rerun"]

    def test_the_note_is_capped(self):
        _, prop = fa.validate_diagnosis({"classification": "translation_error", "note": "n" * 5000}, FINDINGS)
        assert len(prop["note"]) == fa.NOTE_CHARS

    def test_rules_classify_a_throttle_without_a_model(self):
        assert fa.rule_classification("429 Too Many Requests") == "infrastructure"
        assert fa.rule_classification("pre-check A: s006: numbers differ") is None


class TestOutcome:
    def test_still_running(self):
        assert fa.outcome_of({"id": "j", "status": "processing"}, None) is None
        assert fa.outcome_of({"id": "j", "status": "done"}, {"id": "k", "status": "generating"}) is None

    def test_in_review_resolves(self):
        res, out = fa.outcome_of({"id": "j", "status": "done"}, {"id": "k", "status": "in_review"})
        assert res == "resolved" and out["kit_id"] == "k"

    def test_a_failed_retry_starts_round_two_and_then_needs_the_founder(self):
        case = {"id": "c", "round": 1, "proposal": {"note": "n"}, "history": [], "source_job_id": "j0",
                "applied_job_id": "j1"}
        p = fa.next_round(case, {"job_id": "j1", "error": "again"}, NOW)
        assert p["status"] == "new" and p["round"] == 2 and p["source_job_id"] == "j1"
        assert p["history"][0]["proposal"] == {"note": "n"} and p["applied_job_id"] is None
        assert fa.next_round({**case, "round": 2}, {"error": "again"}, NOW)["status"] == "needs_founder"


class TestResumeRule:
    def test_resumes_only_when_every_breaker_failure_is_repaired(self):
        state = {"breaker": True}
        assert fa.breaker_repaired(AT_ON, state, ["j1", "j2"], {"j1", "j2"})
        assert not fa.breaker_repaired(AT_ON, state, ["j1", "j2"], {"j1"})
        assert not fa.breaker_repaired(AT_ON, state, [], set()), "a breaker this agent did not repair"

    def test_never_over_pause_or_off(self):
        assert not fa.breaker_repaired({**AT_ON, "paused": True}, {"breaker": True}, ["j1"], {"j1"})
        assert not fa.breaker_repaired({**AT_ON, "enabled": False}, {"breaker": True}, ["j1"], {"j1"})
        assert not fa.breaker_repaired(AT_ON, {"breaker": False}, ["j1"], {"j1"})


# ── the agent: the tick against an in-memory database ──────────────────────


class _RangeQuery(_Query):
    def gte(self, col, val):
        self.filters.append(("gte", col, val))
        return self

    def _match(self, row):
        rest = [f for f in self.filters if f[0] != "gte"]
        ranges = [f for f in self.filters if f[0] == "gte"]
        saved, self.filters = self.filters, rest
        try:
            ok = super()._match(row)
        finally:
            self.filters = saved
        return ok and all(str(self._value(row, c) or "") >= str(v) for _, c, v in ranges)


class AgentDB(FakeSB):
    def __init__(self):
        super().__init__()
        self.tables.update({"fix_cases": [], "platform_settings": [], "platform_audit_log": []})
        self.cands: list[dict] = []
        self.enqueue_calls = 0

    def table(self, name):
        return _RangeQuery(self, name)

    def rpc(self, name, params=None):
        db, p = self, dict(params or {})

        class R:
            def execute(self_):
                if name == "auto_translate_candidates":
                    return _Res(list(db.cands))
                if name == "fix_case_enqueue":
                    db.enqueue_calls += 1
                    c = next(x for x in db.tables["fix_cases"] if x["id"] == p["p_case_id"])
                    if c["status"] != "approved" or c.get("applied_job_id"):
                        return _Res({"queued": False, "why": "status_" + c["status"]})
                    job = {"id": f"fix-job-{db.enqueue_calls}", "type": "kit_translate", "status": "queued",
                           "params": {"source_kit_id": c["source_kit_id"], "language": c["language"],
                                      "fix_case_id": c["id"], "fix_note": (c.get("proposal") or {}).get("note")}}
                    db.tables["jobs"].append(job)
                    c.update(status="applied", applied_job_id=job["id"])
                    return _Res({"queued": True, "job_id": job["id"]})
                raise AssertionError(name)
        return R()

    def setting(self, key, value):
        self.tables["platform_settings"].append({"key": key, "value": value})

    def case(self):
        return self.tables["fix_cases"][0]


def _malay_failure(db, job_id="j-fail", error="translation of part 1 failed pre-check A: s006: numbers differ",
                   evidence=True, auto=True):
    db.cands = [{"source_kit_id": SRC_KIT, "topic_id": TOPIC, "topic_title": "Pressure", "language": "ms",
                 "hold": "job_failed", "hold_detail": "1 failed job(s) in 7 days: " + error}]
    stage = {"part": 1, "step": "script", "kit_id": "gone"}
    if evidence:
        stage["failure"] = {"part": 1, "evidence": [dict(FINDINGS[0])]}
    db.tables["jobs"].append({"id": job_id, "type": "kit_translate", "status": "error", "error": error,
                              "params": {"source_kit_id": SRC_KIT, "language": "ms", "auto": auto},
                              "stage": stage, "created_at": "2026-10-21T02:34:00+00:00",
                              "updated_at": "2026-10-21T02:37:15+00:00"})


def _model(reply):
    calls = []

    def ask(language, error, findings):
        calls.append(findings)
        return reply, 0.03, "claude-sonnet-5-5"
    ask.calls = calls
    return ask


FP_REPLY = {"classification": "check_false_positive", "explanation": "the article 'a' was written as 1",
            "verdicts": [{"where": "s006", "verdict": "faithful", "why": "'1 meter' renders 'a metre'"}],
            "note": "Write the English article 'a'/'an' as a Malay word, never the digit 1.", "waive": ["s006"]}


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "anthropic")          # no Bedrock client-work hold in tests
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    d = AgentDB()
    d.setting("fix_agent", {"enabled": True, "summary_email": "f@example.com"})
    d.setting("auto_translate", dict(AT_ON))
    d.setting("auto_translate_state", {"breaker": True, "breaker_at": "2026-10-21T02:38:05+00:00"})
    monkeypatch.setattr(fa, "ask_model", _model(FP_REPLY))
    return d


class TestTick:
    def test_off_does_nothing(self, db):
        db.tables["platform_settings"][0]["value"]["enabled"] = False
        _malay_failure(db)
        assert fa.tick(db, now=NOW) == {"action": "off"}
        assert db.tables["fix_cases"] == []

    def test_env_switch(self, db, monkeypatch):
        monkeypatch.delenv(fa.ENV, raising=False)
        assert fa.maybe_tick(db, now=NOW) is None

    def test_detect_diagnose_propose_then_wait_for_the_founder(self, db):
        _malay_failure(db)
        fa.tick(db, now=NOW)
        c = db.case()
        assert c["status"] == "proposed" and c["source_job_id"] == "j-fail" and c["round"] == 1
        assert c["diagnosis"]["classification"] == "check_false_positive"
        assert c["proposal"]["waivers"][0]["extra"] == ["1", "1"]
        assert c["proposal"]["risk"] == "low" and "blast_radius" in c["proposal"]
        assert fa.ask_model.calls[0][0]["target"].startswith("kerana"), "the model saw the kept evidence"
        # nothing is queued before the founder approves; a second tick changes nothing
        fa.tick(db, now=NOW + timedelta(minutes=1))
        assert db.enqueue_calls == 0 and len(db.tables["fix_cases"]) == 1
        assert {a["action"] for a in db.tables["platform_audit_log"]} == {"fix_case_propose"}

    def test_a_throttle_is_a_plain_retry_without_a_model_call(self, db):
        _malay_failure(db, error="429 RESOURCE_EXHAUSTED", evidence=False)
        fa.tick(db, now=NOW)
        assert db.case()["proposal"]["actions"] == ["rerun"] and fa.ask_model.calls == []

    def test_an_old_failure_is_reproduced_when_the_lane_is_free(self, db, monkeypatch):
        _malay_failure(db, evidence=False)
        seen = []
        monkeypatch.setattr(fa, "reproduce", lambda sb, case, job: seen.append(job["id"]) or
                            {"reproduced": True, "passed": False, "part": 1, "findings": [dict(FINDINGS[0])],
                             "cost_usd": 0.2})
        fa.tick(db, now=NOW)
        assert seen == ["j-fail"] and db.case()["evidence"]["reproduced"] is True
        assert float(db.case()["cost_usd"]) == pytest.approx(0.23)

    def test_a_reproduction_waits_while_a_translation_runs(self, db, monkeypatch):
        _malay_failure(db, evidence=False)
        db.tables["jobs"].append({"id": "busy", "type": "kit_translate", "status": "processing", "params": {}})
        monkeypatch.setattr(fa, "reproduce", lambda *a: pytest.fail("reproduced while the lane was busy"))
        fa.tick(db, now=NOW)
        assert db.case()["status"] == "new"

    def test_approved_waits_for_the_lane_then_queues_exactly_once(self, db):
        _malay_failure(db)
        fa.tick(db, now=NOW)
        db.case().update(status="approved", decided_by="founder")
        db.tables["jobs"].append({"id": "busy", "type": "kit_translate", "status": "processing", "params": {}})
        fa.tick(db, now=NOW + timedelta(minutes=1))
        assert db.enqueue_calls == 0 and db.case()["status"] == "approved"
        db.tables["jobs"] = [j for j in db.tables["jobs"] if j["id"] != "busy"]
        fa.tick(db, now=NOW + timedelta(minutes=2))
        fa.tick(db, now=NOW + timedelta(minutes=3))
        assert db.enqueue_calls == 1 and db.case()["status"] == "applied"

    def test_a_user_job_holds_the_retry(self, db):
        _malay_failure(db)
        fa.tick(db, now=NOW)
        db.case().update(status="approved")
        db.tables["jobs"].append({"id": "u1", "type": "presentation", "status": "processing", "params": {},
                                  "updated_at": NOW.isoformat()})
        fa.tick(db, now=NOW + timedelta(minutes=1))
        assert db.enqueue_calls == 0

    def test_the_retry_reaching_review_resolves_and_resumes_the_translate_agent(self, db):
        _malay_failure(db)
        fa.tick(db, now=NOW)
        db.case().update(status="approved")
        fa.tick(db, now=NOW + timedelta(minutes=1))
        job = next(j for j in db.tables["jobs"] if j["id"] == db.case()["applied_job_id"])
        job.update(status="done", stage={"kit_id": "new-kit"})
        db.tables["topic_kits"].append({"id": "new-kit", "status": "in_review", "translation_status": "in_review",
                                        "source_kit_id": SRC_KIT, "language": "ms"})
        fa.tick(db, now=NOW + timedelta(minutes=5))
        assert db.case()["status"] == "resolved" and db.case()["outcome"]["kit_id"] == "new-kit"
        at_settings = next(r for r in db.tables["platform_settings"] if r["key"] == "auto_translate")["value"]
        assert at_settings["breaker_cleared_at"] == (NOW + timedelta(minutes=5)).isoformat()
        assert at_settings["paused"] is False and at_settings["enabled"] is True, "Pause and Off are never touched"
        actions = [a["action"] for a in db.tables["platform_audit_log"]]
        assert "fix_case_resolve" in actions and "fix_agent_resume_auto_translate" in actions

    def test_no_resume_while_another_breaker_failure_is_unrepaired(self, db):
        _malay_failure(db)
        db.tables["jobs"].append({"id": "j-other", "type": "kit_translate", "status": "error", "error": "x",
                                  "params": {"source_kit_id": "other", "language": "ms", "auto": True},
                                  "created_at": "2026-10-21T02:30:00+00:00"})
        fa.tick(db, now=NOW)
        db.case().update(status="applied", applied_job_id="r1")
        db.tables["jobs"].append({"id": "r1", "type": "kit_translate", "status": "done", "stage": {"kit_id": "nk"}})
        db.tables["topic_kits"].append({"id": "nk", "status": "in_review", "source_kit_id": SRC_KIT, "language": "ms"})
        fa.tick(db, now=NOW + timedelta(minutes=5))
        assert db.case()["status"] == "resolved"
        at_settings = next(r for r in db.tables["platform_settings"] if r["key"] == "auto_translate")["value"]
        assert at_settings["breaker_cleared_at"] == AT_ON["breaker_cleared_at"]

    def test_a_failed_retry_goes_to_round_two_then_to_the_founder(self, db):
        _malay_failure(db)
        fa.tick(db, now=NOW)
        db.case().update(status="applied", applied_job_id="r1")
        db.tables["jobs"].append({"id": "r1", "type": "kit_translate", "status": "error", "error": "429 overloaded"})
        fa.tick(db, now=NOW + timedelta(minutes=5))
        c = db.case()
        assert c["round"] == 2 and c["status"] == "proposed" and c["proposal"]["actions"] == ["rerun"]
        assert c["history"][0]["source_job_id"] == "j-fail"
        c.update(status="applied", applied_job_id="r2")
        db.tables["jobs"].append({"id": "r2", "type": "kit_translate", "status": "error", "error": "again"})
        fa.tick(db, now=NOW + timedelta(minutes=10))
        assert db.case()["status"] == "needs_founder"
        fa.tick(db, now=NOW + timedelta(minutes=11))
        assert len(db.tables["fix_cases"]) == 1, "a failure already seen never opens another case"

    def test_a_rejected_proposal_is_not_reopened(self, db):
        _malay_failure(db)
        fa.tick(db, now=NOW)
        db.case().update(status="rejected", updated_at=(NOW + timedelta(minutes=1)).isoformat())
        fa.tick(db, now=NOW + timedelta(minutes=2))
        assert len(db.tables["fix_cases"]) == 1 and db.case()["status"] == "rejected"

    def test_a_diagnosis_that_keeps_failing_goes_to_the_founder(self, db, monkeypatch):
        _malay_failure(db)

        def broken(*_a):
            raise RuntimeError("model down")
        monkeypatch.setattr(fa, "ask_model", broken)
        fa.tick(db, now=NOW)
        assert db.case()["status"] == "new" and db.case()["diagnosis"]["attempts"] == 1
        fa.tick(db, now=NOW + timedelta(minutes=1))
        assert db.case()["status"] == "needs_founder"


# ── after a deploy: the running code re-checks the open cases ──────────────

MALAY_PAIR = {"where": "s006", "what": "numbers differ", "extra": ["1", "1"],
              "source": "because a metre has one hundred centimetres? A square metre is a grid.",
              "target": "kerana 1 meter ada 100 sentimeter? 1 meter persegi ialah grid."}


class TestRecheck:
    def test_a_pair_the_current_check_accepts_passes(self):
        rc = fa.recheck_case({"evidence": {"findings": [MALAY_PAIR]}}, "abc1234", NOW)
        assert rc["passes"] is True and rc["checked"] == 1 and rc["passing"] == 1 and rc["commit"] == "abc1234"

    def test_a_real_error_still_fails(self):
        bad = {**MALAY_PAIR, "target": "kerana 1 meter ada 1000 sentimeter? 1 meter persegi ialah grid."}
        assert fa.recheck_case({"evidence": {"findings": [bad]}}, "c", NOW)["passes"] is False

    def test_a_maths_question_pair(self):
        f = {"where": "examples[3].problem", "what": "maths in the question altered",
             "source": "A line passes through the points A(0, 3) and B(4, 11).",
             "target": "Una recta pasa por los puntos A(0, 3) y B(4, 11)."}
        assert fa.recheck_case({"evidence": {"findings": [f]}}, "c", NOW)["passes"] is True

    def test_nothing_to_check_is_unknown_not_a_pass(self):
        assert fa.recheck_case({"evidence": {}}, "c", NOW)["passes"] is None
        other = {**MALAY_PAIR, "what": "protected field altered"}
        assert fa.recheck_case({"evidence": {"findings": [other]}}, "c", NOW)["passes"] is None

    def test_the_latest_round_with_strings_is_used(self):
        case = {"evidence": {}, "history": [{"evidence": {"findings": [MALAY_PAIR]}}, {"evidence": {}}]}
        assert fa.recheck_case(case, "c", NOW)["passes"] is True

    def test_a_known_fixed_error_names_its_fix(self):
        case = {"issue": "x", "outcome": {"error": "translation reply did not return 18 items (got list)"}}
        rc = fa.recheck_case(case, "c", NOW)
        assert rc["fixed_by"] == "#316" and rc["passes"] is None
        assert fa.recheck_case({"issue": "did not return 98 items (got dict)"}, "c", NOW)["fixed_by"] is None

    def test_the_tick_checks_each_open_case_once_per_commit(self, db, monkeypatch):
        monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "40cda5993b")
        db.tables["fix_cases"] = [
            {"id": "c1", "status": "needs_founder", "evidence": {"findings": [MALAY_PAIR]}, "history": []},
            {"id": "c2", "status": "resolved", "evidence": {"findings": [MALAY_PAIR]}},
            {"id": "c3", "status": "approved", "evidence": {}, "issue": "did not return 10 items (got list)"},
        ]
        assert fa.recheck_open(db, NOW) == 2
        c1, c2, c3 = db.tables["fix_cases"]
        assert c1["evidence"]["recheck"]["passes"] is True and c1["evidence"]["recheck"]["commit"] == "40cda59"
        assert c1["evidence"]["findings"] == [MALAY_PAIR], "the saved evidence is kept"
        assert "recheck" not in c2["evidence"], "a closed case is left alone"
        assert c3["evidence"]["recheck"]["fixed_by"] == "#316"
        assert fa.recheck_open(db, NOW) == 0, "once per commit"
        monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "ffff0000")
        assert fa.recheck_open(db, NOW) == 2, "a new deploy checks again"
