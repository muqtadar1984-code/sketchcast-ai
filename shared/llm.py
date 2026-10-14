"""One factory: give it a language, get the right provider's client.

Call sites should never construct ClaudeClient or GeminiClient directly — the
whole point of the script-family split is that exactly one place decides, and
that place is here. See shared/model_routing.py for why the routing key is the
SCRIPT and not the language.
"""

from __future__ import annotations

import contextvars
import logging
import os

from shared.model_routing import ANTHROPIC, GEMINI, KIMI, provider_for

# THE SCRIPT FALLBACK SEAM. A teacher's job that has waited its whole
# rate-limit budget on the script model is sent back once more with
# params.script_fallback = true (worker/client.rate_limit_deferral); on that
# run the script call goes to SCRIPT_FALLBACK_MODEL instead of the script
# role's model. Today that is the lighter Gemini model (the same one #217
# switches to after a slow drop); a Bedrock-hosted Claude plugs in here
# later as the provider behind the same flag. "" disables the seam.
_SCRIPT_FALLBACK: contextvars.ContextVar[bool] = contextvars.ContextVar("script_fallback", default=False)
_log = logging.getLogger(__name__)


def set_script_fallback(on: bool) -> None:
    """The job thread says whether THIS run's script goes to the fallback model."""
    _SCRIPT_FALLBACK.set(bool(on))


def script_fallback_on() -> bool:
    return bool(_SCRIPT_FALLBACK.get())


def script_fallback_model() -> str:
    from shared import text_models
    return os.getenv("SCRIPT_FALLBACK_MODEL", text_models.FLASH_LITE_3_5).strip()


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
    if script_fallback_on() and script_fallback_model():
        from shared import phase_clock
        _log.warning("script fallback: this run's script goes to %s (the job spent its "
                     "rate-limit budget on the script role's model)", script_fallback_model())
        phase_clock.count("script_fallback")
        return client_for(language, model=script_fallback_model())
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
