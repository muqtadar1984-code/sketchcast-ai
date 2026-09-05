"""Shared Claude API client — reused by all agents."""

from __future__ import annotations

import base64
import json
import threading as _threading
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from anthropic import Anthropic, RateLimitError
try:  # older SDKs may not expose every class
    from anthropic import APIConnectionError, APIStatusError, InternalServerError
except Exception:  # noqa: BLE001
    APIConnectionError = APIStatusError = InternalServerError = ()

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


# ── spend attribution ────────────────────────────────────────────────────────
# Ambient labels for whatever job the current thread is working on. Set once by
# the worker; every model call made underneath inherits them, so spend can be
# attributed to a lesson without threading a context object through eight
# agents. Thread-local because the worker runs jobs concurrently.
_USAGE_CTX = _threading.local()
_USAGE_LOCK = _threading.Lock()


def set_usage_context(**labels) -> None:
    """Label every model call on this thread (generation_id, book_id, kind,
    engine, ...). Call with no arguments to clear."""
    _USAGE_CTX.labels = {k: v for k, v in labels.items() if v is not None}


def _usage_labels() -> dict:
    return dict(getattr(_USAGE_CTX, "labels", {}) or {})


def log_external_usage(service: str, **fields) -> None:
    """Record a NON-text model call — image generation, vision annotation.

    These were never logged at all: the measured cost per lesson counted only
    the text calls, so image and vision spend was invisible in a pipeline that
    generates dozens of images per lesson.
    """
    ClaudeClient._log_usage({"service": service, **fields})


# ── transient failure policy ─────────────────────────────────────────────────
# Only RateLimitError was retried, so a 529 "overloaded" — the most common
# transient failure, and the one that arrives precisely when several schools
# generate at once — killed a whole lesson on its FIRST occurrence, along with
# everything already spent on it.
#
# Deliberately NOT "retry all 5xx". A retry is only safe where the operation is
# idempotent and cheap to repeat. This is the TEXT path: a repeated call costs
# input tokens and returns a fresh completion, which is acceptable. Image
# generation is NOT retried this way — it is the expensive, non-idempotent call
# and has its own per-lesson budget instead.
#
# 400/401/403/404 and 422 are deterministic: the same request will fail the same
# way, so retrying only burns money and delays the error the caller needs.
_RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, RateLimitError):
        return True
    if APIConnectionError and isinstance(exc, APIConnectionError):
        return True          # never reached the server; safe to repeat
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    return isinstance(status, int) and status in _RETRY_STATUS


def _backoff_seconds(attempt: int) -> float:
    """Exponential with jitter. Without jitter, every worker that hit the same
    overload retries in lockstep and recreates it."""
    import random
    return min(60.0, 2.0 ** (attempt + 1)) + random.uniform(0.0, 1.5)


def _get_api_key() -> str:
    """Read the API key from the environment.

    This used to `import streamlit` first, to read a secrets file that does
    not exist on the worker: 1.68 s of process start and a ~150 MB dependency
    tree resident in the process that renders video frames, every time, to
    fall through to the env var anyway. The Streamlit UI is gone.
    """
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY not found. Set it as an environment variable."
        )
    return key


def _as_document(src: str):
    """`src` parsed as a NON-EMPTY object or array, or None. The same
    acceptance _repair_json's candidate loop uses, so a reading this walk
    calls "a document" is one the caller would have kept."""
    try:
        out = json.loads(src, strict=False)
    except Exception:  # noqa: BLE001 — any parse failure means "not a document"
        return None
    return out if isinstance(out, (dict, list)) and out else None


def _scan_inner_quotes(text: str, pair_parity: bool) -> tuple[str, int]:
    """One walk of `text`, escaping bare inner quotes; see _escape_inner_quotes.

    `pair_parity` off reproduces the pre-2026-09-05 reading exactly, so the
    caller can hold the two side by side. Returns (source, flips), flips being
    the number of would-be closers parity handed back to the prose.
    """
    import re as _re
    key_shape = _re.compile(r'\s*"(?:[^"\\]|\\.)*"\s*:')
    starters = set('"{[-0123456789')
    out, i, n = [], 0, len(text)
    stack: list[str] = []          # containers we are inside: "{" or "["
    expect_key: list[bool] = []    # per container: is the next string a key?
    in_str = esc = is_key = False
    quotes_in_str = 0              # quotes already escaped INSIDE this string
    flips = 0

    def parity_says_prose() -> bool:
        # An ODD number of quotes already escaped in this string means the
        # model is mid-phrase — it opened `"Ar-Rahman` and this is the closing
        # half — and the closing half of a pair cannot also be the string's
        # terminator. English quotes come in twos; a JSON boundary does not
        # care how many came before it. Held to the two branches that were
        # guesses already: `}`, `]` and end-of-input stay absolute, so a reply
        # whose last prose quote the model simply dropped still closes at its
        # container instead of running off the end.
        nonlocal flips
        if pair_parity and quotes_in_str % 2:
            flips += 1
            return True
        return False

    def closes(j: int) -> bool:
        if j >= n:
            return True
        c = text[j]
        top = stack[-1] if stack else None
        if c in "}]":
            # a closer never follows a prose quote; a MIS-NESTED closer is
            # _rebalance_json's job and must reach it as structure
            return True
        if c == ":":
            if is_key:
                return True
            # A colon after a VALUE string is never valid JSON, so this is
            # either prose ('ask "why?": because') or a mangled object
            # ('"who":"alice":"bob"'). If a JSON value follows the colon the
            # two readings are indistinguishable LOCALLY — and this is exactly
            # the shape that killed Sara Hamaydeh's first lesson (gen
            # eb12963c, 2026-09-05): 22,538 chars, 6,168 output tokens against
            # a 30,000 cap, NO truncation reported by the provider, dead at
            # char 14,380 on the Islamic-studies habit of glossing a
            # transliterated name with a colon — `the name "Ar-Rahman": "the
            # Most Merciful" is the one we say most often`. Parity breaks the
            # tie: an odd count says the string is inside a pair and cannot
            # end here. '"who":"alice":"bob"' has an EVEN count (zero) and
            # still fails loudly, which is the reading nobody may guess.
            k = j + 1
            while k < n and text[k] in " \t\r\n":
                k += 1
            if k >= n:
                return True
            d = text[k]
            if not (d in starters
                    or any(text.startswith(lit, k) for lit in ("true", "false", "null"))):
                return False
            return not parity_says_prose()
        if c != ",":
            return False
        k = j + 1
        while k < n and text[k] in " \t\r\n":
            k += 1
        if k >= n or text[k] in "]}":
            return True      # a trailing comma; a later rule strips it
        if top == "{":
            if key_shape.match(text, j + 1) is None:
                return False
            # '"amanah", "khalifah": a caretaker' — the second quoted word
            # reads as a key, so the comma looked structural. An odd count
            # says the FIRST pair is still open, so it is not.
            return not parity_says_prose()
        d = text[k]
        if not (d in starters
                or any(text.startswith(lit, k) for lit in ("true", "false", "null"))):
            return False
        # An array element boundary. An odd count means the boundary would
        # fall between the halves of one quoted phrase and split it across two
        # elements — the only SILENT corruption measured in this class:
        # ["remember "amanah", "khalifah" today"] became the two slide points
        # 'remember "amanah' and 'khalifah" today', words no model wrote.
        return not parity_says_prose()

    while i < n:
        c = text[i]
        if esc:
            out.append(c)
            if c == '"':
                quotes_in_str += 1   # a quote the MODEL escaped — still a pair half
            esc = False
        elif in_str:
            if c == "\\":
                out.append(c)
                esc = True
            elif c == '"':
                j = i + 1
                while j < n and text[j] in " \t\r\n":
                    j += 1
                if closes(j):
                    in_str = False
                    out.append(c)
                else:
                    out.append('\\"')
                    quotes_in_str += 1
            else:
                out.append(c)
        else:
            if c == '"':
                in_str = True
                quotes_in_str = 0
                is_key = bool(stack) and stack[-1] == "{" and expect_key[-1]
            elif c == "{":
                stack.append("{")
                expect_key.append(True)
            elif c == "[":
                stack.append("[")
                expect_key.append(False)
            elif c in "}]":
                if stack:
                    stack.pop()
                    expect_key.pop()
            elif c == ":" and stack and stack[-1] == "{":
                expect_key[-1] = False
            elif c == "," and stack and stack[-1] == "{":
                expect_key[-1] = True
            out.append(c)
        i += 1
    return "".join(out), flips


def _escape_inner_quotes(text: str):
    """Escape double quotes the model left bare INSIDE a string.

    Measured: '...often called the "powerhouses" of the cell...' inside a
    dialogue line; then 'called "organelles", tiny structures' and 'ask
    "why?": because…' — a quoted word before a comma or colon is how English
    prose quotes a word, and the first rule ("a quote before structural
    punctuation closes the string") turned a complete 54,000-character reply
    into "produced no segments" on 2026-09-04.

    So the walk tracks the containers it is inside and closes a string only
    where JSON could actually continue:
      inside an OBJECT — a `,` closes only if a KEY follows ("name":); a `:`
        closes only if this string IS the key; `}` closes.
      inside an ARRAY — a `,` closes if an element follows (a string, a
        container, a number, a literal); `]` closes.
    Valid JSON is a no-op.

    That was still not enough. Sara Hamaydeh's first lesson (gen eb12963c,
    2026-09-05, "Islamic y6") died on a reply the walk had every rule for:
    22,538 chars, 6,168 output tokens against a 30,000 cap, the provider
    reporting NO truncation, dead at char 14,380. Islamic-studies prose
    glosses a transliterated name with a colon — `the name "Ar-Rahman": "the
    Most Merciful"` — and both branches above read the CLOSING half of a
    quoted phrase as the end of the string. Three shapes fail that way: the
    colon gloss, `"amanah", "khalifah": a caretaker`, and — the dangerous one,
    because it failed silently — an enumeration inside an array element, where
    ["remember "amanah", "khalifah" today"] parsed into two slide points
    carrying words no model wrote.

    PAIR PARITY is the signal that was missing: a quote can only be a closer
    when the quotes already escaped inside this string are EVEN. An odd count
    means the string sits between the halves of a pair, and the second half of
    a pair is not a terminator.

    PARITY DOES NOT GET THE LAST WORD. It is a claim about prose, and a wrong
    repair silently changes what a teacher's lesson says. So wherever parity
    overrode a closer, BOTH readings are walked to the end and both parsed,
    and the outcome turns on which of them is a document at all:
      * only parity's reading parses  -> parity's reading  (the three shapes)
      * only the old reading parses   -> the old reading   (parity was wrong;
        the model really had dropped a quote)
      * both parse and they DIFFER    -> None. Two readings of one lesson is
        the one thing this layer may never choose between.
      * neither parses                -> parity's reading, so the rest of the
        ladder still has something to work on. Nothing has been chosen while
        nothing yet parses.
    """
    primary, flips = _scan_inner_quotes(text, pair_parity=True)
    if not flips:
        return primary
    legacy, _ = _scan_inner_quotes(text, pair_parity=False)
    a, b = _as_document(primary), _as_document(legacy)
    if a is not None and b is not None:
        return None if a != b else primary
    return legacy if a is None and b is not None else primary


def _fix_bad_escapes(text: str) -> str:
    """A backslash that does not start a JSON escape (LaTeX '\\(', '5\\%', a
    stray path, '\\u' not followed by four hex digits) is an "Invalid
    \\escape" for json.loads; doubling it keeps the character the model
    meant. '\\'' is the one exception — an apostrophe the model over-escaped
    — and becomes a plain apostrophe. An escaped backslash pair is consumed
    whole so its second half is never mistaken for a lone backslash.

    Known ambiguity, left alone: '\\frac' and '\\times' begin with the VALID
    escapes \\f and \\t, so LaTeX in narration still reaches the text as a
    form feed / tab plus the rest of the word; the alternative — guessing
    which valid escapes are really LaTeX — is worse."""
    import re as _re
    text = _re.sub(r"(?<!\\)\\'", "'", text)
    return _re.sub(r'\\\\|\\u(?![0-9a-fA-F]{4})|\\(?!["\\/bfnrtu])',
                   lambda m: "\\\\" if m.group(0) == "\\\\" else "\\\\" + m.group(0)[1:],
                   text)


def json_fault(text, radius: int = 120) -> str | None:
    """Where and why json.loads rejects `text`, with a window of the text
    around the spot — or None when it parses. For an error message: the raw
    reply used to be dumped to /tmp on the container, which is gone by the
    time anyone reads the job error, so the fault itself has to travel. The
    window is repr-escaped, so control characters cannot mangle a log line
    or a DB column."""
    try:
        if isinstance(text, (bytes, bytearray)):
            text = bytes(text).decode("utf-8", "replace")
        text = str(text)
        try:
            json.loads(text, strict=False)
            return None
        except json.JSONDecodeError as exc:
            lo, hi = max(0, exc.pos - radius), min(len(text), exc.pos + radius)
            window = repr(text[lo:hi])[1:-1]
            return (f"{exc.msg} at line {exc.lineno} col {exc.colno} "
                    f"(char {exc.pos} of {len(text)}): …{window}…")
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


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


def _substitute_closers(text: str):
    """The OTHER reading of a mis-nested closer: the model wrote the wrong
    bracket CHARACTER for the structure it was closing — `]` for `}`.

    Measured 2026-09-04 (gen f0e65c2f, char 4,698 of 39,132):
        "layers": ["nucleus"] ] ],  "key_point": …
    the action object closed with `]`. _rebalance_json reads every mismatch
    as an omission and closes level after level down to the next array, which
    drags the step's key_point out of its object and the reply still fails.
    Read as a substitution it is one character. Both readings are offered as
    candidates and the richest parse wins.

    Each mismatched closer is swapped for the one the open structure needs and
    closes exactly one level; a closer with nothing open is dropped. Same
    severed-tail refusal as _rebalance_json. Returns the corrected source, or
    None when nothing was swapped or the text was truncated."""
    out, stack = [], []
    ins = esc = swapped = False
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
            if not stack:
                swapped = True      # a closer with nothing open: drop it
                continue
            need = "}" if stack[-1] == "{" else "]"
            if c != need:
                c = need
                swapped = True
            stack.pop()
        out.append(c)
    if ins or esc or not swapped:
        return None
    tail = "".join(out).rstrip()
    if not tail or tail[-1] in ",:":
        return None          # a value was severed — leave the failure loud
    while stack:
        out.append("]" if stack.pop() == "[" else "}")
    return "".join(out)


def _drop_superfluous_closers(text: str):
    """A closer the model wrote ONE TOO MANY of, mid-document.

    Measured on Sara Hamaydeh's lost lesson (gen eb12963c, job 8e3d26dd,
    2026-09-05) — the malformation the 300-character issue context had cut
    away, read back off the 500-character jobs.error row:
        "assets": {"creation_scene": "…illustrating Allah's creation
        effortlessly."}}, "semantic_regions": ["allah_central_script", …
    The assets object is closed, and then closed again. That second `}` shut
    the CHAPTER object, which left "semantic_regions" standing in the chapters
    ARRAY as though it were an element, and json.loads stopped at its colon:
    "Expecting ',' delimiter", char 14,380 of 22,538, on 6,168 output tokens
    against a 30,000 cap that the provider never called truncated.

    Neither neighbour reaches it. _rebalance_json only INSERTS closers it
    believes are missing; _substitute_closers only swaps a closer for a
    different character, or drops one with nothing open. This one is
    well-formed, correctly matched to the object it closes, and simply not
    wanted.

    Decidable, not guessed. It fires only where the container the closer
    returns into is an ARRAY and the next thing past the comma is a KEY:
    `["a", "b": 1]` is not valid JSON under any reading, so there are no two
    readings to choose between. Everything else — a closer that returns into
    an object, a mismatched closer, a comma followed by a real element — is
    left to the rules that own it. Same severed-tail refusal as its
    neighbours: a reply cut off mid-value must stay a loud failure.

    Returns the corrected source, or None when nothing was dropped, the text
    was truncated, or the shape belongs to another rule.
    """
    import re as _re
    key_shape = _re.compile(r'\s*"(?:[^"\\]|\\.)*"\s*:')
    out, stack = [], []
    ins = esc = dropped = False
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        i += 1
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
            out.append(c)
            continue
        if c in "}]":
            want = "{" if c == "}" else "["
            if not stack or stack[-1] != want:
                return None      # a MIS-nested closer — _substitute_closers' shape
            if len(stack) >= 2 and stack[-2] == "[":
                j = i
                while j < n and text[j] in " \t\r\n":
                    j += 1
                if j < n and text[j] == "," and key_shape.match(text, j + 1):
                    # closing here would put a KEY into an array. The closer is
                    # the thing that is wrong: drop it, stay in the object, and
                    # let the members that follow land where the schema puts them.
                    dropped = True
                    continue
            stack.pop()
        out.append(c)
    if ins or esc or not dropped:
        return None
    tail = "".join(out).rstrip()
    if not tail or tail[-1] in ",:":
        return None          # a value was severed — leave the failure loud
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
    # 0. a backslash that is not an escape (LaTeX, a percent sign, a path)
    fixed = _fix_bad_escapes(text)
    candidates.append(fixed)
    # 1. SSML attribute quotes inside a JSON string. The first shape measured
    #    was <break time="0.3s"/>; the rule covers every SSML tag — prosody,
    #    emphasis, say-as, break strength — since a tag never legitimately sits
    #    outside a string in this schema, and single quotes are valid SSML.
    #    Anchored on the tag NAMES: a bare `<[^<>]*>` matched from a `<` in one
    #    string to a `>` in a later one (a maths lesson's "3 < 5" … "7 > 2")
    #    and rewrote the structural quotes between them (review, 2026-09-04).
    #    No single-letter names (`<p`, `<s`, `<w` fire on terse maths like
    #    "0<p and … p>1"), and the tag body may not contain a structural
    #    `"` `,`/`:` `"` — so a match can never span two JSON strings.
    fixed = _re.sub(r'</?(?:break|prosody|emphasis|say-as|phoneme|speak|sub|voice|lang|mark|audio)\b'
                    r'(?:(?!"\s*[,:]\s*")[^<>])*>',
                    lambda m: m.group(0).replace('\\"', '"').replace('"', "'"), fixed)
    candidates.append(fixed)
    # 2. a key emitted TWICE, its own name standing in as the value:
    #    {"who":"who":"teacher"}  ->  {"who":"teacher"}
    #    Measured on a real reply that failed a whole lesson. A string value
    #    followed by a colon is never valid JSON, so there is nothing to lose;
    #    the rule is held to the case where the stray value REPEATS its key,
    #    because that is unambiguous about which half to drop.
    fixed = _re.sub(r'"(\w+)"\s*:\s*"\1"\s*:', r'"\1":', fixed)
    candidates.append(fixed)
    # 2b. a value wrapped in brackets: {"who":("teacher")} -> {"who":"teacher"}
    #     The prompt said 'Speakers: "teacher" (primary explanatory voice)'
    #     and the model copied that FORM into the JSON. The prose has been
    #     fixed, but a reply already in flight still has to survive.
    fixed = _re.sub(r':\s*\(\s*("(?:[^"\\]|\\.)*")\s*\)', r": \1", fixed)
    candidates.append(fixed)
    # 2c. the "line" key dropped, its text left hanging off the speaker:
    #     {"who": "teacher": "And then..."} -> {"who": "teacher", "line": "..."}
    #     Every malformation measured on this path so far has corrupted this
    #     one dialogue object, so it gets its own rule.
    #     Restricted to the two KNOWN speaker values. With a valid speaker the
    #     repair is decidable from the schema — a dialogue entry has exactly
    #     `who` and `line`, so a trailing string can only be the line. With an
    #     arbitrary value it would be a GUESS about which key went missing,
    #     and guessing intent is the one thing this layer must never do.
    #     A SECOND form of the same slip, measured on episode 3: the key is not
    #     dropped, it is transposed into the value slot with the real text left
    #     bare behind it:
    #       {"who": "teacher": "line", "A group of similar cells..."}
    #     This has to run BEFORE the dropped-key rule, which would otherwise
    #     consume the same prefix and yield {"line": "line", "..."} — still
    #     invalid, and now harder to read. Decidable on the same grounds: a
    #     known speaker, the literal key name sitting where its value belongs,
    #     and a dialogue schema with exactly two fields.
    fixed = _re.sub(
        r'"who"\s*:\s*("(?:teacher|student)")\s*:\s*"line"\s*,\s*(?=")',
        r'"who": \1, "line": ', fixed)
    candidates.append(fixed)
    fixed = _re.sub(r'"who"\s*:\s*("(?:teacher|student)")\s*:\s*(?=")',
                    r'"who": \1, "line": ', fixed)
    candidates.append(fixed)
    # 2d. bare quotes inside a string ('called the "powerhouses" of the cell')
    escaped = _escape_inner_quotes(fixed)
    if escaped is None:
        # The walk found two readings of this reply that BOTH parse and
        # disagree about the words. Its refusal has to end the whole repair,
        # not just drop one candidate: when two readings parse, the reply is
        # structurally sound apart from that one quote, so the tail-walk and
        # the re-balancers below would go on to rediscover one of the two by
        # luck and the richest-parse rule would pick the LONGER — which for an
        # enumeration split across array elements is the corrupt one. A lesson
        # that fails is recoverable; a lesson that ships words the model never
        # wrote is not.
        return None
    fixed = escaped
    candidates.append(fixed)
    # 3. trailing commas
    decommaed = _re.sub(r",\s*([}\]])", r"\1", fixed)
    candidates.append(decommaed)
    # 3. mis-nested closers — TWO readings, both offered, richest parse wins:
    #    the model FORGOT a closer (_rebalance_json inserts the missing ones),
    #    or it wrote the WRONG closer character (_substitute_closers swaps it).
    #    Measured 2026-09-04 (gen f0e65c2f): `"layers": ["nucleus"] ] ],` — an
    #    object closed with `]`. Read as an omission that `]` closes every level
    #    down to the next array and drags the step's key_point out of its
    #    object; read as a substitution it is one character, and the reply parses.
    # 3. and the THIRD reading: the model wrote a closer it did not need.
    #    Measured on gen eb12963c (2026-09-05) — `"assets": {...}}` closed the
    #    chapter object early and stranded its remaining keys in the chapters
    #    array. Neither reading above can see it: the closer is present,
    #    matched and well-formed, just surplus.
    for src in (fixed, decommaed):
        mended = _rebalance_json(src)
        if mended:
            candidates.append(mended)
        swapped = _substitute_closers(src)
        if swapped:
            candidates.append(swapped)
        surplus = _drop_superfluous_closers(src)
        if surplus:
            candidates.append(surplus)
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
    # Take the RICHEST parse, not the first one.
    #
    # The tail-walk above produces truncated prefixes, and a prefix can
    # happen to parse: one real reply came back with `segments` intact and a
    # chapter that had lost its elements, so the adapter reported
    # EMPTY_CHAPTER, the visual plan was dropped, and all 42 segments
    # rendered as plain cards. Returning the first candidate that parsed is
    # what chose that. Preferring the one carrying the most content makes a
    # full repair beat a lucky prefix every time.
    best, best_size = None, -1
    for cand in candidates:
        try:
            # strict=False: a raw newline or tab INSIDE a string is a
            # control character json.loads rejects by default; the model
            # emits them in long narration and they are harmless.
            out = json.loads(cand, strict=False)
        except Exception:
            continue
        if not isinstance(out, (dict, list)) or not out:
            continue
        size = len(json.dumps(out, default=str))
        if size > best_size:
            best, best_size = out, size
    return best


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
        response_schema: dict | None = None,
    ) -> dict:
        """Send a text prompt, return parsed JSON dict.

        `response_schema` is accepted and IGNORED, for the same reason
        `cache_prefix` is accepted by GeminiClient and not implemented:
        worker/process.py holds either object without knowing which, so a
        keyword one of them understands must not raise on the other.
        Anthropic constrains output through tool schemas, not a response
        schema, so honouring it here would be a different mechanism
        wearing the same name.

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
            except Exception as exc:  # noqa: BLE001 — classified below
                if not _is_transient(exc):
                    raise            # deterministic: retrying only burns money
                wait = _backoff_seconds(attempt)
                logger.warning("transient model failure (%s); retrying in "
                               "%.1fs (%d/%d)", type(exc).__name__, wait,
                               attempt + 1, retries)
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
            except Exception as exc:  # noqa: BLE001 — classified below
                if not _is_transient(exc):
                    raise
                time.sleep(_backoff_seconds(attempt))
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
            # Say WHERE it broke. The reply itself does not survive the
            # container, so this line is what a later reader has.
            logger.error("model JSON unparseable and unrepairable — %s",
                         json_fault(text) or "it parsed, but carried no usable object")
            return {"raw_text": text}

    @staticmethod
    def _log_usage(usage: dict):
        """Append one usage entry to the token log.

        Two properties this needs and did not have:

        WHAT IT WAS FOR. Entries carried only token counts and a timestamp,
        so no spend could ever be attributed to a lesson, a book or an
        engine. "What did this generation cost?" and "is the semantic path
        cheaper than legacy?" were unanswerable from our own data. The
        ambient labels below are set by the worker for the duration of a job.

        ONE WRITER AT A TIME. It read the whole file, appended and rewrote it
        with no lock, under WORKER_CONCURRENCY job threads AND a RENDER_WORKERS
        render pool — a guaranteed lost-update race, and O(n^2) rewriting of a
        file that only grows. It is now line-delimited and appended under a
        lock, so a concurrent write cannot destroy another's entry.
        """
        entry = {**usage, **_usage_labels(),
                 "timestamp": datetime.now(timezone.utc).isoformat()}
        try:
            with _USAGE_LOCK:
                with open(TOKEN_LOG_PATH.with_suffix(".jsonl"), "a",
                          encoding="utf-8") as f:
                    f.write(json.dumps(entry, default=str) + "\n")
        except Exception:
            pass  # Don't crash if logging fails
