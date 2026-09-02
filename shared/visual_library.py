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


def canonical_key(value: str) -> str:
    """Stable concept identity; order/noise changes do not create new assets."""
    tokens = [t for t in re.split(r"[^a-z0-9]+", str(value).lower()) if t]
    core = [t for t in tokens if t not in _STOP] or tokens
    return "_".join(sorted(set(core))) or "asset"


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


def _score(row: dict[str, Any], query_key: str, prompt: str, ctx: LibraryContext) -> float:
    key = str(row.get("canonical_key") or row.get("asset_key") or "")
    desc = str(row.get("description") or "")
    concepts = row.get("concepts") or []
    row_tokens = _tokens(key, desc, " ".join(map(str, concepts)))
    query_tokens = _tokens(query_key, prompt, ctx.topic, " ".join(ctx.concepts))
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


def _local_meta_path(cache_dir: Path, key: str) -> Path:
    return cache_dir / canonical_key(key) / "meta.json"


def _local_png_path(cache_dir: Path, key: str) -> Path:
    return cache_dir / canonical_key(key) / "asset.png"


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
         *, min_score: float | None = None) -> dict[str, Any] | None:
    """Find an approved reusable asset without invoking an AI model."""
    ctx = infer_context(key, prompt, context)
    threshold = float(os.getenv("VISUAL_LIBRARY_MIN_SCORE", "0.58")) if min_score is None else min_score

    rows = _local_candidates()
    best = max(rows, key=lambda r: _score(r, key, prompt, ctx), default=None)
    best_score = _score(best, key, prompt, ctx) if best else 0.0

    sb = _sb()
    if sb is not None:
        try:
            remote = (sb.table("visual_assets").select("*")
                      .eq("status", "approved").limit(250).execute().data or [])
            remote_best = max(remote, key=lambda r: _score(r, key, prompt, ctx), default=None)
            remote_score = _score(remote_best, key, prompt, ctx) if remote_best else 0.0
            if remote_score > best_score:
                best, best_score = remote_best, remote_score
        except Exception as exc:  # noqa: BLE001
            logger.debug("visual library remote search skipped: %s", exc)

    if best is None or best_score < threshold:
        return None
    return {**best, "match_score": round(best_score, 4), "context": ctx.__dict__}


def hydrate(key: str, prompt: str, cache_dir: Path,
            context: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Download a remote approved asset into the renderer's existing cache."""
    hit = find(key, prompt, context)
    if not hit:
        return None
    png = _local_png_path(cache_dir, str(hit.get("asset_key") or key))
    meta = _local_meta_path(cache_dir, str(hit.get("asset_key") or key))
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
        metadata = {**hit, "provenance": "visual_library", "library_asset_id": hit.get("id")}
        meta.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        logger.info("visual library hit: %s <- %s (score %.2f)", key, hit.get("asset_key"), hit.get("match_score", 0))
        return hit
    except Exception as exc:  # noqa: BLE001
        logger.warning("visual library hydration failed for %s: %s", key, exc)
        return None


def publish_generated(asset_key: str, prompt: str, png_path: Path,
                      metadata: dict[str, Any] | None = None,
                      context: dict[str, Any] | None = None) -> bool:
    """Publish a newly generated, already-validated image as a reusable asset.

    The binary goes to Supabase Storage; metadata goes to Postgres. A matching
    canonical key is never silently overwritten. The asset is inserted as
    ``approved`` because it has already passed the renderer's deterministic
    image validation (coverage/baked-text checks). A later human-review field
    can demote it without deleting history.
    """
    if not png_path.exists():
        return False
    ctx = infer_context(asset_key, prompt, context)
    digest = hashlib.sha256(png_path.read_bytes()).hexdigest()
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
        "quality": (metadata or {}).get("quality", "renderer_validated"),
    }
    register_local(row)

    sb = _sb()
    if sb is None:
        return True
    try:
        storage_path = f"generated/{canonical_key(asset_key)}/{digest[:16]}.png"
        with png_path.open("rb") as fh:
            sb.storage.from_(BUCKET).upload(
                storage_path, fh,
                {"content-type": "image/png", "cache-control": "31536000", "upsert": "false"},
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
