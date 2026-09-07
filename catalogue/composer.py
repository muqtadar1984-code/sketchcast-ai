"""The question-set COMPOSER: a blueprint + the approved bank of a topic →
the exact items of one worksheet, deterministically; and the worker's
rendering of a composed set into the two DOCX artifacts. Catalogue Phase 3,
decision 9 (2026-09-06). No model call anywhere in this module.

``compose(items, spec, seed)``
  * ``spec`` is a ``question_set_blueprints.spec``: ``{count, objective_ratio,
    difficulty_mix {"1": 0.5, …}, preset, total_marks}``;
  * the count is split objective / subjective by ``objective_ratio`` with the
    LARGEST-REMAINDER method (so 7 × 0.5 is 4 + 3, never 3 + 3 or 4 + 4), and
    each mode's count is split again into difficulty BUCKETS by
    ``difficulty_mix`` the same way; a missing mix is one bucket per mode;
  * every bucket is filled EXACTLY: its candidates (mode, difficulty) are
    shuffled by ``random.Random(seed)`` after a sort by id — so the input
    order of the items cannot change the answer — then taken ROUND-ROBIN over
    ``objective_ref`` so a 6-item bucket on a 3-objective topic draws two per
    objective when the bank allows it;
  * a bucket the bank cannot fill raises ``Unsatisfiable`` naming every short
    bucket (mode, difficulty, need, have). NEVER padded from another
    difficulty: a "hard" blueprint that quietly served easy items would lie
    to the teacher who chose it, and the portal's ``canCompose`` pre-check
    exists so the reviewer sees the shortfall before a generation is spent.

``render_question_set(sb, gen, generation_id, out_dir, base, branding,
language)`` is what ``worker/process.py``'s catalogue branch calls for a
``worksheet`` generation carrying ``params.question_set_id``: it loads the
set and its blueprint, composes when the set has no ``question_ids`` yet
(and writes them back FIRST, so a crash after that point re-renders the same
paper instead of composing a different one), renders through
``docgen.bank_worksheet.write_set`` with the curriculum block from
``params.curriculum_header``, and uploads ``{base}/worksheet.docx`` (kind
``docx``) and ``{base}/answer_key.docx`` (kind ``answer_key_docx``) exactly
as the document block of process.py does. Returns the generation title
``"<topic title> · <blueprint name>"``.
"""

from __future__ import annotations

import logging
import random
from pathlib import Path
from typing import Iterable, Optional

from docgen.docx_builder import header_lines_of
from docgen.strings import _t
from worker import client as db

log = logging.getLogger("worker.composer")

OBJECTIVE_TYPES = frozenset({"mcq", "true_false", "fill_blank", "match", "assertion_reason"})
SUBJECTIVE_TYPES = frozenset({"short_answer", "long_answer", "numerical", "diagram_label"})
MODES = ("objective", "subjective")
DIFFICULTIES = (1, 2, 3, 4, 5)
STATUS_APPROVED = "approved"
DEFAULT_RATIO = 0.5
MAX_COUNT = 60
_QUERY_CHUNK = 150


class Unsatisfiable(RuntimeError):
    """The bank cannot fill the blueprint; ``shortfalls`` lists every short
    bucket as ``{mode, difficulty, need, have}``."""

    def __init__(self, shortfalls: list[dict]):
        self.shortfalls = list(shortfalls)
        parts = [f"{s['mode']} difficulty {s['difficulty'] if s['difficulty'] is not None else 'any'}: "
                 f"need {s['need']}, have {s['have']}" for s in self.shortfalls]
        super().__init__("the approved bank cannot fill the blueprint — " + "; ".join(parts))


# ── arithmetic ─────────────────────────────────────────────────────────


def largest_remainder(total: int, weights: dict) -> dict:
    """Apportion ``total`` over the keys of ``weights`` by the largest-
    remainder method: floors first, then one extra to the keys with the
    largest fractional parts (ties in key order, so the result is a pure
    function of its arguments). Non-positive or non-numeric weights count
    as 0; when nothing weighs anything, everything is 0. Pure."""
    total = max(0, int(total))
    clean: dict = {}
    for k, w in weights.items():
        try:
            w = float(w)
        except (TypeError, ValueError):
            w = 0.0
        clean[k] = w if w > 0 else 0.0
    mass = sum(clean.values())
    if total == 0 or mass <= 0:
        return {k: 0 for k in clean}
    exact = {k: total * w / mass for k, w in clean.items()}
    out = {k: int(exact[k]) for k in clean}
    left = total - sum(out.values())
    order = sorted(clean, key=lambda k: -(exact[k] - out[k]))  # stable: ties keep key order
    for k in order[:left]:
        out[k] += 1
    return out


def mode_of(item: dict) -> str:
    """objective / subjective — from the row, else from the item type."""
    m = str(item.get("answer_mode") or "").strip().lower()
    if m in MODES:
        return m
    return "objective" if str(item.get("item_type") or "") in OBJECTIVE_TYPES else "subjective"


def _difficulty(item: dict) -> Optional[int]:
    try:
        d = int(item.get("difficulty"))
    except (TypeError, ValueError):
        return None
    return d if d in DIFFICULTIES else None


def parse_spec(spec: Optional[dict]) -> tuple[int, float, dict]:
    """``(count, objective_ratio, difficulty_mix)`` from a blueprint spec,
    tolerant of the JSON shapes the portal writes: string keys "1".."5",
    a ratio outside [0, 1] clamped, a count clamped to 1..MAX_COUNT."""
    spec = spec if isinstance(spec, dict) else {}
    try:
        count = int(spec.get("count"))
    except (TypeError, ValueError):
        raise ValueError("blueprint spec has no count") from None
    count = max(1, min(MAX_COUNT, count))
    try:
        ratio = float(spec.get("objective_ratio", DEFAULT_RATIO))
    except (TypeError, ValueError):
        ratio = DEFAULT_RATIO
    ratio = max(0.0, min(1.0, ratio))
    mix: dict = {}
    raw_mix = spec.get("difficulty_mix") if isinstance(spec.get("difficulty_mix"), dict) else {}
    for k, w in raw_mix.items():
        try:
            d, w = int(k), float(w)
        except (TypeError, ValueError):
            continue
        if d in DIFFICULTIES and w > 0:
            mix[d] = mix.get(d, 0.0) + w
    return count, ratio, mix


def bucket_plan(spec: Optional[dict]) -> list[tuple[str, Optional[int], int]]:
    """``[(mode, difficulty | None, n)]`` — the exact fill the blueprint asks
    for, objective buckets first, difficulty ascending; None = any
    difficulty (no mix given). Zero buckets are dropped. Pure."""
    count, ratio, mix = parse_spec(spec)
    split = largest_remainder(count, {"objective": ratio, "subjective": 1.0 - ratio})
    plan: list[tuple[str, Optional[int], int]] = []
    for mode in MODES:
        n_mode = split[mode]
        if n_mode == 0:
            continue
        if not mix:
            plan.append((mode, None, n_mode))
            continue
        per = largest_remainder(n_mode, mix)
        for d in sorted(per):
            if per[d]:
                plan.append((mode, d, per[d]))
    return plan


def _sorted_pool(items: Iterable[dict]) -> list[dict]:
    return sorted((it for it in items if isinstance(it, dict)), key=lambda it: str(it.get("id") or ""))


def compose(items: list[dict], spec: dict, seed) -> list[dict]:
    """The rules in the module docstring. Returns the chosen items, bucket by
    bucket (objective first, difficulty ascending), or raises
    ``Unsatisfiable``. Deterministic in (items as a set, spec, seed)."""
    rng = random.Random(int(seed or 0))
    pool = _sorted_pool(items)
    chosen: list[dict] = []
    used: set[str] = set()
    shortfalls: list[dict] = []
    for mode, diff, n in bucket_plan(spec):
        cands = [it for it in pool
                 if str(it.get("id")) not in used and mode_of(it) == mode
                 and (diff is None or _difficulty(it) == diff)]
        rng.shuffle(cands)
        # Round-robin over objectives, group order = first appearance after
        # the shuffle: a bucket never draws twice from one objective while
        # another objective still has an unused item.
        groups: dict[str, list[dict]] = {}
        for it in cands:
            groups.setdefault(str(it.get("objective_ref") or ""), []).append(it)
        picked: list[dict] = []
        while len(picked) < n and any(groups.values()):
            for key in list(groups):
                if len(picked) >= n:
                    break
                if groups[key]:
                    picked.append(groups[key].pop(0))
        if len(picked) < n:
            shortfalls.append({"mode": mode, "difficulty": diff, "need": n, "have": len(picked)})
        chosen.extend(picked)
        used.update(str(it.get("id")) for it in picked)
    if shortfalls:
        raise Unsatisfiable(shortfalls)
    return chosen


# ── database edges ─────────────────────────────────────────────────────


def _rows(res) -> list[dict]:
    return list(getattr(res, "data", None) or [])


def _chunks(seq: list, n: int) -> Iterable[list]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def load_bank(sb, topic_id: str, language: str) -> list[dict]:
    """The APPROVED items of a topic in a language — the only items a set may
    contain (per-item approval is the route's guarded UPDATE)."""
    return _rows(sb.table("topic_questions").select("*").eq("topic_id", topic_id)
                 .eq("language", language).eq("status", STATUS_APPROVED).execute())


def load_items_by_id(sb, ids: list[str], *, topic_id: Optional[str] = None,
                     language: Optional[str] = None) -> list[dict]:
    """The rows for ``ids`` in the ORDER of ``ids``; a missing id raises —
    a set that names a deleted item cannot be rendered as itself.

    Every row must still be ``approved`` (and, when given, belong to the
    set's topic and language): ``compose`` only ever picks approved items,
    but the ids are re-read on a re-run, and an item a reviewer rejected or
    retired in between — for a factual error, typically — must not be
    printed on the student sheet with its answer in the key. The worker
    re-checks the article gate the same way (decision 13); this is the
    per-item one."""
    by_id: dict[str, dict] = {}
    for chunk in _chunks(list(dict.fromkeys(ids)), _QUERY_CHUNK):
        for row in _rows(sb.table("topic_questions").select("*").in_("id", chunk).execute()):
            by_id[str(row.get("id"))] = row
    missing = [i for i in ids if str(i) not in by_id]
    if missing:
        raise RuntimeError(f"question set names {len(missing)} item(s) that no longer exist: {missing[:5]}")
    withdrawn = [i for i in ids if str(by_id[str(i)].get("status") or "") != STATUS_APPROVED]
    if withdrawn:
        states = sorted({str(by_id[str(i)].get("status") or "?") for i in withdrawn})
        raise RuntimeError(f"question set names {len(withdrawn)} item(s) no longer approved "
                           f"({', '.join(states)}): {withdrawn[:5]}")
    foreign = [i for i in ids
               if (topic_id and str(by_id[str(i)].get("topic_id") or "") != str(topic_id))
               or (language and str(by_id[str(i)].get("language") or "").lower() != str(language).lower())]
    if foreign:
        raise RuntimeError(f"question set names {len(foreign)} item(s) of another topic or language: {foreign[:5]}")
    return [by_id[str(i)] for i in ids]


def _one(sb, table: str, row_id: str) -> Optional[dict]:
    rows = _rows(sb.table(table).select("*").eq("id", row_id).limit(1).execute())
    return rows[0] if rows else None


def render_question_set(sb, gen: dict, generation_id: str, out_dir: Path, base: str, branding: dict,
                        language: str) -> str:
    """See the module docstring. Raises on anything that stops a render (the
    caller's catalogue branch turns that into the generation's error)."""
    from docgen.bank_worksheet import total_marks, write_set

    params = gen.get("params") if isinstance(gen.get("params"), dict) else {}
    set_id = params.get("question_set_id")
    if not isinstance(set_id, str) or not set_id:
        raise RuntimeError("worksheet generation has no params.question_set_id")
    qset = _one(sb, "question_sets", set_id)
    if qset is None:
        raise RuntimeError(f"question set {set_id} not found")
    blueprint = _one(sb, "question_set_blueprints", str(qset.get("blueprint_id") or ""))
    if blueprint is None:
        raise RuntimeError(f"blueprint {qset.get('blueprint_id')} of question set {set_id} not found")
    language = (str(language or qset.get("language") or "en")).strip().lower()
    topic_id = params.get("topic_id") or next(iter(qset.get("topic_ids") or []), None)
    if not topic_id:
        raise RuntimeError(f"question set {set_id} names no topic")
    topic = _one(sb, "topics", str(topic_id)) or {}
    seed = int(qset.get("seed") or 0)

    ids = [str(i) for i in (qset.get("question_ids") or []) if i]
    if ids:
        items = load_items_by_id(sb, ids, topic_id=str(topic_id), language=language)
    else:
        items = compose(load_bank(sb, str(topic_id), language), blueprint.get("spec") or {}, seed)
        ids = [str(it.get("id")) for it in items]
        # Written BEFORE rendering: from here a re-run renders THIS paper.
        sb.table("question_sets").update({"question_ids": ids}).eq("id", set_id).execute()
        log.info("question set %s composed: %d item(s) from blueprint %s", set_id, len(ids), blueprint.get("name"))

    blueprint_name = str(blueprint.get("name") or "").strip() or "Question set"
    title = f"{str(topic.get('title') or '').strip() or 'Topic'} · {blueprint_name}"
    subtitle = (f"{_t('n_questions', language).format(n=len(items))} · "
                f"{_t('total_marks', language)}: {total_marks(items)}")
    paths = write_set(Path(out_dir), title, subtitle, items,
                      header_lines=header_lines_of(params.get("curriculum_header")),
                      language=language, template=(branding or {}).get("docx_template"),
                      blueprint_name=blueprint_name, rng=random.Random(seed))

    # The same two uploads, in the same order and with the same kinds, as the
    # document block of worker/process.py: the 'docx' row first, then the
    # answer key under its own kind (its presence is the app's proof the
    # student document is key-free).
    dest = f"{base}/worksheet.docx"
    db.upload_artifact(sb, str(paths[0]), dest)
    db.add_artifact_row(sb, generation_id, "docx", dest)
    key_dest = f"{base}/answer_key.docx"
    db.upload_artifact(sb, str(paths[1]), key_dest)
    try:
        db.add_artifact_row(sb, generation_id, "answer_key_docx", key_dest)
    except Exception as exc:  # noqa: BLE001 — a missing enum value must not fail the set
        log.warning("answer_key_docx row skipped for %s: %s", generation_id, exc)
    return title


__all__ = [
    "OBJECTIVE_TYPES", "SUBJECTIVE_TYPES", "MODES", "DIFFICULTIES", "STATUS_APPROVED", "MAX_COUNT",
    "Unsatisfiable", "largest_remainder", "mode_of", "parse_spec", "bucket_plan", "compose",
    "load_bank", "load_items_by_id", "render_question_set",
]
