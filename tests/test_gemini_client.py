"""GeminiClient: token accounting, response shaping, and interface parity.

The accounting is the part worth pinning. Vertex bills Gemini's thinking tokens
at the OUTPUT rate, and reports them in a THIRD field the Claude shape has no
equivalent for. Measured on a real generation: 5,022 answer tokens against 1,917
thinking tokens. Dropping thoughtsTokenCount would understate that call's cost
by 38% — and 88% of SketchCast's per-generation cost is output tokens, so the
error would land squarely on the number the financial model is built from.
"""
from __future__ import annotations

import pytest

from shared.claude_client import ClaudeClient
from shared.gemini_client import GEMINI_PRICING, GeminiClient


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("VERTEX_PROJECT_ID", "sketchcast")
    monkeypatch.setenv("VERTEX_REGION", "global")
    return GeminiClient(model="gemini-2.5-flash")


def usage_response(prompt=0, answer=0, thoughts=0, text="{}", finish="STOP"):
    return {
        "candidates": [{"content": {"role": "model", "parts": [{"text": text}]},
                        "finishReason": finish}],
        "usageMetadata": {
            "promptTokenCount": prompt,
            "candidatesTokenCount": answer,
            "thoughtsTokenCount": thoughts,
            "totalTokenCount": prompt + answer + thoughts,
        },
    }


# ── token accounting ─────────────────────────────────────────────────────

def test_thinking_tokens_are_billed_as_output(client):
    """THE regression this file exists for."""
    u = client.track_tokens(usage_response(prompt=20, answer=5_022, thoughts=1_917))
    assert u["output_tokens"] == 5_022 + 1_917, "thoughts must be folded into output"

    in_rate, out_rate = GEMINI_PRICING["gemini-2.5-flash"]
    expected = (20 * in_rate + 6_939 * out_rate) / 1_000_000
    assert u["estimated_cost_usd"] == pytest.approx(round(expected, 6))


def test_ignoring_thoughts_would_understate_cost_by_38_percent(client):
    """Pins the magnitude, so a future 'simplification' that drops the field
    fails loudly rather than quietly shaving a third off recorded spend."""
    with_thoughts = client.track_tokens(usage_response(prompt=20, answer=5_022, thoughts=1_917))
    naive = GeminiClient(model="gemini-2.5-flash")
    without = naive.track_tokens(usage_response(prompt=20, answer=5_022, thoughts=0))
    ratio = with_thoughts["estimated_cost_usd"] / without["estimated_cost_usd"]
    assert 1.35 < ratio < 1.40


def test_session_usage_accumulates_like_claude(client):
    client.track_tokens(usage_response(prompt=100, answer=200, thoughts=50))
    client.track_tokens(usage_response(prompt=10, answer=20, thoughts=5))
    assert client.session_usage["calls"] == 2
    assert client.session_usage["input_tokens"] == 110
    assert client.session_usage["output_tokens"] == 275
    assert client.session_usage["cost_usd"] > 0


def test_usage_dict_has_the_same_keys_as_claude(client):
    """process.py folds one client's session_usage into another's; mismatched
    keys would silently drop a bucket from jobs.usage."""
    gemini_keys = set(client.track_tokens(usage_response(prompt=1, answer=1)))
    claude_keys = {
        "input_tokens", "output_tokens", "cache_read_input_tokens",
        "cache_creation_input_tokens", "total_tokens", "estimated_cost_usd",
    }
    assert gemini_keys == claude_keys


def test_unknown_model_falls_back_to_a_rate_rather_than_crashing(monkeypatch):
    monkeypatch.setenv("VERTEX_PROJECT_ID", "p")
    c = GeminiClient(model="gemini-99-experimental")
    assert c.track_tokens(usage_response(prompt=1_000_000, answer=0))["estimated_cost_usd"] > 0


# ── response shaping ─────────────────────────────────────────────────────

def test_text_extraction_survives_a_safety_block(client):
    """A blocked candidate carries no `content` — never index blindly."""
    assert client._text({"candidates": [{"finishReason": "SAFETY"}]}) == ""
    assert client._text({}) == ""


def test_text_extraction_reads_the_first_text_part(client):
    assert client._text(usage_response(text='{"ok":true}')) == '{"ok":true}'


def test_json_extraction_is_shared_with_claude(client):
    """Same fenced-JSON handling both sides, so a provider swap can't change
    how a malformed reply is parsed."""
    fenced = '```json\n{"a": 1}\n```'
    assert ClaudeClient._extract_json(fenced) == {"a": 1}


# ── config ───────────────────────────────────────────────────────────────

def test_requires_a_project(monkeypatch):
    monkeypatch.delenv("VERTEX_PROJECT_ID", raising=False)
    with pytest.raises(RuntimeError, match="VERTEX_PROJECT_ID"):
        GeminiClient()


def test_endpoint_targets_vertex_not_ai_studio(client):
    """AI Studio bills to a separate account that GCP credits do not cover."""
    url = client._url()
    assert url.startswith("https://aiplatform.googleapis.com/")
    assert "generativelanguage" not in url
    assert "/projects/sketchcast/locations/global/" in url
    assert url.endswith(":generateContent")


def test_thinking_is_disabled_in_the_request_body(client, monkeypatch):
    """+38% output tokens if this regresses."""
    captured = {}

    class _Res:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return usage_response(prompt=1, answer=1)

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(json)
        return _Res()

    monkeypatch.setattr("shared.gemini_client.requests.post", fake_post)
    monkeypatch.setattr("shared.gemini_client._access_token", lambda: "tok")
    client.analyze("hello")

    assert captured["generationConfig"]["thinkingConfig"]["thinkingBudget"] == 0


def _capture_body(client, monkeypatch) -> dict:
    captured = {}

    class _Res:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return usage_response(prompt=1, answer=1)

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(json)
        return _Res()

    monkeypatch.setattr("shared.gemini_client.requests.post", fake_post)
    monkeypatch.setattr("shared.gemini_client._access_token", lambda: "tok")
    client.analyze("hello")
    return captured


def test_structured_json_output_is_requested_by_default(client, monkeypatch):
    """Two of the founder's lessons failed on 2026-09-04 with COMPLETE replies
    whose JSON was malformed (a wrong bracket; quotes in prose). Every caller
    of this client parses JSON, so the model is asked to emit only that."""
    monkeypatch.delenv("GEMINI_JSON_MODE", raising=False)
    body = _capture_body(client, monkeypatch)
    assert body["generationConfig"]["responseMimeType"] == "application/json"


def test_structured_output_has_a_kill_switch(client, monkeypatch):
    monkeypatch.setenv("GEMINI_JSON_MODE", "0")
    body = _capture_body(client, monkeypatch)
    assert "responseMimeType" not in body["generationConfig"]
    assert body["generationConfig"]["thinkingConfig"]["thinkingBudget"] == 0, "the rest of the config is untouched"


# ── structured output ────────────────────────────────────────────────────
#
# responseMimeType ASKS for JSON. responseSchema CONSTRAINS decoding to a
# shape, which is the difference between a malformation that has to be
# repaired and one that cannot happen. Sara Hamaydeh's lesson (gen eb12963c,
# 2026-09-05) failed with the mime type already on: 22,538 chars, 6,168 output
# tokens against a 30,000 cap, no truncation reported, unparseable at char
# 14,380. These pin who may send a schema, and that a bad one can never take a
# caller down.

SCHEMA = {"type": "OBJECT", "properties": {"verdict": {"type": "STRING"}},
          "required": ["verdict"]}


def _capture_with(client, monkeypatch, status=200, **kwargs) -> list[dict]:
    """Every body posted, so a fallback shows up as a SECOND request."""
    bodies: list[dict] = []

    class _Res:
        def __init__(self, code):
            self.status_code = code
            self.text = "schema rejected"

        def raise_for_status(self): pass
        def json(self): return usage_response(prompt=1, answer=1)

    def fake_post(url, headers=None, json=None, timeout=None):
        bodies.append(json)
        # The first call gets `status`; any retry succeeds, so the test can
        # see the fallback rather than a loop.
        return _Res(status if len(bodies) == 1 else 200)

    monkeypatch.setattr("shared.gemini_client.requests.post", fake_post)
    monkeypatch.setattr("shared.gemini_client._access_token", lambda: "tok")
    client.analyze("hello", **kwargs)
    return bodies


def test_a_schema_is_not_sent_unless_the_flag_is_on(client, monkeypatch):
    """SAFE default. Constrained decoding changes generation on the calls that
    earn the money; OFF keeps today's request byte-for-byte."""
    monkeypatch.delenv("GEMINI_RESPONSE_SCHEMA", raising=False)
    body = _capture_with(client, monkeypatch, response_schema=SCHEMA)[0]
    assert "responseSchema" not in body["generationConfig"]
    assert body["generationConfig"]["responseMimeType"] == "application/json"


def test_the_flag_sends_the_callers_schema(client, monkeypatch):
    monkeypatch.setenv("GEMINI_RESPONSE_SCHEMA", "1")
    body = _capture_with(client, monkeypatch, response_schema=SCHEMA)[0]
    assert body["generationConfig"]["responseSchema"] == SCHEMA
    assert body["generationConfig"]["responseMimeType"] == "application/json", \
        "Vertex rejects a schema without the mime type — the two travel together"


def test_a_caller_that_names_no_schema_is_unchanged(client, monkeypatch):
    monkeypatch.setenv("GEMINI_RESPONSE_SCHEMA", "1")
    body = _capture_with(client, monkeypatch)[0]
    assert "responseSchema" not in body["generationConfig"], \
        "the flag enables schemas; it does not invent one"


def test_json_mode_off_takes_the_schema_with_it(client, monkeypatch):
    monkeypatch.setenv("GEMINI_RESPONSE_SCHEMA", "1")
    monkeypatch.setenv("GEMINI_JSON_MODE", "0")
    body = _capture_with(client, monkeypatch, response_schema=SCHEMA)[0]
    assert "responseSchema" not in body["generationConfig"]
    assert "responseMimeType" not in body["generationConfig"]


def test_a_schema_vertex_rejects_falls_back_instead_of_failing_the_job(client, monkeypatch):
    """A 400 on a schema is not a degraded lesson, it is an outage: Vertex
    would reject EVERY call that carried it. Drop it once and let the reply
    take its chances with the salvage."""
    monkeypatch.setenv("GEMINI_RESPONSE_SCHEMA", "1")
    bodies = _capture_with(client, monkeypatch, status=400, response_schema=SCHEMA)
    assert len(bodies) == 2, "one constrained attempt, one unconstrained retry"
    assert "responseSchema" in bodies[0]["generationConfig"]
    assert "responseSchema" not in bodies[1]["generationConfig"]
    assert bodies[1]["generationConfig"]["thinkingConfig"]["thinkingBudget"] == 0, \
        "the rest of the config survives the fallback"


def test_a_400_without_a_schema_is_still_an_error(client, monkeypatch):
    """The fallback is scoped to the schema. An ordinary 400 must keep
    raising, or a broken prompt would look like a model that returned nothing."""
    monkeypatch.delenv("GEMINI_RESPONSE_SCHEMA", raising=False)

    class _Res:
        status_code = 400
        text = "bad request"

        def raise_for_status(self):
            raise RuntimeError("400")

        def json(self): return {}

    monkeypatch.setattr("shared.gemini_client.requests.post",
                        lambda *a, **k: _Res())
    monkeypatch.setattr("shared.gemini_client._access_token", lambda: "tok")
    with pytest.raises(RuntimeError):
        client.analyze("hello")


def test_the_free_text_caller_does_not_ask_for_json(client, monkeypatch, tmp_path):
    """transcribe_images returns the reply VERBATIM to the chapter-vision
    path. Asking for a JSON mime type there makes the model wrap a page of
    transcription in a JSON string and hands the quotes on as if they were the
    page. The old docstring claimed no such caller existed."""
    img = tmp_path / "p.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    bodies = []

    class _Res:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return usage_response(prompt=1, answer=1, text="page one")

    def fake_post(url, headers=None, json=None, timeout=None):
        bodies.append(json)
        return _Res()

    monkeypatch.delenv("GEMINI_JSON_MODE", raising=False)
    monkeypatch.setattr("shared.gemini_client.requests.post", fake_post)
    monkeypatch.setattr("shared.gemini_client._access_token", lambda: "tok")
    out = client.transcribe_images([img], "transcribe")
    assert out["text"] == "page one"
    assert "responseMimeType" not in bodies[0]["generationConfig"]
    # and the JSON callers still ask
    client.analyze("hello")
    assert bodies[1]["generationConfig"]["responseMimeType"] == "application/json"


def test_claude_accepts_the_same_keyword(monkeypatch):
    """process.py holds either client without knowing which, so a keyword one
    understands must not raise on the other — the mirror of GeminiClient
    accepting cache_prefix it cannot use."""
    import inspect
    from shared.claude_client import ClaudeClient
    sig = inspect.signature(ClaudeClient.analyze)
    assert "response_schema" in sig.parameters
    assert sig.parameters["response_schema"].default is None


def test_the_script_call_names_no_schema():
    """The call that failed is the one payload Vertex's schema dialect cannot
    describe: "assets" is an object whose KEYS the model invents per lesson,
    and responseSchema (the OpenAPI 3.0 subset) has no additionalProperties. A
    schema is closed, so one that omitted `assets` would return well-formed
    lessons with every generated illustration missing. Pinned so that nobody
    adds one without reading why it was left out."""
    import inspect
    from agent3_scripts import script_generator
    src = inspect.getsource(script_generator.generate_episode_script)
    assert "response_schema" not in src.split("client.analyze(")[1][:200], \
        "if this changes, _response_schema_enabled's reasoning must change with it"
    assert "additionalProperties" in inspect.getsource(script_generator), \
        "the reason must travel with the call site"
