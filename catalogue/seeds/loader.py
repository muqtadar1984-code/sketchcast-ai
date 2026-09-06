"""Load one curriculum seed file into ``curricula`` / ``curriculum_nodes`` and
open a ``topic_candidates`` row for every LEAF node. Idempotent.

Seed file shape (one curriculum per file, written by hand or by a parser):

    {
      "curriculum": {"code", "name", "kind", "country", "edition", "source_url"},
      "nodes": [
        {"code", "grade", "strand", "sub_strand", "title", "description",
         "parent_code", "sort"},
        ...
      ]
    }

``code`` is unique within the file; ``parent_code`` names another node's
``code`` in the SAME file (or is null / absent for a root). Nodes may appear
in any order — a parent after its children is fine.

Write order, and why it is two passes:
  1. ``curricula`` upserted on ``code`` (a real unique column, so PostgREST's
     ``on_conflict`` can name it) — only when the row is new or a field the
     file carries is stored differently.
  2. ``curriculum_nodes`` upserted on ``(curriculum_id, code)`` WITHOUT
     ``parent_id`` — again only the rows that are new or whose stored fields
     differ from the file: the stored rows (id, code and every node column)
     are read first and compared field by field, so ``nodes_updated`` is the
     number of rows that actually changed, never the number the file has.
     Parents are resolved by code → id, and an id exists only after the row
     does, so no single pass can set parents for a file whose parents come
     after their children.
  3. Every node's ``parent_id`` is read back and set where it differs from
     what ``parent_code`` says — an UPDATE per node that is wrong, none for a
     node that is already right. Together with the diffs in 1 and 2 that is
     what makes the second run silent: it reads, compares, and writes nothing.
  4. ``topic_candidates {source_kind: 'curriculum', node_id, raw_title,
     normalized}`` for LEAF nodes only (a strand or a unit is a grouping, not
     a topic), inserting only the keys the node does not already carry.

``dry_run=True`` parses and validates the file and reports what WOULD be
written; it never touches the client (which may then be None).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterable, Optional

from catalogue.key import canonical_key

log = logging.getLogger("worker.seeds")

_CURRICULUM_FIELDS = ("code", "name", "kind", "country", "edition", "source_url")
_NODE_FIELDS = ("code", "grade", "strand", "sub_strand", "title", "description", "sort")
RAW_TITLE_MAX = 120
_CHUNK = 200


class SeedError(ValueError):
    """The seed file is malformed. Nothing has been written when this is raised
    from the validation step; the loader validates BEFORE its first write."""


# ── pure half ──────────────────────────────────────────────────────────


def read_seed(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _code(node: dict) -> str:
    return str(node.get("code") or "").strip()


def _parent_code(node: dict) -> str:
    p = node.get("parent_code")
    return str(p).strip() if p is not None else ""


def validate_seed(seed: dict, where: str = "seed") -> None:
    """Raise ``SeedError`` on anything the loader could not write cleanly:
    a missing curriculum code or name, a node without code or title, a
    duplicate node code, a parent_code no node in the file has."""
    if not isinstance(seed, dict):
        raise SeedError(f"{where}: top level must be an object")
    cur = seed.get("curriculum")
    if not isinstance(cur, dict) or not str(cur.get("code") or "").strip():
        raise SeedError(f"{where}: curriculum.code is required")
    if not str(cur.get("name") or "").strip():
        raise SeedError(f"{where}: curriculum.name is required")
    nodes = seed.get("nodes")
    if not isinstance(nodes, list):
        raise SeedError(f"{where}: nodes must be a list")
    codes: set[str] = set()
    for i, n in enumerate(nodes):
        if not isinstance(n, dict):
            raise SeedError(f"{where}: nodes[{i}] must be an object")
        code = _code(n)
        if not code:
            raise SeedError(f"{where}: nodes[{i}] has no code")
        if code in codes:
            raise SeedError(f"{where}: node code {code!r} appears twice")
        codes.add(code)
        if not str(n.get("title") or "").strip():
            raise SeedError(f"{where}: node {code!r} has no title")
    for n in nodes:
        parent = _parent_code(n)
        if parent and parent not in codes:
            raise SeedError(f"{where}: node {_code(n)!r} names parent {parent!r}, "
                            "which is not in the file")
        if parent and parent == _code(n):
            raise SeedError(f"{where}: node {_code(n)!r} is its own parent")


def leaf_codes(nodes: Iterable[dict]) -> list[str]:
    """Codes of the nodes no other node names as its parent — in file order."""
    nodes = list(nodes)
    parents = {_parent_code(n) for n in nodes if _parent_code(n)}
    return [_code(n) for n in nodes if _code(n) not in parents]


def candidate_for_node(node: dict) -> Optional[dict]:
    """The ``topic_candidates`` payload for a leaf node, minus ``node_id``;
    None when the title has no key material.

    The key is taken from the STORED title (truncated to the column's 120
    characters), so every row satisfies ``normalized == canonical_key(raw_title)``
    — the invariant the harvest rows keep, and the one a reader of the table
    can check without the seed file in hand."""
    title = " ".join(str(node.get("title") or "").split())[:RAW_TITLE_MAX]
    key = canonical_key(title)
    if not key:
        return None
    return {"source_kind": "curriculum", "raw_title": title, "normalized": key}


CANDIDATE_MODES = ("leaves", "none")


def candidate_mode(seed: dict) -> str:
    """``curriculum.candidates`` in the seed file: ``"leaves"`` (default) queues
    a topic_candidates row per leaf node; ``"none"`` queues nothing.

    Cambridge 0893's leaves are learning-objective SENTENCES ("Understand that
    all organisms are made of cells…"), not topic names — 200 of them would bury
    the real names in the queue. Those nodes are covered from the portal's
    Curricula screen ("Create topic from node"), so its file says "none". CBSE's
    leaves are chapter and topic NAMES, so its file keeps the default. The key
    is a seed-file instruction, not a column: _CURRICULUM_FIELDS drops it."""
    mode = str(seed.get("curriculum", {}).get("candidates") or "leaves").strip().lower()
    if mode not in CANDIDATE_MODES:
        raise SeedError(f"curriculum.candidates must be one of {CANDIDATE_MODES}, got {mode!r}")
    return mode


def plan_seed(seed: dict) -> dict:
    """What a load WOULD do, from the file alone (dry-run's whole output)."""
    nodes = seed["nodes"]
    leaves = leaf_codes(nodes)
    by_code = {_code(n): n for n in nodes}
    cands = [candidate_for_node(by_code[c]) for c in leaves] if candidate_mode(seed) == "leaves" else []
    return {
        "curriculum": str(seed["curriculum"]["code"]).strip(),
        "nodes": len(nodes),
        "roots": sum(1 for n in nodes if not _parent_code(n)),
        "leaves": len(leaves),
        "candidates": sum(1 for c in cands if c),
    }


def _row_for_curriculum(cur: dict) -> dict:
    row = {k: cur.get(k) for k in _CURRICULUM_FIELDS if k in cur}
    row["code"] = str(cur["code"]).strip()
    if not row.get("kind"):
        row["kind"] = "syllabus"
    return row


def _row_for_node(node: dict, curriculum_id: str) -> dict:
    row = {k: node.get(k) for k in _NODE_FIELDS if k in node}
    row["code"] = _code(node)
    row["curriculum_id"] = curriculum_id
    return row


def _chunks(seq: list, n: int) -> Iterable[list]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _rows(res) -> list[dict]:
    return list(getattr(res, "data", None) or [])


def _same(a, b) -> bool:
    """One stored field against the file's. Equal values are equal; so are a
    number and its string ("7" in the file, 7 back from an integer column) —
    PostgREST would store them as the same value, so they are not a reason to
    write. None only equals None."""
    if a == b:
        return True
    if a is None or b is None:
        return False
    return str(a) == str(b)


def _differs(want: dict, have: dict) -> bool:
    """True when any field the payload carries is stored differently. Only
    the payload's columns are compared — PostgREST's upsert SETs only those,
    so a column the file omits is never a reason to write."""
    return any(not _same(have.get(k), v) for k, v in want.items())


# ── database half ──────────────────────────────────────────────────────


def _upsert_curriculum(sb, cur_row: dict) -> tuple[str, bool]:
    """Returns (curriculum_id, created). The row is upserted only when it is
    new or a field the file carries is stored differently — a second run of
    an unchanged file reads it and writes nothing."""
    before = _rows(sb.table("curricula").select("id," + ",".join(_CURRICULUM_FIELDS))
                   .eq("code", cur_row["code"]).execute())
    if before and not _differs(cur_row, before[0]):
        return before[0]["id"], False
    sb.table("curricula").upsert(cur_row, on_conflict="code").execute()
    after = _rows(sb.table("curricula").select("id").eq("code", cur_row["code"]).execute())
    if not after:
        raise RuntimeError(f"curriculum {cur_row['code']!r} was not written")
    return after[0]["id"], not before


def _upsert_nodes(sb, curriculum_id: str, nodes: list[dict]) -> tuple[dict[str, str], int, int]:
    """Pass 1: every node row WITHOUT parent_id, upserted on (curriculum_id,
    code) — but only the rows that are new or whose stored fields differ from
    the file. The curriculum's rows are read once (id, code and every
    _NODE_FIELDS column) and compared field by field, so an unchanged file
    upserts nothing and ``updated`` counts the rows that actually changed.
    Returns (code → id, created, updated)."""
    cols = "id,curriculum_id," + ",".join(_NODE_FIELDS)  # code is among the fields
    existing = {r["code"]: r for r in _rows(
        sb.table("curriculum_nodes").select(cols).eq("curriculum_id", curriculum_id).execute())}
    rows = [_row_for_node(n, curriculum_id) for n in nodes]
    to_write = [r for r in rows if r["code"] not in existing or _differs(r, existing[r["code"]])]
    for chunk in _chunks(to_write, _CHUNK):
        sb.table("curriculum_nodes").upsert(chunk, on_conflict="curriculum_id,code").execute()
    if to_write:
        ids = {r["code"]: r["id"] for r in _rows(
            sb.table("curriculum_nodes").select("id,code").eq("curriculum_id", curriculum_id).execute())}
    else:
        ids = {code: r["id"] for code, r in existing.items()}
    missing = [r["code"] for r in rows if r["code"] not in ids]
    if missing:
        raise RuntimeError(f"curriculum_nodes not written for codes {missing[:5]}")
    created = sum(1 for r in to_write if r["code"] not in existing)
    return ids, created, len(to_write) - created


def _set_parents(sb, curriculum_id: str, nodes: list[dict], ids: dict[str, str]) -> int:
    """Pass 2: parent_id from parent_code, written only where it differs.
    Returns the number of nodes updated."""
    current = {r["id"]: r.get("parent_id") for r in _rows(
        sb.table("curriculum_nodes").select("id,parent_id").eq("curriculum_id", curriculum_id).execute())}
    changed = 0
    for n in nodes:
        parent_code = _parent_code(n)
        want = ids[parent_code] if parent_code else None
        node_id = ids[_code(n)]
        if current.get(node_id) == want:
            continue
        sb.table("curriculum_nodes").update({"parent_id": want}).eq("id", node_id).execute()
        changed += 1
    return changed


def _open_candidates(sb, nodes: list[dict], ids: dict[str, str], mode: str = "leaves") -> tuple[int, int]:
    """Pass 3: a topic_candidates row per LEAF node whose key it does not
    already carry — unless the file says ``candidates: none``. Returns
    (inserted, already_present)."""
    if mode == "none":
        return 0, 0
    leaves = leaf_codes(nodes)
    by_code = {_code(n): n for n in nodes}
    wanted: list[dict] = []
    for code in leaves:
        c = candidate_for_node(by_code[code])
        if c:
            c["node_id"] = ids[code]
            wanted.append(c)
    if not wanted:
        return 0, 0
    have: set[tuple[str, str]] = set()
    node_ids = [w["node_id"] for w in wanted]
    for chunk in _chunks(node_ids, _CHUNK):
        res = (sb.table("topic_candidates").select("node_id,normalized")
               .eq("source_kind", "curriculum").in_("node_id", chunk).execute())
        have |= {(r["node_id"], r["normalized"]) for r in _rows(res)}
    new = [w for w in wanted if (w["node_id"], w["normalized"]) not in have]
    for chunk in _chunks(new, _CHUNK):
        sb.table("topic_candidates").insert(chunk).execute()
    return len(new), len(wanted) - len(new)


def load_seed(sb, path: str | Path, dry_run: bool = False) -> dict:
    """Load one seed file. Returns counts:

        {"file", "curriculum", "dry_run", "nodes", "roots", "leaves",
         "candidates",                      # the file's own plan
         "curriculum_id", "curriculum_created": 0|1,
         "nodes_created", "nodes_updated",  # rows written: new / stored fields differed
         "parents_set",
         "candidates_created", "candidates_existing"}   # what was written

    Every count is of rows actually WRITTEN: a second run of an unchanged
    file reports zeros for all of them (and ``candidates_existing`` for the
    candidates it found already filed).

    On a dry run only the plan is returned; the client is never called and
    may be None.
    """
    path = Path(path)
    seed = read_seed(path)
    validate_seed(seed, where=path.name)
    plan = plan_seed(seed)
    out = {"file": str(path), "dry_run": bool(dry_run), **plan}
    if dry_run:
        return out
    if sb is None:
        raise RuntimeError("load_seed needs a Supabase client unless dry_run=True")

    cur_id, created = _upsert_curriculum(sb, _row_for_curriculum(seed["curriculum"]))
    nodes = seed["nodes"]
    ids, n_created, n_updated = _upsert_nodes(sb, cur_id, nodes)
    parents_set = _set_parents(sb, cur_id, nodes, ids)
    c_created, c_existing = _open_candidates(sb, nodes, ids, candidate_mode(seed))
    out.update({
        "curriculum_id": cur_id,
        "curriculum_created": int(created),
        "nodes_created": n_created,
        "nodes_updated": n_updated,
        "parents_set": parents_set,
        "candidates_created": c_created,
        "candidates_existing": c_existing,
    })
    log.info("seed %s: %s", path.name, out)
    return out


__all__ = ["SeedError", "read_seed", "validate_seed", "leaf_codes", "candidate_for_node",
           "candidate_mode", "CANDIDATE_MODES", "plan_seed", "load_seed", "RAW_TITLE_MAX"]
