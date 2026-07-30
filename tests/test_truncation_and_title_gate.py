"""Regressions for Sara's two generation failures on a scanned one-chapter book.

* "Script generation produced no segments" — Claude's reply hit max_tokens and
  the truncated JSON silently downgraded to raw_text (zero segments). Pinned:
  analyze() now detects stop_reason == "max_tokens" and retries ONCE, streamed,
  at double the budget (capped at 32000), reusing the same messages list so a
  cache_control block keeps hitting the 1h prompt cache.

* "Chapter check failed: 'DocScanner 16 Jun 2026 3 21 Pm (1)' …" — a
  single-chapter book's title falls back to the BOOK title (here a scanner
  filename), which page content can never match, so the title-vs-content gate
  failed 100% deterministically. Pinned: _title_gate_applies() disables the
  gate for whole-book/book-titled chapters while keeping it for the
  multi-chapter mislabel case it was built for (Mona's Unit-3 bug).
"""

from __future__ import annotations

from types import SimpleNamespace

from shared.claude_client import ClaudeClient
from worker.process import _title_gate_applies


# ── the title gate predicate ───────────────────────────────────────────────────

SCAN_TITLE = "DocScanner 16 Jun 2026 3 21 Pm (1)"


def test_gate_off_for_single_chapter_filename_title():
    # Sara's exact case: 1 chapter, chapter title == book title == filename.
    assert _title_gate_applies(SCAN_TITLE, SCAN_TITLE, 1, False) is False


def test_gate_off_for_any_single_chapter_book():
    # One chapter = whole book = no boundary that could be wrong.
    assert _title_gate_applies("Key Skills", "Env. Management", 1, False) is False


def test_gate_off_when_chapter_repeats_book_title_multichapter():
    # A book-titled chapter carries no chapter-specific topic to verify.
    assert _title_gate_applies("Biology", "Biology", 12, False) is False


def test_gate_title_match_is_case_and_space_insensitive():
    assert _title_gate_applies("  biology ", "Biology", 12, False) is False


def test_gate_off_for_cumulative_papers():
    assert _title_gate_applies("Revision — Chapters 1–3", "Biology", 12, True) is False


def test_gate_still_on_for_real_multichapter_titles():
    # The original mislabel protection (Unit 3 pointing at the wrong pages)
    # must stay fully active.
    assert _title_gate_applies("Unit 3: Computer storage", "Cambridge Computing", 6, False) is True


def test_gate_on_with_missing_book_title():
    assert _title_gate_applies("Unit 3: Computer storage", None, 6, False) is True


# ── the truncation retry ───────────────────────────────────────────────────────

TRUNCATED = '{"segments": [\n  {\n    "type": "hook",\n    "text": "Imagine you wake up one morning and the river near your sch'
FULL = '{"segments": [{"type": "hook", "text": "Imagine you wake up."}]}'


class _Usage:
    input_tokens = 10
    output_tokens = 5
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


def _msg(text: str, stop_reason: str):
    return SimpleNamespace(
        content=[SimpleNamespace(text=text)], stop_reason=stop_reason, usage=_Usage(),
    )


def _client(monkeypatch) -> ClaudeClient:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    c = ClaudeClient()
    monkeypatch.setattr(ClaudeClient, "_log_usage", staticmethod(lambda usage: None))
    return c


def test_truncated_reply_is_retried_streamed_at_double_budget(monkeypatch):
    c = _client(monkeypatch)
    seen = {"create": [], "stream": []}
    monkeypatch.setattr(c, "_create", lambda system, messages, max_tokens: (
        seen["create"].append(max_tokens), _msg(TRUNCATED, "max_tokens"))[1])
    monkeypatch.setattr(c, "_create_stream", lambda system, messages, max_tokens: (
        seen["stream"].append(max_tokens), _msg(FULL, "end_turn"))[1])

    out = c.analyze("write the script", max_tokens=16000)

    assert out["data"]["segments"][0]["type"] == "hook"  # parsed, not raw_text
    assert seen["create"] == [16000]
    assert seen["stream"] == [32000]  # doubled
    assert c.session_usage["calls"] == 2  # the wasted attempt is still billed


def test_clean_reply_never_retries(monkeypatch):
    c = _client(monkeypatch)
    streamed = []
    monkeypatch.setattr(c, "_create", lambda system, messages, max_tokens: _msg(FULL, "end_turn"))
    monkeypatch.setattr(c, "_create_stream", lambda system, messages, max_tokens: (
        streamed.append(max_tokens), _msg(FULL, "end_turn"))[1])

    out = c.analyze("write the script", max_tokens=16000)

    assert out["data"]["segments"]
    assert streamed == []


def test_retry_budget_caps_at_32000(monkeypatch):
    c = _client(monkeypatch)
    seen = {"stream": []}
    monkeypatch.setattr(c, "_create", lambda system, messages, max_tokens: _msg(TRUNCATED, "max_tokens"))
    monkeypatch.setattr(c, "_create_stream", lambda system, messages, max_tokens: (
        seen["stream"].append(max_tokens), _msg(FULL, "end_turn"))[1])

    c.analyze("p", max_tokens=20000)
    assert seen["stream"] == [32000]  # min(40000, 32000)


def test_no_retry_when_already_at_cap(monkeypatch):
    # At 32000 there is no headroom — keep the loud downstream failure instead
    # of an infinite retry loop.
    c = _client(monkeypatch)
    streamed = []
    monkeypatch.setattr(c, "_create", lambda system, messages, max_tokens: _msg(TRUNCATED, "max_tokens"))
    monkeypatch.setattr(c, "_create_stream", lambda system, messages, max_tokens: (
        streamed.append(max_tokens), _msg(FULL, "end_turn"))[1])

    out = c.analyze("p", max_tokens=32000)
    assert streamed == []
    assert "raw_text" in out["data"]  # still the loud-fail path


def test_retry_reuses_the_same_messages_for_cache_hits(monkeypatch):
    # The cache_control block must ride along on the retry (1h cache re-read).
    c = _client(monkeypatch)
    captured = {}
    monkeypatch.setattr(c, "_create", lambda system, messages, max_tokens: (
        captured.setdefault("first", messages), _msg(TRUNCATED, "max_tokens"))[1])
    monkeypatch.setattr(c, "_create_stream", lambda system, messages, max_tokens: (
        captured.setdefault("retry", messages), _msg(FULL, "end_turn"))[1])

    c.analyze("part prompt", cache_prefix="G" * 5000, max_tokens=16000)

    assert captured["retry"] is captured["first"]
    assert captured["first"][0]["content"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
