"""Persistent visual knowledge library for SketchCast.

The library is deliberately separate from the renderer. It stores metadata in
Postgres and binary assets in Supabase Storage, while keeping a local cache for
fast renders. Generated visuals become reusable assets after the existing
renderer validation succeeds.

Lookup order:
  1. exact/canonical local cache
  2. approved Supabase library match
  3. caller's normal AI generation path

A generated asset is published with curriculum/subject/grade metadata and a
semantic description. The first implementation uses deterministic token
matching; the schema leaves room for pgvector/embeddings later without making
embeddings a runtime dependency today.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shared.asset_keys import (all_noise, canonical_key, core_tokens,
                               distinguishes, is_avatar_key)
from shared.asset_keys import tokens as _tokens_of

logger = logging.getLogger(__name__)

BUCKET = os.getenv("VISUAL_LIBRARY_BUCKET", "visual-assets")
LIBRARY_DIR = Path(os.getenv(
    "VISUAL_LIBRARY_DIR",
    str(Path(__file__).resolve().parents[1] / "storage" / "visual_library"),
))

_STOP = {
    "a", "an", "and", "the", "of", "for", "to", "in", "on", "with",
    "image", "images", "illustration", "illustrations", "diagram", "diagrams",
    "drawing", "drawings", "picture", "pictures", "visual", "visuals", "asset",
    "assets", "sketch", "art", "show", "showing", "simple", "educational",
    "whiteboard", "style", "hand", "drawn",
}


@dataclass(frozen=True)
class LibraryContext:
    """Curriculum context attached to an asset at ingest/lookup time."""

    curriculum: str = "generic"
    subject: str = "general"
    grade: str = "k12"
    topic: str = ""
    concepts: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "LibraryContext":
        value = value or {}
        concepts = value.get("concepts") or ()
        if isinstance(concepts, str):
            concepts = (concepts,)
        return cls(
            curriculum=str(value.get("curriculum") or "generic").strip().lower(),
            subject=str(value.get("subject") or "general").strip().lower(),
            grade=str(value.get("grade") or "k12").strip().lower(),
            topic=str(value.get("topic") or "").strip().lower(),
            concepts=tuple(str(x).strip().lower() for x in concepts if str(x).strip()),
        )


# canonical_key is imported from shared.asset_keys, NOT defined here. This
# module had its own copy whose stop-list kept "cell"/"figure", so for every
# *_cell key in the biology curriculum it disagreed with the renderer's fold:
# hydrate() filed a downloaded picture under cache/cell_ciliated/ while the
# renderer only ever reads cache/ciliated/. Every cell-key library hit landed
# where nobody looks and the picture was generated again.
#
# _STOP stays, but only for scoring PROSE (descriptions, prompts), where words
# like "showing" and "whiteboard" are genuinely noise.


# ── avatars are not educational visuals ──────────────────────────────────────
# The schema has always had asset_type/role/age_band, but publish_generated
# never set them, so every avatar was stored as asset_type='visual' (the column
# default). Measured on the real library: 9 avatars, all typed 'visual'.
#
# That is not a cosmetic mislabel. `find()` is the reuse path for EDUCATIONAL
# artwork, and it filtered on status alone — so a lesson asking for a picture
# of a person could be handed the teacher avatar, and a persistent character
# would appear mid-diagram. The two retrieval domains share one table by
# design; asset_type is the only thing keeping them apart.

_AVATAR_ROLES = ("teacher", "student")
_AGE_BAND_RE = re.compile(r"(\d{1,2}_\d{1,2})")


# is_avatar_key is imported from shared.asset_keys: the renderer needs the
# SAME answer, to keep an unresolvable avatar out of the placeholder tier.


def avatar_fields(key: str) -> dict[str, Any]:
    """asset_type/role/age_band for an asset key.

    Returns the ordinary-visual shape for anything that is not an avatar, so
    callers can merge it unconditionally.
    """
    k = str(key or "").strip().lower()
    if not is_avatar_key(k):
        return {"asset_type": "visual", "role": None, "age_band": None}
    role = next((r for r in _AVATAR_ROLES if r in k), None)
    m = _AGE_BAND_RE.search(k)
    return {"asset_type": "avatar", "role": role,
            "age_band": m.group(1) if m else None}


# ── format is a SECOND axis, not a second library ─────────────────────────────
# asset_type says what an asset is FOR (educational visual vs persistent
# character). asset_format says what its BYTES ARE. They are independent: an
# avatar PNG and an educational SVG differ on both, and neither implies the
# other. There is exactly ONE library — an SVG is a row with a different
# format, stored beside the PNGs in the same bucket, and it is never
# rasterised to fit the older path. The markup is the canonical asset.

ASSET_FORMATS = ("png", "svg")
DEFAULT_FORMAT = "png"
CONTENT_TYPES = {"png": "image/png", "svg": "image/svg+xml"}


def normalize_format(value: Any) -> str:
    """A format name, or DEFAULT_FORMAT for anything unrecognised.

    Accepts what callers actually hold: 'svg', '.svg', 'SVG', a Path suffix.
    Anything else reads as a PNG, because that is what every asset published
    before this column existed is.
    """
    v = str(value or "").strip().lower().lstrip(".")
    return v if v in ASSET_FORMATS else DEFAULT_FORMAT


def row_format(row: dict[str, Any] | None) -> str:
    """The format of a STORED row, without needing the column to exist.

    Belt-and-braces exactly like is_avatar_row: the 230 rows published before
    asset_format existed carry no such key, and they are all PNGs. When the
    column is absent the stored object's own extension answers instead, so a
    row written by a newer worker against an un-migrated database still reads
    correctly.
    """
    if not row:
        return DEFAULT_FORMAT
    explicit = str(row.get("asset_format") or "").strip().lower()
    if explicit in ASSET_FORMATS:
        return explicit
    path = str(row.get("storage_path") or row.get("local_cache_path") or "")
    suffix = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return suffix if suffix in ASSET_FORMATS else DEFAULT_FORMAT


def row_group_ids(row: dict[str, Any] | None) -> list[str]:
    """The EXACT group ids stored on a row, in drawing order."""
    if not row:
        return []
    return [str(g) for g in (row.get("group_ids") or [])]


def row_has_parts(row: dict[str, Any] | None, wanted) -> bool:
    """Whether a stored asset contains every part a lesson wants to label,
    answered from the ROW — no download.

    Storage is exact and matching is tolerant, and this is the seam between
    them: the row records "chloroplasts" verbatim, a lesson asks for
    "chloroplast", and the same matcher the renderer uses to pick layers
    decides they are the same part. Using a different rule here would let the
    library promise a part the renderer then cannot find.

    A row with no recorded groups (every PNG, and any SVG published before the
    column existed) answers False for a real request rather than guessing.
    """
    want = [str(w) for w in (wanted or []) if str(w).strip()]
    if not want:
        return True
    from spike.scene_engine.vector_assets import match_layer_ids
    available = row_group_ids(row)
    if not available:
        return False
    return all(match_layer_ids(available, [w]) for w in want)


def is_avatar_row(row: dict[str, Any] | None) -> bool:
    """Whether a stored row is an avatar, by type OR by key.

    Deliberately belt-and-braces. A missing asset_type reads as a VISUAL (that
    is the column default and what every pre-fix row carries), so the key check
    is what keeps an already-published avatar out of educational retrieval
    without a backfill.
    """
    if not row:
        return False
    if str(row.get("asset_type") or "").lower() == "avatar":
        return True
    return is_avatar_key(str(row.get("asset_key") or row.get("canonical_key") or ""))


def _tokens(*values: str) -> set[str]:
    text = " ".join(str(v or "") for v in values).lower()
    return {t for t in re.split(r"[^a-z0-9]+", text) if t and t not in _STOP}


def infer_context(key: str, prompt: str, context: dict[str, Any] | None = None) -> LibraryContext:
    """Fill missing context conservatively from explicit values and text.

    This is deliberately lightweight. It is metadata enrichment, not a claim
    that a model can reliably infer curriculum alignment from arbitrary prose.
    Explicit caller context always wins.
    """
    base = LibraryContext.from_dict(context)
    toks = _tokens(key, prompt)
    subject = base.subject
    if subject == "general":
        for name, words in {
            "biology": {"cell", "mitochondria", "nucleus", "heart", "plant", "photosynthesis", "neuron", "dna"},
            "chemistry": {"atom", "molecule", "bond", "acid", "alkali", "electrolysis", "reaction"},
            "physics": {"force", "energy", "circuit", "magnet", "wave", "lens", "velocity", "pulley"},
            "mathematics": {"triangle", "fraction", "equation", "angle", "probability", "graph", "algebra", "circle"},
            "geography": {"river", "volcano", "climate", "latitude", "longitude", "erosion", "tectonic"},
            "history": {"roman", "medieval", "renaissance", "reformation", "revolution", "empire", "castle"},
            "computer_science": {"algorithm", "binary", "network", "storage", "cpu", "database", "internet"},
        }.items():
            if toks & words:
                subject = name
                break
    topic = base.topic or str(key).strip().lower()
    return LibraryContext(base.curriculum, subject, base.grade, topic, base.concepts)


# The "Name the layer groups exactly: a, b, c." tail addresses the VISION
# annotator, not the reader, and it is the same shape on every asset prompt in
# the library. Scored, it is a bag of shared tokens that lifts every
# comparison — part of how "ciliated_cell" reached 1.23 against a red blood
# cell. Stripped from both sides, exactly as raster_assets strips it before
# generating.
_LAYER_TAIL = re.compile(r"\s*name the layer groups exactly:[^.]*\.?", re.I)


def strip_layer_tail(text: str) -> str:
    return _LAYER_TAIL.sub("", str(text or ""))


# ── a match must be ABOUT the thing that was asked for ───────────────────────
# Scoring is a bag of tokens over key + description, so "ciliated_cell" scored
# 1.23 against red_blood_cell (shared: cell, blood-vessel prose, the boilerplate
# tail, plus +0.40 of subject/curriculum/grade bonuses) — over the 0.58
# threshold. It was accepted, hydrated, and the neurone board in fa8c0d7d shows
# a red blood cell. A wrong picture is not a smaller version of a missing one:
# it teaches something false while looking confident, and no report catches it.
#
# So before any score can clear the bar, the REQUESTED key and the candidate's
# own key must share at least one core token. Prose alone can never carry a
# match.

# "sk" is the auto-sketch NAMESPACE, not part of what the picture is of.
# Left in, every sketch key overlapped every other sketch key on that one
# token — which is how sk_boat matched sk_ant at 0.90 and the "boat" in
# fa8c0d7d is an ant. Removed, sk_person still answers a request for a person.
_KEY_NAMESPACE_TOKENS = frozenset({"sk"})

# Connectives never say WHICH picture an asset is. Left in, `cells_to_tissue`
# and `tissues_to_organ_diagram` shared exactly one guard token -- "to" -- and
# scored 0.771 against each other, over the 0.58 default, so the whole
# levels-of-organisation family could still serve one another's diagrams.
#
# Subtracted HERE and not added to KEY_NOISE on purpose: KEY_NOISE decides the
# cache directory, and folding "to" away would make `cells_to_tissue` and a
# plain `tissue` one cache entry -- two different pictures in one file.
_CONNECTIVES = frozenset({"to", "for", "in", "on", "with", "from", "into",
                          "by", "at", "vs", "versus"})


def guard_tokens(value: str) -> set[str]:
    """The tokens a key must share with another key to be about the same
    thing: its core tokens, minus the namespace prefix and the connectives."""
    return core_tokens(value) - _KEY_NAMESPACE_TOKENS - _CONNECTIVES


def _fallback_tokens(value: str) -> set[str]:
    """Every token of a key, for a key that has no distinguishing one."""
    return set(_tokens_of(value)) - _KEY_NAMESPACE_TOKENS - _CONNECTIVES


def key_guard_ok(query_key: str, row: dict[str, Any] | None) -> bool:
    """Whether `row` is even a candidate for `query_key`.

    A necessary condition, not a sufficient one — the score still has to clear
    the threshold. What it forbids is a match carried entirely by PROSE.
    """
    if not row:
        return False
    ak, ck = row.get("asset_key") or "", row.get("canonical_key") or ""
    # Same cache identity, same picture — by definition, everywhere else in
    # this system. Checked first so a key with nothing distinguishing in it
    # ("figure_3") can still be answered by ITSELF, which the abstention rule
    # below would otherwise refuse.
    qc = canonical_key(query_key)
    if qc and qc in {canonical_key(k) for k in (ak, ck) if k}:
        return True
    q = guard_tokens(query_key)
    r = guard_tokens(ak) | guard_tokens(ck)
    undistinguished = (all_noise(query_key)
                       or all(all_noise(k) for k in (ak, ck) if k))
    # The shared token has to be one that NARROWS. `guard_tokens` keeps
    # numerals (they are what separates `stage_2` from `stage_3` in the
    # cache), so without this `stage_3` and `phase_3` share "3" and each
    # becomes a candidate for the other's picture -- the same collapse the
    # canonical key was just taught to avoid, arriving through the guard.
    if q and r and not undistinguished and any(distinguishes(t) for t in q & r):
        return True
    # A key made ENTIRELY of noise ("cell_diagram", "cells") keeps its noise
    # words through core_tokens' fallback, while every candidate row has had
    # them stripped -- so the guard could never be satisfied and a request the
    # library answers correctly today (cell_diagram -> animal_cell_diagram,
    # score 1.40) became a paid regeneration and fresh 429 exposure. When
    # either side carries nothing distinguishing, the guard has nothing to
    # assert: compare the raw tokens and let the score decide.
    if not q or not r or undistinguished:
        qf, rf = _fallback_tokens(query_key), (_fallback_tokens(ak)
                                               | _fallback_tokens(ck))
        shared = qf & rf
        # …but the abstention still has to REST on something. Raw-token
        # overlap alone let `cell_diagram` be a candidate for
        # `volcano_diagram` on the word "diagram", and `the_picture` for
        # anything at all: every key in the library carries a medium word, so
        # an all-noise request became eligible for the whole catalogue with
        # only the threshold left in the way. At least one shared token must
        # name something — not a medium word, not a bare numeral.
        carriers = {t for t in shared if distinguishes(t)}
        ok = bool(carriers)
        # Logged, because this is the one path where the guard abstains: the
        # threshold is the only thing standing between the request and a wrong
        # picture, and "why did cells match X" has to be answerable.
        logger.debug("key guard abstained for %r vs %r/%r (nothing "
                     "distinguishing on %s); shared=%s carrying=%s",
                     query_key, ak, ck,
                     "the request" if all_noise(query_key) else "the row",
                     sorted(shared), sorted(carriers))
        if shared and not ok:
            logger.info("visual library: refused %s <- %s/%s — the only "
                        "tokens they share (%s) name a medium or an index, "
                        "not a subject", query_key, ak, ck,
                        ", ".join(sorted(shared)))
        return ok
    return False


def _score(row: dict[str, Any], query_key: str, prompt: str, ctx: LibraryContext) -> float:
    key = str(row.get("canonical_key") or row.get("asset_key") or "")
    desc = strip_layer_tail(row.get("description") or "")
    concepts = row.get("concepts") or []
    row_tokens = _tokens(key, desc, " ".join(map(str, concepts)))
    query_tokens = _tokens(query_key, strip_layer_tail(prompt), ctx.topic,
                           " ".join(ctx.concepts))
    if not query_tokens or not row_tokens:
        return 0.0
    overlap = len(query_tokens & row_tokens) / max(1, len(query_tokens))
    score = overlap
    if str(row.get("subject") or "").lower() == ctx.subject:
        score += 0.20
    if str(row.get("curriculum") or "").lower() in {ctx.curriculum, "generic"}:
        score += 0.10
    if str(row.get("grade") or "").lower() in {ctx.grade, "k12", "all"}:
        score += 0.10
    return score


def _asset_dir(cache_dir: Path, key: str, fmt: str = DEFAULT_FORMAT) -> Path:
    """The renderer's cache directory for `key` in `fmt`.

    The SVG layout is asked of the module that OWNS it rather than reproduced
    here. Two copies of a path fold is the exact bug that made every *_cell
    library hit land where nobody reads and be paid for again; there is no
    reason to reintroduce it one format later.
    """
    if normalize_format(fmt) == "svg":
        from spike.scene_engine.svg_assets import svg_cache_dir
        return svg_cache_dir(cache_dir, key)
    return cache_dir / canonical_key(key)


def _local_meta_path(cache_dir: Path, key: str,
                     fmt: str = DEFAULT_FORMAT) -> Path:
    return _asset_dir(cache_dir, key, fmt) / "meta.json"


def _local_asset_path(cache_dir: Path, key: str,
                      fmt: str = DEFAULT_FORMAT) -> Path:
    return _asset_dir(cache_dir, key, fmt) / f"asset.{normalize_format(fmt)}"


def _sb():
    """Lazy Supabase admin client; visual library remains usable offline."""
    if not os.getenv("SUPABASE_URL") or not os.getenv("SUPABASE_SERVICE_ROLE_KEY"):
        return None
    try:
        from worker.client import admin
        return admin()
    except Exception as exc:  # noqa: BLE001
        logger.debug("visual library Supabase client unavailable: %s", exc)
        return None


def _local_candidates() -> list[dict[str, Any]]:
    index = LIBRARY_DIR / "index.json"
    if not index.exists():
        return []
    try:
        data = json.loads(index.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        logger.warning("visual library index is unreadable: %s", index)
        return []


def _write_local_index(rows: list[dict[str, Any]]) -> None:
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    tmp = LIBRARY_DIR / "index.json.tmp"
    tmp.write_text(json.dumps(rows, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(LIBRARY_DIR / "index.json")


def register_local(row: dict[str, Any]) -> None:
    """Register metadata locally; safe to call from every generation."""
    rows = _local_candidates()
    key = str(row.get("asset_key") or row.get("canonical_key") or "")
    rows = [r for r in rows if str(r.get("asset_key") or r.get("canonical_key")) != key]
    rows.append(row)
    _write_local_index(rows)


def find(key: str, prompt: str, context: dict[str, Any] | None = None,
         *, min_score: float | None = None,
         asset_format: str | None = None) -> dict[str, Any] | None:
    """Find an approved reusable asset without invoking an AI model.

    `asset_format` restricts the candidates to one format. The two tiers ask
    for what they can actually USE: the SVG tier wants markup it can parse
    into layers, the raster tier wants pixels. Serving either the other's
    bytes would be a cache miss dressed as a hit — a file written where the
    caller does not look, which is a mistake this module has already made
    once.
    """
    ctx = infer_context(key, prompt, context)
    threshold = float(os.getenv("VISUAL_LIBRARY_MIN_SCORE", "0.58")) if min_score is None else min_score
    # `guarded`: only rows whose OWN key shares a core token with the request
    # may be served. best_match() without it still sees everything, so the
    # near-miss evidence the threshold argument runs on keeps flowing.
    best, best_score, source = best_match(key, prompt, context, _ctx=ctx,
                                          guarded=True,
                                          asset_format=asset_format)
    if best is None or best_score < threshold:
        near, near_score, _ = best_match(key, prompt, context, _ctx=ctx,
                                         asset_format=asset_format)
        if near is not None and near_score >= threshold and not key_guard_ok(key, near):
            logger.info("visual library: refused %s <- %s (score %.2f) — the "
                        "keys share no concept token", key,
                        near.get("asset_key"), near_score)
        return None
    return {**best, "match_score": round(best_score, 4),
            "match_source": source, "context": ctx.__dict__}


def threshold_now() -> float:
    return float(os.getenv("VISUAL_LIBRARY_MIN_SCORE", "0.58"))


def best_match(key: str, prompt: str, context: dict[str, Any] | None = None,
               *, _ctx: LibraryContext | None = None, guarded: bool = False,
               asset_format: str | None = None):
    """The best candidate and its score, WITHOUT applying the threshold.

    Split out of find() so a near-miss is observable. find() returns None
    below the cut, which is correct behaviour and useless evidence: the
    question "is 0.58 the right threshold" cannot be answered from data that
    only records the matches we already accepted. The decision log records
    this score whether or not it cleared the bar.

    `guarded=True` (what find() uses) restricts the candidates to rows whose
    own key shares a core token with the request, so the best SERVEABLE row is
    chosen rather than the best-scoring one being chosen and then refused.

    `asset_format` restricts them to one format. It is applied in PYTHON, not
    in the remote query: the column does not exist on an un-migrated database,
    and a filter that errors there would take the whole remote search down
    with it rather than one row.

    Returns (row | None, score, source) where source is 'local' or 'remote'.
    """
    ctx = _ctx or infer_context(key, prompt, context)
    want_format = normalize_format(asset_format) if asset_format else None

    def _eligible(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if want_format is not None:
            candidates = [r for r in candidates
                          if row_format(r) == want_format]
        return [r for r in candidates if key_guard_ok(key, r)] if guarded \
            else candidates

    # Educational retrieval NEVER sees avatars. Filtered on both sides: the
    # remote query narrows in Postgres (cheap, and keeps the 250-row window for
    # real visuals rather than spending it on avatars), and both result sets
    # are filtered again in Python by is_avatar_row(), which also catches rows
    # published before asset_type was set.
    rows = _eligible([r for r in _local_candidates() if not is_avatar_row(r)])
    best = max(rows, key=lambda r: _score(r, key, prompt, ctx), default=None)
    best_score = _score(best, key, prompt, ctx) if best else 0.0
    source = "local" if best is not None else "none"

    sb = _sb()
    if sb is not None:
        try:
            remote = (sb.table("visual_assets").select("*")
                      .eq("status", "approved")
                      .neq("asset_type", "avatar")
                      .limit(250).execute().data or [])
            remote = _eligible([r for r in remote if not is_avatar_row(r)])
            remote_best = max(remote, key=lambda r: _score(r, key, prompt, ctx), default=None)
            remote_score = _score(remote_best, key, prompt, ctx) if remote_best else 0.0
            if remote_score > best_score:
                best, best_score, source = remote_best, remote_score, "remote"
        except Exception as exc:  # noqa: BLE001
            logger.debug("visual library remote search skipped: %s", exc)

    return best, best_score, source


# ── decision log ─────────────────────────────────────────────────────────────
# One line per visual request, so the threshold can be judged on evidence
# rather than on taste. A false MISS costs an image call; a false HIT teaches
# the wrong thing while looking confident, which is not the same kind of
# mistake. The borderline band is what this exists to expose.
#
# Two sinks, no new infrastructure: a JSONL file for local analysis, and one
# structured line on the normal logger so the same record is searchable in
# Railway's log stream, where the worker's filesystem does not survive a
# redeploy.
DECISION_LOG = Path(os.getenv(
    "VISUAL_LIBRARY_DECISION_LOG",
    str(LIBRARY_DIR / "decisions.jsonl")))
_DECISION_LOCK = __import__("threading").Lock()
DECISION_PREFIX = "VISUAL_LIBRARY_DECISION"


def log_decision(record: dict[str, Any]) -> None:
    """Append one retrieval decision. Never raises: instrumentation that can
    break a render is worse than no instrumentation."""
    from datetime import datetime, timezone
    entry = {**record, "timestamp": datetime.now(timezone.utc).isoformat()}
    try:
        logger.info("%s %s", DECISION_PREFIX, json.dumps(entry, default=str))
    except Exception:  # noqa: BLE001
        pass
    try:
        with _DECISION_LOCK:
            DECISION_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(DECISION_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
    except Exception:  # noqa: BLE001
        pass


def hydrate(key: str, prompt: str, cache_dir: Path,
            context: dict[str, Any] | None = None,
            *, asset_format: str | None = None) -> dict[str, Any] | None:
    """Download a remote approved asset into the renderer's existing cache.

    Cached under the REQUESTED key, not the matched one. The caller looks for
    the path built from ``canonical_key(key)`` — that is the only path it will
    ever check — so filing the download under the match's key left the file
    somewhere nobody looks.

    Measured end-to-end: a reworded request for a volcano cross-section
    matched the stored asset at score 1.00 and the renderer generated a second
    image anyway, adding a duplicate row for a concept the library already
    had. Semantic matching that cannot deliver its match is just an expensive
    way to agree with itself. That holds for SVG exactly as it does for PNG —
    the format decides the FILENAME, never the identity.

    `asset_format` says which tier is asking. The bytes are written unchanged:
    an SVG is stored and served as markup, never rasterised to fit the older
    path.
    """
    hit = find(key, prompt, context, asset_format=asset_format)
    if not hit:
        return None
    fmt = row_format(hit)
    target = _local_asset_path(cache_dir, key, fmt)
    meta = _local_meta_path(cache_dir, key, fmt)
    if target.exists():
        return hit
    path = str(hit.get("storage_path") or "")
    sb = _sb()
    if not path or sb is None:
        return None
    try:
        data = sb.storage.from_(BUCKET).download(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        metadata = {**hit, "provenance": "visual_library",
                    "library_asset_id": hit.get("id"), "asset_format": fmt,
                    "group_ids": list(hit.get("group_ids") or []),
                    "group_count": int(hit.get("group_count") or 0)}
        meta.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        logger.info("visual library hit: %s <- %s (%s, score %.2f)", key,
                    hit.get("asset_key"), fmt, hit.get("match_score", 0))
        return hit
    except Exception as exc:  # noqa: BLE001
        logger.warning("visual library hydration failed for %s: %s", key, exc)
        return None


def find_avatar(key: str) -> dict[str, Any] | None:
    """The approved library row for an AVATAR key — by exact canonical key,
    never by meaning.

    Educational retrieval (find / best_match / hydrate) is avatar-blind on
    purpose: a teacher's face must never be served as a diagram. But nothing
    ever looked avatars up the other way, so on every fresh container the
    renderer generated a NEW teacher and publish_generated() added a NEW
    approved row — five copies of avatar_female_teacher by 2026-09-04, and a
    different face in every lesson. The oldest approved row wins so the face
    the founder has already seen stays the face."""
    sb = _sb()
    if sb is None:
        return None
    ck = canonical_key(key)
    try:
        # Filtered by KEY in SQL and by is_avatar_row in Python — not by
        # asset_type in SQL: a row published before asset_type existed
        # carries the column default 'visual', and the key is the signal
        # that has always been there.
        rows = (sb.table("visual_assets").select("*")
                .eq("status", "approved").eq("canonical_key", ck)
                .order("created_at").limit(20).execute().data or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("visual library avatar lookup failed for %s: %s", key, exc)
        return None
    for row in rows:
        if is_avatar_row(row):
            return row
    return None


def hydrate_avatar(key: str, cache_dir: Path) -> dict[str, Any] | None:
    """Put the library's avatar for `key` where the renderer looks
    (cache_dir/canonical_key(key)/asset.png), or return None so the caller
    may generate. Cached files are left alone."""
    hit = find_avatar(key)
    if not hit:
        return None
    fmt = row_format(hit)
    png = _local_asset_path(cache_dir, key, fmt)
    meta = _local_meta_path(cache_dir, key, fmt)
    if png.exists():
        return hit
    path = str(hit.get("storage_path") or "")
    sb = _sb()
    if not path or sb is None:
        return None
    try:
        data = sb.storage.from_(BUCKET).download(path)
        png.parent.mkdir(parents=True, exist_ok=True)
        png.write_bytes(data)
        metadata = {**hit, "provenance": "visual_library",
                    "library_asset_id": hit.get("id"), "asset_format": fmt}
        meta.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        logger.info("visual library avatar: %s <- %s", key, hit.get("id"))
        return hit
    except Exception as exc:  # noqa: BLE001
        logger.warning("visual library avatar hydration failed for %s: %s", key, exc)
        return None


def publish_generated(asset_key: str, prompt: str, asset_path: Path,
                      metadata: dict[str, Any] | None = None,
                      context: dict[str, Any] | None = None,
                      *, asset_format: str | None = None) -> bool:
    """Publish a newly generated, already-validated asset as a reusable one.

    The bytes go to Supabase Storage; metadata goes to Postgres. A matching
    canonical key is never silently overwritten. The asset is inserted as
    ``approved`` because it has already passed the renderer's deterministic
    validation. A later human-review field can demote it without deleting
    history.

    Format-agnostic: it takes a PATH and a FORMAT, not a PNG. The format is
    inferred from the file when the caller does not say, so every existing
    call site keeps publishing exactly what it published before.

    This is also where the STRICT gate lives, and it lives here on purpose —
    every publisher goes through this function, including the one-shot
    migration script, so an SVG that breaks the asset contract cannot enter
    the library by another door. Refusal returns False; the render that
    produced it is untouched and still draws.
    """
    asset_path = Path(asset_path)
    if not asset_path.exists():
        return False
    fmt = normalize_format(asset_format
                           or (metadata or {}).get("asset_format")
                           or asset_path.suffix)
    data = asset_path.read_bytes()
    group_ids: list[str] = []
    if fmt == "svg":
        from spike.scene_engine.svg_validate import validate_svg_document
        verdict = validate_svg_document(data.decode("utf-8", "replace"))
        if not verdict.ok:
            logger.warning("visual library: refusing to publish %s — the SVG "
                           "breaks the asset contract (%s)",
                           asset_key, verdict.reason)
            return False
        # EXACT ids, in drawing order. They are the labelling contract, so the
        # library can answer whether an asset contains the part a lesson wants
        # to label without downloading it.
        group_ids = list(verdict.group_ids)
    ctx = infer_context(asset_key, prompt, context)
    digest = hashlib.sha256(data).hexdigest()
    row = {
        "asset_key": str(asset_key),
        "canonical_key": canonical_key(asset_key),
        "description": prompt,
        "curriculum": ctx.curriculum,
        "subject": ctx.subject,
        "grade": ctx.grade,
        "topic": ctx.topic,
        "concepts": list(ctx.concepts),
        "status": "approved",
        "provenance": "generated",
        "content_hash": digest,
        # what the asset actually passed, not a generic word: an SVG cleared
        # the publish contract above, a PNG cleared the renderer's coverage
        # and baked-text checks
        "quality": (metadata or {}).get(
            "quality", "svg_contract_validated" if fmt == "svg"
            else "renderer_validated"),
        "asset_format": fmt,
        "group_ids": group_ids,
        "group_count": len(group_ids),
        # Without this the column default ('visual') applied to everything, and
        # the whole avatar roster entered the educational library.
        **avatar_fields(asset_key),
    }
    register_local(row)

    sb = _sb()
    if sb is None:
        return True
    # One approved avatar per canonical key. A second face for the same
    # teacher is not a new asset, it is a different teacher; the roster is
    # looked up by key (find_avatar), so a duplicate would never be served
    # and would only keep the library growing by one row per deploy.
    if is_avatar_key(asset_key) and find_avatar(asset_key) is not None:
        logger.info("visual library: avatar %s already published; not adding another", asset_key)
        return True
    try:
        # One bucket, one layout, the extension carried by the format:
        # generated/<canonical>/<hash>.<ext>. There is no parallel SVG store.
        storage_path = f"generated/{canonical_key(asset_key)}/{digest[:16]}.{fmt}"
        with asset_path.open("rb") as fh:
            sb.storage.from_(BUCKET).upload(
                storage_path, fh,
                {"content-type": CONTENT_TYPES[fmt],
                 "cache-control": "31536000", "upsert": "false"},
            )
        row["storage_path"] = storage_path
        # Idempotency is by content hash, while canonical_key remains searchable.
        existing = (sb.table("visual_assets").select("id")
                    .eq("content_hash", digest).limit(1).execute().data or [])
        if not existing:
            sb.table("visual_assets").insert(row).execute()
        else:
            row["id"] = existing[0].get("id")
        logger.info("visual library published: %s (%s/%s/%s)",
                    asset_key, ctx.curriculum, ctx.subject, ctx.grade)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("visual library publish failed for %s: %s", asset_key, exc)
        return True  # local cache/index remains useful


def seed_from_catalog(catalog_path: Path | None = None) -> int:
    """Register the checked-in catalogue without copying binaries."""
    path = catalog_path or Path(__file__).resolve().parents[1] / "visual_library" / "catalog.json"
    if not path.exists():
        return 0
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            return 0
        for row in rows:
            register_local(row)
        return len(rows)
    except Exception as exc:  # noqa: BLE001
        logger.warning("visual library catalog seed failed: %s", exc)
        return 0
