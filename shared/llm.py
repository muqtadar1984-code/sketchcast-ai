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


def script_client(language: str | None):
    """The client for the EPISODE SCRIPT — the one call that writes a video.

    Routes exactly like `client_for`, then pins the `script` role's model, and
    ONLY on the Gemini path, for the reason `analysis_client` gives: routing
    belongs to the language, and a Gemini id handed to Claude or Kimi is a 404
    at best.

    Split out on 2026-09-09. The artifact model's script length is bimodal on
    identical input — 219.4 chars per analysed topic on one run and 108.0 on
    the next, a 5.9-minute lesson and a 2.4-minute one from the same article.
    The thin drafts are not missing topics; one covered 30 of 30 after its
    retry and was still refused by the depth gate. One call per part, and the
    call the entire video is made of, so it can afford a model that holds it.
    """
    from shared import text_models

    if provider_for(language) != GEMINI:
        return client_for(language)
    return client_for(language, model=text_models.resolve(text_models.SCRIPT).id)


def analysis_client(language: str | None):
    """The client for the COMBINED ANALYSIS — the one call that turns a
    chapter's text into the concept list every artifact is written from.

    Routes exactly like `client_for`, then pins the `analysis` role's model —
    but ONLY on the Gemini path. The routing decision belongs to the language
    (Arabic goes to Claude, Han scripts to Kimi) and a Gemini id handed to
    another provider's client would be a 404 at best; the role names a Gemini
    model, so it is Gemini's to apply.

    Split out on 2026-09-08. The artifact model returned this call's JSON
    malformed and the analyzer logged "combined analysis returned no concepts
    for a 2378-word chunk" — a kit built, completed and reached review
    "grounded in nothing but the title", 12 segments where the same article
    gives 35. One call per part, so it can afford a model that holds it.
    """
    from shared import text_models

    if provider_for(language) != GEMINI:
        return client_for(language)
    return client_for(language, model=text_models.resolve(text_models.ANALYSIS).id)
