"""``figure_render`` — an article's DRAFT figures become labelled visual
assets: a visual-library hit when one exists (zero cost), else ONE generation
through the scene engine's §20 ladder, published into the library for every
later lesson. Phase 2b of the catalogue plan (2026-09-06).

Job shape: ``{id, type: 'figure_render', params: {article_id, force?},
generation_id: None, book_id: None}``. It is an OBSERVER job
(``worker.client.OBSERVER_JOB_TYPES``): it owns no generation, so nothing here
writes ``generations``. It finishes its OWN job row, done or error, and never
raises; run.py dispatches it in the catalogue's last lane.

WHY a separate job from the article. Authoring is one text call and is never
short of capacity; a diagram is an image call against a pool that is a TOTAL
image outage for real users while it is busy (~1/min per pool, see the
never-starve rule). Splitting them lets the article reach its reviewer at once
and lets the figures wait for a quiet queue — and be re-enqueued when this job
pauses.

Per figure (``render_figure``), in order:
  1. asset key = ``canonical_key(figure_key)`` (else the spec's subject, else
     the caption — the same rule article.py filed the row under); prompt from
     the spec (``figure_prompt``): subject, the parts to show, the style, the
     notes, a no-text instruction, and the "Name the layer groups exactly:
     a, b, c." tail the visual catalogue puts on every prompt so the asset
     arrives anchorable (the tail addresses the vision annotator / the SVG
     model's group ids, never the image model — raster_assets strips it
     before generating);
  2. LIBRARY FIRST: ``shared.visual_library.find`` (through the backend), SVG
     then PNG. ``find`` applies ``VISUAL_LIBRARY_MIN_SCORE`` and the key
     guard; here the hit must ALSO carry every part the spec names
     (``row_has_parts``: exact storage, tolerant matching) and a library id.
     A hit costs nothing: no image call, no vision call;
  3. else GENERATE — but a generation call may START only when
       * no BUILDER job is live (``builder_queued``: any job with status
         queued OR processing whose type is not an observer type —
         presentations, decks, documents, index_book, exams; a processing
         builder on a sibling thread of a ``WORKER_CONCURRENCY > 1`` worker
         is real contention for the same image pool): the job stops with
         ``paused: builder jobs queued`` and finishes DONE with the remaining
         figures still draft, so re-enqueueing it picks up where it left;
       * this job's image budget holds: ``reset_image_budget(job_id)`` at the
         start, at most ``IMAGE_CALLS_PER_LESSON`` generations per job and
         never once the engine's own ``image_budget_exhausted`` says so —
         stop with ``paused: image budget spent``.
     The ladder is the engine's: ``get_svg_asset`` when ``SCENE_SVG_ASSETS=1``
     (text generation, no image quota, ``<g id>`` groups ARE the parts), else
     ``get_raster_asset(allow_generate=True)``. Both module functions are the
     visual-library wrapper's (shared/visual_library_integration), so a
     generated asset is normally published on the way out; the job then finds
     the row by CONTENT HASH of the cached file, and publishes it itself
     (``publish_generated``) when the wrapper did not.
     THE WRAPPER HYDRATES BY SCORE ALONE. Its ``_decide``/``_decide_svg`` copy
     the library's best match into the cache before the generator runs, never
     asking about parts — so the asset step 2 rejected for missing parts is
     exactly what step 3 gets back (same bytes, same row by content hash), and
     the cache ``meta.json`` says ``provenance: visual_library``. The job reads
     that provenance (``served_by_library``) and, when the served asset still
     lacks a part the spec names, REFUSES (``FigureRefused``): the figure
     stays draft with ``render_error`` naming the asset and the missing parts,
     so the reviewer approves a fuller asset or adds the parts, instead of a
     figure marked rendered with null group ids nobody notices. A hydrated
     asset that does carry every part is accepted and counted as reused (no
     image call was made). Reaching past the wrapper to the unwrapped ladder
     is deliberately NOT done: the wrapper is the reuse policy for the whole
     engine, and this job is not the place to fork it;
  4. write ``article_figures``: ``visual_asset_id``, ``labels`` reconciled
     against the asset's group ids (``reconcile_labels``: a part found keeps
     its group id, a part not found keeps ``group_id: null`` so the reviewer
     sees the gap), ``status 'rendered'``, ``render_error`` cleared — or, on
     any failure, ``render_error`` set and the row left ``draft``.

The library context (``set_context``) is the topic's: curriculum family from
the depth node's curriculum code ("cambridge", "cbse"), the topic's subject,
the depth grade, the topic title — so a published figure is filed where the
library's curriculum-aware scoring finds it again.

Idempotent: only ``draft`` figures are touched (all of them with
``params.force``), so a re-run after a pause or a failure renders what is
left and never redraws a rendered figure.

Finish: error when any figure failed ("n of m figures failed; re-run to
retry"), else done — a pause is done with the note in ``stage.paused``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from catalogue.harvest import clean_heading
from catalogue.key import canonical_key
from shared.visual_library import row_group_ids, row_has_parts
from worker import client as db

log = logging.getLogger("worker.figures")

JOB_TYPE = "figure_render"
STATUS_DRAFT = "draft"
STATUS_RENDERED = "rendered"
DEFAULT_STYLE = "whiteboard diagram"
# The tail every visual-catalogue prompt carries (visual_library.strip_layer_tail
# and raster_assets.part_names_from_prompt both key on this exact phrase).
LAYER_TAIL = "Name the layer groups exactly: "
NO_TEXT_RULE = "No text, letters, numbers or labels anywhere in the drawing."
PAUSED_BUILDERS = "paused: builder jobs queued"
PAUSED_BUDGET = "paused: image budget spent"
# A builder in either of these states is real contention for the image pool.
BUILDER_LIVE_STATUSES = ("queued", "processing")
PROVENANCE_LIBRARY = "visual_library"
# raster_assets reads the same variable (default 24) for the engine's own
# per-generation budget; this job caps its generation COUNT at the same number
# so the two never disagree about how much one job may spend.
IMAGE_CALLS_PER_LESSON_DEFAULT = 24
FORMATS_LIBRARY_FIRST = ("svg", "png")
_LAYER_TAIL_RE = re.compile(r"\s*name the layer groups exactly:[^.\n]*\.?", re.I)


def max_generations() -> int:
    try:
        v = int(str(os.getenv("IMAGE_CALLS_PER_LESSON", "") or "").strip() or IMAGE_CALLS_PER_LESSON_DEFAULT)
        return v if v > 0 else IMAGE_CALLS_PER_LESSON_DEFAULT
    except (TypeError, ValueError):
        return IMAGE_CALLS_PER_LESSON_DEFAULT


# ── the pure parts ─────────────────────────────────────────────────────


def asset_key_for(figure: dict) -> str:
    """The library key of a figure row: its ``figure_key`` (already a
    catalogue key when article.py wrote it), else the spec's subject, else
    the caption. "" when nothing carries key material."""
    spec = figure.get("spec") if isinstance(figure.get("spec"), dict) else {}
    for cand in (figure.get("figure_key"), spec.get("subject"), figure.get("caption")):
        k = canonical_key(cand) if isinstance(cand, str) else ""
        if k:
            return k
    return ""


def spec_parts(figure: dict) -> list[str]:
    spec = figure.get("spec") if isinstance(figure.get("spec"), dict) else {}
    out: list[str] = []
    for p in (spec.get("parts") if isinstance(spec.get("parts"), list) else []):
        t = clean_heading(p) if isinstance(p, str) else ""
        if t and t.lower() not in {o.lower() for o in out}:
            out.append(t)
    return out


def figure_prompt(figure: dict) -> str:
    """The asset prompt for a figure spec. Pure.

    ``<style> of <subject>, showing <parts>. <notes> <no-text rule> Name the
    layer groups exactly: <parts>.`` — the tail is the visual catalogue's
    contract: the parts named there become the asset's group ids (SVG) or
    its annotated regions (PNG), which is what makes the figure labellable
    without a second call."""
    spec = figure.get("spec") if isinstance(figure.get("spec"), dict) else {}
    subject = clean_heading(spec.get("subject")) or clean_heading(figure.get("caption")) or asset_key_for(figure).replace("_", " ")
    style = clean_heading(spec.get("style")) or DEFAULT_STYLE
    parts = spec_parts(figure)
    notes = _LAYER_TAIL_RE.sub("", clean_heading(spec.get("notes"))).strip()
    text = f"A {style} of {subject}"
    if parts:
        text += ", showing " + ", ".join(parts)
    text = text.rstrip(".") + "."
    if notes:
        text += " " + notes.rstrip(".") + "."
    text += " " + NO_TEXT_RULE
    if parts:
        text += " " + LAYER_TAIL + ", ".join(parts) + "."
    return text


def _same_part(part: str, group_id: str) -> bool:
    ck = canonical_key(part)
    return bool(ck) and (ck == canonical_key(group_id) or ck == str(group_id).strip().lower())


def reconcile_labels(parts: list[str], group_ids: list[str]) -> list[dict]:
    """``[{group_id, label}]`` for the spec's parts against the ASSET's group
    ids: the catalogue key first (case, separators, plurals), then the
    renderer's own tolerant matcher (substring, Latin plurals) so the label
    finds the same layer the renderer would draw; a part the asset does not
    carry keeps ``group_id: None``."""
    available = [str(g) for g in (group_ids or []) if str(g).strip()]
    out: list[dict] = []
    for part in parts:
        gid = next((g for g in available if _same_part(part, g)), None)
        if gid is None and available:
            gid = _tolerant_match(part, available)
        out.append({"group_id": gid, "label": part})
    return out


def _tolerant_match(part: str, available: list[str]) -> Optional[str]:
    try:
        from spike.scene_engine.vector_assets import match_layer_ids
        hits = match_layer_ids(available, [part])
        return hits[0] if hits else None
    except Exception as exc:  # noqa: BLE001 — a label is a hint, never a failure
        log.debug("figures: tolerant matcher unavailable (%s)", exc)
        return None


def content_hash(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def served_by_library(rendered: "Rendered", rejected_hit: Optional[dict], row: Optional[dict]) -> bool:
    """Whether the file the ladder handed back came from the library rather
    than a generation: the cache ``meta.json`` says so (``provenance:
    visual_library`` — written by hydrate() and _hydrate_local_library(), and
    rewritten to "generated" by the renderers when they redraw in place), or
    the row the bytes hash to IS the hit this job already rejected. Pure."""
    if str((rendered.meta or {}).get("provenance") or "") == PROVENANCE_LIBRARY:
        return True
    hit_id = str((rejected_hit or {}).get("id") or "")
    return bool(hit_id) and str((row or {}).get("id") or "") == hit_id


def missing_parts(group_ids: list[str], parts: list[str]) -> list[str]:
    """The spec parts an asset with ``group_ids`` does not carry, by the SAME
    rule ``row_has_parts`` answers with (exact storage, tolerant matching), so
    the refusal names exactly the parts the acceptance test failed on."""
    if row_has_parts({"group_ids": list(group_ids or [])}, parts):
        return []
    available = [str(g) for g in (group_ids or []) if str(g).strip()]
    if not available:
        return list(parts)
    from spike.scene_engine.vector_assets import match_layer_ids
    return [p for p in parts if not match_layer_ids(available, [p])]


def curriculum_family(code: object) -> str:
    """The library's curriculum vocabulary ("cambridge", "cbse") from a
    curriculum code ("cambridge_ls_science_0893"): its first token."""
    toks = [t for t in re.split(r"[^a-z0-9]+", str(code or "").lower()) if t]
    return toks[0] if toks else "generic"


# ── the backend: everything that costs money or touches the engine ─────


@dataclass
class Rendered:
    """What the ladder produced: the cached file, its format and the parts it
    is known to carry (SVG layer ids; PNG regions with a box)."""

    path: Path
    fmt: str
    group_ids: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)


@dataclass
class FigureBackend:
    """The seams a test replaces. Production uses ``default_backend()``."""

    set_context: Callable[..., None]
    find: Callable[[str, str, dict], Optional[dict]]
    generate: Callable[[str, str], Optional[Rendered]]
    publish: Callable[[str, str, Rendered, dict], bool]
    budget_exhausted: Callable[[], bool]
    reset_budget: Callable[[str], None]


def default_backend() -> FigureBackend:
    """The real engine, imported lazily: importing ``spike.scene_engine``
    installs the visual-library wrapper and indexes the local asset cache,
    which a test of this module's logic must never trigger."""
    from shared import visual_library as lib
    from shared import visual_library_integration as integration
    from spike.scene_engine import raster_assets as ra
    from spike.scene_engine import svg_assets as sa
    from shared.asset_keys import canonical_key as asset_canonical

    def find(key: str, prompt: str, context: dict) -> Optional[dict]:
        for fmt in FORMATS_LIBRARY_FIRST:
            hit = lib.find(key, prompt, context, asset_format=fmt)
            if hit:
                return hit
        return None

    def generate(key: str, prompt: str) -> Optional[Rendered]:
        if os.getenv("SCENE_SVG_ASSETS", "").strip() == "1":
            asset = sa.get_svg_asset(key, prompt, None, True)
            if asset is not None:
                path = sa.svg_cache_dir(None, key) / "asset.svg"
                return Rendered(path, "svg", list(asset.layer_ids()), _meta(path.parent))
        asset = ra.get_raster_asset(key, prompt, None, True)
        if asset is not None and asset.trace:
            path = ra.CACHE_DIR / asset_canonical(key) / "asset.png"
            groups = [n for n, boxes in (asset.regions or {}).items() if boxes]
            return Rendered(path, "png", groups, _meta(path.parent))
        return None

    def publish(key: str, prompt: str, rendered: Rendered, context: dict) -> bool:
        return bool(lib.publish_generated(key, prompt, rendered.path, rendered.meta, context,
                                          asset_format=rendered.fmt))

    return FigureBackend(set_context=integration.set_context, find=find, generate=generate, publish=publish,
                         budget_exhausted=ra.image_budget_exhausted, reset_budget=ra.reset_image_budget)


def _meta(asset_dir: Path) -> dict:
    try:
        return json.loads((asset_dir / "meta.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


# ── database edges ─────────────────────────────────────────────────────


def _rows(res) -> list[dict]:
    return list(getattr(res, "data", None) or [])


def load_article(sb, article_id: str) -> Optional[dict]:
    rows = _rows(sb.table("topic_articles").select("id,topic_id,title,language,depth_node_id")
                 .eq("id", article_id).limit(1).execute())
    return rows[0] if rows else None


def load_figures(sb, article_id: str, force: bool) -> list[dict]:
    q = sb.table("article_figures").select("*").eq("article_id", article_id)
    if not force:
        q = q.eq("status", STATUS_DRAFT)
    rows = _rows(q.order("sort").execute())
    return sorted(rows, key=lambda r: (int(r.get("sort") or 0), str(r.get("figure_key") or "")))


def library_context(sb, article: dict) -> dict:
    """curriculum family / subject / grade / topic for the library, from the
    topic and its depth node. Best-effort: a missing row means the generic
    context, never a failed job."""
    ctx = {"curriculum": "generic", "subject": "general", "grade": "k12", "topic": ""}
    try:
        topic_rows = _rows(sb.table("topics").select("id,title,subject,depth_node_id")
                           .eq("id", article.get("topic_id")).limit(1).execute())
        topic = topic_rows[0] if topic_rows else {}
        ctx["topic"] = clean_heading(topic.get("title")) or clean_heading(article.get("title"))
        ctx["subject"] = clean_heading(topic.get("subject")).lower() or "general"
        node_id = article.get("depth_node_id") or topic.get("depth_node_id")
        if node_id:
            nodes = _rows(sb.table("curriculum_nodes").select("id,grade,curriculum_id").eq("id", node_id).limit(1).execute())
            if nodes:
                ctx["grade"] = clean_heading(nodes[0].get("grade")).lower() or "k12"
                curs = _rows(sb.table("curricula").select("id,code").eq("id", nodes[0].get("curriculum_id")).limit(1).execute())
                if curs:
                    ctx["curriculum"] = curriculum_family(curs[0].get("code"))
    except Exception as exc:  # noqa: BLE001
        log.warning("figures: library context incomplete (%s); using what was read", exc)
    return ctx


def builder_queued(sb) -> bool:
    """Whether any job a real user is waiting on is LIVE — queued or
    processing — of a type that is not an observer (presentations, decks,
    documents, index_book, exams). THE never-starve gate: a generation call
    may not start while one is. Processing counts because a worker with
    ``WORKER_CONCURRENCY > 1`` runs a builder on a sibling thread while this
    job runs, and that builder's image calls share the pool."""
    res = (sb.table("jobs").select("id,type,status").in_("status", list(BUILDER_LIVE_STATUSES))
           .not_.in_("type", sorted(db.OBSERVER_JOB_TYPES)).limit(1).execute())
    return bool(_rows(res))


def lookup_asset(sb, rendered: Rendered) -> Optional[dict]:
    """The library row for a rendered file, by CONTENT HASH — publish's own
    idempotency key, so the row found is the one for exactly these bytes."""
    try:
        digest = content_hash(rendered.path)
    except OSError as exc:
        raise RuntimeError(f"rendered asset unreadable: {exc}") from exc
    rows = _rows(sb.table("visual_assets").select("id,asset_key,group_ids,vision,asset_format,status")
                 .eq("content_hash", digest).limit(1).execute())
    return rows[0] if rows else None


def mark_rendered(sb, figure_id: str, asset_id: str, labels: list[dict]) -> None:
    (sb.table("article_figures")
     .update({"visual_asset_id": asset_id, "labels": labels, "status": STATUS_RENDERED, "render_error": None})
     .eq("id", figure_id).execute())


def mark_failed(sb, figure_id: str, error: str) -> None:
    (sb.table("article_figures").update({"status": STATUS_DRAFT, "render_error": error[:1000]})
     .eq("id", figure_id).execute())


# ── the job ────────────────────────────────────────────────────────────


class _Pause(Exception):
    """Stop the job here; the remaining figures stay draft for a re-run."""

    def __init__(self, note: str):
        super().__init__(note)
        self.note = note


class FigureRefused(RuntimeError):
    """This module declined to mark a figure rendered; the message is written
    for the reviewer and is stored in ``render_error`` as it stands (no
    exception-type prefix)."""


def render_figure(sb, backend: FigureBackend, figure: dict, context: dict, gate: Callable[[], None]) -> str:
    """One figure: library first, else the ladder (after ``gate`` allows a
    generation call). Returns 'reused' or 'generated'; raises on failure."""
    key = asset_key_for(figure)
    if not key:
        raise RuntimeError("figure has no key material (figure_key, subject or caption)")
    prompt = figure_prompt(figure)
    parts = spec_parts(figure)

    hit = backend.find(key, prompt, dict(context))
    if hit and hit.get("id") and row_has_parts(hit, parts):
        mark_rendered(sb, figure["id"], str(hit["id"]), reconcile_labels(parts, row_group_ids(hit)))
        log.info("figure %s: library hit %s (score %s)", key, hit.get("asset_key"), hit.get("match_score"))
        return "reused"

    gate()  # the never-starve rule and the budget, checked as the call would start
    rendered = backend.generate(key, prompt)
    if rendered is None:
        raise RuntimeError("the generation ladder produced no asset")
    row = lookup_asset(sb, rendered)
    if row is None:
        backend.publish(key, prompt, rendered, dict(context))
        row = lookup_asset(sb, rendered)
    if row is None:
        raise RuntimeError("asset rendered but not found in the visual library after publish")
    groups = row_group_ids(row) or list(rendered.group_ids)
    reused = served_by_library(rendered, hit, row)
    if reused:
        lacking = missing_parts(groups, parts)
        if lacking:
            raise FigureRefused(
                f"library asset {row.get('asset_key') or (hit or {}).get('asset_key') or key} lacks parts "
                f"{', '.join(lacking)}; hydrated instead of generated — approve a fuller asset or add the parts")
    mark_rendered(sb, figure["id"], str(row["id"]), reconcile_labels(parts, groups))
    log.info("figure %s: %s (%s, %d groups)", key, "library asset hydrated by the wrapper" if reused else "generated",
             rendered.fmt, len(groups))
    return "reused" if reused else "generated"


def render_figures(sb, job_id: str, params: dict, backend: Optional[FigureBackend] = None) -> dict:
    """The job proper; returns the summary also written to ``jobs.stage``."""
    article_id = params.get("article_id")
    if not isinstance(article_id, str) or not article_id:
        raise RuntimeError("figure_render job without params.article_id")
    force = bool(params.get("force"))
    article = load_article(sb, article_id)
    if not article:
        raise RuntimeError(f"article {article_id} not found")
    figures = load_figures(sb, article_id, force)
    stage = {"phase": "figures", "step": "render", "article_id": article_id, "total": len(figures),
             "done": 0, "reused": 0, "generated": 0, "failed": 0, "paused": None}
    db.set_stage(sb, job_id, dict(stage))
    db.set_progress(sb, job_id, 5)
    if not figures:
        return {**stage, "step": "done"}

    if backend is None:
        backend = default_backend()
    backend.reset_budget(job_id)
    context = library_context(sb, article)
    backend.set_context(**context)
    cap = max_generations()
    errors: list[str] = []

    def gate() -> None:
        if builder_queued(sb):
            raise _Pause(PAUSED_BUILDERS)
        if stage["generated"] >= cap or backend.budget_exhausted():
            raise _Pause(PAUSED_BUDGET)

    for fig in figures:
        try:
            outcome = render_figure(sb, backend, fig, context, gate)
            stage[outcome] += 1
        except _Pause as pause:
            stage["paused"] = pause.note
            log.warning("figure job %s %s; %d figure(s) left draft for a re-run",
                        job_id, pause.note, len(figures) - stage["done"])
            break
        except Exception as exc:  # noqa: BLE001 — the next figure still runs
            detail = str(exc) if isinstance(exc, FigureRefused) else f"{type(exc).__name__}: {exc}"
            msg = f"{fig.get('figure_key') or '?'}: {detail}"[:300]
            errors.append(msg)
            stage["failed"] += 1
            try:
                mark_failed(sb, fig["id"], detail)
            except Exception as exc2:  # noqa: BLE001
                log.error("figure %s: could not record the failure: %s", fig.get("id"), exc2)
            log.warning("figure failed — %s", msg)
        stage["done"] += 1
        db.set_stage(sb, job_id, dict(stage))
        db.set_progress(sb, job_id, 5 + int(90 * stage["done"] / max(1, len(figures))))

    summary = {**stage, "step": "paused" if stage["paused"] else "done"}
    if errors:
        summary["errors"] = errors[:5]
    return summary


def run_figure_render_job(sb, job: dict, backend: Optional[FigureBackend] = None) -> Optional[dict]:
    """Entry point for run.py. Self-contained: finishes the job row itself
    (done with the summary in ``stage``; error with the message) and never
    raises. ``backend`` is for tests; production uses the real engine.
    Returns the summary, or None when nothing could run."""
    job_id = job["id"]
    try:
        params = job.get("params") if isinstance(job.get("params"), dict) else {}
        summary = render_figures(sb, job_id, params, backend=backend)
        db.set_stage(sb, job_id, summary)
        if summary.get("failed"):
            first = (summary.get("errors") or ["?"])[0]
            db.finish_job(sb, job_id, None, error=(
                f"{summary['failed']} of {summary['total']} figures failed; re-run to retry them. "
                f"First: {first}")[:4000])
            log.error("figures %s: %s", summary.get("article_id"), summary)
        else:
            db.finish_job(sb, job_id)  # no generation: an observer job owns none
            log.info("figures %s: %s", summary.get("article_id"), summary)
        return summary
    except Exception as exc:  # noqa: BLE001
        log.error("figure job %s failed: %s", job_id, exc)
        try:
            db.finish_job(sb, job_id, None, error=f"{type(exc).__name__}: {exc}"[:4000])
        except Exception as exc2:  # noqa: BLE001
            log.error("figure job %s: could not record the failure: %s", job_id, exc2)
        return None


__all__ = [
    "JOB_TYPE", "STATUS_DRAFT", "STATUS_RENDERED", "LAYER_TAIL", "NO_TEXT_RULE", "PAUSED_BUILDERS",
    "PAUSED_BUDGET", "BUILDER_LIVE_STATUSES", "PROVENANCE_LIBRARY", "FORMATS_LIBRARY_FIRST", "max_generations",
    "asset_key_for", "spec_parts", "figure_prompt", "reconcile_labels", "content_hash", "served_by_library",
    "missing_parts", "curriculum_family", "Rendered", "FigureBackend", "FigureRefused",
    "default_backend", "load_article", "load_figures", "library_context", "builder_queued", "lookup_asset",
    "mark_rendered", "mark_failed", "render_figure", "render_figures", "run_figure_render_job",
]
