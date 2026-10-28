"""The asset-correctness gate: does the picture show what the lesson labels?

Three checks, one model (Haiku 5.5 on Bedrock, `ASSET_GATE_MODEL`), each
asked ONCE per (picture, request) pair and cached on the library row.

  G1  reuse   `same_subject`  — text. Before a library row is hydrated for a
              request, do the row's generation prompt and the request describe
              the same subject with the same named parts? Judged in
              `visual_library._find_one` / `find_colour`, so every reuse path
              (local index, remote, refresh, colour shelf) is covered.
  G2  content `verify_asset`  — vision. At the composer's seam (every picture
              on disk, nothing paid downstream) each distinct diagram is shown
              to the model with the request and the parts the plan points at.
              A picture that does not depict the request is rejected in the
              library, evicted from the cache, regenerated ONCE with reuse
              suspended and re-verified; still wrong → the lesson is refused
              under IMAGE_GATE=strict (a bad video is worse than no video).
  G3  publish `verify_asset`  — vision, in `visual_library.publish_generated`:
              a freshly generated picture that fails is published `rejected`,
              so it exists (for the record) and is never served.

THE CASE. Kit eef426bd asked `two_cells` for a plant cell beside an animal
cell (parts plant_cell_left, animal_cell_right, plant_wall, plant_vacuole,
animal_centrioles). The colour shelf served `two_cells__colour`, an approved
row drawn from "a small bacterium ... beside a larger cell with a round
nucleus" — same key, same layout, different organisms — and the lesson wrote
"Plant cell" with an arrow into the bacterium. Nothing between the key match
and the finished video ever asked what the picture WAS. This module asks.

SWITCHES.  `ASSET_GATE` (default `strict`): `0`/`off` — no calls, every reuse
and publish behaves as before; `warn` — every verdict is computed and logged,
nothing is acted on; `strict` — verdicts act.  `ASSET_GATE_CALLS_PER_LESSON`
(default 8) caps G2's vision calls per generation; a key G1 flagged `unsure`
(`needs_g2`) and a re-verify after regeneration are exempt. G1 text calls are
not budgeted: a pair is judged once ever and identical descriptions cost
nothing. `IMAGE_CALLS_PER_LESSON` still bounds the regeneration itself.

SPEND. Every call goes through ClaudeClient, so it lands in the per-generation
ledger as text spend under `bedrock:claude-haiku-5-5` like any other call;
beside that it writes a `service="asset_gate"` line (check, key, model, usd)
that the ledger folds into `gate_calls` / `gate_usd` / `gate_by_check` — a
split-out of `cost_usd`, not an addition to `total_usd`.

NOTHING HERE KNOWS WHAT A CELL IS. The mechanical pre-check compares tokens
the two descriptions contain; the verdicts are the model's; the parts are the
prompt's own layer-group names, passed verbatim.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)

GATE_ENV = "ASSET_GATE"
MODEL_ENV = "ASSET_GATE_MODEL"
BUDGET_ENV = "ASSET_GATE_CALLS_PER_LESSON"
DEFAULT_MODEL = "claude-haiku-5-5"
DEFAULT_BUDGET = 8
REUSE_PROMPT_VERSION = "gate-reuse-v1"
CONTENT_PROMPT_VERSION = "gate-content-v1"
SAME, DIFFERENT, UNSURE = "same", "different", "unsure"
# how many candidates _find_one may refuse before it gives up on the library
CANDIDATE_ROUNDS = 3
_MAX_PROMPT_CHARS = 1500

# ── switches ─────────────────────────────────────────────────────────────────


def gate_mode() -> str:
    """`off`, `warn` or `strict` (the default and the meaning of `1`)."""
    v = str(os.getenv(GATE_ENV, "") or "").strip().lower()
    if v in ("0", "off", "false", "no"):
        return "off"
    if v == "warn":
        return "warn"
    if v and v not in ("1", "on", "true", "yes", "strict"):
        logger.warning("%s=%r is not off/warn/strict; using strict", GATE_ENV, v)
    return "strict"


def gate_on() -> bool:
    return gate_mode() != "off"


def gate_model() -> str:
    chosen = os.getenv(MODEL_ENV, "").strip()
    if chosen and not chosen.startswith("claude-"):
        logger.warning("%s=%r is not a Claude model; using %s", MODEL_ENV, chosen, DEFAULT_MODEL)
        return DEFAULT_MODEL
    return chosen or DEFAULT_MODEL


def calls_budget() -> int:
    try:
        return max(0, int(os.getenv(BUDGET_ENV, "") or DEFAULT_BUDGET))
    except ValueError:
        return DEFAULT_BUDGET


# ── the model ────────────────────────────────────────────────────────────────
# One seam for tests: replace `_CLIENT_FACTORY`. tests/conftest.py installs a
# factory that raises, so a gate test that forgets its fake fails loudly
# instead of spending.


def _default_client():
    from shared.claude_client import ClaudeClient
    return ClaudeClient(model=gate_model())


_CLIENT_FACTORY: Callable[[], Any] = _default_client

_SYSTEM = ("You check educational drawings before a lesson writes labels onto "
           "them. Answer with JSON only, no prose outside the object.")


def _ask_text(prompt: str, max_tokens: int = 200) -> dict:
    client = _CLIENT_FACTORY()
    out = client.analyze(prompt, system=_SYSTEM, max_tokens=max_tokens, retries=2)
    return {**out, "model": getattr(client, "model", gate_model()),
            "backend": getattr(client, "backend", "unknown")}


def _ask_vision(png_path: Path, prompt: str, max_tokens: int = 400) -> dict:
    client = _CLIENT_FACTORY()
    out = client.analyze_image(str(png_path), prompt, max_tokens=max_tokens, retries=2)
    return {**out, "model": getattr(client, "model", gate_model()),
            "backend": getattr(client, "backend", "unknown")}


def _note_spend(check: str, key: str | None, out: dict) -> None:
    """The gate's own ledger line. The text spend itself is already recorded
    by ClaudeClient.track_tokens; this names WHICH check spent it."""
    try:
        from shared.claude_client import log_external_usage
        usage = out.get("usage") or {}
        log_external_usage("asset_gate", check=check, asset_key=key,
                           model=out.get("model"), backend=out.get("backend"),
                           input_tokens=int(usage.get("input_tokens") or 0),
                           output_tokens=int(usage.get("output_tokens") or 0),
                           usd=float(usage.get("estimated_cost_usd") or 0.0),
                           prompt_version=(REUSE_PROMPT_VERSION if check == "reuse"
                                           else CONTENT_PROMPT_VERSION))
    except Exception:  # noqa: BLE001 — accounting never breaks a render
        pass


# ── hashing ──────────────────────────────────────────────────────────────────


def prompt_hash(prompt: str) -> str:
    return hashlib.sha1(str(prompt or "").strip().encode("utf-8")).hexdigest()


def content_hash_of(png_path: Path) -> str:
    return hashlib.sha256(Path(png_path).read_bytes()).hexdigest()


def _row_identity(row: dict | None) -> str:
    row = row or {}
    return str(row.get("content_hash") or row.get("id")
               or row.get("local_cache_path") or row.get("asset_key") or "")


# ── the mechanical pre-check ────────────────────────────────────────────────
# Words that describe HOW a picture is laid out or drawn, not WHAT it shows.
# Two descriptions sharing only these are not about the same thing.
_LAYOUT_WORDS = frozenset({
    "left", "right", "side", "sides", "beside", "top", "bottom", "middle",
    "centre", "center", "central", "above", "below", "between", "inside",
    "outside", "inner", "outer", "around", "across", "along", "into", "from",
    "its", "it", "is", "are", "be", "by", "at", "as", "or", "that", "this",
    "these", "those", "each", "both", "one", "two", "three", "four", "single",
    "pair", "small", "smaller", "large", "larger", "big", "thick", "thin",
    "round", "rounded", "irregular", "rectangular", "oval", "long", "short",
    "clear", "clearly", "simple", "plain", "white", "background", "labelled",
    "labeled", "label", "labels", "no", "not", "without", "do", "does",
    "name", "named", "names", "layer", "layers", "group", "groups", "exactly",
    "shown", "shows", "showing", "view", "cross", "section", "diagram",
})


def subject_tokens(text: str) -> set[str]:
    """The tokens of a description that could name a subject."""
    from shared.visual_library import _tokens, strip_layer_tail
    return {t for t in _tokens(strip_layer_tail(text))
            if t not in _LAYOUT_WORDS and not t.isdigit() and len(t) > 1}


def row_subject_tokens(row: dict | None, row_description: str | None = None) -> set[str]:
    row = row or {}
    from shared.visual_library import _tokens
    desc = row_description if row_description is not None else row.get("description")
    toks = subject_tokens(str(desc or ""))
    toks |= _tokens(str(row.get("asset_key") or ""), str(row.get("canonical_key") or ""),
                    " ".join(map(str, row.get("concepts") or []))) - _LAYOUT_WORDS
    return {t for t in toks if t and t not in ("colour",)}


def shares_subject_token(requested_prompt: str, row: dict | None,
                         row_description: str | None = None) -> bool:
    return bool(subject_tokens(requested_prompt) & row_subject_tokens(row, row_description))


def _normalised(text: str) -> str:
    from shared.visual_library import strip_layer_tail
    return re.sub(r"\s+", " ", strip_layer_tail(text)).strip().lower()


# ── caches ───────────────────────────────────────────────────────────────────
_CACHE_LOCK = threading.Lock()
_REUSE_CACHE: dict[tuple[str, str], dict] = {}
_CONTENT_CACHE: dict[tuple[str, str], dict] = {}


def reset_caches() -> None:
    """Tests: forget every verdict and every budget."""
    with _CACHE_LOCK:
        _REUSE_CACHE.clear()
        _CONTENT_CACHE.clear()
    with _BUDGET_LOCK:
        _BUDGET.clear()


def _stored_verdict(section: str, row_id: str | None, phash: str) -> dict | None:
    """The row's cached verdict for this request, or None."""
    if not row_id:
        return None
    try:
        from shared.visual_library import read_gate
        entry = ((read_gate(row_id) or {}).get(section) or {}).get(phash)
        return dict(entry) if isinstance(entry, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _store_verdict(section: str, row_id: str | None, phash: str, entry: dict) -> None:
    if not row_id:
        return
    try:
        from shared.visual_library import record_gate
        record_gate(row_id, section, phash, entry)
    except Exception:  # noqa: BLE001
        logger.debug("asset gate: could not record the %s verdict on %s", section, row_id,
                     exc_info=True)


# ── budget (G2 vision calls per generation) ─────────────────────────────────
_BUDGET_LOCK = threading.Lock()
_BUDGET: dict[str, int] = {}


def _generation() -> str:
    try:
        from shared import phase_clock
        return str(phase_clock.current() or "")
    except Exception:  # noqa: BLE001
        return ""


def _take_vision_unit(force: bool) -> bool:
    cap = calls_budget()
    with _BUDGET_LOCK:
        gid = _generation()
        n = _BUDGET.get(gid, 0)
        if not force and n >= cap:
            return False
        _BUDGET[gid] = n + 1
        return True


def gate_budget_state() -> dict:
    with _BUDGET_LOCK:
        return {"generation": _generation(), "used": _BUDGET.get(_generation(), 0),
                "cap": calls_budget()}


# ── G1: the reuse check ─────────────────────────────────────────────────────


def _reuse_prompt(requested: str, requested_parts: list[str], stored: str,
                  stored_parts: list[str]) -> str:
    return (
        f"[{REUSE_PROMPT_VERSION}] A lesson wants to reuse a stored drawing instead of "
        "generating a new one. Decide whether the stored drawing depicts what the lesson "
        "requested.\n\n"
        f"REQUESTED: {requested[:_MAX_PROMPT_CHARS]}\n"
        f"REQUESTED NAMED PARTS: {', '.join(requested_parts) or '(none)'}\n\n"
        f"STORED DRAWING WAS MADE FROM: {stored[:_MAX_PROMPT_CHARS]}\n"
        f"STORED DRAWING'S NAMED PARTS: {', '.join(stored_parts) or '(none)'}\n\n"
        "Do these two descriptions depict the same subject with the same named parts? "
        "Different organisms, objects, structures or processes are DIFFERENT, even when "
        "they share a layout (\"two cells side by side\") or a part name. More or less "
        "detail of the same subject is still the same. Answer exactly:\n"
        '{"verdict": "same" | "different" | "unsure", "reason": "<one sentence>"}'
    )


def same_subject(requested_prompt: str, row_description: str,
                 requested_parts: list[str] | None = None,
                 row_parts: list[str] | None = None, *,
                 row: dict | None = None, key: str | None = None) -> dict:
    """G1. {"verdict": same|different|unsure, "reason", "source", "model"}.

    `source` says how the verdict was reached: off, identical, precheck,
    cache, model, or error (an unreachable model is `unsure`, never a refusal
    and never a silent reuse: the content check downstream is then mandatory).
    """
    from shared.visual_library import prompt_part_names
    if not gate_on():
        return {"verdict": SAME, "reason": "asset gate off", "source": "off"}
    requested_parts = list(requested_parts if requested_parts is not None
                           else prompt_part_names(requested_prompt))
    row_parts = list(row_parts if row_parts is not None
                     else prompt_part_names(row_description))
    req_n, row_n = _normalised(requested_prompt), _normalised(row_description or "")
    if req_n and req_n == row_n:
        return {"verdict": SAME, "reason": "the stored prompt is this request",
                "source": "identical"}
    if not shares_subject_token(requested_prompt, row, row_description):
        return {"verdict": DIFFERENT, "source": "precheck",
                "reason": "the request and the stored description share no subject word"}
    ident = _row_identity(row) or hashlib.sha1(row_n.encode()).hexdigest()
    phash = prompt_hash(requested_prompt)
    ck = (ident, phash)
    with _CACHE_LOCK:
        hit = _REUSE_CACHE.get(ck)
    if hit is None:
        hit = _stored_verdict("reuse", (row or {}).get("id"), phash)
        if hit is not None:
            with _CACHE_LOCK:
                _REUSE_CACHE[ck] = hit
    if hit is not None:
        return {**hit, "source": "cache"}
    try:
        out = _ask_text(_reuse_prompt(requested_prompt, requested_parts,
                                      row_description or "", row_parts))
        _note_spend("reuse", key, out)
        data = out.get("data") if isinstance(out.get("data"), dict) else {}
        verdict = str(data.get("verdict") or "").strip().lower()
        if verdict not in (SAME, DIFFERENT, UNSURE):
            verdict = UNSURE
        entry = {"verdict": verdict, "reason": str(data.get("reason") or "")[:300],
                 "model": out.get("model"), "prompt_version": REUSE_PROMPT_VERSION,
                 "content_hash": (row or {}).get("content_hash"), "at": _now_iso()}
    except Exception as exc:  # noqa: BLE001 — the gate never fails a render
        logger.warning("asset gate (reuse) could not reach the model for %s: %s", key, exc)
        return {"verdict": UNSURE, "reason": f"model unavailable: {type(exc).__name__}",
                "source": "error"}
    with _CACHE_LOCK:
        _REUSE_CACHE[ck] = entry
    _store_verdict("reuse", (row or {}).get("id"), phash, entry)
    logger.info("ASSET GATE reuse %s <- %s: %s (%s)", key, (row or {}).get("asset_key"),
                verdict, entry["reason"])
    return {**entry, "source": "model"}


# the wrapper reads what G1 did for a key when it writes its decision line
_TRAIL = threading.local()


def _trail() -> dict:
    t = getattr(_TRAIL, "by_key", None)
    if t is None:
        t = _TRAIL.by_key = {}
    return t


def reuse_verdict(key: str, prompt: str, row: dict) -> tuple[dict | None, bool]:
    """What `_find_one` / `find_colour` ask: (verdict or None when off, refuse?).
    `refuse` is True only under strict for a `different` verdict; `warn` logs
    the same verdict and reuses anyway. `unsure` reuses and flags `needs_g2`."""
    mode = gate_mode()
    if mode == "off":
        return None, False
    v = same_subject(prompt, str(row.get("description") or ""), row=row, key=key)
    v = dict(v)
    v["needs_g2"] = v["verdict"] == UNSURE
    refuse = mode == "strict" and v["verdict"] == DIFFERENT
    if v["verdict"] == DIFFERENT and mode == "warn":
        logger.warning("ASSET_GATE=warn: strict would have refused %s <- %s — %s",
                       key, row.get("asset_key"), v.get("reason"))
    from shared.asset_keys import canonical_key
    rec = _trail().setdefault(canonical_key(key), {"refused": [], "verdict": None})
    rec["verdict"] = v["verdict"]
    if refuse:
        rec["refused"].append({"id": row.get("id"), "asset_key": row.get("asset_key"),
                               "reason": v.get("reason"), "source": v.get("source")})
    return v, refuse


def g1_trail(key: str, *, clear: bool = True) -> dict:
    """{"refused": [...], "verdict": ...} for `key`, then forgotten."""
    from shared.asset_keys import canonical_key
    ck = canonical_key(key)
    rec = _trail().get(ck) or {"refused": [], "verdict": None}
    if clear:
        _trail().pop(ck, None)
    return rec


# ── reuse suspension (the G2 regeneration draws fresh) ──────────────────────
_SUSPENDED: contextvars.ContextVar[frozenset] = contextvars.ContextVar(
    "asset_gate_reuse_suspended", default=frozenset())


@contextlib.contextmanager
def suspend_reuse(key: str):
    from shared.asset_keys import canonical_key
    token = _SUSPENDED.set(_SUSPENDED.get() | {canonical_key(key)})
    try:
        yield
    finally:
        _SUSPENDED.reset(token)


def reuse_suspended(key: str) -> bool:
    from shared.asset_keys import canonical_key
    return canonical_key(key) in _SUSPENDED.get()


# ── G2 / G3: the content check ──────────────────────────────────────────────


def _content_prompt(requested: str, part_names: list[str],
                    label_layer_pairs: list[tuple[str, str]] | None) -> str:
    pairs = "; ".join(f'"{t}" -> {l}' for t, l in (label_layer_pairs or []))
    return (
        f"[{CONTENT_PROMPT_VERSION}] A lesson will write labels and draw arrows onto this "
        "picture. Judge whether the picture shows what was requested.\n\n"
        f"REQUESTED: {requested[:_MAX_PROMPT_CHARS]}\n"
        f"NAMED PARTS (regions the lesson may point at): {', '.join(part_names) or '(none)'}\n"
        f"LABELS THE LESSON WILL WRITE: {pairs or '(none listed)'}\n\n"
        "For each named part say whether the drawing in that region is that thing: "
        "\"ok\" (drawn and is that thing), \"wrong\" (something else is drawn there), "
        "\"missing\" (not drawn). Then overall: does the picture depict the requested "
        "subject, so that the labels would be true of what they point at? A picture of "
        "a different organism, object or process is NOT ok however similar the layout. "
        "Answer exactly:\n"
        '{"ok": true | false, "parts": {"<part>": "ok" | "wrong" | "missing"}, '
        '"reason": "<one sentence naming what the picture actually shows>"}'
    )


def verify_asset(png_path: Path | str, requested_prompt: str,
                 part_names: list[str] | None = None,
                 label_layer_pairs: list[tuple[str, str]] | None = None, *,
                 row_id: str | None = None, content_hash: str | None = None,
                 key: str | None = None, force: bool = False) -> dict:
    """G2/G3. {"ok": True|False|None, "parts": {...}, "reason", "source", "model"}.

    `ok` is None when the check did not run (gate off, budget spent, model
    unreachable, file unreadable) — callers treat only `ok is False` as a
    failure. `force` ignores the per-lesson budget (a re-verify after
    regeneration, a key G1 flagged `needs_g2`).
    """
    from shared.visual_library import prompt_part_names
    if not gate_on():
        return {"ok": None, "parts": {}, "reason": "asset gate off", "source": "off"}
    png_path = Path(png_path)
    try:
        chash = content_hash or content_hash_of(png_path)
    except OSError as exc:
        return {"ok": None, "parts": {}, "reason": f"unreadable: {exc}", "source": "error"}
    parts = list(part_names if part_names is not None else prompt_part_names(requested_prompt))
    phash = prompt_hash(requested_prompt)
    ck = (chash, phash)
    with _CACHE_LOCK:
        hit = _CONTENT_CACHE.get(ck)
    if hit is None:
        hit = _stored_verdict("content", row_id, phash)
        if hit is not None and str(hit.get("content_hash") or chash) == chash:
            with _CACHE_LOCK:
                _CONTENT_CACHE[ck] = hit
        else:
            hit = None
    if hit is not None:
        return {**hit, "source": "cache"}
    if not _take_vision_unit(force):
        logger.warning("asset gate: %s not checked — %s=%d vision call(s) spent this lesson",
                       key, BUDGET_ENV, calls_budget())
        return {"ok": None, "parts": {}, "reason": "gate budget spent", "source": "budget"}
    try:
        out = _ask_vision(png_path, _content_prompt(requested_prompt, parts, label_layer_pairs))
        _note_spend("content", key, out)
        data = out.get("data") if isinstance(out.get("data"), dict) else {}
        raw_parts = data.get("parts") if isinstance(data.get("parts"), dict) else {}
        judged = {str(p): str(raw_parts.get(p) or "missing").strip().lower() for p in parts}
        for p, v in judged.items():
            if v not in ("ok", "wrong", "missing"):
                judged[p] = "missing"
        ok = bool(data.get("ok")) and not any(v == "wrong" for v in judged.values())
        entry = {"ok": ok, "parts": judged, "reason": str(data.get("reason") or "")[:300],
                 "model": out.get("model"), "prompt_version": CONTENT_PROMPT_VERSION,
                 "content_hash": chash, "prompt_hash": phash, "at": _now_iso()}
    except Exception as exc:  # noqa: BLE001 — the gate never fails a render by itself
        logger.warning("asset gate (content) could not reach the model for %s: %s", key, exc)
        return {"ok": None, "parts": {}, "reason": f"model unavailable: {type(exc).__name__}",
                "source": "error"}
    with _CACHE_LOCK:
        _CONTENT_CACHE[ck] = entry
    _store_verdict("content", row_id, phash, entry)
    (logger.info if ok else logger.warning)(
        "ASSET GATE content %s: %s — %s (parts %s)", key, "ok" if ok else "WRONG",
        entry["reason"], judged)
    return {**entry, "source": "model"}


def content_gate_doc(verdict: dict | None) -> dict:
    """The `vision.gate` document a fresh row is born with, from a verdict."""
    if not verdict or verdict.get("ok") is None or not verdict.get("prompt_hash"):
        return {}
    entry = {k: v for k, v in verdict.items() if k != "source"}
    return {"content": {verdict["prompt_hash"]: entry}}


# ── the seam: every diagram of a lesson ─────────────────────────────────────


def _skip_reason(key: str) -> str | None:
    from shared.asset_keys import is_avatar_key
    k = str(key or "")
    if is_avatar_key(k) or k.startswith("avatar_"):
        return "avatar"
    if k.startswith("sk_"):
        return "sk_asset"
    if k == "hand_pen":
        return "hand"
    return None


def label_layer_pairs(scenes: Iterable[dict]) -> dict[str, list[tuple[str, str]]]:
    """asset key -> [(label text, layer)] for every arrow whose head anchors a
    layer of an illustration and whose tail sits on a text element."""
    out: dict[str, list[tuple[str, str]]] = {}
    for scene in scenes or []:
        if not isinstance(scene, dict):
            continue
        assets, texts = {}, {}
        for el in scene.get("elements") or []:
            if not isinstance(el, dict):
                continue
            if el.get("type") == "illustration" and isinstance(el.get("asset"), str):
                assets[el.get("id")] = el["asset"]
            elif el.get("type") == "text" and isinstance(el.get("text"), str):
                texts[el.get("id")] = el["text"]
        for el in scene.get("elements") or []:
            if not isinstance(el, dict) or el.get("type") != "arrow":
                continue
            head, tail = el.get("head"), el.get("tail")
            if not (isinstance(head, dict) and isinstance(head.get("layer"), str)):
                continue
            ak = assets.get(head.get("el"))
            if not ak:
                continue
            text = texts.get(tail.get("el")) if isinstance(tail, dict) else None
            pair = (str(text or "").strip() or "(arrow)", head["layer"])
            if pair not in out.setdefault(ak, []):
                out[ak].append(pair)
    return out


def _evict(asset_dir: Path) -> None:
    for name in ("asset.png", "meta.json"):
        try:
            (asset_dir / name).unlink(missing_ok=True)
        except OSError:
            pass


def verify_lesson_assets(entries, *, scenes=None, pending_keys=(), cache_dir=None,
                         regenerate=None) -> dict:
    """G2 over every distinct non-avatar diagram of a lesson.

    Returns {"checked", "ok": [keys], "refused": {key: reason}, "regenerated":
    [keys], "skipped": {key: why}, "warned": {key: reason}}. `refused` is what
    the composer raises on under IMAGE_GATE=strict; under ASSET_GATE=warn
    nothing is acted on and the same keys land in `warned`.
    """
    out: dict[str, Any] = {"checked": 0, "ok": [], "refused": {}, "regenerated": [],
                           "skipped": {}, "warned": {}}
    mode = gate_mode()
    if mode == "off":
        return out
    from spike.scene_engine import raster_assets as ra
    from shared import visual_library as vl
    from shared.asset_keys import canonical_key
    regenerate = regenerate or ra.get_raster_asset
    pairs_by_asset = {canonical_key(k): v for k, v in label_layer_pairs(scenes or []).items()}
    pending = {canonical_key(k) for k in (pending_keys or ())}
    seen: set[str] = set()
    for key, prompt in entries or []:
        ck = canonical_key(key)
        if ck in seen:
            continue
        seen.add(ck)
        why = _skip_reason(key)
        if why:
            out["skipped"][key] = why
            continue
        if ck in pending:
            out["skipped"][key] = "pending"
            continue
        fetch_key = ra.colour_key(key)          # the cache entry the warm pass filled
        asset_dir = ra.cache_dir_for(fetch_key, cache_dir)
        png, meta = asset_dir / "asset.png", asset_dir / "meta.json"
        if not png.exists():
            out["skipped"][key] = "no_picture"
            continue
        md = _read_json(meta)
        parts = list(vl.prompt_part_names(prompt))
        pairs = pairs_by_asset.get(ck) or []
        needs_g2 = bool((md.get("gate") or {}).get("needs_g2"))
        res = verify_asset(png, prompt, parts, pairs, row_id=md.get("library_asset_id"),
                           content_hash=md.get("content_hash"), key=key, force=needs_g2)
        if res.get("ok") is None:
            out["skipped"][key] = str(res.get("source") or "unchecked")
            continue
        out["checked"] += 1
        if res["ok"]:
            out["ok"].append(key)
            continue
        reason = str(res.get("reason") or "the picture does not show the request")
        if mode == "warn":
            logger.warning("ASSET_GATE=warn: strict would have rejected %s — %s", key, reason)
            out["warned"][key] = reason
            continue
        # strict: the row never serves again, the cache entry goes, one fresh
        # draw with reuse suspended, then the same question of the new picture
        row_id = str(md.get("library_asset_id") or "")
        if row_id:
            vl.reject_asset(row_id, f"gate_g2: {reason}")
        with ra.asset_lock(fetch_key):
            _evict(asset_dir)
        logger.warning("ASSET GATE %s rejected (%s); regenerating once with library reuse off",
                       key, reason)
        with suspend_reuse(key):
            got = None
            try:
                got = regenerate(key, prompt)
            except Exception as exc:  # noqa: BLE001
                logger.warning("asset gate: regeneration of %s failed: %s", key, exc)
        out["regenerated"].append(key)
        asset_dir = ra.cache_dir_for(fetch_key, cache_dir)
        png, meta = asset_dir / "asset.png", asset_dir / "meta.json"
        if got is None or not png.exists():
            out["refused"][key] = f"content_wrong: {reason}; regeneration produced no picture"
            continue
        md2 = _read_json(meta)
        res2 = verify_asset(png, prompt, parts, pairs, row_id=md2.get("library_asset_id"),
                            key=key, force=True)
        if res2.get("ok") is False:
            out["refused"][key] = f"content_wrong: {res2.get('reason') or reason}"
        else:
            out["ok"].append(key)
    if out["refused"]:
        logger.error("ASSET GATE refused %d picture(s): %s", len(out["refused"]),
                     "; ".join(f"{k}: {r}" for k, r in out["refused"].items()))
    return out


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
