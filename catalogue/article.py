"""``topic_article`` — a topic becomes a DRAFT knowledge article: ordered
markdown sections with figure specs, a glossary, misconceptions, worked
examples and claims, written at the depth of the deepest mapped curriculum.
One model call, text only, never an image; a staff reviewer approves it in the
portal (plan §1.3, Gate 1). Phase 2b of the catalogue plan (2026-09-06).

Job shape: ``{id, type: 'topic_article', params: {topic_id, language='en',
hints?, source_article_id?}, generation_id: None, book_id: None}``. It is an
OBSERVER job (``worker.client.OBSERVER_JOB_TYPES``): it owns no generation, so
nothing here writes ``generations``. It finishes its OWN job row, done or
error, and never raises; run.py only dispatches, in the catalogue's last lane.

WHY (plan §1.2). The article is the single source of truth: the whole kit —
video, deck, lesson plan, activity, case study, question bank — is generated
from it and from nothing else, so one correction here propagates everywhere.
The depth rule (§0 decision 2): a topic is taught at the DEPTH of the deepest
curriculum that maps to it, and the article names which one set it
(``depth_node_id`` + ``depth_rationale``), so a reviewer can see why a Stage 7
learner gets the Class 9 treatment.

What the job reads:
  * the topic, its ``topic_curriculum_map`` rows and their ``curriculum_nodes``
    (grouped by curriculum + grade in the prompt, each node's objective
    STATEMENT verbatim — description, else title — as the coverage target);
  * the depth node: ``topics.depth_node_id`` when set, else the mapped node
    with the deepest grade (numeric grades compare as numbers; a tie keeps the
    first mapping) — chosen here and RECORDED on the topic, once;
  * prerequisite topic titles (``topics.prerequisites``);
  * the previous version for continuity (its headings and glossary terms), or
    — when ``params.source_article_id`` names a version — that version's full
    text, so a regeneration REVISES it under the reviewer's ``hints`` rather
    than starting over. The source must belong to the SAME topic and language
    as the job (else the job errors: revising another topic's text into this
    one would be a silent content mix-up). ``hints`` keep their line breaks
    (stripped, capped at ``HINTS_MAX_CHARS``); ``language`` is read stripped
    and lower-cased, default 'en' — the same key the 0114 one-live-job index
    coalesces to.

What it writes (``write_article`` + ``write_figures``):
    topic_articles  {topic_id, version = max existing + 1 for (topic, language),
                     language, source_article_id, title, objectives, sections
                     [{id, heading, body_md, figure_keys, covers}], glossary,
                     misconceptions, worked_examples, claims, depth_node_id,
                     depth_rationale, word_count, status 'draft',
                     author 'model', notes = hints}
    A version another writer lands first (Postgres 23505 on the unique
    (topic, language, version)) is retried at the next number — versions are
    reloaded, up to ``VERSION_RETRIES`` more inserts, never another model call.
    article_figures {article_id, figure_key, caption, spec {subject, parts,
                     style, notes}, labels [{group_id: canonical_key(part),
                     label: part}], sort, status 'draft'}
The figures are RENDERED by the separate ``figure_render`` job
(catalogue/figures.py): authoring must never wait on image capacity.

The reply is VALIDATED in code (``validate_article``), never trusted:
  * the reply must be an object; ``objectives``, ``sections``, ``glossary``,
    ``misconceptions``, ``claims`` and ``figures`` must be lists (a missing
    one fails the job — the article's parts are the kit's inputs);
    ``worked_examples`` may be absent (history has none);
  * a section needs a non-empty ``body_md``; one without is dropped, and
    fewer than ``MIN_SECTIONS`` (3) surviving sections fails the job, as do
    more than ``MAX_SECTIONS`` (10) — a 200-section reply is not an article;
  * ``body_md`` and ``solution_md`` are prose in markdown, so HTML tags
    (``<script>``/``<style>`` blocks whole), markdown images ``![alt](url)``,
    bare URLs and the target of a link ``[text](url)`` (the text stays) are
    STRIPPED before anything else is judged, and the stripping is recorded;
  * section ids are re-issued ``s1..sN`` when missing or duplicated, and the
    model's ids are mapped so claims and figures still resolve;
  * a figure needs key material (``figure_key``, else the spec's subject, else
    the caption); duplicates by key merge into the first; at most
    ``MAX_FIGURES`` (8) survive, in the model's order; a spec naming more than
    ``MAX_PARTS_PER_FIGURE`` (12) parts keeps the first 12 and the count
    dropped is recorded; a reply declaring no figure at all, and a figure no
    section references, are recorded (the figure is kept for the reviewer);
  * ``figure_keys`` on a section are canonicalised, deduplicated and pruned
    to declared figures; a ``claim.section_id`` naming no section is set to
    None (the fact is kept, the pointer is not); ``covers`` keeps only codes
    from the coverage target, and the codes no section covers are recorded in
    the summary as ``uncovered`` for the reviewer;
  * ``title`` and ``heading`` are read as strings only (a number or an object
    where a string belongs is treated as absent, never ``str()``-ed);
  * ``word_count`` is computed from the (stripped) section bodies, never
    taken from the reply; fewer than ``WORDS_FLOOR`` (300) words fails the
    job — the prompt asks for 900, and a 200-word reply is a refusal or a
    truncation, not an article.
  Every repair is listed in the summary (``repairs``) so a reviewer can see
  what the model got wrong.

Idempotent per job (``earlier_attempt`` / ``resume_article``). The stage is
written with ``article_id``, ``version`` and the validated ``figure_specs``
IMMEDIATELY after the article insert, BEFORE the figures are written — so a
process kill or a transient error inside ``write_figures`` leaves a record of
what exists. A re-run of the same job row (the reaper requeues it) then:
  * ``stage.step`` 'done' → finishes the row done, writes nothing (as before);
  * ``stage.article_id`` set, step not terminal → RESUMES: writes the figures
    from ``stage.figure_specs`` when the article has none yet (no model call),
    and finishes done with ``resumed: true`` in the summary.
The window between the article insert and that stage write is the one place
a kill still costs a second version; it is a single round trip wide.

Quota: ONE text call per job (≈ 16k output tokens at most). Never an image,
never a vision call.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from catalogue.harvest import clean_heading
from catalogue.key import canonical_key
from shared.llm import client_for
from worker import client as db

log = logging.getLogger("worker.article")

JOB_TYPE = "topic_article"
DEFAULT_LANGUAGE = "en"
STATUS_DRAFT = "draft"
AUTHOR_MODEL = "model"

MIN_SECTIONS = 3
# The prompt asks for 4-8; ten is the most a reviewer can still read as one
# article. Beyond that the model has written an outline or a list, not prose.
MAX_SECTIONS = 10
MAX_FIGURES = 8
# The vision annotator reads at most 12 part names off a prompt tail
# (raster_assets.part_names_from_prompt); a spec naming more would lose them.
MAX_PARTS_PER_FIGURE = 12
WORDS_MIN, WORDS_MAX = 900, 1600
# Below this the reply is a refusal, a truncation or a stub — never an
# article a reviewer should be asked to read. A third of the asked minimum.
WORDS_FLOOR = 300
# Reviewer notes are stored on the row and put in the prompt verbatim; a cap
# keeps a pasted document out of both.
HINTS_MAX_CHARS = 4000
# Extra insert attempts when another writer lands our version number first.
VERSION_RETRIES = 3
STEP_FIGURES = "figures"
TERMINAL_STEPS = frozenset({"done", "already_done"})
# A 1,600-word article with its apparatus is ~6k output tokens; 16k leaves
# room for a verbose model without inviting a 30k essay.
MAX_TOKENS = 16000
_QUERY_CHUNK = 150
_PREVIOUS_BODY_CHARS = 12000

SYSTEM_PROMPT = (
    "You are an experienced science teacher writing the reference explanation of one topic "
    "for a school knowledge base that other teachers will review. You reply with JSON only: "
    "no prose before or after it, no markdown fences."
)

# The contract, in the teacher's voice. Module constant so the portal and the
# tests can read exactly what the model was asked; build_article_prompt puts
# the topic-specific material above it.
ARTICLE_PROMPT = f"""TASK: write the complete explanation of this topic that a strong teacher would give, at the DEPTH of the deepest mapped curriculum named above. The article is the single source of truth for every lesson, deck, worksheet and question later generated on this topic, so it must be complete, correct and self-contained.

RULES
1. Teach at the depth of the DEPTH CURRICULUM named above and say why in "depth_rationale" (one or two sentences naming that curriculum).
2. Length {WORDS_MIN}-{WORDS_MAX} words of body text across 4-8 sections. Each section has a short heading and a "body_md" in markdown: paragraphs, bullet lists, bold key terms, simple tables where they help. No images, no HTML, no links.
3. Address EVERY coverage-target statement in at least one section. For each section list the CODES it addresses in "covers". Use the codes exactly as written; never invent a code.
4. Write the learning objectives of the article in "objectives" (ids "o1", "o2", ...), one sentence each, in the form a teacher would put on the board.
5. Define every key term the body uses in "glossary" as {{term, definition}} - a definition is one or two plain sentences a learner can quote.
6. Give 3-6 common misconceptions in "misconceptions" (ids "m1", ...), each as {{misconception, correction}}: what learners wrongly believe, and the correct idea with its reason.
7. Give 1-3 worked examples in "worked_examples" (ids "w1", ...) where the subject allows - a calculation, an application, a step-by-step explanation - each as {{problem, solution_md}}. Where the subject has none, return an empty list.
8. List EVERY factual claim, formula and definition the body states, once each, in "claims" (ids "c1", ...) as {{text, section_id}} pointing at the section that states it. Questions will be written from these claims, so a claim must be precise and checkable.
9. Plan 2-5 figures in "figures". Each is a labelled whiteboard diagram: {{figure_key, caption, spec: {{subject, parts, style, notes}}}} where "figure_key" is a short snake_case identity ("plant_cell", "digestive_system"), "subject" says what is drawn, "parts" lists the parts the diagram must show so they can be labelled (2-{MAX_PARTS_PER_FIGURE} short names, singular, no articles), "style" is "whiteboard diagram", and "notes" are drawing instructions. Reference a figure from the section that uses it by putting its figure_key in that section's "figure_keys". Never plan a figure no section uses.
10. Never quote or paraphrase a textbook passage; explain in your own words. British English spelling throughout. No first person and no address to the reader ("I", "we", "you"). No exclamation marks, no filler, no closing summary of what was said.
11. The reply is JSON only, exactly this shape:
{{"title": "...", "objectives": [{{"id": "o1", "text": "..."}}], "sections": [{{"id": "s1", "heading": "...", "body_md": "...", "figure_keys": ["plant_cell"], "covers": ["7Bs.01"]}}], "glossary": [{{"term": "...", "definition": "..."}}], "misconceptions": [{{"id": "m1", "misconception": "...", "correction": "..."}}], "worked_examples": [{{"id": "w1", "problem": "...", "solution_md": "..."}}], "claims": [{{"id": "c1", "text": "...", "section_id": "s1"}}], "figures": [{{"figure_key": "plant_cell", "caption": "...", "spec": {{"subject": "...", "parts": ["cell wall", "nucleus"], "style": "whiteboard diagram", "notes": "..."}}}}], "depth_rationale": "..."}}"""

# The closed payload shape, for GeminiClient's constrained decoding
# (ClaudeClient accepts and ignores it — see shared/claude_client.py). The
# OpenAPI 3.0 subset: no additionalProperties, every property named.
_STR = {"type": "string"}
_STR_LIST = {"type": "array", "items": {"type": "string"}}
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": _STR,
        "objectives": {"type": "array", "items": {
            "type": "object", "properties": {"id": _STR, "text": _STR}, "required": ["id", "text"]}},
        "sections": {"type": "array", "items": {
            "type": "object",
            "properties": {"id": _STR, "heading": _STR, "body_md": _STR,
                           "figure_keys": _STR_LIST, "covers": _STR_LIST},
            "required": ["id", "heading", "body_md"]}},
        "glossary": {"type": "array", "items": {
            "type": "object", "properties": {"term": _STR, "definition": _STR},
            "required": ["term", "definition"]}},
        "misconceptions": {"type": "array", "items": {
            "type": "object", "properties": {"id": _STR, "misconception": _STR, "correction": _STR},
            "required": ["misconception", "correction"]}},
        "worked_examples": {"type": "array", "items": {
            "type": "object", "properties": {"id": _STR, "problem": _STR, "solution_md": _STR},
            "required": ["problem", "solution_md"]}},
        "claims": {"type": "array", "items": {
            "type": "object", "properties": {"id": _STR, "text": _STR, "section_id": _STR},
            "required": ["text", "section_id"]}},
        "figures": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "figure_key": _STR, "caption": _STR,
                "spec": {"type": "object",
                         "properties": {"subject": _STR, "parts": _STR_LIST, "style": _STR, "notes": _STR},
                         "required": ["subject", "parts"]}},
            "required": ["figure_key", "caption", "spec"]}},
        "depth_rationale": _STR,
    },
    "required": ["title", "objectives", "sections", "glossary", "misconceptions", "claims",
                 "figures", "depth_rationale"],
}

REQUIRED_ARRAYS = ("objectives", "sections", "glossary", "misconceptions", "claims", "figures")
DEFAULT_STYLE = "whiteboard diagram"
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
# Markup the prompt forbids in a body ("No images, no HTML, no links") and
# the portal must never render from a model reply. Order matters: a block's
# content goes with its block, an image's alt text goes with its image, a
# link keeps its text, a tag goes, a bare URL goes.
_HTML_BLOCK = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.I | re.S)
_HTML_TAG = re.compile(r"</?[A-Za-z][^<>]*>|<!--.*?-->", re.S)
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_BARE_URL = re.compile(r"(?<![\w/])(?:https?://|www\.)[^\s<>()\[\]]+", re.I)
_MARKUP = (("html", _HTML_BLOCK, ""), ("image", _MD_IMAGE, ""), ("link", _MD_LINK, r"\1"),
           ("html", _HTML_TAG, ""), ("url", _BARE_URL, ""))
_BLANKS = re.compile(r"[ \t]{2,}")


class ArticleInvalid(RuntimeError):
    """The reply cannot become an article; the job fails and says why."""


# ── the material the prompt is built from ──────────────────────────────


@dataclass
class Mapping:
    """One ``topic_curriculum_map`` row joined to its node and curriculum."""

    node: dict
    curriculum: dict
    coverage: str = "full"

    @property
    def code(self) -> str:
        return str(self.node.get("code") or "").strip()

    @property
    def grade(self) -> str:
        return clean_heading(self.node.get("grade"))

    @property
    def statement(self) -> str:
        return clean_heading(self.node.get("description")) or clean_heading(self.node.get("title"))


def _rows(res) -> list[dict]:
    return list(getattr(res, "data", None) or [])


def _chunks(seq: list, n: int) -> Iterable[list]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _grade_key(grade: object) -> tuple:
    """Numeric grades compare as numbers ("9" > "10" is wrong); a non-numeric
    grade ("Stage 7", "Upper Secondary") sorts below every number, so it never
    wins the depth unless nothing numeric is mapped."""
    g = clean_heading(grade)
    m = re.search(r"\d+", g)
    if m:
        return (1, int(m.group(0)), g)
    return (0, 0, g)


def pick_depth_node(topic: dict, mappings: list[Mapping]) -> Optional[dict]:
    """The node that sets the depth: ``topics.depth_node_id`` when it names a
    mapped node, else the mapped node with the deepest grade (ties keep the
    first mapping), else None. Pure."""
    wanted = topic.get("depth_node_id")
    if wanted:
        for m in mappings:
            if m.node.get("id") == wanted:
                return m.node
    best: Optional[Mapping] = None
    for m in mappings:
        if best is None or _grade_key(m.grade) > _grade_key(best.grade):
            best = m
    return best.node if best else None


def load_topic(sb, topic_id: str) -> Optional[dict]:
    rows = _rows(sb.table("topics").select("*").eq("id", topic_id).limit(1).execute())
    return rows[0] if rows else None


def load_mappings(sb, topic_id: str) -> list[Mapping]:
    """The topic's curriculum mappings with their nodes and curricula, in
    mapping order. A mapping whose node is gone is skipped."""
    maps = _rows(sb.table("topic_curriculum_map").select("node_id,coverage").eq("topic_id", topic_id).execute())
    node_ids = [m["node_id"] for m in maps if m.get("node_id")]
    nodes: dict[str, dict] = {}
    for chunk in _chunks(list(dict.fromkeys(node_ids)), _QUERY_CHUNK):
        for n in _rows(sb.table("curriculum_nodes").select("*").in_("id", chunk).execute()):
            if n.get("id"):
                nodes[n["id"]] = n
    cur_ids = sorted({n.get("curriculum_id") for n in nodes.values() if n.get("curriculum_id")})
    curricula: dict[str, dict] = {}
    for chunk in _chunks(cur_ids, _QUERY_CHUNK):
        for c in _rows(sb.table("curricula").select("id,code,name").in_("id", chunk).execute()):
            if c.get("id"):
                curricula[c["id"]] = c
    out: list[Mapping] = []
    for m in maps:
        node = nodes.get(m.get("node_id"))
        if node is None:
            continue
        cur = curricula.get(node.get("curriculum_id")) or {"id": node.get("curriculum_id"), "code": "?", "name": "?"}
        out.append(Mapping(node=node, curriculum=cur, coverage=str(m.get("coverage") or "full")))
    return out


def load_node(sb, node_id: str) -> Optional[dict]:
    rows = _rows(sb.table("curriculum_nodes").select("*").eq("id", node_id).limit(1).execute())
    return rows[0] if rows else None


def load_curriculum(sb, curriculum_id: Optional[str]) -> Optional[dict]:
    if not curriculum_id:
        return None
    rows = _rows(sb.table("curricula").select("id,code,name").eq("id", curriculum_id).limit(1).execute())
    return rows[0] if rows else None


def load_prerequisite_titles(sb, topic: dict) -> list[str]:
    ids = [p for p in (topic.get("prerequisites") or []) if isinstance(p, str) and p]
    titles: list[str] = []
    for chunk in _chunks(list(dict.fromkeys(ids)), _QUERY_CHUNK):
        for t in _rows(sb.table("topics").select("id,title").in_("id", chunk).execute()):
            title = clean_heading(t.get("title"))
            if title and title not in titles:
                titles.append(title)
    return titles


def load_versions(sb, topic_id: str, language: str) -> list[dict]:
    rows = _rows(sb.table("topic_articles").select("*").eq("topic_id", topic_id).eq("language", language).execute())
    return sorted(rows, key=lambda r: int(r.get("version") or 0))


def load_article(sb, article_id: str) -> Optional[dict]:
    rows = _rows(sb.table("topic_articles").select("*").eq("id", article_id).limit(1).execute())
    return rows[0] if rows else None


# ── the prompt ─────────────────────────────────────────────────────────


def _curriculum_label(cur: Optional[dict]) -> str:
    if not cur:
        return "?"
    name = clean_heading(cur.get("name")) or clean_heading(cur.get("code")) or "?"
    code = clean_heading(cur.get("code"))
    return f"{name} (code {code})" if code and code != name else name


def coverage_block(mappings: list[Mapping]) -> str:
    """The coverage target: every mapped statement VERBATIM, grouped by
    curriculum then grade, ``CODE: STATEMENT`` per line. Pure."""
    groups: dict[tuple[str, str], list[Mapping]] = {}
    for m in mappings:
        groups.setdefault((_curriculum_label(m.curriculum), m.grade or "?"), []).append(m)
    if not groups:
        return "(no curriculum statements are mapped to this topic yet)"
    lines: list[str] = []
    for (label, grade), members in groups.items():
        lines.append(f"{label} - grade/stage {grade}")
        for m in members:
            partial = " (partial coverage)" if m.coverage == "partial" else ""
            lines.append(f"  {m.code or '?'}: {m.statement or '?'}{partial}")
    return "\n".join(lines)


def _previous_block(previous: Optional[dict], full: bool) -> str:
    if not previous:
        return ""
    sections = [s for s in (previous.get("sections") or []) if isinstance(s, dict)]
    if full:
        body = "\n\n".join(f"## {clean_heading(s.get('heading'))}\n{str(s.get('body_md') or '').strip()}"
                           for s in sections)
        return (f"\nPREVIOUS VERSION (version {previous.get('version')}, to be REVISED - keep what is correct, "
                f"change what the reviewer's notes ask for):\nTitle: {clean_heading(previous.get('title'))}\n"
                f"{body[:_PREVIOUS_BODY_CHARS]}\n")
    heads = "; ".join(clean_heading(s.get("heading")) for s in sections if clean_heading(s.get("heading")))
    terms = "; ".join(clean_heading(g.get("term")) for g in (previous.get("glossary") or [])
                      if isinstance(g, dict) and clean_heading(g.get("term")))
    return (f"\nPREVIOUS VERSION (version {previous.get('version')}, for continuity of terms and structure):\n"
            f"Headings: {heads or '(none)'}\nGlossary terms: {terms or '(none)'}\n")


def build_article_prompt(topic: dict, language: str, mappings: list[Mapping], depth_node: Optional[dict],
                         depth_curriculum: Optional[dict], prerequisites: list[str],
                         previous: Optional[dict] = None, hints: Optional[str] = None,
                         revise: bool = False) -> str:
    """The one text the model sees. Pure. ``revise`` says ``previous`` is the
    version to revise (params.source_article_id) rather than mere context."""
    title = clean_heading(topic.get("title")) or "?"
    subject = clean_heading(topic.get("subject")) or "science"
    summary = clean_heading(topic.get("summary"))
    if depth_node is not None:
        depth = (f"{_curriculum_label(depth_curriculum)} - grade/stage {clean_heading(depth_node.get('grade')) or '?'}"
                 f" ({clean_heading(depth_node.get('code')) or '?'}: "
                 f"{clean_heading(depth_node.get('description')) or clean_heading(depth_node.get('title')) or '?'})")
    else:
        depth = "no curriculum is mapped yet; teach at lower-secondary depth (age 11-14)"
    head = (
        "You are writing a knowledge article for the SketchCast topic catalogue.\n\n"
        f"Topic: {title}\n"
        f"Subject: {subject}\n"
        f"Language of the article: {language}\n"
        + (f"Catalogue summary: {summary}\n" if summary else "")
        + f"Prerequisite topics (assume these are known): {', '.join(prerequisites) or '(none)'}\n"
        f"DEPTH CURRICULUM (teach at this depth): {depth}\n\n"
        "COVERAGE TARGET - the mapped curriculum statements, grouped by curriculum and grade "
        "(CODE: STATEMENT):\n"
        f"{coverage_block(mappings)}\n"
    )
    # Verbatim, line breaks kept: a reviewer's numbered notes are a list, and
    # collapsing them into one line would hand the model a run-on sentence.
    notes = f"\nREVIEWER NOTES (a regeneration; address every note):\n{_s(hints)}\n" if _s(hints) else ""
    return head + _previous_block(previous, full=revise) + notes + "\n" + ARTICLE_PROMPT


def ask_model(client, prompt: str):
    """One call; the reply's payload — a dict when the model behaved, else
    whatever came back (validate_article refuses it with a reason)."""
    reply = client.analyze(prompt, system=SYSTEM_PROMPT, max_tokens=MAX_TOKENS,
                           response_schema=RESPONSE_SCHEMA)
    if isinstance(reply, dict) and "data" in reply:
        return reply.get("data")
    return reply


# ── validation and repair ──────────────────────────────────────────────


@dataclass
class Article:
    title: str
    objectives: list = field(default_factory=list)
    sections: list = field(default_factory=list)
    glossary: list = field(default_factory=list)
    misconceptions: list = field(default_factory=list)
    worked_examples: list = field(default_factory=list)
    claims: list = field(default_factory=list)
    figures: list = field(default_factory=list)
    depth_rationale: str = ""
    word_count: int = 0
    uncovered: list = field(default_factory=list)
    repairs: list = field(default_factory=list)


def _s(value: object) -> str:
    """A string field: the text stripped, or "" for anything that is not a
    non-empty string."""
    return value.strip() if isinstance(value, str) else ""


def _line(value: object) -> str:
    """A one-line string field (title, heading, caption): whitespace collapsed
    and trimmed, or "" for anything that is not a string — a number or an
    object where a string belongs is absent, never ``str()``-ed into text."""
    return clean_heading(value) if isinstance(value, str) else ""


def strip_markup(text: str) -> tuple[str, list[str]]:
    """Body markdown without the markup the prompt forbids. Pure.

    ``<script>``/``<style>`` blocks go whole (their content is not prose),
    other HTML tags go, ``![alt](url)`` goes, ``[text](url)`` keeps its text,
    a bare URL goes. Returns the text and the KINDS removed ('html', 'image',
    'link', 'url'), in the order the passes run, so the repair names what the
    model did without quoting the payload back into the summary."""
    kinds: list[str] = []
    out = text or ""
    for kind, rx, repl in _MARKUP:
        new = rx.sub(repl, out)
        if new != out and kind not in kinds:
            kinds.append(kind)
        out = new
    if kinds:
        out = "\n".join(_BLANKS.sub(" ", ln).rstrip() for ln in out.splitlines()).strip()
    return out, kinds


def word_count(sections: Iterable[dict]) -> int:
    """Words in the section bodies (markdown punctuation is not a word). Pure."""
    return sum(len(_WORD.findall(str(s.get("body_md") or ""))) for s in sections)


def figure_key_of(item: dict) -> str:
    """A figure's identity: ``canonical_key`` of its key, else of its spec's
    subject, else of its caption — "" when none carries key material."""
    spec = item.get("spec") if isinstance(item.get("spec"), dict) else {}
    for cand in (item.get("figure_key"), spec.get("subject"), item.get("caption")):
        k = canonical_key(cand) if isinstance(cand, str) else ""
        if k:
            return k
    return ""


def _parts(raw: object, key: str, repairs: list[str]) -> list[str]:
    out: list[str] = []
    for p in (raw if isinstance(raw, list) else []):
        t = _line(p)
        if t and canonical_key(t) and t.lower() not in {o.lower() for o in out}:
            out.append(t[:60])
    if len(out) > MAX_PARTS_PER_FIGURE:
        repairs.append(f"{len(out) - MAX_PARTS_PER_FIGURE} part(s) dropped over {MAX_PARTS_PER_FIGURE} on figure {key}")
        out = out[:MAX_PARTS_PER_FIGURE]
    return out


def _validate_figures(raw: list, repairs: list[str]) -> list[dict]:
    if not raw:
        repairs.append("0 figures declared")
    figures: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            repairs.append("figure dropped: not an object")
            continue
        key = figure_key_of(item)
        if not key:
            repairs.append("figure dropped: no key material")
            continue
        if key in seen:
            repairs.append(f"duplicate figure merged: {key}")
            continue
        spec = item.get("spec") if isinstance(item.get("spec"), dict) else {}
        caption = _line(item.get("caption")) or _line(spec.get("subject")) or key.replace("_", " ")
        subject = _line(spec.get("subject")) or caption
        seen.add(key)
        figures.append({
            "figure_key": key,
            "caption": caption[:500],
            "spec": {"subject": subject[:300], "parts": _parts(spec.get("parts"), key, repairs),
                     "style": _line(spec.get("style")) or DEFAULT_STYLE,
                     "notes": _line(spec.get("notes"))[:1000]},
        })
    if len(figures) > MAX_FIGURES:
        repairs.append(f"{len(figures) - MAX_FIGURES} figure(s) over the cap of {MAX_FIGURES} dropped")
        figures = figures[:MAX_FIGURES]
    if raw and not figures:
        repairs.append("no usable figure declared")
    return figures


def _validate_sections(raw: list, repairs: list[str]) -> tuple[list[dict], dict[str, str]]:
    """Sections with re-issued ids; returns them and the model-id → final-id
    map (claims and the like are written against the model's ids)."""
    sections: list[dict] = []
    id_map: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, dict):
            repairs.append("section dropped: not an object")
            continue
        body, stripped = strip_markup(_s(item.get("body_md")))
        if not body:
            repairs.append(f"section dropped: empty body ({_line(item.get('heading')) or '?'})")
            continue
        final = f"s{len(sections) + 1}"
        if stripped:
            repairs.append(f"markup stripped from {final}: {', '.join(stripped)}")
        model_id = _s(item.get("id"))
        if model_id and model_id not in id_map:
            id_map[model_id] = final
        elif model_id:
            repairs.append(f"duplicate section id re-issued: {model_id} -> {final}")
        sections.append({
            "id": final,
            "heading": _line(item.get("heading"))[:200] or f"Section {len(sections) + 1}",
            "body_md": body,
            "figure_keys": item.get("figure_keys"),   # resolved once the figures are known
            "covers": item.get("covers"),
        })
    return sections, id_map


def _validate_named(raw: object, fields: tuple[str, ...], prefix: str, what: str,
                    repairs: list[str]) -> list[dict]:
    """Objects carrying every field in ``fields`` as non-empty strings, with
    ids ``<prefix>1..``; anything else is dropped and recorded."""
    out: list[dict] = []
    for item in (raw if isinstance(raw, list) else []):
        if not isinstance(item, dict) or not all(_s(item.get(f)) for f in fields):
            repairs.append(f"{what} dropped: incomplete")
            continue
        row = {f: _s(item.get(f)) for f in fields}
        if prefix:
            row = {"id": f"{prefix}{len(out) + 1}", **row}
        out.append(row)
    return out


def validate_article(raw: object, coverage_codes: list[str], fallback_title: str) -> Article:
    """The rules in the module docstring, applied in order. Pure. Raises
    ``ArticleInvalid`` when the reply cannot become an article."""
    if not isinstance(raw, dict):
        raise ArticleInvalid("model reply is not a JSON object")
    for name in REQUIRED_ARRAYS:
        if not isinstance(raw.get(name), list):
            raise ArticleInvalid(f"model reply has no '{name}' list")
    repairs: list[str] = []

    sections, id_map = _validate_sections(raw["sections"], repairs)
    if len(sections) < MIN_SECTIONS:
        raise ArticleInvalid(f"only {len(sections)} usable section(s); at least {MIN_SECTIONS} needed")
    if len(sections) > MAX_SECTIONS:
        raise ArticleInvalid(f"{len(sections)} sections; at most {MAX_SECTIONS} make one article")
    words = word_count(sections)
    if words < WORDS_FLOOR:
        raise ArticleInvalid(f"only {words} words of body text; at least {WORDS_FLOOR} needed")

    figures = _validate_figures(raw["figures"], repairs)
    declared = {f["figure_key"] for f in figures}
    used: set[str] = set()
    known_codes = {c.lower(): c for c in coverage_codes}
    covered: set[str] = set()
    for s in sections:
        keys: list[str] = []
        for k in (s["figure_keys"] if isinstance(s["figure_keys"], list) else []):
            ck = canonical_key(k) if isinstance(k, str) else ""
            if ck in declared:
                if ck not in keys:      # a repeat of a declared key is a dedupe, not a dangling key
                    keys.append(ck)
            elif ck:
                repairs.append(f"dangling figure key dropped from {s['id']}: {ck}")
        s["figure_keys"] = keys
        used.update(keys)
        covers: list[str] = []
        for c in (s["covers"] if isinstance(s["covers"], list) else []):
            real = known_codes.get(str(c).strip().lower()) if isinstance(c, str) else None
            if real and real not in covers:
                covers.append(real)
        s["covers"] = covers
        covered.update(covers)
    for f in figures:
        if f["figure_key"] not in used:
            repairs.append(f"figure declared but unused: {f['figure_key']}")

    worked: list[dict] = []
    for w in _validate_named(raw.get("worked_examples"), ("problem", "solution_md"), "w", "worked example", repairs):
        solution, stripped = strip_markup(w["solution_md"])
        if not solution:
            repairs.append(f"worked example dropped: only markup ({w['id']})")
            continue
        if stripped:
            repairs.append(f"markup stripped from {w['id']}: {', '.join(stripped)}")
        worked.append({**w, "id": f"w{len(worked) + 1}", "solution_md": solution})

    claims: list[dict] = []
    for item in raw["claims"]:
        if not isinstance(item, dict) or not _s(item.get("text")):
            repairs.append("claim dropped: no text")
            continue
        # Resolved through the model-id map ONLY: after a dropped section the
        # re-issued ids overlap the model's, so a raw "s2" may name a
        # different section from the model's "s2".
        model_sid = _s(item.get("section_id"))
        sid = id_map.get(model_sid)
        if sid is None and model_sid:
            repairs.append(f"claim section reference dropped: {model_sid}")
        claims.append({"id": f"c{len(claims) + 1}", "text": _s(item.get("text")), "section_id": sid})

    return Article(
        title=_line(raw.get("title"))[:300] or fallback_title,
        objectives=_validate_named(raw["objectives"], ("text",), "o", "objective", repairs),
        sections=sections,
        glossary=_validate_named(raw["glossary"], ("term", "definition"), "", "glossary entry", repairs),
        misconceptions=_validate_named(raw["misconceptions"], ("misconception", "correction"), "m",
                                       "misconception", repairs),
        worked_examples=worked,
        claims=claims,
        figures=figures,
        depth_rationale=_s(raw.get("depth_rationale"))[:2000],
        word_count=words,
        uncovered=[c for c in coverage_codes if c not in covered],
        repairs=repairs,
    )


# ── database edges ─────────────────────────────────────────────────────


def next_version(versions: list[dict]) -> int:
    return max([int(v.get("version") or 0) for v in versions] + [0]) + 1


def figure_labels(parts: Iterable[str]) -> list[dict]:
    """``[{group_id, label}]`` for a spec's parts — the group id is the
    catalogue key of the part name, which is what an SVG's ``<g id>`` is
    expected to be (lowercase snake case, plural folded). Pure."""
    return [{"group_id": canonical_key(p) or None, "label": p} for p in parts]


def write_article(sb, topic_id: str, language: str, version: int, article: Article,
                  depth_node_id: Optional[str], hints: Optional[str],
                  source_article_id: Optional[str]) -> str:
    row = {
        "topic_id": topic_id,
        "version": version,
        "language": language,
        "source_article_id": source_article_id,
        "title": article.title,
        "objectives": article.objectives,
        "sections": article.sections,
        "glossary": article.glossary,
        "misconceptions": article.misconceptions,
        "worked_examples": article.worked_examples,
        "claims": article.claims,
        "depth_node_id": depth_node_id,
        "depth_rationale": article.depth_rationale or None,
        "word_count": article.word_count,
        "status": STATUS_DRAFT,
        "author": AUTHOR_MODEL,
        "notes": _s(hints) or None,
    }
    res = sb.table("topic_articles").insert(row).execute()
    rows = _rows(res)
    if not rows or not rows[0].get("id"):
        raise RuntimeError("topic_articles insert returned no id")
    return rows[0]["id"]


def _is_duplicate_key(exc: BaseException) -> bool:
    """Postgres 23505 (unique_violation) as postgrest surfaces it: an
    ``APIError`` whose ``code`` is the SQLSTATE; the text is the fallback."""
    return str(getattr(exc, "code", "") or "") == "23505" or "23505" in str(exc)


def write_article_versioned(sb, topic_id: str, language: str, versions: list[dict], article: Article,
                            depth_node_id: Optional[str], hints: Optional[str],
                            source_article_id: Optional[str]) -> tuple[str, int]:
    """``write_article`` at ``max(versions) + 1`` — and when another writer
    (a second replica, a hand insert) lands that number first, the versions
    are reloaded and the next number tried, up to ``VERSION_RETRIES`` more
    times. The model is never asked again: the article is already in hand.
    Returns ``(article_id, version)``."""
    for attempt in range(1 + VERSION_RETRIES):
        version = next_version(versions)
        try:
            return write_article(sb, topic_id, language, version, article, depth_node_id, hints,
                                 source_article_id), version
        except Exception as exc:  # noqa: BLE001 — only the duplicate is retried
            if not _is_duplicate_key(exc) or attempt == VERSION_RETRIES:
                raise
            log.warning("article: version %s of %s/%s taken by another writer; retrying (%d/%d)",
                        version, topic_id, language, attempt + 1, VERSION_RETRIES)
            versions = load_versions(sb, topic_id, language)
    raise AssertionError("unreachable")  # pragma: no cover


def write_figures(sb, article_id: str, figures: list[dict]) -> int:
    rows = [{
        "article_id": article_id,
        "figure_key": f["figure_key"],
        "caption": f["caption"],
        "spec": f["spec"],
        "labels": figure_labels(f["spec"].get("parts") or []),
        "sort": i,
        "status": STATUS_DRAFT,
    } for i, f in enumerate(figures)]
    if rows:
        sb.table("article_figures").insert(rows).execute()
    return len(rows)


def record_depth_node(sb, topic: dict, node_id: Optional[str]) -> None:
    """Set ``topics.depth_node_id`` once, when the topic has none."""
    if node_id and not topic.get("depth_node_id"):
        sb.table("topics").update({"depth_node_id": node_id}).eq("id", topic["id"]).execute()
        topic["depth_node_id"] = node_id


def earlier_attempt(sb, job: dict) -> Optional[dict]:
    """The stage an earlier attempt of THIS job row left once its article was
    inserted (``stage.article_id`` set), from the row itself or the database
    copy of it; None when no article exists for this job yet."""
    stage = job.get("stage")
    if not (isinstance(stage, dict) and stage.get("article_id")):
        try:
            rows = _rows(sb.table("jobs").select("stage").eq("id", job["id"]).limit(1).execute())
        except Exception as exc:  # noqa: BLE001
            log.warning("article: job row unreadable, assuming no earlier attempt: %s", exc)
            return None
        stage = rows[0].get("stage") if rows else None
    if isinstance(stage, dict) and stage.get("article_id"):
        return {**stage, "article_id": str(stage["article_id"])}
    return None


def existing_article_id(sb, job: dict) -> Optional[str]:
    """The article an earlier attempt of THIS job row wrote, or None."""
    stage = earlier_attempt(sb, job)
    return stage["article_id"] if stage else None


def _finish_summary(stage: dict, n_figures: int) -> dict:
    """The final summary: the pre-figures stage with the figure COUNT in
    place of the specs (article_figures holds them now) and step done."""
    return {**{k: v for k, v in stage.items() if k != "figure_specs"}, "step": "done", "figures": n_figures}


def resume_article(sb, job_id: str, stage: dict) -> dict:
    """Finish what an earlier attempt of this job row started. The article
    exists (``stage.article_id``); a terminal step means the figures do too
    and there is nothing to write. Otherwise the attempt died between the
    article insert and the figure insert (or inside it): the figures are
    written from ``stage.figure_specs`` unless the article already has rows —
    the insert is one statement, so it is all or none. No model call."""
    article_id = str(stage["article_id"])
    if stage.get("step") in TERMINAL_STEPS:
        return {**stage, "step": "already_done"}
    if load_article(sb, article_id) is None:
        raise RuntimeError(f"article {article_id} recorded by an earlier attempt of job {job_id} is gone")
    have = _rows(sb.table("article_figures").select("id").eq("article_id", article_id).execute())
    if have:
        n_figures = len(have)
        log.info("article job %s: %s already has its %d figure(s); finishing", job_id, article_id, n_figures)
    else:
        specs = [f for f in (stage.get("figure_specs") or []) if isinstance(f, dict) and f.get("figure_key")]
        n_figures = write_figures(sb, article_id, specs)
        log.info("article job %s: resumed — %d figure(s) written for %s", job_id, n_figures, article_id)
    db.set_progress(sb, job_id, 95)
    return {**_finish_summary(stage, n_figures), "resumed": True}


# ── the job ────────────────────────────────────────────────────────────


def author_article(sb, job_id: str, params: dict, client=None) -> dict:
    """The authoring proper: load, one call, validate, write. Returns the
    summary also written to ``jobs.stage``. Raises on any failure (the entry
    point turns that into the row's error)."""
    topic_id = params.get("topic_id")
    if not isinstance(topic_id, str) or not topic_id:
        raise RuntimeError("topic_article job without params.topic_id")
    language = _s(params.get("language")).lower() or DEFAULT_LANGUAGE
    hints = _s(params.get("hints"))[:HINTS_MAX_CHARS] or None
    source_id = _s(params.get("source_article_id")) or None

    stage: dict = {"phase": "article", "step": "load", "topic_id": topic_id, "language": language}
    db.set_stage(sb, job_id, dict(stage))
    db.set_progress(sb, job_id, 5)

    topic = load_topic(sb, topic_id)
    if not topic:
        raise RuntimeError(f"topic {topic_id} not found")
    mappings = load_mappings(sb, topic_id)
    depth_node = pick_depth_node(topic, mappings)
    if depth_node is None and topic.get("depth_node_id"):
        depth_node = load_node(sb, topic["depth_node_id"])  # set by hand to an unmapped node
    depth_curriculum = None
    if depth_node is not None:
        depth_curriculum = next((m.curriculum for m in mappings if m.curriculum.get("id") == depth_node.get("curriculum_id")),
                                None) or load_curriculum(sb, depth_node.get("curriculum_id"))
        record_depth_node(sb, topic, depth_node.get("id"))
    prerequisites = load_prerequisite_titles(sb, topic)
    versions = load_versions(sb, topic_id, language)
    source = load_article(sb, source_id) if source_id else None
    if source_id and source is None:
        raise RuntimeError(f"source article {source_id} not found")
    if source is not None:
        src_topic, src_lang = source.get("topic_id"), (_s(source.get("language")).lower() or DEFAULT_LANGUAGE)
        if src_topic != topic_id or src_lang != language:
            raise RuntimeError(
                f"source article {source_id} belongs to topic {src_topic} in {src_lang}, not to this job's "
                f"topic {topic_id} in {language}; a revision must start from a version of the same article")
    previous = source or (versions[-1] if versions else None)
    codes = [m.code for m in mappings if m.code]

    stage.update({"step": "author", "depth_node_id": depth_node.get("id") if depth_node else None,
                  "mapped": len(mappings)})
    db.set_stage(sb, job_id, dict(stage))
    db.set_progress(sb, job_id, 20)
    if client is None:
        client = client_for(language)
    prompt = build_article_prompt(topic, language, mappings, depth_node, depth_curriculum, prerequisites,
                                  previous=previous, hints=hints, revise=source is not None)
    raw = ask_model(client, prompt)
    usage = getattr(client, "session_usage", None)
    if isinstance(usage, dict) and usage.get("calls"):
        db.set_job_usage(sb, job_id, usage)

    stage["step"] = "validate"
    db.set_stage(sb, job_id, dict(stage))
    db.set_progress(sb, job_id, 80)
    article = validate_article(raw, codes, clean_heading(topic.get("title")) or "Untitled")

    stage["step"] = "write"
    db.set_stage(sb, job_id, dict(stage))
    article_id, version = write_article_versioned(sb, topic_id, language, versions, article,
                                                  depth_node.get("id") if depth_node else None, hints, source_id)
    # The record a re-run resumes from, written BEFORE the figures: from here
    # on a kill or a failed figure insert costs a resume, not a second
    # version. figure_specs is the validated list write_figures is about to
    # store; the final summary swaps it for the count.
    stage.update({
        "step": STEP_FIGURES, "article_id": article_id, "version": version,
        "title": article.title, "word_count": article.word_count,
        "sections": len(article.sections), "claims": len(article.claims),
        "uncovered": article.uncovered, "repairs": article.repairs[:20],
        "source_article_id": source_id, "figure_specs": article.figures,
    })
    db.set_stage(sb, job_id, dict(stage))
    n_figures = write_figures(sb, article_id, article.figures)
    db.set_progress(sb, job_id, 95)
    return _finish_summary(stage, n_figures)


def run_article_job(sb, job: dict, client=None) -> Optional[dict]:
    """Entry point for run.py. Self-contained: finishes the job row itself
    (done with the summary in ``stage``; error with the message) and never
    raises. ``client`` is for tests; production builds ``client_for(language)``.
    Returns the summary, or None when nothing could run."""
    job_id = job["id"]
    try:
        params = job.get("params") if isinstance(job.get("params"), dict) else {}
        earlier = earlier_attempt(sb, job)
        if earlier:
            summary = resume_article(sb, job_id, earlier)
            db.set_stage(sb, job_id, summary)
            db.finish_job(sb, job_id)
            log.info("article job %s: earlier attempt produced %s; %s", job_id, earlier["article_id"],
                     "resumed" if summary.get("resumed") else "nothing to do")
            return summary
        summary = author_article(sb, job_id, params, client=client)
        db.set_stage(sb, job_id, summary)
        db.finish_job(sb, job_id)  # no generation: an observer job owns none
        log.info("article %s: %s", summary.get("topic_id"), summary)
        return summary
    except Exception as exc:  # noqa: BLE001
        log.error("article job %s failed: %s", job_id, exc)
        try:
            db.finish_job(sb, job_id, None, error=f"{type(exc).__name__}: {exc}"[:4000])
        except Exception as exc2:  # noqa: BLE001
            log.error("article job %s: could not record the failure: %s", job_id, exc2)
        return None


__all__ = [
    "JOB_TYPE", "DEFAULT_LANGUAGE", "MIN_SECTIONS", "MAX_SECTIONS", "MAX_FIGURES", "MAX_PARTS_PER_FIGURE",
    "MAX_TOKENS", "WORDS_FLOOR", "HINTS_MAX_CHARS", "VERSION_RETRIES", "STEP_FIGURES", "TERMINAL_STEPS",
    "SYSTEM_PROMPT", "ARTICLE_PROMPT", "RESPONSE_SCHEMA", "REQUIRED_ARRAYS", "ArticleInvalid", "Mapping",
    "Article", "pick_depth_node", "load_topic", "load_mappings", "load_prerequisite_titles", "load_versions",
    "load_article", "coverage_block", "build_article_prompt", "ask_model", "strip_markup", "word_count",
    "figure_key_of", "validate_article", "next_version", "figure_labels", "write_article",
    "write_article_versioned", "write_figures", "record_depth_node", "earlier_attempt", "existing_article_id",
    "resume_article", "author_article", "run_article_job",
]
