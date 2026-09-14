"""Tolerant reading of a catalogue model reply, shared by every job that
asks for JSON and validates it (article, questions).

Why this exists (2026-09-13). Twenty ``topic_article`` jobs ran; three
failed with ``ArticleInvalid: model reply has no 'objectives' list`` and a
``topic_questions`` job with ``QuestionsInvalid: model reply has no 'items'
list``. Every one succeeded on a plain re-queue. The worker log had the
truth the job error hid: each reply was COMPLETE (3.5–4.6k output tokens
against a 16k cap) and had exactly one slip in its JSON —

  * ``covers": [``      — a key missing its OPENING quote (char 2857 of 19246)
  * ``spec": {``        — the same slip (char 17552 of 18274)
  * ``- "parts": [``    — a markdown bullet in front of a key (char 14089 of 15305)
  * ``"misconception_ref": "m3"`` written INSIDE an options array, where a
    pair cannot live (char 7334 of 29010)

The client's own salvage (``_repair_json``) could not mend any of them and
returned its ``{"raw_text": text}`` wrapper — a dict, so the validator's
"is it an object?" check passed and its "has it the list?" check produced
the misleading message. Nothing was ever nested or fenced.

What this module does, in order:

  1. ``unwrap_reply`` — a string, or the client's ``raw_text`` wrapper, is
     parsed again with the slips above mended AT THE DECODER'S FAULT
     POSITION. Nothing is rewritten anywhere else in the text: a bullet or
     an unquoted word inside a body paragraph is prose and stays prose. A
     reply nested under one wrapper key (``{"article": {...}}``) is
     unwrapped when the wrapper holds a single object that carries the
     expected key. Anything else is returned as it came, for the validator
     to refuse with its own reason.
  2. ``reply_fault`` — when the reply still is not JSON, the decoder's
     reason and a window of the text, so the job error says what actually
     happened ("Expecting property name … at char 2857 of 19246: …").
  3. ``ask_validated`` — ask once, validate; on the validator's OWN
     exception class ask ONCE more with the same prompt and validate again.
     The second refusal is the job's. Both calls are billed and both land in
     the client's session usage, which the jobs already record.

Rule for repairs, the same one ``_repair_json`` keeps: mend only what is
decidable from JSON's grammar, and never invent content. A stray pair in an
array is DROPPED, not hoisted into the enclosing object — where the model
meant it to go is a guess, and a dropped optional field is a repair the
summary names, while a field placed on the wrong item is a lie nobody sees.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Callable, Optional, TypeVar

from shared.claude_client import _repair_json, json_fault

log = logging.getLogger("worker.reply")

RAW_TEXT = "raw_text"        # the client's wrapper for a reply it could not parse
MAX_MENDS = 16               # slips mended per reply before giving up
_FENCE_OPEN = re.compile(r"^\s*```[a-zA-Z0-9_-]*[ \t]*\r?\n")
_FENCE_CLOSE = re.compile(r"\r?\n[ \t]*```\s*$")
# At the fault position, a property name that lost its opening quote:
# ``covers": [``. Only where the decoder EXPECTED a name — inside a string
# the decoder never stops here.
_UNQUOTED_KEY = re.compile(r'([A-Za-z_][A-Za-z0-9_\-]*)"\s*:')
# At the fault position, a markdown bullet in front of a quoted key:
# ``- "parts": [``.
_BULLET_KEY = re.compile(r'[-*•]\s+(?=")')
_DECODER = json.JSONDecoder(strict=False)

T = TypeVar("T")


def strip_fences(text: str) -> str:
    """The text without a surrounding markdown fence. Pure."""
    out = text or ""
    if _FENCE_OPEN.match(out):
        out = _FENCE_OPEN.sub("", out, count=1)
        out = _FENCE_CLOSE.sub("", out, count=1)
    return out.strip()


def _mend_at(text: str, pos: int, msg: str) -> tuple[Optional[str], Optional[str]]:
    """One mend at the decoder's fault position, or ``(None, None)`` when
    the text there is not a slip this module knows. Returns the mended text
    and a note naming the repair. Pure."""
    m = _UNQUOTED_KEY.match(text, pos)
    if m:
        return text[:pos] + '"' + text[pos:], f"opening quote restored on key '{m.group(1)}' at char {pos}"
    m = _BULLET_KEY.match(text, pos)
    if m:
        return text[:pos] + text[m.end():], f"stray bullet removed before a key at char {pos}"
    if msg.startswith("Expecting ',' delimiter") and text.startswith(":", pos):
        # A string element of an ARRAY followed by ``:`` — a pair written
        # where only values may sit. Find the string that precedes the colon
        # and the value that follows it, and drop the whole pair. ONLY inside
        # an array: in an object the same fault is a value followed by a
        # colon (``{"who": "who": "teacher"}``), which the shared salvage
        # already reads correctly and this rule would maim.
        start = _string_start_before(text, pos)
        if start is not None and _container_at(text, start) == "[":
            try:
                _, end = _DECODER.raw_decode(text, _skip_ws(text, pos + 1))
            except (json.JSONDecodeError, ValueError):
                return None, None
            key = text[start + 1:pos].rstrip()[:-1]
            cut_from = _comma_before(text, start)
            return text[:cut_from] + text[end:], f"stray pair '{key}' dropped from an array at char {start}"
    return None, None


def _skip_ws(text: str, i: int) -> int:
    while i < len(text) and text[i] in " \t\r\n":
        i += 1
    return i


def _string_start_before(text: str, colon: int) -> Optional[int]:
    """Index of the opening quote of the string that ends just before
    ``colon`` (whitespace allowed between), or None when there is no such
    string — then the colon is some other fault and not ours to mend."""
    i = colon - 1
    while i >= 0 and text[i] in " \t\r\n":
        i -= 1
    if i < 0 or text[i] != '"':
        return None
    j = i - 1
    while j >= 0:
        if text[j] == '"':
            # A quote preceded by an odd run of backslashes is escaped.
            k, run = j - 1, 0
            while k >= 0 and text[k] == "\\":
                run += 1
                k -= 1
            if run % 2 == 0:
                return j
        j -= 1
    return None


def _comma_before(text: str, i: int) -> int:
    """Where the cut starts: the comma that separates the dropped pair from
    the element before it, when there is one, else the pair itself."""
    j = i - 1
    while j >= 0 and text[j] in " \t\r\n":
        j -= 1
    return j if j >= 0 and text[j] == "," else i


def _container_at(text: str, i: int) -> Optional[str]:
    """The innermost open bracket (``[`` or ``{``) enclosing position ``i``,
    read by walking the text from the start outside strings; None at the top
    level. Strings are skipped with their escapes honoured, so a bracket
    inside a body paragraph does not count. Pure."""
    stack: list[str] = []
    in_string = False
    k = 0
    while k < i:
        ch = text[k]
        if in_string:
            if ch == "\\":
                k += 1
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "[{":
            stack.append(ch)
        elif ch in "]}" and stack:
            stack.pop()
        k += 1
    return stack[-1] if stack else None


def parse_reply_text(text: str) -> tuple[object, list[str]]:
    """``(parsed, repairs)`` for a reply's text, or ``(None, repairs)`` when
    it cannot be read. The decoder is run, its fault position mended when
    the slip there is a known one, and run again — up to ``MAX_MENDS`` times.
    When the mends are spent or the fault is unknown, the client's shared
    salvage gets the mended text (a trailing comma AFTER a restored quote is
    its job, not ours). Pure."""
    out = strip_fences(text)
    repairs: list[str] = []
    for _ in range(MAX_MENDS):
        try:
            return json.loads(out, strict=False), repairs
        except json.JSONDecodeError as exc:
            mended, note = _mend_at(out, exc.pos, exc.msg)
            if mended is None:
                break
            out, repairs = mended, repairs + [note]
    salvaged = _repair_json(out)
    if salvaged is not None:
        return salvaged, repairs + ["salvaged by the shared JSON repair"]
    return None, repairs


def unwrap_reply(raw: object, key: str) -> tuple[object, list[str]]:
    """The reply as the validator should see it, and the repairs made on the
    way. ``key`` is the list the validator requires (``objectives``,
    ``items``). A string or a ``raw_text`` wrapper is parsed with the slips
    mended; a single-key wrapper object around an object that carries ``key``
    is opened. Anything else — including a reply that still will not parse —
    is returned unchanged, so the validator's own refusal stands and
    ``reply_fault`` can say why. Pure."""
    repairs: list[str] = []
    text = _reply_text(raw)
    if text is not None:
        parsed, repairs = parse_reply_text(text)
        if parsed is None:
            return {RAW_TEXT: text}, repairs
        raw = parsed
    if isinstance(raw, dict) and key not in raw and len(raw) == 1:
        (wrapper, inner), = raw.items()
        if isinstance(inner, dict) and key in inner:
            repairs.append(f"reply unwrapped from '{wrapper}'")
            return inner, repairs
    return raw, repairs


def _reply_text(raw: object) -> Optional[str]:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict) and set(raw) == {RAW_TEXT} and isinstance(raw[RAW_TEXT], str):
        return raw[RAW_TEXT]
    return None


def reply_fault(raw: object) -> Optional[str]:
    """Why an unparsed reply is not JSON — the decoder's message with its
    position and a window of the text — or None when ``raw`` is not the
    client's ``raw_text`` wrapper. Pure."""
    if not isinstance(raw, dict):
        return None
    text = _reply_text(raw)
    if text is None:
        return None
    body = strip_fences(text)
    if not body:
        return "empty reply"
    return json_fault(body) or "it parsed, but carried no usable object"


def ask_validated(ask: Callable[[], object], validate: Callable[[object], T], *,
                  invalid: type[BaseException], what: str, stage: Optional[dict] = None) -> T:
    """``validate(ask())``, and on ``invalid`` — the validator's own class,
    never a transport error — ONE more ``ask()`` with the same prompt and one
    more ``validate``. The second refusal propagates. The first refusal is
    logged and, when ``stage`` is given, appended there under
    ``reply_retries`` (a job may make more than one validated call) so the
    job summary shows the call was paid twice and why."""
    try:
        return validate(ask())
    except invalid as first:
        reason = f"{what}: {type(first).__name__}: {first}"[:300]
        log.warning("%s — model reply refused; asking once more with the same prompt", reason)
        if stage is not None:
            stage.setdefault("reply_retries", []).append(reason)
        return validate(ask())


__all__ = ["RAW_TEXT", "MAX_MENDS", "strip_fences", "parse_reply_text", "unwrap_reply", "reply_fault",
           "ask_validated"]
