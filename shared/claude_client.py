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

    def __init__(self, model: str = "claude-sonnet-4-6"):
        self.client = Anthropic(api_key=_get_api_key())
        self.model = model

    # ── text analysis ────────────────────────────────────────────────

    def analyze(
        self,
        prompt: str,
        system: str = "You are an expert educational content analyst.",
        max_tokens: int = 4096,
        retries: int = 3,
    ) -> dict:
        """Send a text prompt, return parsed JSON dict."""
        response = self._call(system=system, prompt=prompt, max_tokens=max_tokens, retries=retries)
        text = response.content[0].text
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
        text = response.content[0].text
        usage = self.track_tokens(response)
        parsed = self._extract_json(text)
        return {"data": parsed, "usage": usage}

    # ── batch image analysis ─────────────────────────────────────────

    def analyze_images_batch(
        self,
        image_paths: list[str | Path],
        prompt: str,
        max_tokens: int = 4096,
        retries: int = 3,
    ) -> dict:
        """Send multiple images + prompt in a single Claude Vision call."""
        content: list[dict] = []
        for src in image_paths:
            path = Path(src)
            if not path.exists():
                continue
            with open(path, "rb") as f:
                img_bytes = f.read()
            ext = path.suffix.lower().lstrip(".")
            media_map = {
                "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "png": "image/png", "gif": "image/gif", "webp": "image/webp",
            }
            media_type = media_map.get(ext, "image/png")
            b64 = base64.standard_b64encode(img_bytes).decode("utf-8")
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": b64},
            })
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
        text = response.content[0].text
        usage = self.track_tokens(response)
        parsed = self._extract_json(text)
        return {"data": parsed, "usage": usage}

    # ── token tracking ───────────────────────────────────────────────

    def track_tokens(self, response) -> dict:
        """Extract token usage and estimate cost from a response."""
        inp = getattr(response.usage, "input_tokens", 0)
        out = getattr(response.usage, "output_tokens", 0)
        total = inp + out
        # Sonnet pricing: $3/M input, $15/M output
        cost = (inp * 3 + out * 15) / 1_000_000
        usage = {
            "input_tokens": inp,
            "output_tokens": out,
            "total_tokens": total,
            "estimated_cost_usd": round(cost, 6),
        }
        self._log_usage(usage)
        return usage

    # ── internals ────────────────────────────────────────────────────

    def _call(self, system: str, prompt: str, max_tokens: int, retries: int):
        messages = [{"role": "user", "content": prompt}]
        return self._call_messages(system=system, messages=messages, max_tokens=max_tokens, retries=retries)

    def _call_messages(self, system: str, messages: list, max_tokens: int, retries: int):
        for attempt in range(retries):
            try:
                return self.client.messages.create(
                    model=self.model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=messages,
                )
            except RateLimitError:
                wait = 2 ** (attempt + 1)
                time.sleep(wait)
        # Final attempt without catching
        return self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
        )

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
