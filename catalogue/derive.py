"""``topic_derive`` — a curriculum's leaves become GROUPED ``topic_candidates``
a curator approves. One model call per cluster, text only, never an image.

Job shape: ``{id, type: 'topic_derive', params: {curriculum_id}, generation_id:
None, book_id: None}``. It is an OBSERVER job (``worker.client.
OBSERVER_JOB_TYPES``): it owns no generation, so nothing here — and nothing in
run.py on its behalf — may write ``generations.status``. It finishes its OWN
job row, done or error, the way the harvest does; run.py only dispatches, in
the same last lane as ``topic_harvest`` (after every builder).

WHY (plan Phase 2a, 2026-09-06). Curricula are stored faithfully in
``curriculum_nodes``; canonical topics live in ``topics``; ``topic_curriculum_map``
links them. Cambridge 0893's 200 leaves are learning-objective SENTENCES
("Understand that all organisms are made of cells…") grouped under 48
sub-strands (3 stages × 16) — mapping them by hand is slow, and queueing them
as candidates would bury the real names (its seed says ``candidates: none``).
This job asks the model, once per sub-strand, for the canonical TOPIC NAMES a
teacher would give the lessons that teach those objectives, and files each
name as ONE candidate carrying the objectives it covers.

Clusters (``build_clusters``):
  * leaves of kind ``objective`` (stored, or inferred from the code by
    ``catalogue.node_kind`` when kind is NULL) → one cluster per parent node
    (the sub-strand); an orphan objective clusters by (grade, strand,
    sub_strand) under the group's first node;
  * leaves of kind ``topic`` / ``chapter`` (names, CBSE) → one cluster per
    unit, or one per grade's chapter list under that grade's first chapter
    node. For these the model only normalises names and suggests merges; the
    DEFAULT proposal, used when the reply is unusable, is each leaf itself;
  * a childless grouping node (strand / sub_strand / unit) is not a leaf that
    names anything and is left alone.

What is written, per proposed topic (``rows_for_cluster``):
    topic_candidates {source_kind: 'curriculum', node_id: <the cluster's parent>,
                      node_ids: [<the objective nodes it covers>], raw_title,
                      normalized: canonical_key(raw_title), rationale,
                      suggested_topic_id}
``suggested_topic_id`` is an existing topic whose ``canonical_key`` or alias
equals the name's key, or the key of any ``matches`` entry that is an existing
topic title. Idempotent: the keys already filed under the parent node are read
first, a cluster that already HAS candidates is skipped WITHOUT a model call,
and a racing insert is absorbed by the harvest's 23505 fallback.

The reply is VALIDATED in code (``validate_proposals``), never trusted:
  * a name must pass ``catalogue.harvest.is_heading``, carry a non-empty
    ``canonical_key``, and not be one of the cluster's codes; a trailing
    period and surrounding quotes are stripped first; an offender is dropped
    and its codes fall through to the orphan repair;
  * a code the cluster does not have is dropped; a code two topics claim stays
    with the FIRST; two topics with one key are merged;
  * at most ``MAX_TOPICS_PER_CLUSTER`` (8) topics survive — the overflow's
    codes become orphans;
  * every orphan (a code no surviving topic lists) is attached to the topic
    whose name shares the most content words with its statement; with no
    shared word it goes to a topic named from the sub-strand (created if
    needed, merging the smallest topics into it should that breach the cap).
  So every code in the cluster ends up in exactly ONE row.

Quota: text-only calls, one per cluster, SEQUENTIAL — Cambridge is 48 calls
(one per sub-strand). Never an image, never a vision call, never while a
builder waits (the lane guarantees that).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from catalogue.harvest import (
    MAX_HEADING_CHARS, clean_heading, insert_candidates, is_heading, lookup_alias_topics,
    strip_numbering,
)
from catalogue.key import canonical_key, singular_token
from catalogue.node_kind import OBJECTIVE_RE, node_kind
from shared.llm import client_for
from worker import client as db

log = logging.getLogger("worker.derive")

JOB_TYPE = "topic_derive"
MODE_OBJECTIVES = "objectives"
MODE_NAMES = "names"

MAX_TOPICS_PER_CLUSTER = 8
MAX_RATIONALE_CHARS = 500
MAX_MATCHES = 10
# Prompt context, bounded so 51 calls stay cheap: the first N existing titles
# (alphabetical) and the first N open candidates from other curricula.
EXISTING_TITLES_IN_PROMPT = 300
OTHER_CANDIDATES_IN_PROMPT = 300
# A reply for a cluster of ≤ 20 objectives is a few hundred tokens; the client
# retries once at double this if the JSON was truncated.
MAX_TOKENS = 2048
_QUERY_CHUNK = 150

SYSTEM_PROMPT = (
    "You are a curriculum specialist who names the lesson topics of a school science "
    "catalogue. You reply with JSON only: no prose before or after it, no markdown fences."
)

# The closed payload shape, for GeminiClient's constrained decoding
# (ClaudeClient accepts and ignores it — see shared/claude_client.py).
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "topics": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "objective_codes": {"type": "array", "items": {"type": "string"}},
                    "rationale": {"type": "string"},
                    "matches": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "objective_codes"],
            },
        }
    },
    "required": ["topics"],
}

# Words that carry no topic meaning when a statement is matched against a
# name: function words and the verbs every learning objective opens with.
_STOPWORDS = frozenset("""
and the for with that are how their from into this these those between within about
which when where what been being can could will would should may might also than then
its into onto per via such each some any all both either neither not only own same
describe explain understand know identify use using discuss compare investigate evaluate
recognise recognize state give name list outline apply predict plan carry make take
including limited related different similar important simple basic
""".split())
_TOKEN = re.compile(r"[a-z0-9]+")
_MIN_WORD = 3


# ── clusters ───────────────────────────────────────────────────────────


@dataclass
class Cluster:
    """The unit of one model call. ``parent_id`` is the node the candidates
    are filed under; ``leaves`` are the objective / name nodes, in sort order."""

    mode: str
    parent_id: str
    label: str
    grade: Optional[str]
    strand: Optional[str]
    sub_strand: Optional[str]
    leaves: list = field(default_factory=list)

    @property
    def codes(self) -> list[str]:
        return [_code(n) for n in self.leaves]

    def statement(self, node: dict) -> str:
        """What the model reads for a leaf: an objective's full statement (the
        description, which the seed carries in full; the title may be the same
        text), a name's title."""
        if self.mode == MODE_OBJECTIVES:
            return clean_heading(node.get("description")) or clean_heading(node.get("title"))
        return clean_heading(node.get("title"))


def _code(node: dict) -> str:
    return str(node.get("code") or "").strip()


def _sort_key(node: dict) -> tuple:
    s = node.get("sort")
    try:
        f = float(s)
    except (TypeError, ValueError):
        f = float("inf")
    return (f, _code(node))


def _grade_key(grade: object) -> tuple:
    g = str(grade or "").strip()
    try:
        return (0, int(g), "")
    except ValueError:
        return (1, 0, g)


def leaf_mode(leaf: dict) -> Optional[str]:
    """How a leaf is proposed: ``objectives`` (kind objective), ``names``
    (kind topic / chapter), or None for a childless grouping node. A leaf of
    unknown kind is a name when its title is one (``is_heading``) and an
    objective when its title is a sentence."""
    kind = node_kind(leaf)
    if kind == "objective":
        return MODE_OBJECTIVES
    if kind in ("topic", "chapter"):
        return MODE_NAMES
    if kind in ("strand", "sub_strand", "unit"):
        return None
    return MODE_NAMES if is_heading(leaf.get("title")) else MODE_OBJECTIVES


def build_clusters(nodes: Iterable[dict]) -> list[Cluster]:
    """Pure. Leaves (nodes nobody names as parent) grouped by parent node —
    or, for parentless leaves, by (grade, strand, sub_strand) under the
    group's first node. Ordered by grade, then by the parent's position."""
    rows = sorted((n for n in nodes if isinstance(n, dict) and n.get("id")), key=_sort_key)
    by_id = {n["id"]: n for n in rows}
    parents = {n.get("parent_id") for n in rows if n.get("parent_id")}
    groups: dict[tuple, list[dict]] = {}
    for leaf in rows:
        if leaf["id"] in parents:
            continue
        mode = leaf_mode(leaf)
        if mode is None:
            continue
        parent = by_id.get(leaf.get("parent_id"))
        if parent is not None:
            key = ("parent", parent["id"], mode)
        else:
            key = ("group", str(leaf.get("grade") or ""), str(leaf.get("strand") or ""),
                   str(leaf.get("sub_strand") or ""), mode)
        groups.setdefault(key, []).append(leaf)

    clusters: list[Cluster] = []
    for key, members in groups.items():
        first = members[0]
        if key[0] == "parent":
            parent = by_id[key[1]]
            parent_id = parent["id"]
            label = (clean_heading(parent.get("title")) or clean_heading(parent.get("sub_strand"))
                     or _code(parent))
            grade = parent.get("grade") or first.get("grade")
            strand = parent.get("strand") or first.get("strand")
            sub_strand = parent.get("sub_strand") or first.get("sub_strand") or clean_heading(parent.get("title"))
        else:
            parent_id = first["id"]
            grade, strand, sub_strand = first.get("grade"), first.get("strand"), first.get("sub_strand")
            label = (clean_heading(sub_strand) or clean_heading(strand)
                     or f"Grade {clean_heading(grade) or '?'} chapters")
        clusters.append(Cluster(mode=key[-1], parent_id=parent_id, label=label,
                                grade=clean_heading(grade) or None, strand=clean_heading(strand) or None,
                                sub_strand=clean_heading(sub_strand) or None, leaves=members))
    clusters.sort(key=lambda c: (_grade_key(c.grade), _sort_key(c.leaves[0])))
    return clusters


# ── the prompt ─────────────────────────────────────────────────────────


def build_prompt(curriculum: dict, cluster: Cluster, existing_titles: list[str],
                 other_candidates: list[tuple[str, str]]) -> str:
    """The one text the model sees for a cluster. Pure."""
    lines = "\n".join(f"{code}: {cluster.statement(n)}" for code, n in zip(cluster.codes, cluster.leaves))
    existing = "\n".join(f"- {t}" for t in existing_titles[:EXISTING_TITLES_IN_PROMPT]) or "(none yet)"
    others = "\n".join(f"- {t} | {c}" for t, c in other_candidates[:OTHER_CANDIDATES_IN_PROMPT]) or "(none)"
    head = (
        "You are naming lesson topics for the SketchCast topic catalogue.\n\n"
        f"Curriculum: {clean_heading(curriculum.get('name')) or '?'} (code {clean_heading(curriculum.get('code')) or '?'})\n"
        f"Stage / grade: {cluster.grade or '?'}\n"
        f"Strand: {cluster.strand or '?'}\n"
        f"Sub-strand / unit: {cluster.label}\n\n"
    )
    if cluster.mode == MODE_OBJECTIVES:
        task = (
            "LEARNING OBJECTIVES in this sub-strand (one per line, CODE: STATEMENT):\n"
            f"{lines}\n\n"
            "TASK: group every objective under a canonical TOPIC NAME - the name a teacher would "
            "give the lesson that teaches it.\n\n"
            "RULES\n"
            "1. Every objective code above appears in exactly ONE topic. Use the codes exactly as "
            "written; never invent a code.\n"
            "2. A name is a short noun or noun phrase of 2-5 words, the way a teacher labels a lesson "
            "topic (\"Plant and animal cells\", \"Acids and alkalis\", \"Speed and velocity\"). No "
            "verbs, no sentences, no trailing period, no code in the name.\n"
            "3. Prefer fewer, broader topics: a sub-strand normally yields 1-4 topics and never more "
            "than 8.\n"
            "4. Objectives from \"Thinking and Working Scientifically\" or \"Science in Context\" are "
            "SKILLS. Name them as skills (\"Planning an investigation\", \"Evaluating evidence\", "
            "\"Models in science\"), not as content.\n"
        )
    else:
        task = (
            "TOPIC NAMES in this unit (one per line, CODE: NAME):\n"
            f"{lines}\n\n"
            "TASK: normalise these names and suggest merges. The default is one topic per name, "
            "keeping the name as given.\n\n"
            "RULES\n"
            "1. Every code above appears in exactly ONE topic. Use the codes exactly as written; "
            "never invent a code.\n"
            "2. Keep each name as given unless it needs tidying: title case, no numbering, no "
            "trailing period, a short noun or noun phrase of 2-5 words. No verbs, no sentences, no "
            "code in the name.\n"
            "3. Merge two names into one topic only when they are the same topic under two "
            "spellings; never merge different topics. Never more than 8 topics.\n"
            "4. A name that is a chapter title stays a topic in its own right.\n"
        )
    tail = (
        "5. If a topic is the same as one of the EXISTING TOPICS listed below, use that title "
        "verbatim as the name.\n"
        "6. If a topic is the same as an OPEN CANDIDATE from another curriculum listed below, put "
        "that candidate's title in \"matches\" so the two can be linked. \"matches\" may also repeat "
        "an existing topic title. Leave it empty when nothing matches.\n"
        "7. \"rationale\" is one sentence saying why these codes belong together.\n\n"
        f"EXISTING TOPICS (titles already in the catalogue):\n{existing}\n\n"
        f"OPEN CANDIDATES FROM OTHER CURRICULA (title | curriculum code):\n{others}\n\n"
        "Reply with JSON only, exactly this shape:\n"
        '{"topics": [{"name": "Cells", "objective_codes": ["7Bs.01", "7Bs.02"], '
        '"rationale": "...", "matches": ["Cell - Basic Unit of life"]}]}'
    )
    return head + task + tail


def ask_model(client, prompt: str) -> Optional[list]:
    """One call; the reply's ``topics`` list, or None when the reply is not a
    usable shape (the client returns a ``{"raw_text": …}`` stub for JSON it
    could neither parse nor repair)."""
    reply = client.analyze(prompt, system=SYSTEM_PROMPT, max_tokens=MAX_TOKENS,
                           response_schema=RESPONSE_SCHEMA)
    data = reply.get("data") if isinstance(reply, dict) and "data" in reply else reply
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("topics"), list):
        return data["topics"]
    return None


# ── validation and repair ──────────────────────────────────────────────


@dataclass
class Proposal:
    name: str
    key: str
    codes: list = field(default_factory=list)
    rationale: str = ""
    matches: list = field(default_factory=list)


def content_words(text: object) -> set[str]:
    """Lower-cased alphanumeric tokens of three or more characters, minus the
    stopwords and the objective verbs, singularised the way the key is —
    "cells" meets "cell". Pure."""
    out = set()
    for tok in _TOKEN.findall(str(text or "").lower()):
        if len(tok) < _MIN_WORD or tok in _STOPWORDS:
            continue
        out.add(singular_token(tok))
    return out


def clean_name(text: object, codes: Iterable[str] = ()) -> str:
    """A proposed name fit to be a candidate, or "". Whitespace collapsed,
    surrounding quotes and trailing periods stripped, a leading numbering
    token removed, then the harvest's gate (``is_heading``) plus a non-empty
    key — and never one of the cluster's own codes or any LO-shaped code."""
    s = clean_heading(text).strip("\"'“”‘’ ")
    s = strip_numbering(s)[:MAX_HEADING_CHARS].strip()
    # The gate sees the trailing period: "All living things are made of
    # cells." is refused as a sentence BEFORE the period is tidied away.
    if not s or not is_heading(s):
        return ""
    s = s.rstrip(" .").strip()
    if not s or not is_heading(s) or not canonical_key(s):
        return ""
    lowered = s.lower()
    if OBJECTIVE_RE.match(s) or any(lowered == str(c).lower() for c in codes):
        return ""
    return s


def _fallback_name(cluster: Cluster) -> str:
    for cand in (cluster.label, cluster.sub_strand, cluster.strand,
                 f"Grade {cluster.grade or '?'} topics"):
        name = clean_name(cand)
        if name:
            return name
    return "Topics"


def validate_proposals(raw_topics: Optional[list], cluster: Cluster) -> list[Proposal]:
    """The rules in the module docstring, applied in order. Pure. Returns
    proposals covering EVERY code of the cluster exactly once — or an empty
    list when the reply named nothing usable at all (the caller decides
    between the names-mode default and a failure)."""
    codes = cluster.codes
    known = {c.lower(): c for c in codes}
    proposals: list[Proposal] = []
    by_key: dict[str, Proposal] = {}
    claimed: set[str] = set()

    for item in (raw_topics or []):
        if not isinstance(item, dict):
            continue
        name = clean_name(item.get("name"), codes)
        if not name:
            continue
        key = canonical_key(name)
        item_codes: list[str] = []
        raw_codes = item.get("objective_codes")
        if raw_codes is None:
            raw_codes = item.get("codes")
        for c in (raw_codes if isinstance(raw_codes, list) else []):
            real = known.get(str(c).strip().lower())
            if real and real not in claimed and real not in item_codes:
                item_codes.append(real)
        matches = [clean_heading(m) for m in (item.get("matches") or []) if isinstance(m, str)]
        matches = [m for m in matches if m][:MAX_MATCHES]
        rationale = clean_heading(item.get("rationale"))[:MAX_RATIONALE_CHARS]
        claimed.update(item_codes)
        if key in by_key:
            p = by_key[key]
            p.codes.extend(item_codes)
            p.matches.extend(m for m in matches if m not in p.matches)
            continue
        p = Proposal(name=name, key=key, codes=item_codes, rationale=rationale, matches=matches)
        by_key[key] = p
        proposals.append(p)

    proposals = [p for p in proposals if p.codes]
    if not proposals:
        return []

    # The cap: the model's order is its priority order; the overflow's codes
    # are orphans below.
    proposals = proposals[:MAX_TOPICS_PER_CLUSTER]
    claimed = {c for p in proposals for c in p.codes}

    # Orphans: best word overlap, else the sub-strand topic.
    statements = {code: cluster.statement(n) for code, n in zip(codes, cluster.leaves)}
    name_words = [content_words(p.name) for p in proposals]
    unplaced: list[str] = []
    for code in codes:
        if code in claimed:
            continue
        words = content_words(statements[code])
        best, best_n = None, 0
        for p, nw in zip(proposals, name_words):
            n = len(words & nw)
            if n > best_n:
                best, best_n = p, n
        if best is not None:
            best.codes.append(code)
        else:
            unplaced.append(code)
    if unplaced:
        fname = _fallback_name(cluster)
        fkey = canonical_key(fname)
        target = next((p for p in proposals if p.key == fkey), None)
        if target is None:
            target = Proposal(name=fname, key=fkey, codes=[],
                              rationale="Objectives the model left unassigned, grouped under their sub-strand.")
            proposals.append(target)
        target.codes.extend(unplaced)
        while len(proposals) > MAX_TOPICS_PER_CLUSTER:
            smallest = min((p for p in proposals if p is not target), key=lambda p: len(p.codes))
            proposals.remove(smallest)
            target.codes.extend(smallest.codes)

    order = {c: i for i, c in enumerate(codes)}
    for p in proposals:
        p.codes = sorted(dict.fromkeys(p.codes), key=order.__getitem__)
    return proposals


def default_proposals(cluster: Cluster) -> list[Proposal]:
    """Names mode, unusable reply: each leaf is its own topic (what the seed
    loader would have queued), skipping a leaf whose title is not a name."""
    out: list[Proposal] = []
    by_key: dict[str, Proposal] = {}
    for code, n in zip(cluster.codes, cluster.leaves):
        name = clean_name(cluster.statement(n), cluster.codes)
        if not name:
            continue
        key = canonical_key(name)
        if key in by_key:
            by_key[key].codes.append(code)
            continue
        p = Proposal(name=name, key=key, codes=[code], rationale="Default: one topic per listed name.")
        by_key[key] = p
        out.append(p)
    return out


# ── database edges ─────────────────────────────────────────────────────


def _chunks(seq: list, n: int) -> Iterable[list]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _rows(res) -> list[dict]:
    return list(getattr(res, "data", None) or [])


def load_curriculum(sb, curriculum_id: str) -> Optional[dict]:
    rows = _rows(sb.table("curricula").select("id,code,name").eq("id", curriculum_id).limit(1).execute())
    return rows[0] if rows else None


def load_nodes(sb, curriculum_id: str) -> list[dict]:
    return _rows(sb.table("curriculum_nodes").select("*").eq("curriculum_id", curriculum_id).execute())


def existing_keys_by_node(sb, node_ids: list[str]) -> tuple[dict[str, set[str]], set[str]]:
    """``(node_id → {normalized}, derived_nodes)`` over every curriculum
    candidate (any status) filed under the given nodes. The keys: a re-run
    must not re-open what a curator has merged or dismissed. ``derived_nodes``
    are the nodes that already carry a row THIS job filed — one with a
    non-empty ``node_ids`` — and a cluster whose parent is among them is done.

    Why not "any row under the parent": a CBSE grade's chapter list is filed
    under its FIRST chapter node, and the seed loader already queued that
    chapter's own title under the same node (``node_ids`` empty, the column's
    default). That row is the loader's, not ours, and must not stop the run."""
    keys: dict[str, set[str]] = {}
    derived: set[str] = set()
    for chunk in _chunks(list(dict.fromkeys(node_ids)), _QUERY_CHUNK):
        if not chunk:
            continue
        res = (sb.table("topic_candidates").select("node_id,normalized,node_ids")
               .eq("source_kind", "curriculum").in_("node_id", chunk).execute())
        for r in _rows(res):
            if not (r.get("node_id") and r.get("normalized")):
                continue
            keys.setdefault(r["node_id"], set()).add(r["normalized"])
            if r.get("node_ids"):
                derived.add(r["node_id"])
    return keys, derived


def load_existing_titles(sb) -> list[str]:
    """The first EXISTING_TITLES_IN_PROMPT topic titles, alphabetical (sorted
    here too, so the list is the same whatever the client's ordering does)."""
    res = sb.table("topics").select("id,title").order("title").limit(EXISTING_TITLES_IN_PROMPT).execute()
    titles = sorted({clean_heading(r.get("title")) for r in _rows(res)} - {""}, key=str.lower)
    return titles[:EXISTING_TITLES_IN_PROMPT]


def load_other_candidates(sb, curriculum_id: str) -> list[tuple[str, str]]:
    """Open curriculum candidates from OTHER curricula as (raw_title,
    curriculum code), alphabetical, for the prompt's cross-link list."""
    res = (sb.table("topic_candidates").select("node_id,raw_title")
           .eq("source_kind", "curriculum").eq("status", "open").execute())
    cands = [r for r in _rows(res) if r.get("node_id") and clean_heading(r.get("raw_title"))]
    if not cands:
        return []
    node_cur: dict[str, str] = {}
    for chunk in _chunks(list({r["node_id"] for r in cands}), _QUERY_CHUNK):
        for n in _rows(sb.table("curriculum_nodes").select("id,curriculum_id").in_("id", chunk).execute()):
            if n.get("id") and n.get("curriculum_id"):
                node_cur[n["id"]] = n["curriculum_id"]
    other_ids = sorted({cid for cid in node_cur.values() if cid != curriculum_id})
    if not other_ids:
        return []
    codes: dict[str, str] = {}
    for chunk in _chunks(other_ids, _QUERY_CHUNK):
        for c in _rows(sb.table("curricula").select("id,code").in_("id", chunk).execute()):
            if c.get("id"):
                codes[c["id"]] = str(c.get("code") or c["id"])
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for r in cands:
        cid = node_cur.get(r["node_id"])
        if not cid or cid == curriculum_id or cid not in codes:
            continue
        pair = (clean_heading(r["raw_title"]), codes[cid])
        if pair not in seen:
            seen.add(pair)
            out.append(pair)
    out.sort(key=lambda p: (p[0].lower(), p[1]))
    return out[:OTHER_CANDIDATES_IN_PROMPT]


def lookup_topic_ids(sb, keys: list[str]) -> dict[str, str]:
    """``key → topic_id`` through ``topics.canonical_key`` first, then the
    aliases (``topic_aliases.normalized``) for the keys still unmatched."""
    found: dict[str, str] = {}
    wanted = [k for k in dict.fromkeys(keys) if k]
    for chunk in _chunks(wanted, _QUERY_CHUNK):
        res = sb.table("topics").select("id,canonical_key").in_("canonical_key", chunk).execute()
        for r in _rows(res):
            if r.get("canonical_key") and r.get("id") and r["canonical_key"] not in found:
                found[r["canonical_key"]] = r["id"]
    rest = [k for k in wanted if k not in found]
    if rest:
        for k, tid in lookup_alias_topics(sb, rest).items():
            found.setdefault(k, tid)
    return found


def rows_for_cluster(cluster: Cluster, proposals: list[Proposal], topic_ids: dict[str, str],
                     other_by_key: Optional[dict[str, tuple[str, str]]] = None) -> list[dict]:
    """The ``topic_candidates`` payloads for a cluster's proposals. Pure.
    ``topic_ids`` maps keys (the names' and the matches') to existing topics;
    ``other_by_key`` maps keys to another curriculum's open candidate so a
    match against one is recorded in the rationale (there is no column for
    that link yet — the curator reads it)."""
    id_by_code = {code: n["id"] for code, n in zip(cluster.codes, cluster.leaves)}
    rows: list[dict] = []
    for p in proposals:
        suggested = topic_ids.get(p.key)
        notes: list[str] = []
        for m in p.matches:
            mk = canonical_key(m)
            if not mk:
                continue
            if suggested is None and mk in topic_ids:
                suggested = topic_ids[mk]
            if other_by_key and mk in other_by_key and mk != p.key:
                title, code = other_by_key[mk]
                notes.append(f'Also proposed by {code} as "{title}".')
        rationale = " ".join(x for x in [p.rationale, *notes] if x).strip()[:MAX_RATIONALE_CHARS]
        rows.append({
            "source_kind": "curriculum",
            "node_id": cluster.parent_id,
            "node_ids": [id_by_code[c] for c in p.codes if c in id_by_code],
            "raw_title": p.name[:MAX_HEADING_CHARS],
            "normalized": p.key,
            "rationale": rationale or None,
            "suggested_topic_id": suggested,
        })
    return rows


# ── the job ────────────────────────────────────────────────────────────


def derive_curriculum(sb, job_id: str, curriculum: dict, nodes: list[dict], client=None) -> dict:
    """The derive proper. One model call per cluster that has no candidates
    yet; a failing cluster is counted and the next one runs (a re-run skips
    the clusters that succeeded). Returns the summary also written to
    ``jobs.stage``; ``failed > 0`` is the caller's cue to finish with error."""
    curriculum_id = curriculum["id"]
    clusters = build_clusters(nodes)
    stage = {
        "phase": "derive", "step": "clusters", "curriculum": curriculum.get("code"),
        "clusters_total": len(clusters), "clusters_done": 0,
        "proposed": 0, "skipped": 0, "failed": 0, "calls": 0,
    }
    errors: list[str] = []
    db.set_stage(sb, job_id, dict(stage))
    db.set_progress(sb, job_id, 5)

    node_ids = [c.parent_id for c in clusters]
    node_ids += [n["id"] for c in clusters if c.mode == MODE_NAMES for n in c.leaves]
    existing, derived = existing_keys_by_node(sb, node_ids) if node_ids else ({}, set())
    context: Optional[tuple[list[str], list[tuple[str, str]], dict]] = None

    for cluster in clusters:
        if cluster.parent_id in derived:
            stage["skipped"] += 1  # done on an earlier run: no model call
        else:
            try:
                if client is None:
                    client = client_for("en")
                if context is None:
                    others = load_other_candidates(sb, curriculum_id)
                    other_by_key = {}
                    for title, code in others:
                        other_by_key.setdefault(canonical_key(title), (title, code))
                    context = (load_existing_titles(sb), others, other_by_key)
                titles, others, other_by_key = context
                raw = ask_model(client, build_prompt(curriculum, cluster, titles, others))
                stage["calls"] += 1
                proposals = validate_proposals(raw, cluster)
                if not proposals:
                    if cluster.mode != MODE_NAMES:
                        raise RuntimeError("model reply carried no usable topics")
                    proposals = default_proposals(cluster)
                # Names mode: a one-leaf proposal the seed loader already
                # filed under that leaf is not filed again under the unit.
                leaf_keys = {_code(n): existing.get(n["id"], set()) for n in cluster.leaves}
                proposals = [p for p in proposals
                             if not (len(p.codes) == 1 and p.key in leaf_keys.get(p.codes[0], ()))]
                keys = [p.key for p in proposals] + [canonical_key(m) for p in proposals for m in p.matches]
                topic_ids = lookup_topic_ids(sb, keys) if keys else {}
                rows = rows_for_cluster(cluster, proposals, topic_ids, other_by_key)
                already = existing.get(cluster.parent_id, set())
                rows = [r for r in rows if r["normalized"] not in already]
                stage["proposed"] += insert_candidates(sb, rows) if rows else 0
            except Exception as exc:  # noqa: BLE001 — the next cluster still runs
                stage["failed"] += 1
                msg = f"{cluster.label}: {type(exc).__name__}: {exc}"[:300]
                errors.append(msg)
                log.warning("derive %s cluster failed — %s", curriculum.get("code"), msg)
        stage["clusters_done"] += 1
        db.set_stage(sb, job_id, dict(stage))
        db.set_progress(sb, job_id, 5 + int(90 * stage["clusters_done"] / max(1, len(clusters))))

    usage = getattr(client, "session_usage", None) if client is not None else None
    if isinstance(usage, dict) and usage.get("calls"):
        db.set_job_usage(sb, job_id, usage)
    summary = {**stage, "step": "done"}
    if errors:
        summary["errors"] = errors[:5]
    return summary


def run_derive_job(sb, job: dict, client=None) -> Optional[dict]:
    """Entry point for run.py. Self-contained: finishes the job row itself
    (done with the summary in ``stage``; error with the message) and never
    raises. ``client`` is for tests; production builds ``client_for("en")``
    lazily, only when a cluster actually needs a call. Returns the summary
    (also when some clusters failed — the row then says error), or None when
    nothing could run."""
    job_id = job["id"]
    try:
        params = job.get("params") or {}
        curriculum_id = params.get("curriculum_id") if isinstance(params, dict) else None
        if not curriculum_id:
            raise RuntimeError("topic_derive job without params.curriculum_id")
        curriculum = load_curriculum(sb, curriculum_id)
        if not curriculum:
            raise RuntimeError(f"curriculum {curriculum_id} not found")
        nodes = load_nodes(sb, curriculum_id)
        summary = derive_curriculum(sb, job_id, curriculum, nodes, client=client)
        db.set_stage(sb, job_id, summary)
        if summary.get("failed"):
            first = (summary.get("errors") or ["?"])[0]
            db.finish_job(sb, job_id, None, error=(
                f"{summary['failed']} of {summary['clusters_total']} clusters failed; "
                f"re-run to retry them. First: {first}")[:4000])
            log.error("derive %s: %s", curriculum.get("code"), summary)
        else:
            db.finish_job(sb, job_id)  # no generation: an observer job owns none
            log.info("derive %s: %s", curriculum.get("code"), summary)
        return summary
    except Exception as exc:  # noqa: BLE001
        log.error("derive job %s failed: %s", job_id, exc)
        try:
            db.finish_job(sb, job_id, None, error=f"{type(exc).__name__}: {exc}"[:4000])
        except Exception as exc2:  # noqa: BLE001
            log.error("derive job %s: could not record the failure: %s", job_id, exc2)
        return None


__all__ = [
    "JOB_TYPE", "MODE_OBJECTIVES", "MODE_NAMES", "MAX_TOPICS_PER_CLUSTER", "SYSTEM_PROMPT",
    "RESPONSE_SCHEMA", "Cluster", "Proposal", "leaf_mode", "build_clusters", "build_prompt",
    "ask_model", "content_words", "clean_name", "validate_proposals", "default_proposals",
    "existing_keys_by_node", "load_existing_titles", "load_other_candidates", "lookup_topic_ids",
    "rows_for_cluster", "derive_curriculum", "run_derive_job",
]
