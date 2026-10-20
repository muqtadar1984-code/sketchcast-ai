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

HOW that suppression is spelled now depends on the model, which is why the
model id and the thinking dialect are resolved TOGETHER by
shared/text_models.py rather than hardcoded here. `thinkingBudget: 0` — the
line this file carried for a year — is not supported on Gemini 3 at all, so an
id swap alone would have kept working while silently paying for thinking again;
on 3.x the same intent is `thinkingLevel: MINIMAL`, and sending both fields in
one request is a documented 400. `_post` therefore asks the registry for the
fragment instead of writing one.
"""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path

import requests

from shared import model_env, text_models
from shared.claude_client import (
    _RETRY_STATUS,
    _USAGE_LOCK,
    ClaudeClient,
    _backoff_seconds,
    _ensure_google_credentials,
    _merge_usage,
    logger,
)

# $/1M tokens, Vertex list price. Thinking tokens bill at the OUTPUT rate, so
# track_tokens folds thoughtsTokenCount into output rather than counting it
# separately — the bill does the same.
#
# NO LONGER THE FIRST PLACE LOOKED. This table hardcoded only the two 2.5
# rates, so every id the October 2026 migration moves to would have been costed
# at 2.5 Flash's price — silently, forever, in the ledger the financial model
# is built from. `pricing_for` asks shared/text_models.REGISTRY first, where a
# model's rate lives with the rest of its facts; this stays as the fallback for
# ids that PREDATE the registry and are not worth an entry.
#
# WHICH IS WHY gemini-2.5-flash AND gemini-2.5-pro ARE NO LONGER HERE. They
# were, with the same rates the registry now holds — and the registry wins, so
# a future price change made in this table (the one an engineer finds first —
# it is the module-level constant, and tests/test_gemini_client.py used to read
# its rates directly) would have had NO effect on token_log.jsonl or on the
# financial model built from it, and no test would have failed. Two tables
# holding one fact, with the loser silent, is the second-source-of-truth shape
# this migration otherwise avoided. A test asserts the two share no keys.
GEMINI_PRICING = {
    "gemini-2.0-flash": (0.10, 0.40),
}
_DEFAULT_PRICING = (0.30, 2.50)


def pricing_for(model: str) -> tuple[float, float]:
    """($/1M input, $/1M output) for a model id: the registry, then the legacy
    table above, then the default rate an unknown id has always been costed
    at."""
    return (text_models.pricing_for(model)
            or GEMINI_PRICING.get(model, _DEFAULT_PRICING))


_MEDIA = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "png": "image/png", "gif": "image/gif", "webp": "image/webp",
}


def gemini_model(kind: str | None = None) -> str:
    """Mirror of artifact_model(): global default, per-kind override.

    Precedence is unchanged — `GEMINI_MODEL_<KIND>` beats `GEMINI_MODEL` beats
    the default — so nothing an operator has set on Railway means anything
    different. What moved is the DEFAULT: it is no longer a literal here but
    shared/text_models.resolve(ARTIFACT), which reads GEMINI_MODEL itself and
    then falls to the profile in force. The literal it replaced was
    "gemini-2.5-flash", which stops serving on 2026-10-20.
    """
    if kind:
        specific = os.getenv("GEMINI_MODEL_" + kind.upper())
        if specific:
            return specific
    return text_models.resolve(text_models.ARTIFACT).id


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
#
# It is also every 3.x successor's ceiling (verified 2026-09-07 and recorded in
# shared/text_models.REGISTRY), which is what makes the migration safe for the
# 32,000 the script path asks for and the 64,000 the retry doubles it to. This
# stays the retry's own ceiling; `_within_ceiling` is what stops a request
# going over the CHOSEN model's, whatever that model turns out to be.
MAX_OUTPUT_TOKENS = 65536


# A long generation Vertex drops at its own limit. 2026-10-14, "Area of
# Trapeziums and Compound Shapes": the lesson-script call (gemini-3.5-flash,
# a 32k-token reply) sat ~600 s per attempt and came back 503 UNAVAILABLE
# three times, then 499 — the loop sent the IDENTICAL request four times
# and the job failed after 40 minutes; the morning's retry of the same topic
# spent 2357 s in that stage before it got through. Nothing else was in the
# queue: not a burst, a reply too long for the pool's speed that hour. A
# 5xx that arrives after SLOW_DROP_S is read as that, and the remaining
# attempts go to the lighter model, which answers in a fraction of the time
# (the verifier still gates what it writes). VERTEX_SLOW_DROP_MODEL=""
# keeps the old behaviour.
SLOW_DROP_S = float(os.getenv("VERTEX_SLOW_DROP_S", "300"))
SLOW_DROP_MODEL = os.getenv("VERTEX_SLOW_DROP_MODEL", text_models.FLASH_LITE_3_5)

# STREAMING. The maths lesson is one request of up to 20,000 output tokens;
# as a non-streaming :generateContent call Vertex closed it with HTTP 503 at
# the ten-minute mark on every attempt of every maths video on 2026-10-14
# (13:47:59, 13:58:01, and two 39-minute script phases that morning). A
# streamed call (:streamGenerateContent?alt=sse) is answered as it is
# generated, so there is no idle ten minutes for the server to give up on:
# the only timeout that applies is the gap between two chunks. The reply is
# reassembled into the non-streaming shape, so nothing downstream changes.
# VERTEX_STREAM=0 is the kill switch (the old single-shot request).
# The read timeout is per socket read: it bounds the wait for the FIRST
# chunk as much as the gap between two. A thinking request (the maths
# lesson: 32k tokens, MEDIUM thinking) answers nothing for minutes while it
# thinks — at 120 s the very first streamed lesson timed out before its
# first byte (2026-10-14 15:14:40). 600 s, the server's own single-shot
# limit, is the floor; Vertex itself sends the first chunk well inside it.
STREAM_CONNECT_TIMEOUT_S = float(os.getenv("VERTEX_STREAM_CONNECT_TIMEOUT_S", "30"))
STREAM_READ_TIMEOUT_S = float(os.getenv("VERTEX_STREAM_READ_TIMEOUT_S", "600"))   # first chunk, and between chunks
# The read timeout fires only when NOTHING arrives. A stream that keeps
# sending blank keep-alive lines, or chunks with no text (thoughts, empty
# candidates), never trips it — the video job of 2026-10-14 19:14 UTC sat
# FOUR AND A HALF HOURS inside one streamed call (heartbeats alive, nothing
# logged, the worker restarted by hand at 23:42). Two caps, both a
# _Transient 504 the retry ladder and #217's slow-drop switch handle: the
# longest run without a TEXT part, and the whole stream.
STREAM_STALL_S = float(os.getenv("VERTEX_STREAM_STALL_S", "600"))
STREAM_TOTAL_S = float(os.getenv("VERTEX_STREAM_TOTAL_S", "1800"))


def _stream_mode() -> bool:
    return str(os.getenv("VERTEX_STREAM", "1")).strip().lower() not in ("0", "false", "off", "no")


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

    def _url(self, model: str | None = None, stream: bool = False) -> str:
        method = "streamGenerateContent?alt=sse" if stream else "generateContent"
        return (
            f"https://aiplatform.googleapis.com/v1/projects/{self.project}"
            f"/locations/{self.region}/publishers/google/models/{model or self.model}:{method}"
        )

    def _post(self, parts: list[dict], system: str, max_tokens: int,
              response_schema: dict | None = None, wants_json: bool = True,
              strict_schema: bool = False, model: str | None = None) -> dict:
        # `wants_json` is the CALLER's contract, not a preference: a caller
        # that returns the reply as prose (transcribe_images) must not ask for
        # a JSON mime type. The env flag is the second gate.
        json_on = wants_json and _json_mode()
        # A schema needs the mime type with it — Vertex rejects responseSchema
        # on a free-text reply — so the two travel together or not at all.
        # `strict_schema` is the caller's own opt-in: constrained decoding for
        # THIS call whatever the global flag says. The maths lesson uses it —
        # its payload is closed (every property nameable), and with the flag
        # off the model bent the shape twice on the first two production runs
        # (a state as a bare string; a dialogue object where a string
        # belongs). Still dropped once on a 400, like any schema.
        schema_on = bool(response_schema) and json_on and (strict_schema or _response_schema_enabled())
        # The thinking suppression, in whichever dialect THIS model speaks.
        # This used to be a literal {"thinkingBudget": 0}, which is the field
        # Gemini 3 dropped: the guard would have gone on being sent and gone on
        # being ignored, and the +38% surcharge it exists to prevent would have
        # come back with nothing in any log to say so.
        chosen = text_models.resolve(text_models.ARTIFACT, model_id=model or self.model)
        thinking = chosen.thinking_config
        max_tokens = self._within_ceiling(chosen, max_tokens)
        body = {
            "contents": [{"role": "user", "parts": parts}],
            "systemInstruction": {"parts": [{"text": system}]},
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                # See module docstring: thinking bills as output, +38% measured,
                # and this workload gains nothing from it.
                **({"thinkingConfig": thinking} if thinking else {}),
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
        stream = _stream_mode()
        started = time.monotonic()
        try:
            res = requests.post(
                self._url(model, stream=stream),
                headers={
                    "Authorization": f"Bearer {_access_token()}",
                    "Content-Type": "application/json",
                },
                json=body,
                # streamed: connect, then the longest silence before the first
                # chunk or between two; single-shot: the whole reply, which
                # Vertex itself stops at ~600 s
                timeout=(STREAM_CONNECT_TIMEOUT_S, STREAM_READ_TIMEOUT_S) if stream else 600,
                # the kwarg travels only when streaming: the single-shot call is
                # byte-for-byte what it was (and what every test fake answers)
                **({"stream": True} if stream else {}),
            )
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            # No response at all — the first chunk never came, or the socket
            # went. A requests exception is not one the retry ladder knows;
            # on 2026-10-14 15:14:40 a ReadTimeout here failed the first
            # streamed lesson outright. It is a transient like a dropped
            # stream: the ladder retries it, and after SLOW_DROP_S the
            # remaining attempts go to the lighter model.
            raise _Transient(504, f"no response in {time.monotonic() - started:.0f}s "
                                  f"({'streamed' if stream else 'single-shot'}): {exc!r}"[:300]) from exc
        if res.status_code == 429:
            raise _RateLimited(res.text, _retry_after_header(res))
        if res.status_code in _RETRY_STATUS:
            # Vertex answers a one-off 500 while the same model serves the
            # sibling jobs fine (2026-09-22 21:34: a teacher's worksheet and
            # case study, fifteen such in four bursts that day, none on any
            # other day of the fortnight). The Claude path has retried these
            # statuses with backoff for a year; this path let the FIRST one
            # fail the generation. Same list, same backoff, same jitter.
            raise _Transient(res.status_code, res.text[:300])
        if schema_on and res.status_code == 400:
            # Vertex rejects a schema it cannot express (a keyword outside the
            # OpenAPI subset, a nesting depth, a model that does not support
            # constrained decoding) with a 400 on EVERY call — so a bad schema
            # is not a degraded lesson, it is an outage for whatever passed it.
            # Drop the schema once and let the reply take its chances with the
            # salvage, loudly, rather than fail the job.
            raise _SchemaRejected(res.text[:500])
        res.raise_for_status()
        if not stream:
            return res.json()
        return self._drain_stream(res)

    @staticmethod
    def _drain_stream(res) -> dict:
        """Read a :streamGenerateContent?alt=sse body to its end and hand back
        ONE response in the :generateContent shape — the text parts of the
        first candidate joined in order, the last finishReason seen, the last
        usageMetadata seen (Vertex sends the totals in the final chunk) — so
        _text, _finish_reason and track_tokens read it unchanged.

        An error chunk mid-stream is raised like its status would have been
        (429 → _RateLimited, else _Transient); a connection dropped or a gap
        longer than STREAM_READ_TIMEOUT_S between chunks is a _Transient 504
        that says how far the reply got, so the retry ladder (and #217's
        slow-drop switch) treat it like any other transient failure.
        Thought parts (``"thought": true``) are not text and are skipped.
        """
        text_parts: list[str] = []
        finish = ""
        usage = None
        prompt_feedback = None
        chunks = 0
        started = time.monotonic()
        last_text = started
        try:
            for raw in res.iter_lines(decode_unicode=True):
                now = time.monotonic()
                if now - last_text > STREAM_STALL_S or now - started > STREAM_TOTAL_S:
                    chars = sum(len(t) for t in text_parts)
                    why = (f"no text for {now - last_text:.0f}s" if now - last_text > STREAM_STALL_S
                           else f"over its {STREAM_TOTAL_S:.0f}s cap")
                    logger.warning("Vertex text stream stalled (%s) after %d chunk(s), %d chars, %.0fs — cut",
                                   why, chunks, chars, now - started)
                    raise _Transient(504, f"stream stalled: {why} after {chunks} chunk(s), {chars} chars, "
                                          f"{now - started:.0f}s in total")
                if not raw:
                    continue
                line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    chunk = json.loads(payload)
                except ValueError:
                    logger.warning("Vertex text stream: unreadable chunk skipped (%d bytes)", len(payload))
                    continue
                chunks += 1
                err = chunk.get("error") if isinstance(chunk, dict) else None
                if err:
                    code = int(err.get("code") or 503)
                    body = json.dumps(err)[:300]
                    if code == 429:
                        raise _RateLimited(body, None)
                    raise _Transient(code if 500 <= code <= 599 else 503, body)
                for cand in chunk.get("candidates") or []:
                    for part in (cand.get("content") or {}).get("parts") or []:
                        if part.get("thought"):
                            continue
                        if "text" in part:
                            text_parts.append(part["text"])
                            if part["text"]:
                                last_text = time.monotonic()
                    if cand.get("finishReason"):
                        finish = cand["finishReason"]
                if chunk.get("usageMetadata"):
                    usage = chunk["usageMetadata"]
                if chunk.get("promptFeedback"):
                    prompt_feedback = chunk["promptFeedback"]
        except (requests.exceptions.ChunkedEncodingError, requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as exc:
            chars = sum(len(t) for t in text_parts)
            raise _Transient(504, f"stream dropped after {chunks} chunk(s), {chars} chars, "
                                  f"{time.monotonic() - started:.0f}s: {exc!r}"[:300]) from exc
        finally:
            try:
                res.close()
            except Exception:  # noqa: BLE001
                pass
        chars = sum(len(t) for t in text_parts)
        logger.info("Vertex text stream: %d chunk(s), %d chars, %.0fs, finish=%s",
                    chunks, chars, time.monotonic() - started, finish or "-")
        out: dict = {"candidates": [{"content": {"role": "model", "parts": [{"text": "".join(text_parts)}]},
                                     "finishReason": finish}]}
        if usage is not None:
            out["usageMetadata"] = usage
        if prompt_feedback is not None:
            out["promptFeedback"] = prompt_feedback
        return out

    @staticmethod
    def _within_ceiling(chosen: text_models.TextModel, max_tokens: int) -> int:
        """`max_tokens`, never above what this model will actually accept.

        THE REGISTRY'S KNOWLEDGE HAS TO BE LOAD-BEARING OR IT IS A COMMENT.
        maxOutputTokens above a model's published ceiling is a 400
        INVALID_ARGUMENT, and `_call` catches only _RateLimited and
        _SchemaRejected — so a `res.raise_for_status()` here propagates out of
        `_call`, out of `analyze`, and FAILS the generation. It is not a
        degradation anything downstream can absorb.

        The callers that reach highest are the ones that earn the money:
        agent3_scripts/script_generator.py asks for 32,000 on every lesson
        script (30,000 non-conversational), catalogue/questions.py for 24,000,
        and `analyze`'s truncation retry doubles whatever it was given to as
        much as MAX_OUTPUT_TOKENS. Every current registry model tops out at
        exactly 65,536, so this clamps nothing today; it is the guard that lets
        a smaller-ceiling model be added to the registry without taking the
        script path down on the deploy that adds it.

        An id the registry has no entry for has no ceiling to clamp against and
        is passed through untouched — the same choice `_model_id` makes about
        honouring it at all.
        """
        ceiling = chosen.spec.max_output_tokens
        if not ceiling or max_tokens <= ceiling:
            return max_tokens
        model_env.shout(logger,
                        "max_tokens=%d is above %s's published output ceiling "
                        "of %d, which Vertex answers with a 400; sending %d "
                        "instead. A reply that needed the larger budget will "
                        "come back truncated rather than not at all."
                        % (max_tokens, chosen.id, ceiling, ceiling))
        return ceiling

    def _call(self, parts: list[dict], system: str, max_tokens: int, retries: int,
              response_schema: dict | None = None, wants_json: bool = True,
              strict_schema: bool = False) -> dict:
        """``retries`` retried attempts and one final one, every attempt
        through the TEXT provider gate (shared.provider_gate): at most N
        scripts in flight per process — shared across replicas when the
        worker configured it — and silence for everyone after a 429. Four
        kits queued together on 2026-10-13 each ran their own retry ladder
        against the same busy pool and two of them failed after 32 minutes."""
        from shared import phase_clock, provider_gate as _pg
        gate = _pg.gate("vertex_text")
        model: str | None = None            # set after a slow drop: the lighter model from then on
        for attempt in range(retries + 1):
            last = attempt == retries
            token = gate.acquire("Vertex text")
            started = time.monotonic()
            try:
                out = self._post(parts, system, max_tokens, response_schema, wants_json, strict_schema, model=model)
            except _RateLimited as exc:
                gate.release(token)
                gate.outcome(False, retry_after=exc.retry_after)
                phase_clock.count("rate_limit_refusals_text")
                if last:
                    raise
                wait = _pg.backoff_seconds(attempt, base=2.0, cap=60.0, growth=2.0,
                                           retry_after=exc.retry_after)
                logger.warning("Vertex text rate-limited; retrying in %.1fs (%d/%d)%s",
                               wait, attempt + 1, retries,
                               f" (server asked {exc.retry_after:.0f}s)" if exc.retry_after else "")
                time.sleep(wait)
                phase_clock.add("rate_limit_wait", wait)
                phase_clock.count("rate_limit_retries")
                continue
            except _Transient as exc:
                gate.release(token)
                elapsed = time.monotonic() - started
                if last:
                    raise
                wait = _backoff_seconds(attempt)
                if elapsed >= SLOW_DROP_S and SLOW_DROP_MODEL and model is None and SLOW_DROP_MODEL != self.model:
                    # a slow drop: not a blip to wait out, a reply too long
                    # for the pool's speed — the same request again would
                    # sit the same ~600 s and die the same way
                    model = SLOW_DROP_MODEL
                    phase_clock.count("slow_drops_text")
                    logger.warning("Vertex dropped a long request after %.0fs (HTTP %d); the remaining attempts "
                                   "go to %s (%d/%d): %s", elapsed, exc.status, model, attempt + 1, retries, exc.body)
                else:
                    if elapsed >= SLOW_DROP_S:
                        phase_clock.count("slow_drops_text")
                    logger.warning("transient Vertex failure (HTTP %d after %.0fs); retrying in %.1fs (%d/%d): %s",
                                   exc.status, elapsed, wait, attempt + 1, retries, exc.body)
                time.sleep(wait)
                continue
            except _SchemaRejected as exc:
                gate.release(token)
                if last:
                    raise
                logger.error("Vertex rejected responseSchema, retrying unconstrained: %s", exc)
                response_schema = None
                continue
            except BaseException:
                gate.release(token)
                raise
            gate.release(token)
            gate.outcome(True)
            served = model or getattr(self, "model", None) or "unknown"      # a bare test double has no model
            phase_clock.count(f"vertex_text_calls_{served.replace('-', '_').replace('.', '_')}")
            if model is not None:
                # the recovery, measured: how long the lighter model took
                # where the configured one was dropped at ~600 s — the
                # number the streaming change is judged against
                took = time.monotonic() - started
                phase_clock.count("slow_drop_recoveries")
                phase_clock.add("slow_drop_recovery_s", took)
                logger.warning("the lighter model %s answered in %.0fs after the slow drop", model, took)
            return out
        raise AssertionError("unreachable")

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
        in_rate, out_rate = pricing_for(self.model)
        cost = (inp * in_rate + out * out_rate) / 1_000_000
        usage = {
            "input_tokens": inp,
            "output_tokens": out,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "total_tokens": inp + out,
            "estimated_cost_usd": round(cost, 6),
        }
        with _USAGE_LOCK:   # concurrent callers of one client (the bank stage) — see ClaudeClient.track_tokens
            self.session_usage["calls"] += 1
            self.session_usage["input_tokens"] += inp
            self.session_usage["output_tokens"] += out
            self.session_usage["cost_usd"] = round(self.session_usage["cost_usd"] + cost, 6)
        ClaudeClient._log_usage({**usage, "model": self.model, "backend": self.backend})
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
        strict_schema: bool = False,
    ) -> dict:
        """Text prompt in, parsed JSON out.

        `response_schema` is a Vertex responseSchema (the OpenAPI 3.0 subset)
        for callers whose payload is CLOSED — every property nameable up
        front. It is sent only when GEMINI_RESPONSE_SCHEMA is on — or when
        the caller says `strict_schema=True` for this call — and dropped
        for one retry if Vertex rejects it, so a schema can never take a
        caller down. ClaudeClient.analyze accepts and ignores both, so
        process.py can keep holding either object.

        `cache_prefix` is accepted for interface compatibility and simply
        prepended. Vertex context caching is a separate API with its own minimum
        size and lifecycle — NOT Anthropic's cache_control — so it is not wired
        up here. The prefix still works; it just costs full price. That is a
        known cost gap against the Claude path, not an oversight.
        """
        parts = ([{"text": cache_prefix}] if cache_prefix else []) + [{"text": prompt}]
        response = self._call(parts, system, max_tokens, retries, response_schema,
                              strict_schema=strict_schema)
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
                                  response_schema, strict_schema=strict_schema)
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
    """429 from Vertex — retried with backoff, mirroring RateLimitError.
    Carries the server's Retry-After (seconds) when it named one."""
    def __init__(self, body: str, retry_after: float | None = None) -> None:
        super().__init__(body)
        self.retry_after = retry_after


def _retry_after_header(res) -> float | None:
    """The seconds form of a Retry-After header, else None."""
    try:
        v = res.headers.get("Retry-After")
        return float(v) if v is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


class _Transient(requests.HTTPError):
    """A status in claude_client._RETRY_STATUS (500/502/503/504 and kin) from
    Vertex — retried with the Claude client's jittered backoff. An HTTPError
    on purpose: the last attempt lets it through unchanged, so a caller that
    read `500 Server Error` off jobs.error yesterday reads the same shape."""

    def __init__(self, status: int, body: str):
        super().__init__(f"{status} Server Error: transient Vertex failure ({body})")
        self.status = status
        self.body = body


class _SchemaRejected(Exception):
    """400 from Vertex on a request that carried a responseSchema. Never
    surfaced to a caller: _call drops the schema and tries once more, because
    a schema the API dislikes fails EVERY call and would be an outage."""
