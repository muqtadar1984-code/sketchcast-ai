"""Stage A: scope blueprints.

One code path for every book. Subject differences enter only as data: the
brief (curriculum, grades, strand prefixes) and the topic list it selects.
The model plans the COMPLETE scope. The catalogue audit (Stage B) runs
afterwards and never edits the blueprint.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
from pathlib import Path

from psycopg.types.json import Jsonb
from pydantic import ValidationError

from publishing.catalogue_view import CatalogueView
from publishing.schemas import CAPABILITY_KINDS, ROOT, Blueprint, BookBrief

_log = logging.getLogger(__name__)

PROMPTS = ROOT / "prompts"
CONSTITUTION = "constitution_v0_1.md"
BLUEPRINT_CARD = "blueprint_card_v0_2.md"
PROMPT_VERSIONS = {"constitution": "0.1", "blueprint_card": "0.2"}
MAX_TOKENS = 24000

# Exclusion reasons that plan to the SUPPLY rather than to the learner.
SUPPLY_REASON = re.compile(
    r"\b(no (existing )?(catalogue|article|topic|content)|not (yet )?(in|available in) the catalogue|"
    r"catalogue (does not|doesn't) (have|contain)|missing from the catalogue|no material)\b", re.I)

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid() -> str:
    """26-char ULID (48-bit ms time + 80 random bits), Crockford base32."""
    n = (int(time.time() * 1000) << 80) | secrets.randbits(80)
    return "".join(_CROCKFORD[(n >> (5 * i)) & 31] for i in reversed(range(26)))


def load_briefs() -> list[BookBrief]:
    data = json.loads((ROOT / "config" / "briefs.json").read_text(encoding="utf-8"))
    return [BookBrief.model_validate(b) for b in data]


# -- context ---------------------------------------------------------------------
def _grade_of(code: str) -> str | None:
    m = re.match(r"^cbse:(\d+):", code) or re.match(r"^(\d)", code)
    return m.group(1) if m else None


def in_scope_nodes(cat: CatalogueView, brief: BookBrief) -> list[dict]:
    cur = next(c for c in cat.curricula if c["code"] == brief.primary_curriculum)
    pat = re.compile(rf"^({'|'.join(brief.grades)})({'|'.join(brief.strand_prefixes)})\.")
    return sorted((n for n in cat.nodes
                   if n["curriculum_id"] == cur["id"]
                   and (pat.match(n["code"] or "") or n["code"] in brief.extra_node_codes)),
                  key=lambda n: n["code"])


def crosswalk_nodes(cat: CatalogueView, brief: BookBrief, primary: list[dict]) -> list[dict]:
    ids = {c["id"] for c in cat.curricula if c["code"] in brief.crosswalk_curricula}
    out = {}
    if brief.crosswalk_prefixes:
        pat = re.compile(rf"^cbse:({'|'.join(brief.grades)}):({'|'.join(brief.crosswalk_prefixes)})\b")
        out.update({n["id"]: n for n in cat.nodes if n["curriculum_id"] in ids and pat.match(n["code"] or "")})
    primary_ids = {n["id"] for n in primary}
    topics = {m["topic_id"] for m in cat.tcm if m["node_id"] in primary_ids}
    for m in cat.tcm:
        n = cat.node_by_id.get(m["node_id"])
        if m["topic_id"] in topics and n and n["curriculum_id"] in ids:
            out[n["id"]] = n
    return sorted(out.values(), key=lambda n: n["code"])


def topic_pool(cat: CatalogueView, brief: BookBrief) -> list[dict]:
    family = set(brief.catalogue_subjects)
    pool = []
    for t in cat.topics:
        if t["subject"] not in family or t["status"] == "retired":
            continue
        codes = sorted(cat.node_by_id[m["node_id"]]["code"] for m in cat.map_by_topic.get(t["id"], [])
                       if m["node_id"] in cat.node_by_id)
        pool.append({"slug": t["slug"], "title": t["title"], "mapped_objectives": codes})
    return sorted(pool, key=lambda t: t["title"])


def build_prompt(cat: CatalogueView, brief: BookBrief) -> tuple[str, str, dict]:
    nodes = in_scope_nodes(cat, brief)
    cross = crosswalk_nodes(cat, brief, nodes)
    pool = topic_pool(cat, brief)
    system = (PROMPTS / CONSTITUTION).read_text(encoding="utf-8")
    card = (PROMPTS / BLUEPRINT_CARD).read_text(encoding="utf-8").format(
        capability_kinds=", ".join(sorted(CAPABILITY_KINDS)))
    ctx = {
        "book_brief": brief.model_dump(exclude={"strand_prefixes", "crosswalk_prefixes", "extra_node_codes"}),
        "in_scope_objectives": [{"code": n["code"], "grade": n["grade"], "title": n["title"]} for n in nodes],
        "crosswalk_objectives": [{"code": n["code"], "title": n["title"]} for n in cross],
        "catalogue_topics": pool,
    }
    prompt = card + "\n\nINPUT:\n" + json.dumps(ctx, ensure_ascii=False, indent=1)
    return system, prompt, {"in_scope": [n["code"] for n in nodes], "crosswalk": [n["code"] for n in cross],
                            "pool": [t["slug"] for t in pool]}


# -- deterministic checks ----------------------------------------------------------
def check_blueprint(bp: Blueprint, cat: CatalogueView, brief: BookBrief, in_scope: list[str]) -> dict:
    """Every check returns ok + detail. The blueprint is stored either way;
    a failed check blocks Gate B."""
    res: dict[str, dict] = {}
    proposed_codes = {p.code for p in bp.proposed_curriculum_nodes}
    used = {c for ch in bp.chapters for c in ch.node_codes} | {
        c for ch in bp.chapters for rt in ch.required_topics for c in rt.node_codes}
    excluded = {e.code for e in bp.excluded_nodes}

    unknown = sorted(c for c in used | excluded if c not in cat.node_by_code and c not in proposed_codes)
    res["codes_known"] = {"ok": not unknown, "detail": unknown}

    unassigned = sorted(set(in_scope) - used - excluded)
    res["coverage_complete"] = {"ok": not unassigned, "detail": unassigned}

    supply = [f"{e.code}: {e.reason}" for e in bp.excluded_nodes if SUPPLY_REASON.search(e.reason)]
    res["no_supply_exclusions"] = {"ok": not supply, "detail": supply}

    bad_slugs, bad_rt = [], []
    for ch in bp.chapters:
        for rt in ch.required_topics:
            if rt.topic_slug and rt.topic_slug not in cat.topic_by_slug:
                bad_slugs.append(rt.topic_slug)
            if not rt.topic_slug and not rt.proposed_title:
                bad_rt.append(ch.number)
            if rt.topic_slug and cat.topic_by_slug.get(rt.topic_slug, {}).get("status") == "retired":
                bad_slugs.append(f"{rt.topic_slug} (retired)")
    res["topics_resolve"] = {"ok": not bad_slugs and not bad_rt,
                             "detail": {"unknown_slugs": bad_slugs, "chapters_with_empty_topic": bad_rt}}

    empty = [ch.number for ch in bp.chapters if not ch.required_topics]
    review_like = [ch.number for ch in bp.chapters if REVIEW_TITLE.search(ch.title)]
    res["chapters_have_topics"] = {"ok": all(n in review_like for n in empty), "detail": empty}

    numbers = [ch.number for ch in bp.chapters]
    graph = {ch.number: set(ch.prerequisite_chapters) for ch in bp.chapters}
    dangling = sorted({p for ps in graph.values() for p in ps if p not in graph})
    cyclic = _has_cycle(graph)
    res["prerequisites_acyclic"] = {"ok": not cyclic and not dangling,
                                    "detail": {"cycle": cyclic, "dangling": dangling}}
    res["chapter_numbers"] = {"ok": numbers == list(range(1, len(numbers) + 1)), "detail": numbers}

    out_of_band = sorted(c for c in used if c in cat.node_by_code and _grade_of(c) not in brief.grades)
    res["grade_band"] = {"ok": not out_of_band, "detail": out_of_band}

    res["size_bounds"] = {"ok": 6 <= len(bp.chapters) <= 16 and 60 <= bp.estimated_pages <= 400,
                          "detail": {"chapters": len(bp.chapters), "pages": bp.estimated_pages}}

    # Proposed curriculum codes must look like the curriculum's own codes
    # (no invented placeholders such as "proposed:F.01").
    shapes = {_shape(n["code"]) for n in cat.nodes if n["curriculum_id"] == _curriculum_id(cat, brief)}
    bad_codes = sorted(p.code for p in bp.proposed_curriculum_nodes if _shape(p.code) not in shapes)
    res["proposed_codes_official_format"] = {"ok": not bad_codes, "detail": bad_codes}

    review_new = [ch.number for ch in bp.chapters if REVIEW_TITLE.search(ch.title)
                  and any(not rt.topic_slug for rt in ch.required_topics)]
    res["review_chapters_reuse_topics"] = {"ok": not review_new, "detail": review_new}

    proposed = sorted({rt.proposed_title for ch in bp.chapters for rt in ch.required_topics
                       if not rt.topic_slug and rt.proposed_title})
    dup_new = [(a, b) for i, a in enumerate(proposed) for b in proposed[i + 1:] if similar(a, b)]
    pool = [t["title"] for t in cat.topics if t["subject"] in brief.catalogue_subjects and t["status"] != "retired"]
    dup_existing = [(a, b) for a in proposed for b in pool if similar(a, b)]
    res["no_duplicate_proposals"] = {"ok": not dup_new and not dup_existing,
                                     "detail": {"within_book": dup_new, "vs_existing": dup_existing}}

    needs = [k for ch in bp.chapters for k in ch.illustration_needs]
    unknown_kinds = sorted({n for n in needs if capability_kind(n) is None})
    res["capability_kinds"] = {"ok": not unknown_kinds, "detail": unknown_kinds}

    res["all_ok"] = all(v["ok"] for v in res.values())
    return res


REVIEW_TITLE = re.compile(r"\b(review|revision|mastery|consolidation|exam practice)\b", re.I)
_STOP = {"and", "the", "of", "a", "an", "in", "to", "for", "with", "on", "their", "its", "using"}


def _tokens(title: str) -> set[str]:
    return {_word(w) for w in re.split(r"[^a-z0-9]+", title.lower()) if w and w not in _STOP}


def similar(a: str, b: str, threshold: float = 0.6) -> bool:
    """Token overlap (Jaccard) between two topic titles."""
    ta, tb = _tokens(a), _tokens(b)
    return bool(ta and tb) and len(ta & tb) / len(ta | tb) >= threshold


def _shape(code: str) -> str:
    """'8Gg.04' -> '9Aa.99': the format of a curriculum code, not its value."""
    return re.sub(r"[a-z]", "a", re.sub(r"[A-Z]", "A", re.sub(r"[0-9]", "9", code)))


def _curriculum_id(cat: CatalogueView, brief: BookBrief) -> str | None:
    return next((c["id"] for c in cat.curricula if c["code"] == brief.primary_curriculum), None)


def capability_kind(need: str) -> str | None:
    """The capability kind an illustration need starts with.

    Models write either the bare kind ("chart") or the kind followed by what
    to draw ("Apparatus illustration of a friction experiment"). The leading
    words decide; the longest matching kind wins. None = not a known kind
    (a capability gap)."""
    words = [_word(w) for w in re.split(r"[^a-z0-9]+", need.lower()) if w]
    best = None                                   # (-length, position, kind): longest kind wins
    for k in CAPABILITY_KINDS:
        parts = [_word(p) for p in k.split("_")]   # same folding on both sides
        for i in range(len(words) - len(parts) + 1):
            if words[i:i + len(parts)] == parts:
                cand = (-len(parts), i, k)
                best = min(best, cand) if best else cand
                break
    return best[2] if best else None


_ALIASES = {"photograph": "photo", "photographs": "photo", "graph": "chart", "graphs": "chart"}


def _word(w: str) -> str:
    """Alias, then fold a plain plural ("diagrams" -> "diagram")."""
    w = _ALIASES.get(w, w)
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us")) else w


def _has_cycle(graph: dict[int, set[int]]) -> list[int]:
    state: dict[int, int] = {}

    def visit(n, path):
        state[n] = 1
        for m in graph.get(n, ()):
            if state.get(m) == 1:
                return path + [n, m]
            if m in graph and not state.get(m):
                found = visit(m, path + [n])
                if found:
                    return found
        state[n] = 2
        return []

    for n in graph:
        if not state.get(n):
            c = visit(n, [])
            if c:
                return c
    return []


# -- model -------------------------------------------------------------------------
def author_client(provider: str):
    """publish_author role. The pipeline never names a model elsewhere."""
    if provider == "anthropic":
        os.environ.setdefault("LLM_BACKEND", "bedrock")
        from shared.claude_client import ClaudeClient
        return ClaudeClient(model=os.getenv("PUBLISH_CLAUDE_MODEL", "claude-sonnet-5-5"))
    if provider == "gemini":
        from shared.gemini_client import GeminiClient, gemini_model
        return GeminiClient(model=os.getenv("PUBLISH_GEMINI_MODEL") or gemini_model("analysis"))
    raise ValueError(f"unknown provider {provider!r}")


def _load_model_env(env_file: Path) -> None:
    """Only the model credentials; never the Supabase keys."""
    keep = {"VERTEX_PROJECT_ID", "VERTEX_REGION", "GOOGLE_APPLICATION_CREDENTIALS"}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            if k.strip() in keep:
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


def generate(cat: CatalogueView, brief: BookBrief, provider: str) -> dict:
    system, prompt, ctx = build_prompt(cat, brief)
    client = author_client(provider)
    t0 = time.time()
    reply = client.analyze(prompt, system=system, max_tokens=MAX_TOKENS)
    elapsed = time.time() - t0
    data = reply["data"]
    if reply.get("truncated"):
        raise RuntimeError(f"{brief.slug}: blueprint reply hit the output cap ({MAX_TOKENS})")
    try:
        bp = Blueprint.model_validate(data)
    except ValidationError as e:
        raise RuntimeError(f"{brief.slug}: blueprint failed schema validation: {e}") from e
    return {"blueprint": bp, "ctx": ctx, "usage": reply["usage"], "elapsed": elapsed,
            "model": getattr(client, "model", "?")}


def store(conn, cat: CatalogueView, brief: BookBrief, result: dict, provider: str) -> int:
    bp: Blueprint = result["blueprint"]
    checks = check_blueprint(bp, cat, brief, result["ctx"]["in_scope"])
    w = conn.execute("select uid from pub.works where slug=%s", (brief.slug,)).fetchone()
    if not w:
        uid = ulid()
        conn.execute("insert into pub.works (uid, slug, title, subject_strand, primary_curriculum_code, grade_band, "
                     "age_band, brief) values (%s,%s,%s,%s,%s,%s,%s,%s)",
                     (uid, brief.slug, brief.working_title, brief.subject_strand, brief.primary_curriculum,
                      ",".join(brief.grades), brief.age_band, Jsonb(brief.model_dump())))
    else:
        uid = w["uid"]
    rev = conn.execute("select coalesce(max(revision),0)+1 as r from pub.blueprints where work_uid=%s",
                       (uid,)).fetchone()["r"]
    u = result["usage"]
    bp_id = conn.execute(
        "insert into pub.blueprints (work_uid, revision, document, checks, prompt_versions, model, provider, cost_usd, "
        "input_tokens, output_tokens, elapsed_s, snapshot_id) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "returning id",
        (uid, rev, Jsonb({**bp.model_dump(), "_context": result["ctx"]}), Jsonb(checks), Jsonb(PROMPT_VERSIONS),
         result["model"], provider, u.get("estimated_cost_usd"), u.get("input_tokens"), u.get("output_tokens"),
         round(result["elapsed"], 2), cat.snapshot_id)).fetchone()["id"]
    for ch in bp.chapters:
        cuid = ulid()
        conn.execute("insert into pub.blueprint_chapters (chapter_uid, blueprint_id, sort, title, node_codes) "
                     "values (%s,%s,%s,%s,%s)", (cuid, bp_id, ch.number, ch.title, ch.node_codes))
        for rt in ch.required_topics:
            t = cat.topic_by_slug.get(rt.topic_slug) if rt.topic_slug else None
            conn.execute("insert into pub.required_topics (blueprint_id, chapter_uid, topic_id, proposed_title, role, "
                         "node_codes, rationale) values (%s,%s,%s,%s,%s,%s,%s)",
                         (bp_id, cuid, t["id"] if t else None, None if t else (rt.proposed_title or rt.topic_slug),
                          rt.role, rt.node_codes, rt.rationale))
    seen = set()
    for ch in bp.chapters:
        for rt in ch.required_topics:
            if rt.topic_slug and rt.topic_slug in cat.topic_by_slug:
                continue
            title = rt.proposed_title or rt.topic_slug
            if title in seen:
                continue
            seen.add(title)
            conn.execute("insert into pub.proposed_topics (blueprint_id, canonical_title, node_codes, subject_strand, "
                         "rationale) values (%s,%s,%s,%s,%s)",
                         (bp_id, title, rt.node_codes, brief.subject_strand, rt.rationale))
    conn.execute("insert into pub.cost_events (work_uid, stage, provider, model, input_tokens, output_tokens, usd) "
                 "values (%s,'blueprint',%s,%s,%s,%s,%s)",
                 (uid, provider, result["model"], u.get("input_tokens"), u.get("output_tokens"),
                  u.get("estimated_cost_usd")))
    conn.commit()
    _log.info("%s: blueprint rev %s stored (checks all_ok=%s, $%s)", brief.slug, rev, checks["all_ok"],
              u.get("estimated_cost_usd"))
    return bp_id


def run_blueprints(conn, slug: str, *, provider: str, env_file: Path, allow_peak: bool = False,
                   compare_dir: Path | None = None) -> list:
    """`compare_dir` = the model-independence re-run. Writes the blueprint
    and its checks to JSON and leaves the books' stored blueprints alone."""
    from publishing.guard import require_quiet
    _load_model_env(env_file)
    cat = CatalogueView.load(conn)
    out = []
    for brief in load_briefs():
        if slug not in ("all", brief.slug):
            continue
        require_quiet(env_file, allow_peak=allow_peak)     # checked before EVERY model call
        result = generate(cat, brief, provider)
        if compare_dir is None:
            out.append(store(conn, cat, brief, result, provider))
            continue
        compare_dir.mkdir(parents=True, exist_ok=True)
        checks = check_blueprint(result["blueprint"], cat, brief, result["ctx"]["in_scope"])
        path = compare_dir / f"{brief.slug}.{provider}.json"
        path.write_text(json.dumps({"provider": provider, "model": result["model"], "usage": result["usage"],
                                    "elapsed_s": round(result["elapsed"], 2), "checks": checks,
                                    "blueprint": result["blueprint"].model_dump()}, indent=1, ensure_ascii=False),
                        encoding="utf-8")
        conn.execute("insert into pub.cost_events (stage, provider, model, input_tokens, output_tokens, usd) "
                     "values ('blueprint_compare',%s,%s,%s,%s,%s)",
                     (provider, result["model"], result["usage"].get("input_tokens"),
                      result["usage"].get("output_tokens"), result["usage"].get("estimated_cost_usd")))
        conn.commit()
        out.append(path)
    return out
