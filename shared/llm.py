"""One factory: give it a language, get the right provider's client.

Call sites should never construct ClaudeClient or GeminiClient directly — the
whole point of the script-family split is that exactly one place decides, and
that place is here. See shared/model_routing.py for why the routing key is the
SCRIPT and not the language.
"""

from __future__ import annotations

from shared.model_routing import ANTHROPIC, GEMINI, KIMI, provider_for


def client_for(language: str | None, *, model: str | None = None, kind: str | None = None):
    """The client that should serve `language`.

    `model` pins a specific model on the chosen provider. `kind` asks the
    provider for its per-artifact-kind default (Haiku vs Sonnet on Anthropic,
    the GEMINI_MODEL_<KIND> override on Gemini) — pass one or the other.
    """
    provider = provider_for(language)

    if provider == ANTHROPIC:
        from shared.claude_client import ClaudeClient, artifact_model

        return ClaudeClient(model=model or (artifact_model(kind) if kind else None))

    if provider == GEMINI:
        from shared.gemini_client import GeminiClient, gemini_model

        return GeminiClient(model=model or (gemini_model(kind) if kind else None))

    if provider == KIMI:
        # Routable policy, no client. Raising is deliberate: no CJK language is
        # served today, so reaching here means a language shipped without its
        # provider. Falling back to Gemini would silently serve Chinese or
        # Japanese from a model never evaluated on Han scripts — the exact
        # mistake the script-family split exists to prevent.
        raise NotImplementedError(
            f"{language!r} routes to Kimi (Han script), which has no client yet. "
            "Implement shared/kimi_client.py or remove the language."
        )

    raise ValueError(f"unknown provider {provider!r} for language {language!r}")
