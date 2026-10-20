"""How "thinking off" is spelled is a property of the MODEL.

The 5.5 generation refuses {"type": "disabled"} with a 400 naming
{"type": "between_tools"} (live on Bedrock, 2026-10-18). Before this, the
non-streamed call fell back to sending NO thinking parameter (thinking left
to the model's default, billed as output) and the streamed call — the video
script's transport — failed outright, since the 400 arrives when the stream
is opened. Pinned here: the spelling per model, and the one-time switch for a
model that answers with that 400 on either transport.
"""
from __future__ import annotations

import types

import pytest

from shared import claude_client as cc
from shared.claude_client import ClaudeClient, _thinking_off_for

REFUSAL = ('Error code: 400 - {\'message\': \'To turn thinking off on this model, send "thinking": '
           '{"type": "between_tools"} instead of {"type": "disabled"}.\'}')


def _msg(text="{\"ok\": true}"):
    return types.SimpleNamespace(content=[types.SimpleNamespace(text=text)], stop_reason="end_turn",
                                 usage=types.SimpleNamespace(input_tokens=5, output_tokens=3,
                                                             cache_read_input_tokens=0, cache_creation_input_tokens=0))


class _Messages:
    """Refuses {"type": "disabled"} as the 5.5 generation does; records every call."""

    def __init__(self):
        self.calls = []

    def create(self, **kw):
        self.calls.append(("create", kw.get("thinking")))
        if kw.get("thinking") == {"type": "disabled"}:
            raise RuntimeError(REFUSAL)
        return _msg()

    def stream(self, **kw):
        self.calls.append(("stream", kw.get("thinking")))
        refused = kw.get("thinking") == {"type": "disabled"}

        class _Mgr:
            def __enter__(self_inner):
                if refused:                      # the request goes out when the stream opens
                    raise RuntimeError(REFUSAL)
                return types.SimpleNamespace(get_final_message=_msg)

            def __exit__(self_inner, *a):
                return False

        return _Mgr()


def _client(model):
    c = ClaudeClient.__new__(ClaudeClient)
    c.model, c._api_model, c.backend = model, model, "anthropic"
    c._thinking_off = _thinking_off_for(model)
    c.client = types.SimpleNamespace(messages=_Messages())
    return c


@pytest.mark.parametrize("model,spelling", [
    ("claude-sonnet-5-5", "between_tools"), ("claude-haiku-5-5", "disabled"),
    ("claude-opus-5-5", "between_tools"), ("claude-fable-5-1", "between_tools"),
    ("claude-sonnet-4-6", "disabled"), ("claude-sonnet-5", "disabled"), ("claude-haiku-4-5", "disabled"),
    ("claude-opus-4-8", "disabled"),
])
def test_the_spelling_is_the_models(model, spelling):
    assert _thinking_off_for(model) == {"type": spelling}


def test_a_known_55_model_is_spelled_right_first_time_on_both_transports():
    c = _client("claude-sonnet-5-5")
    c._create("s", [{"role": "user", "content": "x"}], 50)
    c._create_stream("s", [{"role": "user", "content": "x"}], 30000)
    assert c.client.messages.calls == [("create", {"type": "between_tools"}), ("stream", {"type": "between_tools"})]


def test_an_unknown_model_that_asks_is_switched_once_non_streamed():
    c = _client("claude-sonnet-9")          # not in the prefixes: learns from the 400
    assert c._thinking_off == {"type": "disabled"}
    c._create("s", [{"role": "user", "content": "x"}], 50)
    assert c.client.messages.calls == [("create", {"type": "disabled"}), ("create", {"type": "between_tools"})]
    c._create("s", [{"role": "user", "content": "x"}], 50)
    assert c.client.messages.calls[-1] == ("create", {"type": "between_tools"}), "remembered for the client"


def test_an_unknown_model_that_asks_is_switched_once_streamed():
    c = _client("claude-sonnet-9")
    out = c._create_stream("s", [{"role": "user", "content": "x"}], 30000)
    assert out.content[0].text == '{"ok": true}'
    assert c.client.messages.calls == [("stream", {"type": "disabled"}), ("stream", {"type": "between_tools"})]


def test_an_unrelated_stream_error_still_raises():
    c = _client("claude-sonnet-4-6")

    class _Boom(_Messages):
        def stream(self, **kw):
            class _M:
                def __enter__(self_inner):
                    raise RuntimeError("overloaded")

                def __exit__(self_inner, *a):
                    return False
            return _M()

    c.client = types.SimpleNamespace(messages=_Boom())
    with pytest.raises(RuntimeError, match="overloaded"):
        c._create_stream("s", [{"role": "user", "content": "x"}], 30000)


def test_a_client_built_without_init_has_the_default():
    c = ClaudeClient.__new__(ClaudeClient)
    assert c._thinking_off == {"type": "disabled"}
    assert cc._THINKING_DISABLED == {"type": "disabled"}
