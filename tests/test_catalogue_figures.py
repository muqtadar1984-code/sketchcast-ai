"""``figure_render``: an article's draft figures become labelled visual
assets — a library hit costs nothing, a miss costs ONE generation, and no
generation starts while a real user's job is queued.

Everything here CALLS things. The engine (library lookup, the §20 ladder,
publish, the image budget) is a fake ``FigureBackend`` that records every
call; the database is the fake Supabase in tests/catalogue_fakes.py. No
network, no model, no image API, no live Supabase — and no import of
``spike.scene_engine`` unless a tolerant label match needs it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from catalogue import figures
from catalogue.figures import (
    LAYER_TAIL, NO_TEXT_RULE, PAUSED_BUDGET, PAUSED_BUILDERS, FigureBackend, Rendered, asset_key_for, content_hash,
    figure_prompt, reconcile_labels, run_figure_render_job,
)
from tests.catalogue_fakes import FakeSB

JOB = "job-fr"
ARTICLE = {"id": "art-1", "topic_id": "t-cell", "version": 1, "language": "en", "title": "Cells", "status": "draft",
           "depth_node_id": "n-cb1"}
TOPIC = {"id": "t-cell", "title": "Cells", "subject": "Biology", "depth_node_id": "n-cb1"}
NODE = {"id": "n-cb1", "curriculum_id": "cur-cbse", "code": "cbse:9:U2:01", "grade": "9"}
CBSE = {"id": "cur-cbse", "code": "cbse_science_086", "name": "CBSE Science"}

PLANT = {"id": "fig-plant", "article_id": "art-1", "figure_key": "plant_cell", "caption": "A plant cell", "sort": 0,
         "status": "draft", "spec": {"subject": "a plant cell in cross-section",
                                     "parts": ["cell wall", "nucleus", "chloroplasts", "sap vacuole"],
                                     "style": "whiteboard diagram", "notes": "Rectangular outline."},
         "labels": [{"group_id": "cell_wall", "label": "cell wall"}]}
ANIMAL = {"id": "fig-animal", "article_id": "art-1", "figure_key": "animal_cell", "caption": "An animal cell", "sort": 1,
          "status": "draft", "spec": {"subject": "an animal cell", "parts": ["cell membrane", "nucleus", "mitochondria"],
                                      "style": "whiteboard diagram", "notes": ""}, "labels": []}

LIB_PLANT = {"id": "va-plant", "asset_key": "plant_cell", "canonical_key": "plant_cell", "status": "approved",
             "asset_format": "svg", "group_ids": ["cell_wall", "nucleus", "chloroplasts", "sap_vacuole", "cytoplasm"],
             "match_score": 0.91}


def _job(**extra):
    return {"id": JOB, "type": "figure_render", "status": "processing", "generation_id": None, "book_id": None,
            "params": {"article_id": "art-1"}, **extra}


def _builder(job_id="job-p", kind="presentation", status="queued"):
    return {"id": job_id, "type": kind, "status": status, "generation_id": "gen-p", "book_id": "book-1"}


def _sb(figs=(PLANT, ANIMAL), jobs=None, assets=(), article=ARTICLE):
    sb = FakeSB()
    sb.tables["topic_articles"] = [dict(article)] if article else []
    sb.tables["topics"] = [dict(TOPIC)]
    sb.tables["curriculum_nodes"] = [dict(NODE)]
    sb.tables["curricula"] = [dict(CBSE)]
    sb.tables["article_figures"] = [dict(f) for f in figs]
    sb.tables["jobs"] = jobs if jobs is not None else [_job()]
    sb.tables["generations"] = [{"id": "gen-p", "status": "queued"}]
    sb.tables["visual_assets"] = [dict(a) for a in assets]
    return sb


# ── the fake engine ─────────────────────────────────────────────────────


class FakeBackend:
    """Scripted: ``hits`` maps an asset key to a library row (or None);
    ``renders`` maps a key to a Rendered, an exception, or None. ``publish``
    inserts the visual_assets row the real publisher would, keyed by the
    file's content hash — unless ``publish_fails``."""

    def __init__(self, tmp_path: Path, sb: FakeSB, hits=None, renders=None, exhausted=False, publish_fails=False):
        self.tmp, self.sb = tmp_path, sb
        self.hits, self.renders = dict(hits or {}), dict(renders or {})
        self.exhausted, self.publish_fails = exhausted, publish_fails
        self.finds, self.generates, self.publishes, self.contexts, self.resets = [], [], [], [], []

    def rendered(self, key, fmt="svg", groups=("cell_wall", "nucleus", "chloroplast_layer"), body=None):
        path = self.tmp / f"{key}.{fmt}"
        path.write_bytes(body or f"<svg>{key}</svg>".encode())
        return Rendered(path=path, fmt=fmt, group_ids=list(groups), meta={"provenance": "generated", "key": key})

    def as_backend(self) -> FigureBackend:
        return FigureBackend(set_context=self._set_context, find=self._find, generate=self._generate,
                             publish=self._publish, budget_exhausted=lambda: self.exhausted,
                             reset_budget=self.resets.append)

    def _set_context(self, **ctx):
        self.contexts.append(ctx)

    def _find(self, key, prompt, context):
        self.finds.append((key, prompt, dict(context)))
        return self.hits.get(key)

    def _generate(self, key, prompt):
        self.generates.append((key, prompt))
        r = self.renders.get(key)
        if isinstance(r, BaseException):
            raise r
        return r

    def _publish(self, key, prompt, rendered, context):
        self.publishes.append((key, rendered.fmt, dict(context)))
        if self.publish_fails:
            return False
        self.sb.tables["visual_assets"].append({
            "id": f"va-{key}", "asset_key": key, "canonical_key": key, "status": "approved",
            "asset_format": rendered.fmt, "group_ids": list(rendered.group_ids),
            "content_hash": content_hash(rendered.path), "description": prompt})
        return True


def _fig(sb, fig_id):
    (row,) = [r for r in sb.tables["article_figures"] if r["id"] == fig_id]
    return row


def _job_row(sb):
    return sb.tables["jobs"][0]


# ── the pure parts ──────────────────────────────────────────────────────


def test_the_figure_prompt_carries_subject_parts_style_notes_the_no_text_rule_and_the_layer_tail():
    text = figure_prompt(PLANT)
    assert text.startswith("A whiteboard diagram of a plant cell in cross-section, showing cell wall, nucleus, chloroplasts, sap vacuole.")
    assert " Rectangular outline. " in text and NO_TEXT_RULE in text
    assert text.endswith(LAYER_TAIL + "cell wall, nucleus, chloroplasts, sap vacuole.")
    assert text.count("Name the layer groups exactly:") == 1
    # A figure with no parts asks for no groups; a spec without a subject reads the caption.
    bare = figure_prompt({"figure_key": "digestive_system", "caption": "The digestive system", "spec": {"parts": []}})
    assert bare == f"A whiteboard diagram of The digestive system. {NO_TEXT_RULE}"
    # A note that repeats the tail does not double it.
    dup = figure_prompt({**PLANT, "spec": {**PLANT["spec"], "notes": "Keep it simple. Name the layer groups exactly: x, y."}})
    assert dup.count("Name the layer groups exactly:") == 1 and "x, y" not in dup


def test_the_asset_key_is_the_catalogue_key_of_the_figure_key_then_subject_then_caption():
    assert asset_key_for(PLANT) == "plant_cell"
    assert asset_key_for({"figure_key": "Plant Cells", "spec": {}}) == "plant_cell"
    assert asset_key_for({"figure_key": "", "spec": {"subject": "the digestive system"}}) == "digestive_system"
    assert asset_key_for({"caption": "An animal cell", "spec": {}}) == "animal_cell"
    assert asset_key_for({"caption": "  ", "spec": {"subject": None}}) == ""


def test_labels_reconcile_exactly_then_by_key_fold_then_tolerantly_and_keep_the_gaps():
    labels = reconcile_labels(["cell wall", "Nucleus", "chloroplast", "sap vacuole", "ribosome"],
                              ["cell_wall", "nucleus", "chloroplasts", "vacuole_sap"])
    assert labels[:3] == [{"group_id": "cell_wall", "label": "cell wall"}, {"group_id": "nucleus", "label": "Nucleus"},
                          {"group_id": "chloroplasts", "label": "chloroplast"}]
    assert labels[3]["label"] == "sap vacuole" and labels[3]["group_id"] in ("vacuole_sap", None)
    assert labels[4] == {"group_id": None, "label": "ribosome"}, "a part the asset lacks keeps a null group"
    assert reconcile_labels(["a"], []) == [{"group_id": None, "label": "a"}]


# ── library first: a hit costs nothing ──────────────────────────────────


def test_a_library_match_with_the_parts_is_reused_without_a_generation(tmp_path):
    sb = _sb(figs=(PLANT,))
    fb = FakeBackend(tmp_path, sb, hits={"plant_cell": LIB_PLANT})
    summary = run_figure_render_job(sb, _job(), backend=fb.as_backend())

    assert fb.generates == [] and fb.publishes == [], "zero cost"
    assert len(fb.finds) == 1 and fb.finds[0][0] == "plant_cell" and fb.finds[0][1] == figure_prompt(PLANT)
    plant = _fig(sb, "fig-plant")
    assert plant["status"] == "rendered" and plant["visual_asset_id"] == "va-plant" and plant["render_error"] is None
    assert plant["labels"] == [{"group_id": "cell_wall", "label": "cell wall"}, {"group_id": "nucleus", "label": "nucleus"},
                               {"group_id": "chloroplasts", "label": "chloroplasts"},
                               {"group_id": "sap_vacuole", "label": "sap vacuole"}]
    assert summary["reused"] == 1 and summary["generated"] == 0 and summary["failed"] == 0 and summary["paused"] is None
    job = _job_row(sb)
    assert job["status"] == "done" and job["stage"] == summary and job["error"] is None
    assert fb.resets == [JOB], "the image budget is this job's own"
    assert fb.contexts == [{"curriculum": "cbse", "subject": "biology", "grade": "9", "topic": "Cells"}]
    assert sb.writes("generations") == []


def test_a_library_match_missing_a_needed_part_is_not_accepted(tmp_path):
    partial = {**LIB_PLANT, "group_ids": ["cell_wall", "nucleus"]}
    sb = _sb(figs=(PLANT,))
    fb = FakeBackend(tmp_path, sb, hits={"plant_cell": partial}, renders={})
    fb.renders["plant_cell"] = fb.rendered("plant_cell")
    run_figure_render_job(sb, _job(), backend=fb.as_backend())
    assert [k for k, _ in fb.generates] == ["plant_cell"], "the hit lacked chloroplasts and the vacuole: generate"
    assert _fig(sb, "fig-plant")["visual_asset_id"] == "va-plant_cell"


def test_a_library_match_without_a_row_id_is_not_accepted(tmp_path):
    sb = _sb(figs=(PLANT,))
    fb = FakeBackend(tmp_path, sb, hits={"plant_cell": {k: v for k, v in LIB_PLANT.items() if k != "id"}})
    fb.renders["plant_cell"] = fb.rendered("plant_cell")
    run_figure_render_job(sb, _job(), backend=fb.as_backend())
    assert len(fb.generates) == 1, "a local index row is not a durable library asset"


# ── a miss: one generation, published, rendered ─────────────────────────


def test_a_miss_generates_once_publishes_and_renders_with_reconciled_labels(tmp_path):
    sb = _sb(figs=(PLANT,))
    fb = FakeBackend(tmp_path, sb)
    fb.renders["plant_cell"] = fb.rendered("plant_cell", groups=["cell_wall", "nucleus", "chloroplasts"])
    summary = run_figure_render_job(sb, _job(), backend=fb.as_backend())

    assert [k for k, _ in fb.generates] == ["plant_cell"], "exactly one generation"
    assert fb.generates[0][1].endswith(LAYER_TAIL + "cell wall, nucleus, chloroplasts, sap vacuole.")
    assert fb.publishes == [("plant_cell", "svg", {"curriculum": "cbse", "subject": "biology", "grade": "9", "topic": "Cells"})]
    plant = _fig(sb, "fig-plant")
    assert plant["status"] == "rendered" and plant["visual_asset_id"] == "va-plant_cell"
    assert plant["labels"] == [{"group_id": "cell_wall", "label": "cell wall"}, {"group_id": "nucleus", "label": "nucleus"},
                               {"group_id": "chloroplasts", "label": "chloroplasts"},
                               {"group_id": None, "label": "sap vacuole"}], "a part the drawing lacks keeps a null group"
    assert summary["generated"] == 1 and summary["reused"] == 0 and _job_row(sb)["status"] == "done"


def test_an_asset_the_wrapper_already_published_is_not_published_again(tmp_path):
    sb = _sb(figs=(PLANT,))
    fb = FakeBackend(tmp_path, sb)
    rendered = fb.rendered("plant_cell", groups=["cell_wall"])
    fb.renders["plant_cell"] = rendered
    sb.tables["visual_assets"].append({"id": "va-existing", "asset_key": "plant_cell", "canonical_key": "plant_cell",
                                       "status": "approved", "content_hash": content_hash(rendered.path),
                                       "group_ids": ["cell_wall", "nucleus", "chloroplasts", "sap_vacuole"]})
    run_figure_render_job(sb, _job(), backend=fb.as_backend())
    assert fb.publishes == [], "found by content hash: the library already has these bytes"
    plant = _fig(sb, "fig-plant")
    assert plant["visual_asset_id"] == "va-existing"
    assert [l["group_id"] for l in plant["labels"]] == ["cell_wall", "nucleus", "chloroplasts", "sap_vacuole"], (
        "the ROW's group ids win over what the ladder reported")


def test_a_publish_that_leaves_no_row_is_a_failure_not_a_rendered_figure(tmp_path):
    sb = _sb(figs=(PLANT,))
    fb = FakeBackend(tmp_path, sb, publish_fails=True)
    fb.renders["plant_cell"] = fb.rendered("plant_cell")
    run_figure_render_job(sb, _job(), backend=fb.as_backend())
    plant = _fig(sb, "fig-plant")
    assert plant["status"] == "draft" and "not found in the visual library" in plant["render_error"]
    assert _job_row(sb)["status"] == "error"


# ── the never-starve rule ───────────────────────────────────────────────


def test_a_queued_builder_job_pauses_before_the_first_generation(tmp_path):
    sb = _sb(jobs=[_job(), _builder()])
    fb = FakeBackend(tmp_path, sb)
    fb.renders["plant_cell"] = fb.rendered("plant_cell")
    fb.renders["animal_cell"] = fb.rendered("animal_cell")
    summary = run_figure_render_job(sb, _job(), backend=fb.as_backend())

    assert fb.generates == [] and fb.publishes == [], "no image call while a real user's job waits"
    assert _fig(sb, "fig-plant")["status"] == "draft" and _fig(sb, "fig-animal")["status"] == "draft"
    assert _fig(sb, "fig-plant").get("render_error") is None, "a pause is not a failure"
    assert summary["paused"] == PAUSED_BUILDERS and summary["step"] == "paused" and summary["done"] == 0
    job = _job_row(sb)
    assert job["status"] == "done" and job["error"] is None, "done, so it can be re-enqueued"
    assert job["stage"]["paused"] == PAUSED_BUILDERS
    assert sb.tables["generations"][0]["status"] == "queued" and sb.writes("generations") == []


@pytest.mark.parametrize("kind", ["presentation", "deck", "worksheet", "lesson_plan", "exam", "index_book"])
def test_every_builder_kind_pauses_a_generation(tmp_path, kind):
    sb = _sb(figs=(PLANT,), jobs=[_job(), _builder(kind=kind)])
    fb = FakeBackend(tmp_path, sb)
    fb.renders["plant_cell"] = fb.rendered("plant_cell")
    summary = run_figure_render_job(sb, _job(), backend=fb.as_backend())
    assert fb.generates == [] and summary["paused"] == PAUSED_BUILDERS


@pytest.mark.parametrize("kind", ["topic_harvest", "topic_derive", "topic_article", "figure_render", "support_diagnose"])
def test_a_queued_observer_job_does_not_pause_a_generation(tmp_path, kind):
    sb = _sb(figs=(PLANT,), jobs=[_job(), {**_builder(kind=kind), "generation_id": None}])
    fb = FakeBackend(tmp_path, sb)
    fb.renders["plant_cell"] = fb.rendered("plant_cell")
    summary = run_figure_render_job(sb, _job(), backend=fb.as_backend())
    assert len(fb.generates) == 1 and summary["paused"] is None


def test_a_processing_or_done_builder_does_not_pause(tmp_path):
    sb = _sb(figs=(PLANT,), jobs=[_job(), _builder(status="processing"), _builder("job-d", status="done")])
    fb = FakeBackend(tmp_path, sb)
    fb.renders["plant_cell"] = fb.rendered("plant_cell")
    assert run_figure_render_job(sb, _job(), backend=fb.as_backend())["paused"] is None


def test_a_builder_arriving_mid_job_pauses_the_next_generation_and_keeps_what_was_done(tmp_path):
    sb = _sb()
    fb = FakeBackend(tmp_path, sb)
    fb.renders["plant_cell"] = fb.rendered("plant_cell")
    fb.renders["animal_cell"] = fb.rendered("animal_cell")
    real_generate = fb._generate

    def generate_then_a_user_arrives(key, prompt):
        sb.tables["jobs"].append(_builder())      # a teacher clicks Generate while we draw
        return real_generate(key, prompt)

    backend = fb.as_backend()
    backend.generate = generate_then_a_user_arrives
    summary = run_figure_render_job(sb, _job(), backend=backend)
    assert [k for k, _ in fb.generates] == ["plant_cell"]
    assert _fig(sb, "fig-plant")["status"] == "rendered" and _fig(sb, "fig-animal")["status"] == "draft"
    assert summary["generated"] == 1 and summary["done"] == 1 and summary["paused"] == PAUSED_BUILDERS

    # Re-enqueued once the queue is quiet: the remaining figure is rendered, the done one untouched.
    sb.tables["jobs"] = [_job(id="job-fr2")]
    fb2 = FakeBackend(tmp_path, sb)
    fb2.renders["animal_cell"] = fb2.rendered("animal_cell")
    summary2 = run_figure_render_job(sb, _job(id="job-fr2"), backend=fb2.as_backend())
    assert [k for k, _ in fb2.generates] == ["animal_cell"] and summary2["total"] == 1
    assert _fig(sb, "fig-animal")["status"] == "rendered"


def test_a_library_hit_is_still_taken_while_a_builder_is_queued(tmp_path):
    """The gate is on GENERATION: a hit spends nothing, so a queued user job
    does not stop the figure from being served from the library."""
    sb = _sb(figs=(PLANT,), jobs=[_job(), _builder()])
    fb = FakeBackend(tmp_path, sb, hits={"plant_cell": LIB_PLANT})
    summary = run_figure_render_job(sb, _job(), backend=fb.as_backend())
    assert summary["reused"] == 1 and summary["paused"] is None and _fig(sb, "fig-plant")["status"] == "rendered"


# ── the image budget ────────────────────────────────────────────────────


def test_the_job_never_exceeds_image_calls_per_lesson(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAGE_CALLS_PER_LESSON", "1")
    sb = _sb()
    fb = FakeBackend(tmp_path, sb)
    fb.renders["plant_cell"] = fb.rendered("plant_cell")
    fb.renders["animal_cell"] = fb.rendered("animal_cell")
    summary = run_figure_render_job(sb, _job(), backend=fb.as_backend())
    assert [k for k, _ in fb.generates] == ["plant_cell"]
    assert summary["paused"] == PAUSED_BUDGET and summary["generated"] == 1
    assert _fig(sb, "fig-animal")["status"] == "draft" and _job_row(sb)["status"] == "done"


def test_the_engine_s_own_exhausted_budget_pauses_too(tmp_path):
    sb = _sb(figs=(PLANT,))
    fb = FakeBackend(tmp_path, sb, exhausted=True)
    fb.renders["plant_cell"] = fb.rendered("plant_cell")
    summary = run_figure_render_job(sb, _job(), backend=fb.as_backend())
    assert fb.generates == [] and summary["paused"] == PAUSED_BUDGET


def test_an_unusable_budget_variable_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("IMAGE_CALLS_PER_LESSON", "many")
    assert figures.max_generations() == 24
    monkeypatch.setenv("IMAGE_CALLS_PER_LESSON", "0")
    assert figures.max_generations() == 24
    monkeypatch.setenv("IMAGE_CALLS_PER_LESSON", "3")
    assert figures.max_generations() == 3


# ── failures ────────────────────────────────────────────────────────────


def test_a_failing_render_writes_render_error_and_leaves_the_figure_draft(tmp_path):
    sb = _sb()
    fb = FakeBackend(tmp_path, sb, renders={"plant_cell": RuntimeError("429 RESOURCE_EXHAUSTED")})
    fb.renders["animal_cell"] = fb.rendered("animal_cell")
    summary = run_figure_render_job(sb, _job(), backend=fb.as_backend())

    plant = _fig(sb, "fig-plant")
    assert plant["status"] == "draft" and plant["render_error"] == "RuntimeError: 429 RESOURCE_EXHAUSTED"
    assert plant.get("visual_asset_id") is None
    assert _fig(sb, "fig-animal")["status"] == "rendered", "the next figure still runs"
    assert summary["failed"] == 1 and summary["generated"] == 1 and summary["errors"] == ["plant_cell: RuntimeError: 429 RESOURCE_EXHAUSTED"]
    job = _job_row(sb)
    assert job["status"] == "error" and "1 of 2 figures failed" in job["error"] and "429" in job["error"]

    # The re-run renders only the failed one and clears its error.
    sb.tables["jobs"] = [_job(id="job-fr2")]
    fb2 = FakeBackend(tmp_path, sb)
    fb2.renders["plant_cell"] = fb2.rendered("plant_cell")
    run_figure_render_job(sb, _job(id="job-fr2"), backend=fb2.as_backend())
    assert [k for k, _ in fb2.generates] == ["plant_cell"]
    assert _fig(sb, "fig-plant")["status"] == "rendered" and _fig(sb, "fig-plant")["render_error"] is None


def test_a_ladder_that_produces_nothing_is_a_failure(tmp_path):
    sb = _sb(figs=(PLANT,))
    fb = FakeBackend(tmp_path, sb, renders={"plant_cell": None})
    run_figure_render_job(sb, _job(), backend=fb.as_backend())
    plant = _fig(sb, "fig-plant")
    assert plant["status"] == "draft" and "produced no asset" in plant["render_error"]
    assert _job_row(sb)["status"] == "error"


def test_a_figure_without_key_material_fails_without_a_call(tmp_path):
    blank = {**PLANT, "id": "fig-blank", "figure_key": "", "caption": "", "spec": {"subject": "", "parts": []}}
    sb = _sb(figs=(blank,))
    fb = FakeBackend(tmp_path, sb)
    run_figure_render_job(sb, _job(), backend=fb.as_backend())
    assert fb.finds == [] and fb.generates == []
    assert "no key material" in _fig(sb, "fig-blank")["render_error"]


def test_force_re_renders_figures_already_rendered(tmp_path):
    sb = _sb(figs=({**PLANT, "status": "rendered", "visual_asset_id": "va-old"}, ANIMAL),
             jobs=[_job(params={"article_id": "art-1", "force": True})])
    fb = FakeBackend(tmp_path, sb, hits={"plant_cell": LIB_PLANT})
    fb.renders["animal_cell"] = fb.rendered("animal_cell")
    summary = run_figure_render_job(sb, _job(params={"article_id": "art-1", "force": True}), backend=fb.as_backend())
    assert summary["total"] == 2 and _fig(sb, "fig-plant")["visual_asset_id"] == "va-plant"
    # Without force a rendered figure is left alone.
    sb2 = _sb(figs=({**PLANT, "status": "rendered", "visual_asset_id": "va-old"},))
    fb2 = FakeBackend(tmp_path, sb2, hits={"plant_cell": LIB_PLANT})
    assert run_figure_render_job(sb2, _job(), backend=fb2.as_backend())["total"] == 0
    assert fb2.finds == [] and _fig(sb2, "fig-plant")["visual_asset_id"] == "va-old"


@pytest.mark.parametrize("params", [None, {}, {"article_id": ""}, {"article_id": 7}, "art-1"])
def test_a_job_without_an_article_id_finishes_with_error(params, tmp_path):
    sb = _sb(jobs=[_job(params=params)])
    fb = FakeBackend(tmp_path, sb)
    run_figure_render_job(sb, _job(params=params), backend=fb.as_backend())
    assert _job_row(sb)["status"] == "error" and "article_id" in _job_row(sb)["error"]


def test_a_missing_article_finishes_the_job_with_error(tmp_path):
    sb = _sb(article=None)
    fb = FakeBackend(tmp_path, sb)
    assert run_figure_render_job(sb, _job(), backend=fb.as_backend()) is None
    assert _job_row(sb)["status"] == "error" and "not found" in _job_row(sb)["error"]
    assert fb.resets == [], "no budget touched, nothing rendered"


def test_an_article_with_no_draft_figures_finishes_done_without_touching_the_engine(tmp_path):
    sb = _sb(figs=())
    fb = FakeBackend(tmp_path, sb)
    summary = run_figure_render_job(sb, _job(), backend=fb.as_backend())
    assert summary["total"] == 0 and summary["step"] == "done" and _job_row(sb)["status"] == "done"
    assert fb.resets == [] and fb.contexts == []


def test_a_context_that_cannot_be_read_falls_back_to_generic(tmp_path):
    sb = _sb(figs=(PLANT,))
    sb.tables["curriculum_nodes"] = []
    sb.tables["topics"] = []
    fb = FakeBackend(tmp_path, sb, hits={"plant_cell": LIB_PLANT})
    run_figure_render_job(sb, _job(), backend=fb.as_backend())
    assert fb.contexts == [{"curriculum": "generic", "subject": "general", "grade": "k12", "topic": "Cells"}]


def test_progress_is_reported_per_figure(tmp_path):
    sb = _sb()
    fb = FakeBackend(tmp_path, sb, hits={"plant_cell": LIB_PLANT})
    fb.renders["animal_cell"] = fb.rendered("animal_cell")
    run_figure_render_job(sb, _job(), backend=fb.as_backend())
    stages = [e[2]["stage"] for e in sb.writes("jobs") if e[0] == "update" and "stage" in e[2]]
    assert [s["done"] for s in stages] == [0, 1, 2, 2] and stages[-1]["step"] == "done"
    progress = [e[2]["progress"] for e in sb.writes("jobs") if e[0] == "update" and "progress" in e[2]]
    assert progress == [5, 50, 95, 100]


def test_the_default_backend_is_built_lazily_and_only_when_a_figure_needs_it(monkeypatch, tmp_path):
    """Production imports the engine on demand; a job with nothing to render
    must never pay for it (and a test must never reach it)."""
    monkeypatch.setattr(figures, "default_backend", lambda: pytest.fail("the engine was imported for nothing"))
    sb = _sb(figs=())
    assert run_figure_render_job(sb, _job())["total"] == 0
