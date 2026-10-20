"""jobs.usage records what a job really cost (2026-10-20, for the pricing strategy).

Measuring a kit's unit cost found four gaps, each pinned here:

* PRICES — Sonnet 5.5 (live on Bedrock since 2026-10-18) was missing from
  MODEL_PRICING and booked at the $3/$15 default; Sonnet 5 at $3/$15 when
  its list price is $2/$10; Opus 5.5 cache reads are 0.05x, not 0.1x;
  Haiku 5.5 has a second rate card above a 100K-token prompt.
* WHO SERVED IT — the row carried no model or backend, so Gemini and Claude
  spend could only be told apart by deploy dates. ``by_model`` splits it.
* PICTURES AND NARRATION — image spend reached the ledger under a key it
  did not read (one ``call``, $0 each) and TTS never reached it at all.
* FAILED ATTEMPTS — the row was written only on success; a failed or
  deferred attempt's spend vanished and the retry recorded only its own.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from shared import claude_client as cc
from shared import usage_ledger
from shared.claude_client import ClaudeClient, claude_cost_usd, set_usage_context
from tests.test_observer_job_guard import GEN
from worker import client as db
from worker import process


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    usage_ledger.reset()
    monkeypatch.setattr(cc, "TOKEN_LOG_PATH", tmp_path / "token_log.json")
    monkeypatch.delenv("BEDROCK_PRICE_FACTOR", raising=False)
    yield
    set_usage_context()
    usage_ledger.reset()


# ── prices ──────────────────────────────────────────────────────────────
class TestPrices:
    @pytest.mark.parametrize("model,rates", [
        ("claude-sonnet-5-5", (2.0, 10.0)),
        ("claude-sonnet-5", (2.0, 10.0)),
        ("claude-haiku-5-5", (0.10, 0.50)),
        ("claude-opus-5-5", (4.0, 20.0)),
    ])
    def test_the_current_models_are_priced(self, model, rates):
        assert cc.MODEL_PRICING[model] == rates
        # 50K in (under Haiku 5.5's long-prompt threshold) and a million out
        assert claude_cost_usd(model, 50_000, 1_000_000) == pytest.approx(rates[0] / 20 + rates[1])

    def test_sonnet_5_5_is_no_longer_booked_at_the_default(self):
        assert claude_cost_usd("claude-sonnet-5-5", 1_000_000, 0) == pytest.approx(2.0)
        assert cc.MODEL_PRICING["claude-sonnet-5-5"] != cc._DEFAULT_PRICING

    def test_cache_reads_are_0_1x_except_opus_5_5_at_0_05x(self):
        assert claude_cost_usd("claude-sonnet-5-5", 0, 0, cache_read=1_000_000) == pytest.approx(0.20)
        assert claude_cost_usd("claude-opus-5-5", 0, 0, cache_read=1_000_000) == pytest.approx(0.20)
        assert claude_cost_usd("claude-opus-5-5", 0, 0, cache_write=1_000_000) == pytest.approx(8.0)

    def test_haiku_5_5_switches_rate_card_above_a_100k_prompt(self):
        short = claude_cost_usd("claude-haiku-5-5", 100_000, 1_000)
        assert short == pytest.approx((100_000 * 0.10 + 1_000 * 0.50) / 1e6)
        # the threshold counts the whole prompt: uncached + cached + written
        long_ = claude_cost_usd("claude-haiku-5-5", 60_000, 1_000, cache_read=50_000)
        assert long_ == pytest.approx((60_000 * 0.50 + 50_000 * 0.05 + 1_000 * 2.50) / 1e6)

    def test_a_dated_snapshot_prices_as_its_alias(self):
        assert claude_cost_usd("claude-haiku-4-5-20251001", 1_000_000, 0) == pytest.approx(1.0)

    def test_bedrock_is_at_parity_unless_the_factor_says_otherwise(self, monkeypatch):
        assert claude_cost_usd("claude-sonnet-5-5", 1_000_000, 0, backend="bedrock") == pytest.approx(2.0)
        monkeypatch.setenv("BEDROCK_PRICE_FACTOR", "1.1")
        assert claude_cost_usd("claude-sonnet-5-5", 1_000_000, 0, backend="bedrock") == pytest.approx(2.2)
        assert claude_cost_usd("claude-sonnet-5-5", 1_000_000, 0, backend="anthropic") == pytest.approx(2.0)


def _claude(model: str, backend: str) -> ClaudeClient:
    c = ClaudeClient.__new__(ClaudeClient)   # no network client needed to price a response
    c.model, c.backend = model, backend
    c.session_usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0,
                       "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "cost_usd": 0.0}
    return c


def _response(inp=0, out=0, cache_read=0, cache_write=0):
    return SimpleNamespace(usage=SimpleNamespace(
        input_tokens=inp, output_tokens=out,
        cache_read_input_tokens=cache_read, cache_creation_input_tokens=cache_write))


# ── who served it ───────────────────────────────────────────────────────
class TestByModel:
    def test_a_claude_call_names_its_model_and_backend_on_the_ledger(self):
        usage_ledger.start("g")
        set_usage_context(generation_id="g")
        usage = _claude("claude-sonnet-5-5", "bedrock").track_tokens(_response(10_000, 2_000, cache_read=5_000))
        assert "model" not in usage, "the returned dict is summed key by key by callers"
        row = usage_ledger.total("g")["by_model"]["bedrock:claude-sonnet-5-5"]
        assert (row["backend"], row["model"], row["calls"]) == ("bedrock", "claude-sonnet-5-5", 1)
        assert (row["input_tokens"], row["output_tokens"], row["cache_read_input_tokens"]) == (10_000, 2_000, 5_000)
        assert row["cost_usd"] == pytest.approx((10_000 * 2 + 5_000 * 0.2 + 2_000 * 10) / 1e6)

    def test_gemini_and_claude_separate_and_sum_to_the_text_total(self):
        usage_ledger.start("g")
        for entry in (
            {"model": "claude-sonnet-5-5", "backend": "bedrock", "input_tokens": 100, "output_tokens": 10, "estimated_cost_usd": 0.5},
            {"model": "claude-sonnet-5-5", "backend": "bedrock", "input_tokens": 100, "output_tokens": 10, "estimated_cost_usd": 0.5},
            {"model": "gemini-3-pro", "backend": "gemini", "input_tokens": 300, "output_tokens": 30, "estimated_cost_usd": 0.25},
        ):
            usage_ledger.add(entry, "g")
        total = usage_ledger.total("g")
        assert set(total["by_model"]) == {"bedrock:claude-sonnet-5-5", "gemini:gemini-3-pro"}
        assert total["by_model"]["bedrock:claude-sonnet-5-5"]["calls"] == 2
        assert sum(r["cost_usd"] for r in total["by_model"].values()) == pytest.approx(total["cost_usd"]) == pytest.approx(1.25)
        assert sum(r["calls"] for r in total["by_model"].values()) == total["calls"] == 3

    def test_a_total_is_a_copy_not_the_live_account(self):
        usage_ledger.start("g")
        usage_ledger.add({"model": "m", "backend": "b", "estimated_cost_usd": 1.0}, "g")
        snap = usage_ledger.total("g")
        usage_ledger.add({"model": "m", "backend": "b", "estimated_cost_usd": 1.0}, "g")
        assert snap["by_model"]["b:m"]["calls"] == 1


# ── pictures and narration ──────────────────────────────────────────────
class TestPicturesAndNarration:
    def test_an_image_books_its_price_and_a_refusal_books_none(self):
        usage_ledger.start("g")
        usage_ledger.add({"service": "image.vertex", "model": "gemini-3-pro-image", "usd": 0.134, "outcome": "image"}, "g")
        usage_ledger.add({"service": "image.vertex", "model": "gemini-3-pro-image", "usd": None, "outcome": "no_image"}, "g")
        usage_ledger.add({"service": "image.aistudio", "model": "gemini-3-pro-image", "usd": 0.134, "outcome": "image"}, "g")
        t = usage_ledger.total("g")
        assert t["calls"] == 0 and t["cost_usd"] == 0.0, "pictures are not text calls"
        assert (t["image_calls"], t["images"], t["images_usd"]) == (3, 2, 0.268)
        assert t["images_by_model"]["image.vertex:gemini-3-pro-image"] == {"calls": 2, "images": 1, "usd": 0.134}

    def test_the_real_image_path_reaches_the_ledger(self):
        from spike.scene_engine import raster_assets as ra
        from shared.image_models import SCENE, resolve
        model = resolve(SCENE)
        usage_ledger.start("g")
        set_usage_context(generation_id="g")
        ra._note_spend("image.vertex", model, produced=True)
        t = usage_ledger.total("g")
        assert t["image_calls"] == 1 and t["images_usd"] == pytest.approx(model.cost_usd or 0.0)

    def test_tts_spend_reaches_the_ledger(self, monkeypatch, tmp_path):
        from shared.tts import cost
        monkeypatch.setattr(cost, "_SPEND_FILE", tmp_path / "tts_spend.json")
        usage_ledger.start("g")
        set_usage_context(generation_id="g")
        cost.record(10_000, "google", "chirp")      # $0.030 / 1K chars
        cost.record(4_000, "edge")                  # free: counted, $0
        t = usage_ledger.total("g")
        assert (t["tts_calls"], t["tts_chars"]) == (2, 14_000)
        assert t["tts_usd"] == pytest.approx(0.30)
        assert t["tts_by_provider"]["google:chirp"] == {"calls": 1, "chars": 10_000, "usd": 0.3}
        assert t["calls"] == 0, "narration is not a text call"


# ── the row ─────────────────────────────────────────────────────────────
class _SB:
    """The two queries set_job_usage makes, against one in-memory jobs row."""

    def __init__(self, usage=None):
        self.usage = usage

    def table(self, name):
        sb = self

        class Q:
            def __init__(self):
                self.patch = None

            def select(self, *_):
                return self

            def update(self, patch):
                self.patch = patch
                return self

            def eq(self, *_):
                return self

            def maybe_single(self):
                return self

            def execute(self):
                if self.patch is not None:
                    sb.usage = self.patch["usage"]
                return SimpleNamespace(data={"usage": sb.usage})
        return Q()


def _attempt(text_usd=0.0, images_usd=0.0, tts_usd=0.0, model="bedrock:claude-sonnet-5-5"):
    backend, name = model.split(":", 1)
    t = usage_ledger._blank()
    t.update(calls=2, input_tokens=100, output_tokens=10, cost_usd=text_usd,
             by_model={model: {"backend": backend, "model": name, "calls": 2, "input_tokens": 100,
                               "output_tokens": 10, "cache_read_input_tokens": 0,
                               "cache_creation_input_tokens": 0, "cost_usd": text_usd}},
             image_calls=1 if images_usd else 0, images=1 if images_usd else 0, images_usd=images_usd,
             tts_calls=1 if tts_usd else 0, tts_usd=tts_usd)
    return t


class TestTheRow:
    def test_the_first_write_carries_the_split_and_an_all_in_total(self):
        sb = _SB()
        db.set_job_usage(sb, "j", _attempt(text_usd=0.9, images_usd=0.5, tts_usd=0.2))
        assert sb.usage["total_usd"] == pytest.approx(1.6)
        assert sb.usage["by_model"]["bedrock:claude-sonnet-5-5"]["cost_usd"] == 0.9

    def test_attempts_add_up_model_by_model(self):
        sb = _SB()
        db.set_job_usage(sb, "j", _attempt(text_usd=0.4, images_usd=0.1))
        db.set_job_usage(sb, "j", _attempt(text_usd=0.6, images_usd=0.3, tts_usd=0.2,
                                           model="gemini:gemini-3-pro"))
        u = sb.usage
        assert (u["calls"], u["cost_usd"], u["images_usd"], u["tts_usd"]) == (4, 1.0, 0.4, 0.2)
        assert u["total_usd"] == pytest.approx(1.6)
        assert set(u["by_model"]) == {"bedrock:claude-sonnet-5-5", "gemini:gemini-3-pro"}
        assert u["by_model"]["bedrock:claude-sonnet-5-5"]["backend"] == "bedrock"

    def test_an_old_shape_row_merges_and_gains_the_new_fields(self):
        sb = _SB({"calls": 5, "input_tokens": 100, "output_tokens": 200, "cost_usd": 1.0})
        db.set_job_usage(sb, "j", _attempt(text_usd=0.5, tts_usd=0.25))
        assert (sb.usage["calls"], sb.usage["cost_usd"], sb.usage["total_usd"]) == (7, 1.5, 1.75)

    def test_a_job_with_pictures_and_narration_but_no_text_is_written(self):
        """A translated kit's video reuses its scripts: no text call, all the
        spend is pictures and voice. The old 'no calls, nothing to write'
        rule left 43 of 47 kit videos in three days with no usage at all."""
        sb = _SB()
        u = _attempt(images_usd=0.6, tts_usd=0.3)
        u.update(calls=0, cost_usd=0.0, by_model={})
        db.set_job_usage(sb, "j", u)
        assert sb.usage["total_usd"] == pytest.approx(0.9)

    def test_nothing_spent_writes_nothing(self):
        sb = _SB()
        db.set_job_usage(sb, "j", usage_ledger._blank())
        assert sb.usage is None

    def test_one_client_reports_its_own_split(self):
        c = _claude("claude-haiku-5-5", "anthropic")
        c.track_tokens(_response(1_000, 100))
        u = db.client_usage(c)
        assert u["by_model"] == {"anthropic:claude-haiku-5-5": {
            "backend": "anthropic", "model": "claude-haiku-5-5", **{k: u[k] for k in db._USAGE_TEXT_KEYS}}}
        assert db.client_usage(object()) is None


# ── failed attempts are real spend ──────────────────────────────────────
class TestEveryAttemptIsRecorded:
    @pytest.fixture
    def sb(self, monkeypatch):
        monkeypatch.setattr(process, "db", db)
        monkeypatch.setattr(db, "merge_generation_params", lambda *a, **k: None)
        return _SB()

    def _spend(self, gid, usd):
        usage_ledger.add({"model": "claude-sonnet-5-5", "backend": "bedrock",
                          "input_tokens": 1000, "output_tokens": 100, "estimated_cost_usd": usd}, gid)
        usage_ledger.add({"service": "image.vertex", "model": "img", "usd": 0.1, "outcome": "image"}, gid)

    def test_a_failed_attempt_then_a_success_both_land_on_the_row(self, sb, monkeypatch):
        job = {"id": "j1", "type": "presentation", "created_at": "1"}

        def fails(sb_, job_, gid):
            self._spend(gid, 0.7)
            raise RuntimeError("Vertex 429 after 32 minutes")
        monkeypatch.setattr(process, "_generate", fails)
        with pytest.raises(RuntimeError):
            process.process_generation(sb, job, GEN)
        assert sb.usage["cost_usd"] == 0.7 and sb.usage["images_usd"] == 0.1

        monkeypatch.setattr(process, "_generate", lambda sb_, job_, gid: self._spend(gid, 0.5))
        process.process_generation(sb, job, GEN)
        assert sb.usage["cost_usd"] == pytest.approx(1.2)
        assert sb.usage["images_usd"] == pytest.approx(0.2)
        assert sb.usage["total_usd"] == pytest.approx(1.4)
        assert sb.usage["by_model"]["bedrock:claude-sonnet-5-5"]["calls"] == 2

    def test_a_deferred_attempt_keeps_what_it_spent(self, sb, monkeypatch):
        def defers(sb_, job_, gid):
            self._spend(gid, 0.3)
            raise db.DeferredJob(60, "waiting out a busy pool")
        monkeypatch.setattr(process, "_generate", defers)
        with pytest.raises(db.DeferredJob):
            process.process_generation(sb, {"id": "j2", "type": "presentation", "created_at": "1"}, GEN)
        assert sb.usage["cost_usd"] == 0.3

    def test_a_success_is_written_once_not_twice(self, sb, monkeypatch):
        monkeypatch.setattr(process, "_generate", lambda sb_, job_, gid: self._spend(gid, 0.5))
        process.process_generation(sb, {"id": "j3", "type": "presentation", "created_at": "1"}, GEN)
        assert sb.usage["cost_usd"] == 0.5 and sb.usage["calls"] == 1
