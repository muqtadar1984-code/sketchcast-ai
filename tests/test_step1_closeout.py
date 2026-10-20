"""Step-1 close-out: spend on the job row from every client and thread, the
claim timestamp, the strict image gate's rate-limit refusal deferring the job,
and the script-fallback seam a teacher's job takes before it pauses."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from shared import llm, phase_clock, usage_ledger
from tests.test_observer_job_guard import GEN, _builder, _fresh
from worker import client as db
from worker import process
import worker.run as run


def _text(total: dict) -> dict:
    """The four original keys of a ledger total — text spend."""
    return {k: total[k] for k in ("calls", "input_tokens", "output_tokens", "cost_usd")}

VERTEX_429 = '{"error": {"code": 429, "message": "Resource exhausted", "status": "RESOURCE_EXHAUSTED"}}'


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    usage_ledger.reset()
    llm.set_script_fallback(False)
    monkeypatch.setattr(process, "db", db)
    run._shutdown.clear()
    with run._inflight_lock:
        run._inflight_jobs.clear()
    yield
    usage_ledger.reset()
    llm.set_script_fallback(False)
    for gid in list(phase_clock._CLOCKS):
        phase_clock.stop(gid)
    with run._inflight_lock:
        run._inflight_jobs.clear()


# ── the ledger ──────────────────────────────────────────────────────────
class TestSpendFromEveryClientAndThread:
    def test_entries_fold_into_their_generation_by_label(self):
        usage_ledger.start("g1")
        usage_ledger.add({"generation_id": "g1", "input_tokens": 100, "output_tokens": 20, "estimated_cost_usd": 0.01})
        usage_ledger.add({"generation_id": "g1", "input_tokens": 5, "output_tokens": 5, "cost_usd": 0.001})
        usage_ledger.add({"generation_id": "g2", "input_tokens": 999})           # no account: dropped
        usage_ledger.add({"input_tokens": 999})                                    # nowhere to go: dropped
        assert _text(usage_ledger.total("g1")) == {"calls": 2, "input_tokens": 105, "output_tokens": 25, "cost_usd": 0.011}
        assert usage_ledger.stop("g1")["calls"] == 2 and usage_ledger.total("g1")["calls"] == 0

    def test_a_render_thread_without_labels_is_attributed_through_the_phase_clock(self):
        """Image and vision calls happen on pool threads that carry no usage
        labels; the composer binds the phase clock into them, and that is
        the generation the spend belongs to."""
        usage_ledger.start("g-render")
        phase_clock.start("g-render")
        gid = phase_clock.current()

        def render_thread():
            tok = phase_clock.bind(gid)
            try:
                usage_ledger.add({"service": "image.vertex", "model": "m", "usd": 0.04, "outcome": "image"})
            finally:
                phase_clock.unbind(tok)
        with ThreadPoolExecutor(max_workers=1) as ex:
            ex.submit(render_thread).result()
        total = usage_ledger.total("g-render")
        # a picture is picture spend, not a text call
        assert _text(total) == {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
        assert (total["image_calls"], total["images"], total["images_usd"]) == (1, 1, 0.04)

    def test_the_token_log_feeds_the_ledger(self, monkeypatch, tmp_path):
        from shared.claude_client import ClaudeClient, set_usage_context
        monkeypatch.setattr("shared.claude_client.TOKEN_LOG_PATH", tmp_path / "token_log.jsonl")
        usage_ledger.start("g-log")
        set_usage_context(generation_id="g-log")
        try:
            ClaudeClient._log_usage({"input_tokens": 10, "output_tokens": 3, "estimated_cost_usd": 0.002})
            ClaudeClient._log_usage({"input_tokens": 1, "output_tokens": 1, "estimated_cost_usd": 0.001})
        finally:
            set_usage_context()
        assert _text(usage_ledger.total("g-log")) == {"calls": 2, "input_tokens": 11, "output_tokens": 4, "cost_usd": 0.003}

    def test_the_job_row_gets_the_ledger_total_not_one_clients(self, monkeypatch):
        written = {}
        monkeypatch.setattr(db, "merge_generation_params", lambda sb, gid, patch: None)
        monkeypatch.setattr(db, "set_job_usage", lambda sb, jid, usage: written.update({jid: usage}))

        def fake_generate(sb, job, gid):
            # two "clients" and a render thread, as a video really spends
            usage_ledger.add({"generation_id": gid, "input_tokens": 1000, "output_tokens": 500, "estimated_cost_usd": 0.1})
            usage_ledger.add({"generation_id": gid, "input_tokens": 2000, "output_tokens": 100, "estimated_cost_usd": 0.2})
        monkeypatch.setattr(process, "_generate", fake_generate)
        process.process_generation(object(), {"id": "j1", "type": "presentation", "created_at": "1"}, GEN)
        assert _text(written["j1"]) == {"calls": 2, "input_tokens": 3000, "output_tokens": 600, "cost_usd": 0.3}
        assert usage_ledger.total(GEN)["calls"] == 0, "the account is closed with the job"

    def test_the_worker_no_longer_reports_one_clients_session(self):
        import inspect
        src = inspect.getsource(process)
        assert "set_job_usage(sb, job_id, built.client.session_usage)" not in src
        assert "set_job_usage(sb, job_id, client.session_usage)" not in src, "jobs.usage comes from the ledger"
        # once, in process_generation's finally: every outcome, every attempt
        assert src.count("usage_ledger.total(generation_id)") == 1


# ── claimed_at ──────────────────────────────────────────────────────────
def test_a_claim_stamps_when_the_worker_took_it():
    sb = _fresh("queued", jobs=[_builder("job-ws", "worksheet")])
    row = db.claim_next_job(sb, job_type=["worksheet"])
    assert row["status"] == "processing" and row["claimed_at"].endswith("+00:00")
    assert row["claimed_at"] >= row["created_at"]


# ── the image gate's refusal defers the job ─────────────────────────────
class TestPicturesRefusedOnQuotaDeferTheJob:
    def test_all_rate_limited_is_a_refusal(self):
        from spike.scene_engine.asset_warm import ImagesIncomplete
        exc = ImagesIncomplete({"cell": "rate_limited", "nucleus": "rate_limited"}, 12)
        assert db.is_rate_limited(exc)
        wrapped = RuntimeError("lesson failed")
        wrapped.__cause__ = exc
        assert db.is_rate_limited(wrapped)

    def test_a_mixed_or_other_cause_is_not(self):
        from spike.scene_engine.asset_warm import ImagesIncomplete
        assert not db.is_rate_limited(ImagesIncomplete({"cell": "rate_limited", "x": "generation_failed"}, 2))
        assert not db.is_rate_limited(ImagesIncomplete({"<warm pass>": "failed: KeyError"}, 1))
        assert not db.is_rate_limited(ImagesIncomplete({}, 1))

    def test_the_run_loop_defers_it_like_a_text_refusal(self, monkeypatch):
        from spike.scene_engine.asset_warm import ImagesIncomplete

        def boom(sb, job, gid):
            raise ImagesIncomplete({"cell": "rate_limited"}, 9)
        monkeypatch.setattr(run, "process_generation", boom)
        monkeypatch.setattr(run, "_claim_catalogue_generation", lambda sb: None)
        sb = _fresh("queued", jobs=[_builder("job-v", "presentation")])
        assert run.run_once(sb) is True
        row = sb.tables["jobs"][0]
        assert row["status"] == "queued" and row["attempts"] == 0 and not row.get("error")
        assert "quota" in row["params"]["deferred_note"]


# ── the script-fallback seam ────────────────────────────────────────────
class TestTheScriptFallbackSeam:
    def test_a_teachers_job_that_spent_its_budget_gets_one_run_on_the_fallback(self, monkeypatch):
        monkeypatch.setattr(db, "deferred_seconds", lambda job: 1800.0)
        job = _builder("job-v", "presentation")
        wait = db.rate_limit_deferral(job, RuntimeError(VERTEX_429))
        assert isinstance(wait, db.DeferredJob) and wait.extra == {"script_fallback": True}
        assert "fallback model" in wait.note
        flagged = {**job, "params": {"script_fallback": True}}
        assert db.rate_limit_deferral(flagged, RuntimeError(VERTEX_429)) is None
        assert db.rate_limit_exhausted_message(flagged, RuntimeError(VERTEX_429)).startswith("paused after")

    def test_a_catalogue_kit_does_not_take_the_fallback(self, monkeypatch):
        monkeypatch.setattr(db, "deferred_seconds", lambda job: 10800.0)
        kit = {**_builder("job-k", "presentation"), "params": {"catalogue": True, "kit_id": "k"}}
        assert db.rate_limit_deferral(kit, RuntimeError(VERTEX_429)) is None

    def test_the_deferral_carries_the_flag_into_the_queue(self, monkeypatch):
        monkeypatch.setattr(db, "deferred_seconds", lambda job: 1800.0)

        def boom(sb, job, gid):
            raise RuntimeError(VERTEX_429)
        monkeypatch.setattr(run, "process_generation", boom)
        monkeypatch.setattr(run, "_claim_catalogue_generation", lambda sb: None)
        sb = _fresh("queued", jobs=[_builder("job-v", "presentation")])
        assert run.run_once(sb) is True
        row = sb.tables["jobs"][0]
        assert row["status"] == "queued" and row["params"]["script_fallback"] is True

    def test_the_job_thread_arms_the_seam_from_the_flag_and_disarms_after(self, monkeypatch):
        monkeypatch.setattr(db, "merge_generation_params", lambda sb, gid, patch: None)
        seen = {}
        monkeypatch.setattr(process, "_generate", lambda sb, job, gid: seen.update({"on": llm.script_fallback_on()}))
        process.process_generation(object(), {"id": "j", "type": "presentation", "created_at": "1",
                                              "params": {"script_fallback": True}}, GEN)
        assert seen["on"] is True and llm.script_fallback_on() is False
        process.process_generation(object(), {"id": "j2", "type": "presentation", "created_at": "1"}, GEN)
        assert seen["on"] is False

    def test_the_script_client_honours_the_seam_on_the_gemini_path(self, monkeypatch):
        from shared import text_models
        monkeypatch.setenv("VERTEX_PROJECT_ID", "sketchcast")
        monkeypatch.setenv("VERTEX_REGION", "global")
        monkeypatch.setattr(llm, "provider_for", lambda lang: llm.GEMINI)
        monkeypatch.delenv("SCRIPT_FALLBACK_MODEL", raising=False)
        assert llm.script_client("en").model == text_models.resolve(text_models.SCRIPT).id
        llm.set_script_fallback(True)
        assert llm.script_client("en").model == text_models.FLASH_LITE_3_5
        monkeypatch.setenv("SCRIPT_FALLBACK_MODEL", "")          # the seam is disabled
        assert llm.script_client("en").model == text_models.resolve(text_models.SCRIPT).id

    def test_taking_the_fallback_is_counted_on_the_phase_clock(self, monkeypatch):
        monkeypatch.setenv("VERTEX_PROJECT_ID", "sketchcast")
        monkeypatch.setattr(llm, "provider_for", lambda lang: llm.GEMINI)
        monkeypatch.delenv("SCRIPT_FALLBACK_MODEL", raising=False)
        clk = phase_clock.start(GEN)
        llm.set_script_fallback(True)
        llm.script_client("en")
        assert clk.counts == {"script_fallback": 1}
