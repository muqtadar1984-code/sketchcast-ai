"""Shared Claude API client — reused by all agents."""

from __future__ import annotations

import base64
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from anthropic import Anthropic, RateLimitError

TOKEN_LOG_PATH = Path(__file__).resolve().parent.parent / "token_log.json"

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


def _first_text(response) -> str:
    """The first TEXT block's text. Extended-thinking models (e.g. Sonnet 5)
    put a ThinkingBlock at content[0] which has no `.text`, so never index
    content[0] blindly — scan for the first block that actually carries text."""
    for block in response.content:
        txt = getattr(block, "text", None)
        if txt is not None:
            return txt
    return ""


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


class ClaudeClient:
    """Reusable Claude API wrapper with retry, JSON parsing, and token logging."""

    def __init__(self, model: str | None = None):
        self.client = Anthropic(api_key=_get_api_key())
        # Model is env-selectable (CLAUDE_MODEL) so it can be flipped to
        # claude-sonnet-5 in Railway without a code change; default stays on 4.6.
        self.model = model or os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
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
            response = self._call_messages(system=system, messages=messages, max_tokens=max_tokens, retries=retries)
        else:
            response = self._call(system=system, prompt=prompt, max_tokens=max_tokens, retries=retries)
        text = _first_text(response)
        usage = self.track_tokens(response)
        parsed = self._extract_json(text)
        return {"data": parsed, "usage": usage}

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

    def _call(self, system: str, prompt: str, max_tokens: int, retries: int):
        messages = [{"role": "user", "content": prompt}]
        return self._call_messages(system=system, messages=messages, max_tokens=max_tokens, retries=retries)

    def _create(self, system: str, messages: list, max_tokens: int):
        """One API call. Extended-thinking is DISABLED: this client is built for
        deterministic small-output JSON (tiny max_tokens, JSON in the first
        block), and thinking-on models (e.g. Sonnet 5) otherwise emit a
        ThinkingBlock and consume the token budget before the answer. Fall back
        to a plain call if a model rejects the thinking parameter."""
        try:
            return self.client.messages.create(
                model=self.model, max_tokens=max_tokens, system=system,
                messages=messages, thinking={"type": "disabled"},
            )
        except TypeError:
            pass  # SDK too old for the param
        except Exception as exc:  # noqa: BLE001 — model rejected the param
            if "thinking" not in str(exc).lower():
                raise
        return self.client.messages.create(
            model=self.model, max_tokens=max_tokens, system=system, messages=messages,
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
