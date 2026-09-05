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
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shared.asset_keys import (all_noise, canonical_key, core_tokens,
                               distinguishes, is_avatar_key, same_word)
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
# The band token of a student key: a school band (`5_7`, `8_10`, `11_12`) or
# a post-school one (`undergrad`, `grad`, `doctorate` — AVATAR_PROMPTS keeps
# faces for them, and a grade-13+ book asks for them). Token-bounded so
# `undergrad` is never read as `grad`. A band the key does not carry is None.
_AGE_BAND_RE = re.compile(r"(?:^|_)(\d{1,2}_\d{1,2}|undergrad|grad|doctorate)(?:_|$)")


# is_avatar_key is imported from shared.asset_keys: the renderer needs the
# SAME answer, to keep an unresolvable avatar out of the placeholder tier.


def avatar_fields(key: str) -> dict[str, Any]:
    """asset_type/role/age_band for an asset key.

    Returns the ordinary-visual shape for anything that is not an avatar, so
    callers can merge it unconditionally.
    """
    k = base_avatar_key(str(key or "").strip().lower())
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

# The columns migration 0104 adds. Code ships through Railway on a push; the
# schema changes only when the founder applies the migration, so there is a
# real window where a worker that knows these columns talks to a database that
# does not. PostgREST answers an unknown column with PGRST204 and writes
# NOTHING — the bytes would already be in storage, so every generation in that
# window would leave an orphaned object and no row. publish_generated therefore
# retries once WITHOUT them rather than losing the row (see _insert_row).
FORMAT_COLUMNS = ("asset_format", "group_ids", "group_count")

# The column migration 0105 adds, degraded SEPARATELY from the three above.
# 0104 is applied to prod (measured read-only 2026-09-05, version
# 20260905054301); 0105 is not. Folding them into one set would mean a schema
# miss on `vision` also threw away asset_format/group_ids/group_count — which
# the live database has — so the degrade walks down one step at a time.
VISION_COLUMNS = ("vision",)


def _png_dimensions(data: bytes) -> tuple[int, int]:
    """(width, height) of PNG bytes, from the IHDR, or (0, 0).

    Read from the BYTES ABOUT TO BE UPLOADED rather than copied out of the
    caller's metadata, because that is the whole safety property of the vision
    payload: regions are pixel coordinates and are meaningful only for an
    image of exactly these dimensions. A number carried along beside them can
    drift; a number measured off them cannot. (0, 0) means "could not tell",
    which no real image matches, so a consumer's dimension check refuses the
    boxes rather than trusting them — fail closed.

    Parsed by hand instead of through Pillow: shared/ is imported by the app
    and the worker alike, and a 25-byte header read is not worth an image
    decode or a dependency.
    """
    sig = bytes((137, 80, 78, 71, 13, 10, 26, 10))    # the PNG magic number
    if len(data) < 24 or data[:8] != sig or data[12:16] != b"IHDR":
        return (0, 0)
    return (int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big"))


def vision_payload(regions: dict[str, Any] | None, annotated_for,
                   baked_text: bool, width: int, height: int) -> dict[str, Any]:
    """The self-describing annotation document stored on a row.

    `annotated_for` is the names ASKED for, not the names found, and the
    difference is load-bearing: a part vision genuinely cannot see must count
    as ANSWERED, or every later lesson re-asks the same question forever and
    the cache never converges.
    """
    boxes = {str(k): [[float(v) for v in box] for box in (val or [])]
             for k, val in (regions or {}).items()}
    asked = [str(n) for n in (annotated_for or [])]
    return {"regions": boxes, "annotated_for": asked,
            "baked_text": bool(baked_text),
            "w": int(width or 0), "h": int(height or 0)}


def row_vision(row: dict[str, Any] | None) -> dict[str, Any]:
    """A row's stored annotation, normalised, or {} for a row that has none.

    Defensive in the same way row_format is: every row published before this
    column existed carries no such key, and a database that predates 0105
    returns rows without it at all.
    """
    v = (row or {}).get("vision")
    if not isinstance(v, dict) or not v:
        return {}
    return vision_payload(v.get("regions"), v.get("annotated_for"),
                          bool(v.get("baked_text")), v.get("w") or 0,
                          v.get("h") or 0)


def vision_group_ids(payload: dict[str, Any] | None) -> list[str]:
    """The parts a raster asset demonstrably CONTAINS: the annotated names
    that came back with at least one box.

    A name that was asked for and not found is deliberately absent. group_ids
    is what row_has_parts answers from, and promising a part the renderer then
    cannot anchor is worse than admitting the asset does not have it.
    """
    out: list[str] = []
    for name, boxes in ((payload or {}).get("regions") or {}).items():
        if boxes:
            out.append(str(name))
    return out


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
    """The parts stored on a row, in drawing order.

    For an SVG these are the EXACT <g id>s taken from the markup at publish;
    for a PNG they are the part names its vision pass found a box for. Two
    ways of learning the same fact — what this asset contains — so callers
    ask one question and never branch on format.
    """
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

    A row with no recorded groups answers False for a real request rather than
    guessing. That was every PNG until 0105: a raster asset learns its parts
    from the paid vision pass, and the answer had nowhere to live, so all 217
    approved non-avatar PNGs measured on 2026-09-05 carried group_count = 0.
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


# ── one lesson, one face ─────────────────────────────────────────────────────
# Founder decision (2026-09-04): the avatar is chosen AT RANDOM among the
# roster's approved faces, restricted only by the narration voice's gender.
# The roster holds several faces per key (five approved avatar_female_teacher
# rows by 2026-09-04, each a different drawing), so "which face" is no longer
# implied by the key. It has to travel WITH the key: the renderer's raster
# cache is per key and shared by every lesson on a container, and the child
# render process resolves assets by key alone. A face-bearing key is
#
#     avatar_teacher_female__face_f3c6fcb3
#
# — the roster key plus the first 8 hex chars of the chosen row's id. Every
# consumer that needs the ROSTER key (the prompt, the student voice's age
# band, publish) strips the suffix with base_avatar_key(); everything keyed
# on cache identity (canonical_key, the asset dir) keeps it, so two faces of
# one teacher never share a cache directory.
FACE_SEP = "__face_"
_FACE_ID_LEN = 8


def face_key(base_key: str, row_id: str) -> str:
    """The asset key for one specific roster face."""
    fid = re.sub(r"[^a-z0-9]", "", str(row_id or "").lower())[:_FACE_ID_LEN]
    base = base_avatar_key(base_key)
    return f"{base}{FACE_SEP}{fid}" if fid else base


def base_avatar_key(key: str) -> str:
    """The roster key under a face-bearing key (identity for non-face keys)."""
    k = str(key or "")
    i = k.find(FACE_SEP)
    return k[:i] if i > 0 else k


def face_id_of(key: str) -> str | None:
    """The row-id prefix a face-bearing key names, else None."""
    k = str(key or "")
    i = k.find(FACE_SEP)
    if i < 0:
        return None
    fid = k[i + len(FACE_SEP):].strip().lower()
    return fid or None


# Gender is NOT a column on visual_assets and the rows carry no metadata JSON,
# so it is read from the KEY the renderer named the face with — the roster is
# generated from AVATAR_PROMPTS, whose keys encode it (`_female`, `_f`, `_m`;
# the two legacy keys `avatar_teacher` / `avatar_student` are male by their
# prompt text). The description is the fallback for a row named some other
# way. Measured on the live roster (13 rows, 2026-09-04): every row resolves
# from its key.
_DESC_FEMALE = re.compile(r"\b(female|woman|girl|schoolgirl|lady|she)\b", re.I)
_DESC_MALE = re.compile(r"\b(male|man|boy|schoolboy|he)\b", re.I)
_LEGACY_MALE_KEYS = {"avatar_teacher", "avatar_student"}


def avatar_gender(row_or_key) -> str | None:
    """'f' | 'm' for a roster row (or a bare key), None when unknowable."""
    if isinstance(row_or_key, dict):
        key = str(row_or_key.get("asset_key") or row_or_key.get("canonical_key") or "")
        desc = str(row_or_key.get("description") or "")
    else:
        key, desc = str(row_or_key or ""), ""
    k = base_avatar_key(key).strip().lower()
    toks = [t for t in re.split(r"[^a-z0-9]+", k) if t]
    if "female" in toks or (toks and toks[-1] == "f"):
        return "f"
    if "male" in toks or (toks and toks[-1] == "m"):
        return "m"
    if k in _LEGACY_MALE_KEYS:
        return "m"
    if _DESC_FEMALE.search(desc):
        return "f"
    if _DESC_MALE.search(desc):
        return "m"
    return None


def avatar_role(row_or_key) -> str | None:
    if isinstance(row_or_key, dict):
        r = str(row_or_key.get("role") or "").strip().lower()
        if r in _AVATAR_ROLES:
            return r
        key = str(row_or_key.get("asset_key") or row_or_key.get("canonical_key") or "")
    else:
        key = str(row_or_key or "")
    return avatar_fields(key)["role"]


def avatar_age_band(row_or_key) -> str | None:
    """'5_7' | '8_10' | ... for a student face; the legacy avatar_student
    (an 11-year-old by its prompt) is the 5_7 band."""
    if isinstance(row_or_key, dict):
        b = str(row_or_key.get("age_band") or "").strip()
        if b:
            return b
        key = str(row_or_key.get("asset_key") or row_or_key.get("canonical_key") or "")
    else:
        key = str(row_or_key or "")
    band = avatar_fields(key)["age_band"]
    if band is None and base_avatar_key(key).strip().lower() == "avatar_student":
        return "5_7"
    return band


# One page size for BOTH roster queries. The listing (every avatar, which
# picks the face) and the family lookup (one canonical key, which serves it)
# must reach equally far: a family is a subset of the roster, so any face the
# listing can pick, the lookup with the same limit can find. Two different
# limits here once meant a picked face past the lookup's page was "gone" and
# silently replaced by the oldest.
ROSTER_LIMIT = 200


def list_avatar_roster() -> list[dict[str, Any]]:
    """Every approved avatar row, oldest first. Empty offline or on error."""
    sb = _sb()
    if sb is None:
        return []
    try:
        rows = (sb.table("visual_assets").select("*")
                .eq("status", "approved").like("asset_key", "avatar%")
                .order("created_at").limit(ROSTER_LIMIT).execute().data or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("visual library avatar roster listing failed: %s", exc)
        return []
    return [r for r in rows if is_avatar_row(r)]


def _pick_rank(seed: str, row: dict[str, Any]) -> tuple:
    """A row's rank in the draw for `seed`: the sha256 of seed and ROW ID,
    so it depends on nothing but the row itself."""
    digest = hashlib.sha256(f"{seed}:{row.get('id') or ''}".encode("utf-8")).hexdigest()
    return (digest, str(row.get("created_at") or ""), str(row.get("id") or ""))


def _stable_pick(rows: list[dict[str, Any]], seed: str) -> dict[str, Any]:
    """A random-but-reproducible member: the same seed (a generation id) picks
    the same face on every part, every retry and every worker; a different
    generation may pick another.

    INSERTION-STABLE (rendezvous hashing): the winner is the candidate with
    the smallest sha256(f"{seed}:{row_id}"), a rank each row owns on its
    own. So the draw is independent of the roster's row order AND of which
    OTHER rows are present: a row approved between a lesson's first run and
    its retry cannot shift the pick from one existing face to another. Only
    two events change the face of a given generation — the chosen row is
    removed (demoted/deleted; the next-ranked face takes over), or a NEW row
    ranks below the chosen one for this seed (then the new face is cast, and
    stays cast from that moment). `random.choice` over the current list, by
    contrast, reshuffled every seed's draw on every insertion."""
    return min(rows, key=lambda r: _pick_rank(seed, r))


def pick_avatar(role: str, gender: str | None, seed: str, *,
                age_band: str | None = None,
                roster: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """The roster face for a lesson: a random approved face of `role` whose
    gender matches the voice's, seeded by the generation so one lesson keeps
    one face.

    Fallbacks, each logged: no face of that gender → any face of that role
    (and band); no face of that role at all → None, which sends the caller
    down the generate path exactly as before the roster existed. A lesson is
    never failed over an avatar.

    The student's AGE BAND is not relaxed. The roster holds school bands only
    (5_7 / 8_10 / 11_12 on 2026-09-04), so a university or graduate book
    (grade 13+) has no face in its band — and a schoolchild must not stand
    in for an undergraduate. Returning None hands the caller its age-matched
    roster key, and the generate-then-publish path draws that face and seeds
    the roster with it; from then on the band has a face to pick.
    """
    rows = list_avatar_roster() if roster is None else list(roster)
    pool = [r for r in rows if avatar_role(r) == role]
    if not pool:
        logger.info("avatar roster: no approved %s face; generating as before", role)
        return None
    cands = pool
    if age_band:
        in_band = [r for r in cands if avatar_age_band(r) == age_band]
        if not in_band:
            logger.info("avatar roster: no %s face in age band %s; generating "
                        "the age-matched face as before", role, age_band)
            return None
        cands = in_band
    if gender:
        of_gender = [r for r in cands if avatar_gender(r) == gender]
        if of_gender:
            cands = of_gender
        else:
            # the gender is the voice's; without a match the lesson still has
            # a face, and the log says the roster is short one drawing
            logger.warning("avatar roster: no approved %s face of gender %r "
                           "(band %s); falling back to any %s face",
                           role, gender, age_band, role)
    return _stable_pick(cands, f"{role}:{seed}")


def cast_avatar_key(role: str, gender: str | None, seed: str, default_key: str, *,
                    age_band: str | None = None,
                    roster: list[dict[str, Any]] | None = None) -> str:
    """The asset KEY a lesson renders `role` with: a face-bearing roster key,
    or `default_key` (today's generate path) when the roster has no face for
    the role.

    Every call READS THE ROSTER, deliberately. There is no per-process memo:
    a memo would answer from a roster snapshot taken minutes or days earlier,
    so a face demoted in the console would keep being cast until the worker
    restarted — and `pick_avatar`'s guarantee that a removed row hands the
    lesson its next-ranked face would be false in a running worker. The cast
    is made ONCE per generation anyway (worker/process.py, before the parts
    loop) and travels with the script, so the memo was buying one Supabase
    select per repeat cast against a whole lesson's work. Determinism comes
    from the seed, not from caching: the same seed over the same roster picks
    the same face every time, in any process."""
    row = pick_avatar(role, gender, str(seed), age_band=age_band, roster=roster)
    if row is None or not row.get("id"):
        return default_key
    key = face_key(str(row.get("asset_key") or default_key), str(row["id"]))
    logger.info("avatar cast: %s -> %s (gender %s, band %s, seed %s)",
                role, key, avatar_gender(row), avatar_age_band(row), seed)
    return key


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


# ── …and it must not make a DIFFERENT claim about that thing ─────────────────
# Sharing a distinguishing token only says two keys are about the same
# subject. On 2026-09-05 `catalyst_energy_profile` was served to BOTH
# `endothermic_energy_profile` and `exothermic_energy_profile`: all three
# share "energy" and "profile", so the guard passed and only the threshold was
# left — and in the production context those score 0.79 and 0.82, which the
# 0.58 default clears twice over. (The 1.09 and 1.12 first written down came
# from a bench context this system cannot produce; see the measurement note
# below.) Those are three different diagrams — products above the reactants,
# products below, two curves — and a board that asked for one and was handed
# another is teaching false chemistry with a confident label on it.
#
# Nothing can be SUBTRACTED to fix this, which is what makes it a different
# defect from the three before it. "sk" was a namespace, the connectives were
# grammar and the layer tail addressed the annotator; "energy" and "profile"
# are exactly what these keys are ABOUT. What separates them is the token each
# key carries that the other does not — so when the request names something
# the candidate does not AND the candidate names something the request does
# not, each key denies the other's claim and neither may answer it.
#
# ── what it costs, measured ──────────────────────────────────────────────────
# The recipe, so the next reader can re-run it: all 100,172 ordered pairs of
# the 317 approved visual rows in the 2026-09-05 library snapshot, each row's
# own stored description as the prompt, and NO explicit caller context —
# which is the production context. `visual_library_integration.set_context`
# is called from nowhere in the repo, so `_CONTEXT` is permanently {} and
# every live lookup runs at curriculum "generic", grade "k12" and a subject
# INFERRED from the words of the key and the prompt.
#
# The guard admitted 1,936 of those pairs; the contrast rule refuses 1,506 of
# them. At the 0.58 default threshold, 272 of the 448 admissions that clear it
# are contrast pairs; at 0.85 it is 50 of 114.
#
# Simulating find() over the whole library at 0.58: of the 125 requests that
# were served a row, 57 keep it, 9 are answered by a different row and 59 lose
# their answer. Every one of the 59 is a different picture — endothermic <-
# exothermic at 0.93 (worse than the incident that WAS reported), methane <-
# water at 1.07, meiosis <- mitosis at 1.07, red_blood_cell__merged <-
# plant_cell_wall__merged at 0.99, beach waves <- a plate boundary at 0.60. At
# 0.85 the same simulation loses 21 and changes 5.
#
# Which of those is the operating point is NOT settled here, and the earlier
# note that called 0.85 "the production threshold" was wrong twice over:
# VISUAL_LIBRARY_MIN_SCORE defaults to 0.58, and the reported incident pair
# scores 0.79 in this context — the 1.09 first written down needs
# ctx.subject="chemistry", while infer_context reads "energy" in the
# endothermic description and answers "physics". A pair that WAS served at
# 0.79 puts the live threshold at or below it, so 0.58 is the column to read.
#
# Of the 9 changed answers at 0.58, 8 are neutral or better: `human_outline`
# stops being served `human_body_organs` and gets `human_body_outline`, and
# `plant_cell_outline` stops being served `plant_and_animal_shrinking`. ONE is
# worse, and it is the honest price of the rule: `plant_cell_outline__merged`
# — a progressive-drawing build step whose description ends "No internal
# structures yet" — loses `plant_cell_wall__merged` (0.86) and falls to
# `plant_cell_diagram` (0.78), the finished cell. The refusal itself is right,
# because an outline and a wall are two steps; what carries the request down
# to the finished cell is the subset hole described next.
#
# Both directions of pure specialisation stay open, because a subset is not a
# contrast: `volcano_cross_section` may still be answered by
# `composite_volcano_cross_section` and vice versa. Closing either costs far
# more than it saves — measured at 0.58, refusing a candidate that merely
# GENERALISES the request loses 15 answers and changes 2 (it would take
# `leaf_microscope_view <- microscope_view` at 1.21, `timeline_cell_discovery
# <- timeline_asset` at 1.30 and `organism_to_cell_animation <-
# organism_to_cell` at 1.30, all the same picture), and refusing one that
# SPECIALISES it loses 21 and changes 10 (including every `sk_*` sketch and
# `volcano_cross_section <- composite_volcano_cross_section`).
#
# The known cost of leaving them open, unchanged by this rule and true on
# either side of it: `core_tokens` strips "cell", so `plant_cell_diagram`
# reduces to the single token {plant} and is a subset of — and an admissible
# answer for — every plant-cell specialisation in the library.
# `find("plant_cells", "A drawing of plant cells.")` returns
# `animal_plant_compare_table__merged` at 1.30, a comparison TABLE, both
# before and after this change. That is a hole in the SHARED-token rule, not
# in this one, and it wants a separate fix.


def _unmatched(these: set[str], those: set[str]) -> set[str]:
    """The tokens of `these` that `those` does not name.

    A re-spelling is not a contrast. `organ_system_diagram` and
    `organs_to_system` hold the same picture and differ by one plural;
    `extraction_of_aluminum` and `extraction_of_aluminium` differ by one
    ocean. A plain set difference reads both as two keys denying each other,
    and that refusal is not a smaller mistake than a wrong picture: it is a
    paid regeneration — ~US$0.04 and ~53 s against an image quota measured at
    about one call a minute — for a diagram the library already holds.

    Measured over the 317 live approved rows, a plain difference makes 36
    library keys unreachable by a request that writes one of their words as a
    regular English plural (`leaves_cross_section` for `leaf_cross_section`,
    `capillaries_exchange`, `teeth_structure`) and 11 more unreachable in the
    other orthography (`extraction_of_aluminum`, `muscle_fiber`,
    `foetus_uterus`, `displacement_reaction_iron_nail_copper_sulphate`) —
    every one of them at a score that clears 0.85 today. The library holds
    both orthographies itself, so both reach find() as request keys.

    So the comparison goes through `asset_keys.same_word`, which folds
    spellings and inflections and nothing else. It is not a similarity
    measure: meiosis/mitosis, nucleolus/nucleus, neutron/neuron and
    endothermic/exothermic are four different pictures and survive it.
    """
    return {t for t in these if not any(same_word(t, u) for u in those)}


_SEPARATORS = re.compile(r"[^a-z0-9]+")


def _letters(key: str) -> str:
    """A key with every separator removed — `back_to_back` and `backtoback`."""
    return _SEPARATORS.sub("", str(key or "").lower())


def _guard_refusal(query_key: str, row: dict[str, Any] | None) -> str | None:
    """WHY `row` is not a candidate for `query_key` — None when it is.

    The reason is returned rather than logged, because this runs inside a
    whole-library filter: `best_match` calls it once per row of the local set
    and once per row of the remote set, and `_decide` performs four such scans
    per uncached asset. A refusal logged here is logged per CANDIDATE, and the
    contrast rule refuses a mean of 4.75 rows per scan against the live
    library — roughly 760 lines for a 40-asset lesson, about 90 % of them
    reporting a row that never came within reach of the threshold, in the one
    stream an operator greps when a lesson fails.

    find() decides once and knows the score, so find() is where the line
    belongs — and it must print THIS reason rather than a fixed one: before
    the contrast rule the only refusal find() could hit was "no shared token",
    and those words are false for a pair that shares two.
    """
    if not row:
        return "there is no row"
    ak, ck = row.get("asset_key") or "", row.get("canonical_key") or ""
    # Same cache identity, same picture — by definition, everywhere else in
    # this system. Checked first so a key with nothing distinguishing in it
    # ("figure_3") can still be answered by ITSELF, which the abstention rule
    # below would otherwise refuse.
    qc = canonical_key(query_key)
    if qc and qc in {canonical_key(k) for k in (ak, ck) if k}:
        return None
    # …and so is the same key with the underscores in different places. Where
    # the model split a compound the library ran together, the two keys share
    # no token at all: `back_to_back_housing_cross_section` against the stored
    # `backtoback_housing_cross_section` leaves "back" against "backtoback",
    # so the contrast rule reads a rival claim and refuses a row scoring 1.40.
    # The canonical key cannot see it either — it sorts TOKENS, and these two
    # do not have the same ones. The same letters in the same order is not a
    # similarity judgement; it is one name, punctuated twice.
    ql = _letters(query_key)
    if ql and ql in {_letters(k) for k in (ak, ck) if k}:
        return None
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
        # Being about the same subject is not being the same picture. See the
        # note above `_unmatched`: the energy profiles shared "energy" and
        # "profile" and got here, and the catalyst curve went onto a board
        # that had asked for an endothermic one.
        q_only, r_only = _unmatched(q, r), _unmatched(r, q)
        if q_only and r_only:
            why = ("each key names something the other denies (%s vs %s); the "
                   "tokens they share (%s) say only that they are about the "
                   "same subject"
                   % (", ".join(sorted(q_only)), ", ".join(sorted(r_only)),
                      ", ".join(sorted(q & r)) or "none literally"))
            logger.debug("visual library: %s is not a candidate for %s — %s",
                         ak or ck, query_key, why)
            return why
        return None
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
        # Logged, because this is the one path where the guard abstains: the
        # threshold is the only thing standing between the request and a wrong
        # picture, and "why did cells match X" has to be answerable.
        logger.debug("key guard abstained for %r vs %r/%r (nothing "
                     "distinguishing on %s); shared=%s carrying=%s",
                     query_key, ak, ck,
                     "the request" if all_noise(query_key) else "the row",
                     sorted(shared), sorted(carriers))
        if carriers:
            return None
        # Kept at INFO where the contrast refusal is not, and the difference
        # is volume: no key in the live library is all-noise, so this path was
        # taken by 0 of the 100,172 ordered pairs. It is reached only by a key
        # a book invented, which is exactly when the evidence is worth having.
        if shared:
            logger.info("visual library: refused %s <- %s/%s — the only "
                        "tokens they share (%s) name a medium or an index, "
                        "not a subject", query_key, ak, ck,
                        ", ".join(sorted(shared)))
            return ("the only tokens they share (%s) name a medium or an "
                    "index, not a subject" % ", ".join(sorted(shared)))
        return "the keys share no token at all"
    return "the keys share no concept token"


def key_guard_ok(query_key: str, row: dict[str, Any] | None) -> bool:
    """Whether `row` is even a candidate for `query_key`.

    A necessary condition, not a sufficient one — the score still has to clear
    the threshold. What it forbids is a match carried entirely by PROSE, and a
    match between two keys that each name something the other denies.
    """
    return _guard_refusal(query_key, row) is None


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
    """Stage and rename, through a scratch name PRIVATE to this writer.

    register_local holds ra.asset_lock(key), which is per-KEY, so two segment
    threads publishing two different assets are genuinely concurrent right
    here. One fixed ``index.json.tmp`` meant they opened the same file with
    truncation and interleaved into it: whoever renamed first published a
    document half-written by the other, and whoever renamed second raised
    FileNotFoundError out through publish_generated and lost its row. A torn
    index.json is worse than either — _local_candidates cannot parse it,
    returns [], and every key the local index held is generated again.
    """
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    _write_atomic(LIBRARY_DIR / "index.json",
                  json.dumps(rows, indent=2, sort_keys=True).encode("utf-8"))


def _index_identity(row: dict[str, Any]) -> tuple[str, str]:
    """What makes two local index rows the SAME row: the key AND the format.

    The key alone was wrong the moment a second format existed. One asset_key
    legitimately has two cached files — ``<canonical>/asset.png`` from the
    raster tier and ``svg_<canonical>/asset.svg`` from the SVG tier — and the
    cache bootstrap indexes both on every worker start. Keyed by asset_key
    alone, the second registration EVICTED the first, so whichever format the
    glob reached last was the only one the index knew: a PNG this container had
    already paid for became invisible to the raster tier, which then generated
    it again. Two rows, one per format, is the correct shape.
    """
    return (str(row.get("asset_key") or row.get("canonical_key") or ""),
            row_format(row))


def register_local(row: dict[str, Any]) -> None:
    """Register metadata locally; safe to call from every generation."""
    rows = _local_candidates()
    ident = _index_identity(row)
    rows = [r for r in rows if _index_identity(r) != ident]
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
        # One line per DECISION, carrying the score and the guard's own
        # reason. Hard-coding "the keys share no concept token" here was true
        # while that was the only way to be refused; the contrast rule refuses
        # pairs that share two, so a fixed reason would tell an operator the
        # opposite of what happened on the very line they grep.
        if near is not None and near_score >= threshold:
            why = _guard_refusal(key, near)
            if why:
                logger.info("visual library: refused %s <- %s (score %.2f) — "
                            "%s", key, near.get("asset_key"), near_score, why)
        return None
    return {**best, "match_score": round(best_score, 4),
            "match_source": source, "context": ctx.__dict__}


def threshold_now() -> float:
    return float(os.getenv("VISUAL_LIBRARY_MIN_SCORE", "0.58"))


# ── the remote candidate set ─────────────────────────────────────────────────
# Read in PAGES, filtered in the query. The old read was a single
# ``.limit(250)`` with every predicate but status and asset_type applied in
# Python afterwards, which is not a tuning knob — it is a silent correctness
# bug with a deadline. Production held 217 approved non-avatar visuals of 230
# rows on 2026-09-05 and the diagram catalogue is still being filled: the
# first row to fall outside the window would simply never be found, and the
# lesson would pay to regenerate a picture the library already holds. Nothing
# would log, because the query succeeded.
#
# The cap that remains is a runaway guard, not a window: a page short of
# PAGE_SIZE ends the read, so the normal cost is one query.
REMOTE_PAGE_SIZE = max(1, int(os.getenv("VISUAL_LIBRARY_PAGE_SIZE") or 500))
REMOTE_ROW_CAP = max(1, int(os.getenv("VISUAL_LIBRARY_MAX_ROWS") or 50000))


def _format_predicate(query, want_format: str):
    """Narrow to one format IN THE QUERY without dropping a legacy row.

    A bare ``eq("asset_format", "png")`` would hide every PNG the library held
    before 0104 — the column is nullable and those rows carry NULL — which is
    the same class of silent drop this module is busy removing, one column
    over. So the default format admits NULL as well, and row_format() decides
    in Python either way.

    Kept to plain equality and IS NULL on purpose: the clause is the one part
    of this read that a database can refuse, and the cheapest way to keep the
    remote search alive is to give it nothing exotic to refuse.
    """
    if want_format != DEFAULT_FORMAT:
        return query.eq("asset_format", want_format)
    # NULL reads as a PNG — normalize_format says so, and every row published
    # before the column existed is one — so a NULL row is a candidate for the
    # default format and for no other.
    return query.or_(f"asset_format.is.null,asset_format.eq.{want_format}")


def _remote_page(sb, want_format: str | None, start: int, end: int):
    q = (sb.table("visual_assets").select("*")
         .eq("status", "approved")
         .neq("asset_type", "avatar"))
    if want_format is not None:
        q = _format_predicate(q, want_format)
    # Ordered because a page is only a page if the order is stable; without it
    # Postgres may return the same row twice and never return another.
    return list(q.order("id").range(start, end).execute().data or [])


def _remote_pages(sb, want_format: str | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    start = 0
    while start < REMOTE_ROW_CAP:
        page = _remote_page(sb, want_format, start,
                            start + REMOTE_PAGE_SIZE - 1)
        rows.extend(page)
        if len(page) < REMOTE_PAGE_SIZE:
            # A short page is the end of the table ONLY while the page size is
            # under the server's own row cap. Raise VISUAL_LIBRARY_PAGE_SIZE
            # past it and every page comes back "short" at the cap, silently
            # reinstating the window this function exists to remove.
            if len(page) == 0 or start + len(page) < REMOTE_ROW_CAP:
                return rows
            logger.warning("visual library: a page came back short at %d rows, "
                           "which is where the server caps a read — set "
                           "VISUAL_LIBRARY_PAGE_SIZE lower than the server cap",
                           len(page))
            return rows
        start += REMOTE_PAGE_SIZE
    # Reaching the guard means rows are being dropped again. It is a decade
    # away at the current rate, but the whole point of this change is that a
    # window nobody can see is worse than one that says so.
    logger.warning("visual library: stopped reading at %d rows (the runaway "
                   "guard); rows past it cannot be matched", len(rows))
    return rows


_FORMAT_FILTER_UNAVAILABLE = False


def _remote_rows(sb, want_format: str | None) -> list[dict[str, Any]]:
    """Every approved, non-avatar row a request in `want_format` could match.

    The format filter is attempted in the query and abandoned whole if the
    database refuses it — an un-migrated schema answers an unknown column with
    PGRST204, and losing the remote search entirely to save a transfer would
    trade a cheap read for every reuse the library exists to provide.
    """
    # Remember the refusal. Between a deploy and 0104 the column does not
    # exist, and asking again on every lookup buys a guaranteed-failing round
    # trip per lesson visual for as long as that window lasts.
    global _FORMAT_FILTER_UNAVAILABLE
    if _FORMAT_FILTER_UNAVAILABLE:
        return _remote_pages(sb, None)
    try:
        return _remote_pages(sb, want_format)
    except Exception as exc:  # noqa: BLE001
        if want_format is None or not _format_filter_refused(exc):
            raise
        logger.info("visual library: this database has no asset_format column "
                    "yet (%s); reading approved rows unfiltered until restart", exc)
        _FORMAT_FILTER_UNAVAILABLE = True
        return _remote_pages(sb, None)


def _format_filter_refused(exc: Exception) -> bool:
    """Whether a failed read is the DATABASE not knowing asset_format.

    Narrow on purpose. Retrying every failure without the filter would double
    the cost of a network blip and hide nothing useful; retrying this one keeps
    the whole remote search alive in the window between a push and 0104.
    """
    return _looks_like_missing_column(exc) or "asset_format" in f"{exc}"


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

    `asset_format` narrows the remote query too — see _remote_rows, which
    also pages rather than truncating. The Python filter below stays the
    authority, because an un-migrated database has no asset_format column and
    a legacy row carries NULL.

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
    # remote query narrows in Postgres, and both result sets are filtered
    # again in Python by is_avatar_row(), which also catches rows published
    # before asset_type was set.
    rows = _eligible([r for r in _local_candidates() if not is_avatar_row(r)])
    best = max(rows, key=lambda r: _score(r, key, prompt, ctx), default=None)
    best_score = _score(best, key, prompt, ctx) if best else 0.0
    source = "local" if best is not None else "none"

    sb = _sb()
    if sb is not None:
        try:
            remote = _remote_rows(sb, want_format)
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
        # Written beside and renamed in: the asset file either does not
        # exist or is complete. A concurrent reader (segments render on
        # parallel threads) must never open a half-written file, take it for
        # a corrupt cache and generate a DIFFERENT image mid-lesson. The
        # per-key asset lock in the renderer wrapper serializes the threads;
        # this makes the file itself safe for any reader that is not holding
        # it. It matters more for SVG, not less: a truncated PNG usually
        # fails to decode, while a truncated SVG can parse into a PARTIAL
        # drawing and be believed.
        _write_atomic(target, data)
        metadata = {**hit, "provenance": "visual_library",
                    "library_asset_id": hit.get("id"), "asset_format": fmt,
                    "group_ids": list(hit.get("group_ids") or []),
                    "group_count": int(hit.get("group_count") or 0),
                    # Normalised rather than passed through: a row from a
                    # database that predates 0105 has no such key at all, and
                    # the renderer must read one shape either way. This is the
                    # whole read path — raster_assets seeds annotated_for from
                    # it and its existing cache guard then skips the vision
                    # call it would otherwise pay for a picture the library
                    # already knows.
                    "vision": row_vision(hit)}
        _write_json_atomic(meta, metadata)
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
    fid = face_id_of(key)
    ck = canonical_key(base_avatar_key(key))
    try:
        # Filtered by KEY in SQL and by is_avatar_row in Python — not by
        # asset_type in SQL: a row published before asset_type existed
        # carries the column default 'visual', and the key is the signal
        # that has always been there.
        rows = (sb.table("visual_assets").select("*")
                .eq("status", "approved").eq("canonical_key", ck)
                .order("created_at").limit(ROSTER_LIMIT).execute().data or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("visual library avatar lookup failed for %s: %s", key, exc)
        return None
    faces = [row for row in rows if is_avatar_row(row)]
    if fid:
        # a face-bearing key names ONE row (see face_key); a face that has
        # since been demoted or deleted falls back to the oldest, and says so
        for row in faces:
            if str(row.get("id") or "").lower().replace("-", "").startswith(fid):
                return row
        if faces:
            logger.warning("visual library: face %s of %s is gone; serving the oldest face",
                           fid, ck)
    return faces[0] if faces else None


# A scratch file is private to its writer (see _write_atomic), which is what
# makes concurrent writers safe — and also what makes an abandoned one
# immortal: a process killed between write_bytes and os.replace leaves behind
# a uniquely named, full-size PNG whose name nothing will ever compute again.
# On the persistent cache volume that is unbounded: one orphan per crash,
# forever. So every writer sweeps the directory it is about to write to.
#
# The age floor is the whole safety argument. A .part younger than this may
# belong to a writer still in flight, and deleting it would be the shared-name
# bug again wearing a stopwatch — one writer removing the file another is
# about to rename in. An hour is orders of magnitude longer than a write of a
# few hundred KB takes, so anything older belongs to nobody.
_SCRATCH_TTL_S = 3600.0


def _sweep_scratch(directory: Path, *, ttl_s: float = _SCRATCH_TTL_S) -> None:
    """Unlink abandoned ``*.part`` scratch files in `directory`.

    Best effort in every direction: an unreadable directory, a file that
    another writer renamed away between the listing and the stat, a
    permission error — none of them may fail the write this precedes. The
    sweep is a tidy-up, never a precondition.
    """
    cutoff = time.time() - ttl_s
    try:
        found = list(directory.glob("*.part"))
    except OSError:
        return
    for stale in found:
        try:
            if stale.stat().st_mtime < cutoff:
                stale.unlink()
        except OSError:
            continue


def _write_atomic(path: Path, data: bytes) -> None:
    """Write `data` to a PRIVATE sibling and rename it into place, so `path` is
    never observable half-written (os.replace is atomic on POSIX and NTFS).

    The scratch name carries this writer's pid and a fresh uuid4 because the
    concurrent case this function exists for is exactly the case one fixed
    ``asset.png.part`` could not survive: two writers of the same key (the
    parent and a segment subprocess, or two workers sharing a cache volume)
    opened the SAME scratch path, so one could rename in a file the other was
    still writing — the torn read, moved one level down. A private name makes
    each writer's scratch file its own.

    The unlink only runs on failure. After a successful os.replace the scratch
    name no longer exists, and an unconditional ``finally`` unlink of a SHARED
    name could only ever delete some other writer's file out from under it.
    """
    _sweep_scratch(path.parent)
    part = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.part")
    try:
        part.write_bytes(data)
        os.replace(part, path)
    except BaseException:
        part.unlink(missing_ok=True)
        raise


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """meta.json goes in the same way asset.png does.

    Making only the PNG atomic moved the torn read one file over rather than
    closing it: meta.json was still a truncating write_text, and a reader that
    catches it mid-write parses nothing, falls back to ``md = {}``, finds no
    ``annotated_for`` and re-runs annotate_regions — a PAID vision call, for a
    file that was perfectly good a moment earlier and is again a moment later.

    Public because raster_assets writes the same file from the other end, and
    a torn read there costs the same call. The private alias below is the name
    this module's own callers have always used.
    """
    _write_atomic(path, json.dumps(payload, indent=2).encode("utf-8"))


_write_json_atomic = write_json_atomic


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
        # Beside-and-rename, for the reason spelled out in hydrate(): a
        # half-written face read by a parallel segment is a SECOND paid
        # avatar mid-lesson.
        _write_atomic(png, data)
        metadata = {**hit, "provenance": "visual_library",
                    "library_asset_id": hit.get("id"), "asset_format": fmt}
        _write_json_atomic(meta, metadata)
        logger.info("visual library avatar: %s <- %s", key, hit.get("id"))
        return hit
    except Exception as exc:  # noqa: BLE001
        logger.warning("visual library avatar hydration failed for %s: %s", key, exc)
        return None


_SCHEMA_MISS = ("pgrst204", "schema cache", "does not exist", "unknown column",
                "could not find")


def _looks_like_missing_column(exc: Exception) -> bool:
    """Whether a failed insert is the database not knowing a column yet.

    PostgREST reports it as PGRST204 with "Could not find the 'x' column of
    'visual_assets' in the schema cache"; a direct Postgres error says
    "column ... does not exist". Anything else — a network blip, a permission
    error, a constraint violation — is NOT this, and must not trigger a retry
    that would only fail the same way or, worse, hide a real problem.
    """
    text = f"{exc}".lower()
    return any(marker in text for marker in _SCHEMA_MISS)


def _insert_row(sb, row: dict[str, Any], asset_key: str) -> None:
    """Insert a library row, degrading one migration at a time if needed.

    Deploys and migrations are separate acts: the worker ships on a push, the
    schema changes when the founder applies the file. Between the two, a row
    carrying columns the database does not have is refused WHOLE — and because
    the bytes are uploaded first, the failure would leave an orphaned storage
    object and no row at all, silently, on every single generation.

    So a schema miss is not fatal, and the retry walks DOWN ONE STEP AT A
    TIME rather than falling to the oldest schema it knows:

        full row  ->  drop `vision` (0105)  ->  also drop the 0104 format
                      columns  ->  raise

    The order is not cosmetic. 0104 is applied to prod and 0105 is not
    (measured read-only 2026-09-05), so the common case in the deploy window
    is a database that knows asset_format/group_ids/group_count perfectly
    well. A single-step degrade would throw those away too on every publish,
    which is how a cost optimisation for one column silently disables the
    part lookup for every asset.

    Nothing that matters is lost at any step. ``row_format`` already answers
    from the stored object's extension when its column is absent, so even a
    fully degraded SVG row reads back as an SVG from
    ``generated/<canonical>/<hash>.svg``. What waits for a migration is the
    part metadata and the cached annotation: lookup optimisations, not the
    asset.
    """
    ladder = [row]
    rungs: list[str] = []    # what to say when falling from ladder[i] to i+1
    for drop, complaint in (
            (VISION_COLUMNS,
             "0105 (%s); publishing %s without the cached vision annotation"),
            (VISION_COLUMNS + FORMAT_COLUMNS,
             "0104 (%s); publishing %s without the format columns")):
        candidate = {k: v for k, v in row.items() if k not in drop}
        # A step that removes nothing is not a step. An SVG carries no vision
        # payload at all, so retrying it minus a column it never had would
        # fail identically — a wasted round trip and a warning naming the
        # wrong migration.
        if len(candidate) == len(ladder[-1]):
            continue
        ladder.append(candidate)
        rungs.append(complaint)

    for i, candidate in enumerate(ladder):
        try:
            sb.table("visual_assets").insert(candidate).execute()
            return
        except Exception as exc:  # noqa: BLE001
            # The last rung has nowhere left to fall: a schema miss there is
            # about a column no migration of ours adds, so it is a real error
            # and must surface rather than be retried into silence.
            if i >= len(rungs) or not _looks_like_missing_column(exc):
                raise
            logger.warning("visual library: schema predates " + rungs[i],
                           exc, asset_key)


def merge_vision(stored: dict[str, Any] | None,
                 fresh: dict[str, Any] | None) -> dict[str, Any]:
    """The row's annotation after a container contributes what it just learned.

    UNION, never replacement. The accumulation that makes an asset converge
    used to live only in the container's meta.json, so any write-back from a
    container whose local baseline was NARROWER than the row shrank the row:
    a lesson naming one part of a three-part picture rewrote it as a one-part
    picture and the other two boxes had to be bought again. Three reachable
    paths produce exactly that baseline — a row flagged ``baked_text`` (which
    ``_lift_library_vision`` deliberately refuses to seed from), a local
    meta.json that already carried its own older ``annotated_for``, and every
    single bind while 0105 is unapplied, because then no payload ever comes
    back to seed from at all.

    Three rules, each with a reason:

    * a stored box is only overwritten by a box, never by a "not found". A
      pass that asks for a part and cannot see it must still mark the part
      ANSWERED — otherwise it is re-asked forever — but it must not erase the
      earlier pass that did see it.
    * ``baked_text`` LATCHES. It describes the object in Supabase Storage, and
      ``scrub_all_text`` only cleans the local copy, so once the stored bytes
      are known to carry words nothing this side of a re-upload makes them
      clean. A later pass over a scrubbed local file reports no text quite
      honestly, and must not be allowed to say so about the stored object.
    * boxes are pixel coordinates. If the two payloads disagree about the
      dimensions they were measured on, they describe different images and
      must not be mixed: the fresh one — measured against the bytes in hand —
      replaces the stored one outright rather than being merged into it.

    This is still read-modify-write, not compare-and-set: two containers that
    read the same row before either writes still lose one another's newest
    names. That race self-heals (the next reader accumulates over whatever it
    finds and writes the wider union back) and costs at worst a repeat call,
    where replacement cost a permanent deletion.
    """
    old, new = row_vision({"vision": stored}), row_vision({"vision": fresh})
    if not new:
        return old
    if not old:
        return new
    if (old["w"], old["h"]) != (new["w"], new["h"]):
        return new
    regions = dict(old["regions"])
    for name, boxes in new["regions"].items():
        if boxes or name not in regions:
            regions[name] = boxes
    asked = list(dict.fromkeys(old["annotated_for"] + new["annotated_for"]))
    return vision_payload(regions, asked,
                          old["baked_text"] or new["baked_text"],
                          new["w"], new["h"])


def _stored_annotation(sb, asset_id: str) -> tuple[dict[str, Any], list[str]]:
    """(the row's vision payload, its group_ids) — or ({}, []) for a row this
    client cannot read.

    ``select("*")`` on purpose: naming ``vision`` would make this fail on a
    database that predates 0105, which is every database in the deploy window
    and precisely when the group columns still need reading.
    """
    try:
        res = sb.table("visual_assets").select("*").eq(
            "id", str(asset_id)).limit(1).execute()
        row = ((getattr(res, "data", None) or [None])[0]) or {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("visual library: could not read %s before recording "
                     "its annotation: %s", asset_id, exc)
        return {}, []
    return row_vision(row), row_group_ids(row)


def record_vision(asset_id: str, payload: dict[str, Any] | None) -> bool:
    """Write a freshly measured vision annotation onto an EXISTING row.

    This is the half that converges the assets already in the library. A
    published PNG carries its annotation from birth, but the 217 approved
    non-avatar PNGs measured on 2026-09-05 predate the column and carry none;
    the renderer re-derived it on every hydration, and CACHE_DIR lives inside
    the Railway container with no volume mounted, so every redeploy wiped the
    local copy and every one of them re-bought the same vision call. Writing
    it back on the first hit makes that one call per asset EVER.

    READ-MODIFY-WRITE, not blind write. PostgREST replaces the columns named
    in an update, so sending only what this container learned SHRANK any row
    that already knew more — and a narrower local baseline is the normal
    case, not the corner one (merge_vision lists the three paths that produce
    one, including every single bind while 0105 is unapplied). The write is
    also aimed: an unfiltered PATCH stamps every row in visual_assets.

    Degrades like _insert_row and for the same reason, but one step shorter:
    the group columns are live in prod today and `vision` is not, so a schema
    miss retries with the 0104 columns alone — the part names are exactly what
    row_has_parts answers from, and they are worth landing even when the
    payload cannot. The return value still reports the payload: False means
    the annotation did not reach the database, whatever else did.

    Never raises. A render must not fail because a cost optimisation could not
    write; the worst case is that the next worker pays for vision again, which
    is precisely today's behaviour.
    """
    if not asset_id or not payload:
        return False
    sb = _sb()
    if sb is None:
        return False
    # Read first. The update replaces these columns whole, so what goes back
    # has to be the union of the row and this container — see merge_vision on
    # why a narrower local baseline is the normal case, not the corner one.
    stored, stored_groups = _stored_annotation(sb, asset_id)
    payload = merge_vision(stored, payload)
    groups = vision_group_ids(payload)
    if not stored or (stored["w"], stored["h"]) == (payload["w"], payload["h"]):
        # The column is older than the payload — until 0105 lands, group_ids
        # is the ONLY thing a bind can leave behind — so it accumulates on its
        # own account rather than being re-derived from a payload that may not
        # exist yet. The one case it must not: a merge that was really a
        # replacement, because the stored boxes turned out to describe an
        # image of other dimensions. Those names are not parts of THIS asset,
        # and group_ids is a promise the renderer then has to keep.
        groups = list(dict.fromkeys(stored_groups + groups))
    full = {"vision": payload, "group_ids": groups, "group_count": len(groups)}
    try:
        sb.table("visual_assets").update(full).eq("id", str(asset_id)).execute()
        return True
    except Exception as exc:  # noqa: BLE001
        if not _looks_like_missing_column(exc):
            logger.warning("visual library: could not record the vision "
                           "annotation for %s: %s", asset_id, exc)
            return False
        logger.info("visual library: schema predates 0105 (%s); recording the "
                    "parts of %s without the annotation", exc, asset_id)
    try:
        sb.table("visual_assets").update(
            {"group_ids": groups, "group_count": len(groups)}
        ).eq("id", str(asset_id)).execute()
    except Exception as exc:  # noqa: BLE001
        logger.info("visual library: could not record the parts of %s "
                    "either: %s", asset_id, exc)
    return False


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
    # A face-bearing key (one roster face, see face_key) is a cache identity,
    # not a roster identity: what gets published — when it gets published at
    # all — is the roster key, so the duplicate check below sees the family.
    asset_key = base_avatar_key(str(asset_key))
    fmt = normalize_format(asset_format
                           or (metadata or {}).get("asset_format")
                           or asset_path.suffix)
    data = asset_path.read_bytes()
    group_ids: list[str] = []
    md = metadata or {}
    # A raster asset's parts cost a paid vision call; an SVG's are free from
    # its <g id>s. Both end up in the SAME two columns, because a lesson asks
    # "do you have a chloroplast" and must not have to know which. What the
    # PNG additionally carries is the payload itself — the boxes, and the
    # dimensions they were measured on — so the call is bought once ever
    # instead of once per worker deploy: CACHE_DIR is inside the Railway
    # container and no volume is mounted, so the local copy dies at every
    # redeploy.
    vision: dict[str, Any] = {}
    if fmt == "png":
        # Two dialects reach here and both are legitimate. raster_assets
        # writes regions/annotated_for/baked_text at the TOP level of its
        # meta.json and has done since long before the library existed; a file
        # the library hydrated and the renderer then re-annotated carries the
        # nested payload as well. The nested one wins when present because it
        # is the one that also knows its dimensions.
        nested = md["vision"] if isinstance(md.get("vision"), dict) else {}
        w, h = _png_dimensions(data)
        vision = vision_payload(nested.get("regions", md.get("regions")),
                                nested.get("annotated_for",
                                           md.get("annotated_for")),
                                bool(md.get("baked_text")), w, h)
        group_ids = vision_group_ids(vision)
        if not vision["regions"] and not vision["annotated_for"]:
            # Nothing was ever asked of vision for this asset (a hand sprite,
            # a prompt with no part names, the one-shot cache migration over a
            # meta.json that predates annotation). An empty document says "not
            # annotated", which is true and re-askable; a document full of
            # empty fields would say "asked and found nothing", which is not.
            vision = {}
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
        "quality": md.get(
            "quality", "svg_contract_validated" if fmt == "svg"
            else "renderer_validated"),
        "asset_format": fmt,
        "group_ids": group_ids,
        "group_count": len(group_ids),
        # Without this the column default ('visual') applied to everything, and
        # the whole avatar roster entered the educational library.
        **avatar_fields(asset_key),
    }
    if vision:
        # Sent only when there is something to say. '{}' is the column
        # default, so a row with no annotation — every SVG, and any PNG whose
        # prompt named no parts — gains nothing by carrying the key and would
        # pay for it: until 0105 is applied, naming the column costs one
        # refused insert per publish (see _insert_row).
        row["vision"] = vision
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
            _insert_row(sb, row, str(asset_key))
        else:
            row["id"] = existing[0].get("id")
        logger.info("visual library published: %s (%s/%s/%s)",
                    asset_key, ctx.curriculum, ctx.subject, ctx.grade)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("visual library publish failed for %s: %s", asset_key, exc)
        # Still True, and deliberately: the False return means ONE thing —
        # "this markup broke the asset contract" — and the decision log reads
        # it that way. Folding a network blip or a permission error into the
        # same answer would make `published: false` ambiguous between "your
        # SVG is defective" and "Supabase was unreachable", which is worse
        # observability, not better. Infrastructure failures announce
        # themselves in the warning above; the local cache/index is written
        # either way and remains useful.
        return True


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
