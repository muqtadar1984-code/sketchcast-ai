"""Gemini on Vertex AI, with the same public surface as ClaudeClient.

Deliberately mirrors ClaudeClient method-for-method — analyze / analyze_image /
analyze_images_batch / transcribe_images / track_tokens / session_usage — so
worker/process.py can hold either object without caring which it has.

WHY VERTEX AND NOT AI STUDIO. The Gemini Developer API
(generativelanguage.googleapis.com + GEMINI_API_KEY) bills to a separate
account that GCP credits do NOT cover. Only aiplatform.googleapis.com is a
Google Cloud service. Measured 2026-08-11: a Vertex Gemini call cost ₹1.67 and
the promotional credit absorbed 100% of it. Every quickstart leads with the
AI Studio key; using it would quietly spend real money.

THINKING IS DISABLED, AND THAT IS A COST DECISION, NOT A STYLE ONE. Gemini
2.5 thinks by default and BILLS THINKING AS OUTPUT. Measured on a real
generation: 5,022 answer tokens against 1,917 thinking tokens — a 38% surcharge
on the tokens that dominate the bill (88% of SketchCast's per-generation cost is
output). This client asks for deterministic small-output JSON and wants none of
it, exactly as ClaudeClient sends thinking={"type": "disabled"}.
"""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path

import requests

from shared.claude_client import (
    ClaudeClient,
    _ensure_google_credentials,
    _merge_usage,
    logger,
)

# $/1M tokens, Vertex list price. Thinking tokens bill at the OUTPUT rate, so
# track_tokens folds thoughtsTokenCount into output rather than counting it
# separately — the bill does the same.
GEMINI_PRICING = {
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-2.0-flash": (0.10, 0.40),
}
_DEFAULT_PRICING = (0.30, 2.50)

_MEDIA = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "png": "image/png", "gif": "image/gif", "webp": "image/webp",
}


def gemini_model(kind: str | None = None) -> str:
    """Mirror of artifact_model(): global default, per-kind override."""
    if kind:
        specific = os.getenv("GEMINI_MODEL_" + kind.upper())
        if specific:
            return specific
    return os.getenv("GEMINI_MODEL", "gemini-2.5-flash")


def _json_mode() -> bool:
    """Ask Gemini for a JSON reply (responseMimeType). ON unless
    GEMINI_JSON_MODE is 0/false/no/off.

    It used to say "every caller of this client parses the reply as JSON, so
    there is no free-text caller to break". That was wrong: transcribe_images
    returns ``self._text(response).strip()`` verbatim to the chapter-vision
    path, so a JSON mime type there asks the model to wrap a page of
    transcription in a JSON string — quotes, escapes and all — and hands the
    result on as if it were prose. Callers that read free text now say so
    (``wants_json=False``) rather than relying on a claim about all of them.

    Note what this flag does NOT do. responseMimeType asks for JSON; it
    constrains nothing. Sara Hamaydeh's lesson (gen eb12963c, 2026-09-05) came
    back with it ON and still failed: 22,538 chars, 6,168 output tokens
    against a 30,000 cap, no truncation reported, unparseable at char 14,380.
    Constraint needs a schema — see _response_schema_enabled.
    """
    return (os.getenv("GEMINI_JSON_MODE") or "1").strip().lower() not in ("0", "false", "no", "off")


def _response_schema_enabled() -> bool:
    """May a caller's responseSchema actually be sent? OFF unless
    GEMINI_RESPONSE_SCHEMA is 1/true/yes/on.

    responseSchema is the real thing: Vertex CONSTRAINS decoding to the schema,
    so a malformed reply becomes impossible rather than repairable. Default OFF
    all the same, because it is a generation-behaviour change on the calls that
    earn the money, unproven in this project, and its failure modes are quiet
    ones — a schema the API dislikes 400s every lesson, and a schema that is
    merely too narrow silently drops the properties it forgot to name. OFF
    keeps today's behaviour byte-for-byte; the flag is how it gets proven on
    one account before it is anyone's default.

    WHY THE SCRIPT CALL PASSES NO SCHEMA. It is the call that failed, and it is
    the one payload that cannot be described here. Vertex's responseSchema is
    the OpenAPI 3.0 subset, which has no additionalProperties — and the
    semantic director's reply contains
        "assets": {"river_valley": "A river seen from above, one clear bend"}
    an object whose KEYS the model invents per lesson, one per illustration.
    A responseSchema is closed: a property it does not name cannot be emitted.
    So there is no schema that keeps `assets` — and a schema without it would
    return well-formed lessons with every generated illustration missing,
    which is the failure this codebase already treats as the worst kind
    (a lesson that ships wrong beats a lesson that fails). `target` is
    polymorphic for the same reason. Until the payload changes shape, the
    script call keeps the belt (claude_client's salvage) and no braces.
    """
    return (os.getenv("GEMINI_RESPONSE_SCHEMA") or "").strip().lower() in ("1", "true", "yes", "on")


def _access_token() -> str:
    """OAuth token from Application Default Credentials.

    Reuses ClaudeClient's credential materialiser so the service-account JSON is
    written to disk once per process and both providers share it.
    """
    _ensure_google_credentials()
    import google.auth
    import google.auth.transport.requests

    creds, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


# Gemini 2.5 Pro's real output ceiling. The truncation-retry guard used to
# compare against 32000: a request AT 32000 therefore got NO retry at all,
# and one at 30000 "doubled" to min(60000, 32000) — a 6% bump that truncated
# again. Long lessons (conversational, multi-chapter visual plans) died on it.
MAX_OUTPUT_TOKENS = 65536


class GeminiClient:
    """Vertex Gemini wrapper — same contract as ClaudeClient."""

    def __init__(self, model: str | None = None):
        self.backend = "gemini"
        self.model = model or gemini_model()
        self.project = os.getenv("VERTEX_PROJECT_ID", "").strip()
        if not self.project:
            raise RuntimeError("Gemini routing requires VERTEX_PROJECT_ID")
        self.region = os.getenv("VERTEX_REGION", "global").strip() or "global"
        self.session_usage = {
            "calls": 0, "input_tokens": 0, "output_tokens": 0,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "cost_usd": 0.0,
        }
        logger.info(
            "LLM backend=gemini project=%s region=%s model=%s",
            self.project, self.region, self.model,
        )

    # ── wire ──────────────────────────────────────────────────────────

    def _url(self) -> str:
        return (
            f"https://aiplatform.googleapis.com/v1/projects/{self.project}"
            f"/locations/{self.region}/publishers/google/models/{self.model}:generateContent"
        )

    def _post(self, parts: list[dict], system: str, max_tokens: int,
              response_schema: dict | None = None, wants_json: bool = True) -> dict:
        # `wants_json` is the CALLER's contract, not a preference: a caller
        # that returns the reply as prose (transcribe_images) must not ask for
        # a JSON mime type. The env flag is the second gate.
        json_on = wants_json and _json_mode()
        # A schema needs the mime type with it — Vertex rejects responseSchema
        # on a free-text reply — so the two travel together or not at all.
        schema_on = bool(response_schema) and json_on and _response_schema_enabled()
        body = {
            "contents": [{"role": "user", "parts": parts}],
            "systemInstruction": {"parts": [{"text": system}]},
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                # See module docstring: thinking bills as output, +38% measured,
                # and this workload gains nothing from it.
                "thinkingConfig": {"thinkingBudget": 0},
                # Ask for JSON. Two of the founder's lessons failed on
                # 2026-09-04, and Sara Hamaydeh's on 2026-09-05, with COMPLETE
                # replies whose JSON was malformed — asking is not constraining,
                # so the salvage in claude_client stays the belt.
                # GEMINI_JSON_MODE=0 switches it off without a deploy.
                **({"responseMimeType": "application/json"} if json_on else {}),
                # Constrained decoding, when the caller can name a schema and
                # the flag is on. See _response_schema_enabled for why the
                # script call is not one of those callers.
                **({"responseSchema": response_schema} if schema_on else {}),
            },
        }
        res = requests.post(
            self._url(),
            headers={
                "Authorization": f"Bearer {_access_token()}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=600,
        )
        if res.status_code == 429:
            raise _RateLimited(res.text)
        if schema_on and res.status_code == 400:
            # Vertex rejects a schema it cannot express (a keyword outside the
            # OpenAPI subset, a nesting depth, a model that does not support
            # constrained decoding) with a 400 on EVERY call — so a bad schema
            # is not a degraded lesson, it is an outage for whatever passed it.
            # Drop the schema once and let the reply take its chances with the
            # salvage, loudly, rather than fail the job.
            raise _SchemaRejected(res.text[:500])
        res.raise_for_status()
        return res.json()

    def _call(self, parts: list[dict], system: str, max_tokens: int, retries: int,
              response_schema: dict | None = None, wants_json: bool = True) -> dict:
        for attempt in range(retries):
            try:
                return self._post(parts, system, max_tokens, response_schema, wants_json)
            except _RateLimited:
                time.sleep(2 ** (attempt + 1))
            except _SchemaRejected as exc:
                logger.error("Vertex rejected responseSchema, retrying unconstrained: %s", exc)
                response_schema = None
        return self._post(parts, system, max_tokens, response_schema, wants_json)

    # ── response shaping ──────────────────────────────────────────────

    @staticmethod
    def _text(response: dict) -> str:
        """First text part of the first candidate, or "".

        A safety block returns candidates with no `content`, so never index
        blindly — the same trap as Claude's ThinkingBlock at content[0].
        """
        for cand in response.get("candidates") or []:
            for part in (cand.get("content") or {}).get("parts") or []:
                if "text" in part:
                    return part["text"]
        return ""

    @staticmethod
    def _finish_reason(response: dict) -> str:
        cands = response.get("candidates") or []
        return (cands[0].get("finishReason") if cands else "") or ""

    def track_tokens(self, response: dict) -> dict:
        """Usage + cost, in ClaudeClient's exact dict shape.

        thoughtsTokenCount is folded into output because Vertex bills it at the
        output rate. Reporting it separately would understate cost.
        """
        u = response.get("usageMetadata") or {}
        inp = int(u.get("promptTokenCount") or 0)
        answer = int(u.get("candidatesTokenCount") or 0)
        thoughts = int(u.get("thoughtsTokenCount") or 0)
        out = answer + thoughts
        in_rate, out_rate = GEMINI_PRICING.get(self.model, _DEFAULT_PRICING)
        cost = (inp * in_rate + out * out_rate) / 1_000_000
        usage = {
            "input_tokens": inp,
            "output_tokens": out,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "total_tokens": inp + out,
            "estimated_cost_usd": round(cost, 6),
        }
        self.session_usage["calls"] += 1
        self.session_usage["input_tokens"] += inp
        self.session_usage["output_tokens"] += out
        self.session_usage["cost_usd"] = round(self.session_usage["cost_usd"] + cost, 6)
        ClaudeClient._log_usage(usage)
        return usage

    # ── public surface (mirrors ClaudeClient) ─────────────────────────

    def analyze(
        self,
        prompt: str,
        system: str = "You are an expert educational content analyst.",
        max_tokens: int = 4096,
        retries: int = 3,
        cache_prefix: str | None = None,
        response_schema: dict | None = None,
    ) -> dict:
        """Text prompt in, parsed JSON out.

        `response_schema` is a Vertex responseSchema (the OpenAPI 3.0 subset)
        for callers whose payload is CLOSED — every property nameable up
        front. It is sent only when GEMINI_RESPONSE_SCHEMA is on, and dropped
        for one retry if Vertex rejects it, so a schema can never take a
        caller down. ClaudeClient.analyze accepts and ignores it, so
        process.py can keep holding either object.

        `cache_prefix` is accepted for interface compatibility and simply
        prepended. Vertex context caching is a separate API with its own minimum
        size and lifecycle — NOT Anthropic's cache_control — so it is not wired
        up here. The prefix still works; it just costs full price. That is a
        known cost gap against the Claude path, not an oversight.
        """
        parts = ([{"text": cache_prefix}] if cache_prefix else []) + [{"text": prompt}]
        response = self._call(parts, system, max_tokens, retries, response_schema)
        usage = self.track_tokens(response)
        parsed = ClaudeClient._extract_json(self._text(response))

        if (
            self._finish_reason(response) == "MAX_TOKENS"
            and max_tokens < MAX_OUTPUT_TOKENS
            and isinstance(parsed, dict) and set(parsed) == {"raw_text"}
        ):
            # Same contract as ClaudeClient: retry ONCE at double the budget
            # when the cap truncated the JSON badly enough that it won't parse.
            # Both attempts are billed and BOTH are reported, so caller
            # aggregates stay consistent with jobs.usage.
            response = self._call(parts, system,
                                  min(max_tokens * 2, MAX_OUTPUT_TOKENS), retries,
                                  response_schema)
            usage = _merge_usage(usage, self.track_tokens(response))
            parsed = ClaudeClient._extract_json(self._text(response))

        # `truncated` is the provider's own verdict on THIS (final) response.
        # Callers must not infer it from usage: usage is the SUM of both
        # attempts, so a successful retry reports far more output tokens than
        # the original cap and would read as a truncation that never happened.
        return {"data": parsed, "usage": usage,
                "truncated": self._finish_reason(response) == "MAX_TOKENS"}

    @staticmethod
    def _image_parts(image_paths: list[str | Path]) -> list[dict]:
        parts: list[dict] = []
        for src in image_paths:
            path = Path(src)
            if not path.exists():
                continue
            parts.append({
                "inlineData": {
                    "mimeType": _MEDIA.get(path.suffix.lower().lstrip("."), "image/png"),
                    "data": base64.standard_b64encode(path.read_bytes()).decode("utf-8"),
                }
            })
        return parts

    def analyze_image(
        self,
        image_source: str | bytes,
        prompt: str,
        max_tokens: int = 2048,
        retries: int = 3,
    ) -> dict:
        if isinstance(image_source, (str, Path)):
            path = Path(image_source)
            if not path.exists():
                return {
                    "data": {"error": f"Image not found: {image_source}"},
                    "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                              "estimated_cost_usd": 0},
                }
            img, ext = path.read_bytes(), path.suffix.lower().lstrip(".")
        else:
            img, ext = image_source, "png"

        parts = [
            {"inlineData": {"mimeType": _MEDIA.get(ext, "image/png"),
                            "data": base64.standard_b64encode(img).decode("utf-8")}},
            {"text": prompt},
        ]
        response = self._call(
            parts,
            "You are an expert educational content analyst specializing in visual materials.",
            max_tokens, retries,
        )
        usage = self.track_tokens(response)
        return {"data": ClaudeClient._extract_json(self._text(response)), "usage": usage}

    def analyze_images_batch(
        self,
        image_paths: list[str | Path],
        prompt: str,
        max_tokens: int = 4096,
        retries: int = 3,
    ) -> dict:
        parts = self._image_parts(image_paths)
        if not parts:
            return {"data": [], "usage": {"input_tokens": 0, "output_tokens": 0,
                                          "total_tokens": 0, "estimated_cost_usd": 0}}
        parts.append({"text": prompt})
        response = self._call(
            parts,
            "You are an expert educational content analyst specializing in visual materials.",
            max_tokens, retries,
        )
        usage = self.track_tokens(response)
        return {"data": ClaudeClient._extract_json(self._text(response)), "usage": usage}

    def transcribe_images(
        self,
        image_paths: list[str | Path],
        prompt: str,
        max_tokens: int = 8000,
        retries: int = 3,
    ) -> dict:
        parts = self._image_parts(image_paths)
        if not parts:
            return {"text": "", "usage": {"input_tokens": 0, "output_tokens": 0,
                                          "total_tokens": 0, "estimated_cost_usd": 0}}
        parts.append({"text": prompt})
        # THE free-text caller: this returns the reply verbatim to the
        # chapter-vision path, so it must not be asked for a JSON mime type —
        # that would wrap a page of transcription in a JSON string and pass
        # the quotes and escapes on as if they were the page.
        response = self._call(
            parts, "You are a precise transcriber of educational materials.",
            max_tokens, retries, wants_json=False,
        )
        usage = self.track_tokens(response)
        return {"text": self._text(response).strip(), "usage": usage}


class _RateLimited(Exception):
    """429 from Vertex — retried with backoff, mirroring RateLimitError."""


class _SchemaRejected(Exception):
    """400 from Vertex on a request that carried a responseSchema. Never
    surfaced to a caller: _call drops the schema and tries once more, because
    a schema the API dislikes fails EVERY call and would be an outage."""
