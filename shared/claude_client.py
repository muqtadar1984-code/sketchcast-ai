"""Shared Claude API client — reused by all agents."""

from __future__ import annotations

import base64
import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from anthropic import Anthropic, RateLimitError

TOKEN_LOG_PATH = Path(__file__).resolve().parent.parent / "token_log.json"

logger = logging.getLogger(__name__)

# $/1M tokens (standard list price). Sonnet 5's intro discount ($2/$10 through
# 2026-08-31) is deliberately NOT encoded — pricing at standard keeps the
# console spend figure a safe upper bound during the promo. Cached reads bill at
# 0.1x input and cache writes at 2x (1-hour TTL); track_tokens applies those.
MODEL_PRICING = {
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-opus-4-8": (5.0, 25.0),
}
_DEFAULT_PRICING = (3.0, 15.0)


# ── backend selection ────────────────────────────────────────────────────
# The same Claude models are reachable two ways:
#   "anthropic" — api.anthropic.com, billed to the Anthropic account (default)
#   "vertex"    — Google Cloud Vertex AI, billed to the GCP project
#
# Vertex exists for two reasons. It lets GCP credits pay for generation, and it
# is a SECOND BILLING PATH: on 2026-08-10 the Anthropic balance hit zero and
# every generation failed with a 400 for ~13 hours. One provider is one point of
# failure no amount of retry logic survives.
#
# Flip with LLM_BACKEND. Nothing else about the request changes — Vertex serves
# the same Messages API, and everything this client uses (vision, 1h prompt
# caching via explicit cache_control, token counts) is supported there.
def llm_backend() -> str:
    return os.getenv("LLM_BACKEND", "anthropic").strip().lower()


# Vertex publishes CURRENT-generation models under the bare first-party id, and
# dated snapshots under an "@version" separator — NOT the "-YYYYMMDD" suffix the
# first-party API uses. Anything absent from this map is sent through unchanged.
#
# Haiku 4.5 is the one we cannot confirm without a live call, so it is listed
# explicitly and every entry is overridable from the environment: a wrong id is
# a 404, and fixing it should be a Railway variable edit, not a redeploy.
_VERTEX_MODEL_IDS = {
    "claude-haiku-4-5": "claude-haiku-4-5@20251001",
}

_CREDENTIALS_PATH: str | None = None


def _vertex_model_map() -> dict[str, str]:
    """The built-in id map, overlaid with VERTEX_MODEL_MAP (JSON) if present."""
    mapping = dict(_VERTEX_MODEL_IDS)
    raw = os.getenv("VERTEX_MODEL_MAP", "").strip()
    if raw:
        try:
            mapping.update(json.loads(raw))
        except json.JSONDecodeError as exc:
            # Fail loudly at construction. A malformed map would silently leave
            # the wrong model id on the wire, and every call would 404 anyway —
            # better to say why than to let it look like an outage.
            raise RuntimeError(f"VERTEX_MODEL_MAP is not valid JSON: {exc}") from exc
    return mapping


def _ensure_google_credentials() -> None:
    """Materialise a service-account JSON from the environment, once per process.

    google-auth's default credential chain wants a FILE path in
    GOOGLE_APPLICATION_CREDENTIALS, but Railway (like most PaaS) only holds
    strings. Write the JSON to a private temp file and point the variable at it.
    A pre-set GOOGLE_APPLICATION_CREDENTIALS always wins, and with neither set
    we fall through to ambient ADC (gcloud login locally, metadata server on GCE).
    """
    global _CREDENTIALS_PATH
    if os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
        return
    raw = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", "").strip()
    if not raw:
        return
    if _CREDENTIALS_PATH is None:
        fd, path = tempfile.mkstemp(prefix="gcp-sa-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(raw)
        os.chmod(path, 0o600)
        _CREDENTIALS_PATH = path
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = _CREDENTIALS_PATH


def _build_client(backend: str):
    """The SDK client for `backend`. Same messages surface either way."""
    if backend == "vertex":
        from anthropic import AnthropicVertex  # extra: anthropic[vertex]

        project = os.getenv("VERTEX_PROJECT_ID", "").strip()
        if not project:
            raise RuntimeError("LLM_BACKEND=vertex requires VERTEX_PROJECT_ID")
        _ensure_google_credentials()
        # "global" spreads across regions and is the least likely to hit a
        # per-region capacity error; override for data-residency requirements.
        region = os.getenv("VERTEX_REGION", "global").strip() or "global"
        return AnthropicVertex(project_id=project, region=region)
    return Anthropic(api_key=_get_api_key())


def artifact_model(kind: str | None = None) -> str:
    """Model for CONTENT GENERATION (chapter analysis + artifact authoring) — Haiku
    by default, ~1/3 the price of Sonnet. Ingestion (chapter detection, vision OCR)
    and the chapter-content check deliberately stay on the general CLAUDE_MODEL
    (Sonnet), since a bad read there poisons every downstream artifact.

    Override globally with ARTIFACT_MODEL, or per artifact kind with
    ARTIFACT_MODEL_<KIND> (e.g. ARTIFACT_MODEL_EXAM_PAPER=claude-sonnet-5) to bump a
    reasoning-heavy kind back to Sonnet if Haiku dips. Revert generation entirely
    with ARTIFACT_MODEL=claude-sonnet-4-6."""
    if kind:
        specific = os.getenv("ARTIFACT_MODEL_" + kind.upper())
        if specific:
            return specific
    return os.getenv("ARTIFACT_MODEL", "claude-haiku-4-5")


def _first_text(response) -> str:
    """The first TEXT block's text. Extended-thinking models (e.g. Sonnet 5)
    put a ThinkingBlock at content[0] which has no `.text`, so never index
    content[0] blindly — scan for the first block that actually carries text."""
    for block in response.content:
        txt = getattr(block, "text", None)
        if txt is not None:
            return txt
    return ""


def _merge_usage(a: dict, b: dict) -> dict:
    """Sum two track_tokens dicts (the truncation-retry path bills two calls;
    the RETURNED usage must cover both so caller aggregates match jobs.usage)."""
    merged = {k: a.get(k, 0) + b.get(k, 0) for k in set(a) | set(b)}
    if "estimated_cost_usd" in merged:
        merged["estimated_cost_usd"] = round(merged["estimated_cost_usd"], 6)
    return merged


def _get_api_key() -> str:
    """Read API key from Streamlit secrets first, then env var."""
    try:
        import streamlit as st
        return st.secrets["ANTHROPIC_API_KEY"]
    except Exception:
        pass
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY not found. Set it in .streamlit/secrets.toml "
            "or as an environment variable."
        )
    return key


def _rebalance_json(text: str):
    """Close structures the model MIS-NESTED, and only those.

    Measured on a real reply: it ended `"actions": []}]}}` where the chapters
    array still needed its `]`, so a `}` arrived while an array was open. The
    text was complete — 2,735 output tokens against a 32,000 cap — just built
    wrong.

    Three consecutive live replies were malformed this way — 1,901, 2,735 and
    3,998 output tokens, every one against a 32,000 cap — so refusing to
    close them fails a whole lesson over a bracket. What must NOT be closed
    is a reply that stopped mid-thought, because completing that fabricates a
    short lesson every downstream check would wave through. The tell is how
    the text ENDS: inside a string, or on a `,`/`:`, means a value was
    severed. Ending on a complete value means every element present is whole
    and only outer closers are missing.

    Truncation proper is not this function's job — it cannot see the token
    count. generate_episode_script compares output_tokens against the cap and
    fails loudly there, which is the only place the measurement exists.

    Returns the corrected source, or None to leave the failure loud.
    """
    out, stack = [], []
    ins = esc = mended = False
    for c in text:
        if esc:
            out.append(c)
            esc = False
            continue
        if ins:
            out.append(c)
            if c == "\\":
                esc = True
            elif c == '"':
                ins = False
            continue
        if c == '"':
            ins = True
            out.append(c)
            continue
        if c in "{[":
            stack.append(c)
        elif c in "}]":
            want = "{" if c == "}" else "["
            if want in stack:
                while stack and stack[-1] != want:
                    out.append("]" if stack[-1] == "[" else "}")
                    stack.pop()
                    mended = True
                stack.pop()
        out.append(c)
    if ins or esc:
        return None
    tail = "".join(out).rstrip()
    if not tail or tail[-1] in ",:":
        return None          # a value was severed — leave the failure loud
    if not stack:
        return "".join(out) if mended else None
    while stack:
        out.append("]" if stack.pop() == "[" else "}")
    return "".join(out)


def _repair_json(text: str):
    """Salvage a reply that is COMPLETE but slightly malformed.

    Measured failures, all of which reached callers as the misleading
    "produced no segments ... almost certainly cut off at the output-token
    cap" (the reply was neither cut off nor near the cap):
      * a stray closing bracket on the tail: ...]}]}]}
      * SSML attribute quotes inside a JSON string: <break time="0.3s"/>
      * a trailing comma before } or ]
    Returns the parsed object, or None if it is genuinely unparseable.
    """
    import re as _re
    text = (text or "").strip()
    if not text:
        return None
    # _extract_json strips fences before it gets here, but this is module
    # -level and callable directly — a fenced reply returning None looks
    # exactly like an unparseable one, which is a trap worth closing.
    if text.startswith("```"):
        text = _re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = _re.sub(r"\s*```$", "", text).strip()
    candidates = []
    # 1. SSML quotes are the one nested-quote case this model emits
    fixed = _re.sub(r'<break\s+time\s*=\s*"([^"]*)"\s*/?>',
                    r"<break time='\1'/>", text)
    candidates.append(fixed)
    # 2. trailing commas
    decommaed = _re.sub(r",\s*([}\]])", r"\1", fixed)
    candidates.append(decommaed)
    # 3. mis-nested closers (a `}` arriving while an array is still open)
    for src in (fixed, decommaed):
        mended = _rebalance_json(src)
        if mended:
            candidates.append(mended)
    # 3. a stray closer sits at the very end of an otherwise good reply, so
    #    walk the tail back — but ONLY over structural punctuation. Trimming
    #    CONTENT would turn a genuinely truncated reply into a plausible,
    #    silently incomplete lesson, which is worse than the loud failure it
    #    replaces: a short script would sail past every downstream check.
    #
    #    Re-balancing the tail needs one closer put BACK (…"chapters": []}]}
    #    is right once the stray ] goes and a } returns). Appending is the
    #    risky direction, though — it is also what would complete a reply cut
    #    off after a whole element — so it is refused once a severing comma
    #    has been stripped, which is the tell that a sibling was lost.
    for cand in list(candidates):
        i, severed = len(cand), False
        while i > 0 and cand[i - 1] in "}] \t\r\n,":
            severed = severed or cand[i - 1] == ","
            i -= 1
            head = cand[:i]
            candidates.append(head)
            if not severed:
                for closer in ("}", "]}", "}]}", "]}]}"):
                    candidates.append(head + closer)
    for cand in candidates:
        try:
            out = json.loads(cand)
        except Exception:
            continue
        if isinstance(out, (dict, list)) and out:
            return out
    return None


class ClaudeClient:
    """Reusable Claude API wrapper with retry, JSON parsing, and token logging."""

    def __init__(self, model: str | None = None):
        self.backend = llm_backend()
        self.client = _build_client(self.backend)
        # Model is env-selectable (CLAUDE_MODEL) so it can be flipped to
        # claude-sonnet-5 in Railway without a code change; default stays on 4.6.
        self.model = model or os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
        # `model` stays the CANONICAL first-party id and `_api_model` is what
        # goes on the wire. They differ on Vertex, and keeping them apart is
        # load-bearing: track_tokens prices via MODEL_PRICING[self.model], so
        # collapsing the two would miss the pricing table for any id carrying a
        # Vertex "@version" suffix, silently fall back to _DEFAULT_PRICING
        # (Sonnet's $3/$15), and bill every Haiku call into jobs.usage at ~3x —
        # straight into the cost basis the financial model is built on.
        self._api_model = (
            _vertex_model_map().get(self.model, self.model)
            if self.backend == "vertex"
            else self.model
        )
        if self.backend == "vertex":
            logger.info(
                "LLM backend=vertex project=%s region=%s model %s -> %s",
                os.getenv("VERTEX_PROJECT_ID"), os.getenv("VERTEX_REGION", "global"),
                self.model, self._api_model,
            )
        # Per-instance running total across every call this client makes — the
        # worker persists it per job (jobs.usage) so spend is attributable to a
        # user/book/generation instead of vanishing with the container.
        self.session_usage = {
            "calls": 0, "input_tokens": 0, "output_tokens": 0,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "cost_usd": 0.0,
        }

    # ── text analysis ────────────────────────────────────────────────

    def analyze(
        self,
        prompt: str,
        system: str = "You are an expert educational content analyst.",
        max_tokens: int = 4096,
        retries: int = 3,
        cache_prefix: str | None = None,
    ) -> dict:
        """Send a text prompt, return parsed JSON dict.

        `cache_prefix` is a large, STABLE block (e.g. the chapter grounding shared
        by all of a book's artifacts) placed first and marked with cache_control,
        so the first artifact pays to write it and the rest read it at ~0.1x. It
        must be byte-identical across calls to hit, and comfortably exceed the
        model's minimum cacheable prefix (~2K tokens) — below that it silently
        won't cache (no error), so short prefixes just behave like a normal call.
        """
        if cache_prefix:
            messages = [{
                "role": "user",
                "content": [
                    {"type": "text", "text": cache_prefix, "cache_control": {"type": "ephemeral", "ttl": "1h"}},
                    {"type": "text", "text": prompt},
                ],
            }]
        else:
            messages = [{"role": "user", "content": prompt}]
        response = self._call_messages(system=system, messages=messages, max_tokens=max_tokens, retries=retries)
        text = _first_text(response)
        usage = self.track_tokens(response)
        parsed = self._extract_json(text)
        if (
            getattr(response, "stop_reason", "") == "max_tokens"
            and max_tokens < 32000
            and isinstance(parsed, dict) and set(parsed) == {"raw_text"}
        ):
            # The reply hit the output cap AND the JSON is truly truncated (a
            # root object/array that still parses is structurally complete —
            # only trailing prose got cut, keep it). Unparseable means callers
            # would see zero data (the "produced no segments" script failure):
            # retry ONCE at double the budget, STREAMED — the SDK refuses long
            # non-streaming calls that could outlive its HTTP timeout. Reusing
            # `messages` keeps the cache_control block, so the retry re-reads
            # the 1h prompt cache instead of re-writing it. Both attempts are
            # billed and BOTH are reported in the returned usage, so callers'
            # aggregates stay consistent with session_usage/jobs.usage.
            response = self._stream_messages(
                system=system, messages=messages,
                max_tokens=min(max_tokens * 2, 32000), retries=retries,
            )
            text = _first_text(response)
            usage = _merge_usage(usage, self.track_tokens(response))
            parsed = self._extract_json(text)
        # See GeminiClient.analyze: usage SUMS both attempts, so it can never
        # be used to infer truncation. This is the provider's verdict on the
        # final response.
        return {"data": parsed, "usage": usage,
                "truncated": getattr(response, "stop_reason", "") == "max_tokens"}

    # ── image analysis ───────────────────────────────────────────────

    def analyze_image(
        self,
        image_source: str | bytes,
        prompt: str,
        max_tokens: int = 2048,
        retries: int = 3,
    ) -> dict:
        """Send image + prompt to Claude Vision, return parsed result."""
        # Build base64 from file path or raw bytes
        if isinstance(image_source, (str, Path)):
            path = Path(image_source)
            if not path.exists():
                return {
                    "data": {"error": f"Image not found: {image_source}"},
                    "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "estimated_cost_usd": 0},
                }
            with open(path, "rb") as f:
                img_bytes = f.read()
            ext = path.suffix.lower().lstrip(".")
        else:
            img_bytes = image_source
            ext = "png"

        media_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp"}
        media_type = media_map.get(ext, "image/png")
        b64 = base64.standard_b64encode(img_bytes).decode("utf-8")

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                    {"type": "text", "text": prompt},
                ],
            }
        ]

        response = self._call_messages(
            system="You are an expert educational content analyst specializing in visual materials.",
            messages=messages,
            max_tokens=max_tokens,
            retries=retries,
        )
        text = _first_text(response)
        usage = self.track_tokens(response)
        parsed = self._extract_json(text)
        return {"data": parsed, "usage": usage}

    # ── batch image analysis ─────────────────────────────────────────

    @staticmethod
    def _image_content(image_paths: list[str | Path]) -> list[dict]:
        """Build the image content blocks for a vision message."""
        content: list[dict] = []
        media_map = {
            "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "png": "image/png", "gif": "image/gif", "webp": "image/webp",
        }
        for src in image_paths:
            path = Path(src)
            if not path.exists():
                continue
            with open(path, "rb") as f:
                img_bytes = f.read()
            media_type = media_map.get(path.suffix.lower().lstrip("."), "image/png")
            b64 = base64.standard_b64encode(img_bytes).decode("utf-8")
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": b64},
            })
        return content

    def analyze_images_batch(
        self,
        image_paths: list[str | Path],
        prompt: str,
        max_tokens: int = 4096,
        retries: int = 3,
    ) -> dict:
        """Send multiple images + prompt in a single Claude Vision call."""
        content = self._image_content(image_paths)
        if not content:
            # No valid images found — return empty result
            return {
                "data": [],
                "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "estimated_cost_usd": 0},
            }
        content.append({"type": "text", "text": prompt})
        messages = [{"role": "user", "content": content}]
        response = self._call_messages(
            system="You are an expert educational content analyst specializing in visual materials.",
            messages=messages,
            max_tokens=max_tokens,
            retries=retries,
        )
        text = _first_text(response)
        usage = self.track_tokens(response)
        parsed = self._extract_json(text)
        return {"data": parsed, "usage": usage}

    def transcribe_images(
        self,
        image_paths: list[str | Path],
        prompt: str,
        max_tokens: int = 8000,
        retries: int = 3,
    ) -> dict:
        """Vision call that returns PLAIN TEXT (no JSON parsing) — for
        transcription, where wrapping long output in JSON risks truncation
        breaking the parse."""
        content = self._image_content(image_paths)
        if not content:
            return {
                "text": "",
                "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "estimated_cost_usd": 0},
            }
        content.append({"type": "text", "text": prompt})
        messages = [{"role": "user", "content": content}]
        response = self._call_messages(
            system="You are a precise transcriber of educational materials.",
            messages=messages,
            max_tokens=max_tokens,
            retries=retries,
        )
        usage = self.track_tokens(response)
        return {"text": _first_text(response).strip(), "usage": usage}

    # ── token tracking ───────────────────────────────────────────────

    def track_tokens(self, response) -> dict:
        """Extract token usage and estimate cost from a response.

        Model-aware (MODEL_PRICING) and cache-aware: cached reads bill at 0.1x
        input, cache writes at 2x (1-hour TTL). `input_tokens` from the API is
        already the UNCACHED remainder, so the three input buckets are additive."""
        u = response.usage
        inp = getattr(u, "input_tokens", 0) or 0
        out = getattr(u, "output_tokens", 0) or 0
        cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(u, "cache_creation_input_tokens", 0) or 0
        in_rate, out_rate = MODEL_PRICING.get(self.model, _DEFAULT_PRICING)
        cost = (
            inp * in_rate
            + cache_read * in_rate * 0.1
            + cache_write * in_rate * 2.0
            + out * out_rate
        ) / 1_000_000
        total = inp + out + cache_read + cache_write
        usage = {
            "input_tokens": inp,
            "output_tokens": out,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_write,
            "total_tokens": total,
            "estimated_cost_usd": round(cost, 6),
        }
        self.session_usage["calls"] += 1
        self.session_usage["input_tokens"] += inp
        self.session_usage["output_tokens"] += out
        self.session_usage["cache_read_input_tokens"] += cache_read
        self.session_usage["cache_creation_input_tokens"] += cache_write
        self.session_usage["cost_usd"] = round(self.session_usage["cost_usd"] + cost, 6)
        self._log_usage(usage)
        return usage

    # ── internals ────────────────────────────────────────────────────

    def _create(self, system: str, messages: list, max_tokens: int):
        """One API call. Extended-thinking is DISABLED: this client is built for
        deterministic small-output JSON (tiny max_tokens, JSON in the first
        block), and thinking-on models (e.g. Sonnet 5) otherwise emit a
        ThinkingBlock and consume the token budget before the answer. Fall back
        to a plain call if a model rejects the thinking parameter."""
        try:
            return self.client.messages.create(
                model=self._api_model, max_tokens=max_tokens, system=system,
                messages=messages, thinking={"type": "disabled"},
            )
        except TypeError:
            pass  # SDK too old for the param
        except Exception as exc:  # noqa: BLE001 — model rejected the param
            if "thinking" not in str(exc).lower():
                raise
        return self.client.messages.create(
            model=self._api_model, max_tokens=max_tokens, system=system, messages=messages,
        )

    def _call_messages(self, system: str, messages: list, max_tokens: int, retries: int):
        for attempt in range(retries):
            try:
                return self._create(system, messages, max_tokens)
            except RateLimitError:
                wait = 2 ** (attempt + 1)
                time.sleep(wait)
        # Final attempt without catching
        return self._create(system, messages, max_tokens)

    def _create_stream(self, system: str, messages: list, max_tokens: int):
        """One STREAMED API call — the truncation-retry path only. Streaming is
        required for large max_tokens (the SDK refuses long non-streaming calls
        that could outlive its HTTP timeout) and get_final_message() returns the
        same Message shape, so _first_text/track_tokens work unchanged. Mirrors
        _create's thinking-disabled contract exactly (see that docstring)."""
        kwargs = dict(model=self._api_model, max_tokens=max_tokens, system=system, messages=messages)
        try:
            with self.client.messages.stream(**kwargs, thinking={"type": "disabled"}) as s:
                return s.get_final_message()
        except TypeError:
            pass  # SDK too old for the param
        except Exception as exc:  # noqa: BLE001 — model rejected the param
            if "thinking" not in str(exc).lower():
                raise
        with self.client.messages.stream(**kwargs) as s:
            return s.get_final_message()

    def _stream_messages(self, system: str, messages: list, max_tokens: int, retries: int):
        """_call_messages, but streamed — same RateLimitError backoff loop."""
        for attempt in range(retries):
            try:
                return self._create_stream(system, messages, max_tokens)
            except RateLimitError:
                time.sleep(2 ** (attempt + 1))
        return self._create_stream(system, messages, max_tokens)

    @staticmethod
    def _extract_json(text: str) -> dict | list:
        """Extract JSON from Claude's response, handling markdown fences."""
        text = text.strip()
        # Strip markdown fences
        if text.startswith("```"):
            lines = text.split("\n")
            lines = lines[1:]  # remove opening fence
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Try to find JSON within the text
            for start_char, end_char in [("{", "}"), ("[", "]")]:
                start = text.find(start_char)
                end = text.rfind(end_char)
                if start != -1 and end > start:
                    try:
                        return json.loads(text[start:end + 1])
                    except json.JSONDecodeError:
                        continue
            repaired = _repair_json(text)
            if repaired is not None:
                logger.warning("model JSON was malformed; repaired it")
                return repaired
            return {"raw_text": text}

    @staticmethod
    def _log_usage(usage: dict):
        """Append usage entry to the token log file."""
        entry = {**usage, "timestamp": datetime.now(timezone.utc).isoformat()}
        try:
            if TOKEN_LOG_PATH.exists():
                with open(TOKEN_LOG_PATH, "r") as f:
                    log = json.load(f)
            else:
                log = []
            log.append(entry)
            with open(TOKEN_LOG_PATH, "w") as f:
                json.dump(log, f, indent=2)
        except Exception:
            pass  # Don't crash if logging fails
