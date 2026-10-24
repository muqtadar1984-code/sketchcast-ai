"""Typed contracts for Sprint 1: book brief, blueprint, audit, plan, pins.

Nothing here names a subject. Subject differences arrive as data (the
brief's strand prefixes, the capability kinds a blueprint asks for).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

ROOT = Path(__file__).resolve().parent

BANK_ORDER = ["none", "basic", "good", "strong", "assessment", "exam_ready"]

# Figure/block kinds the engine can render or validate today or by plan. A
# blueprint may only request kinds from this list; anything else is reported
# as a capability gap rather than improvised.
CAPABILITY_KINDS = {
    "math_expr", "worked_steps", "geometry_figure", "chart", "number_line",
    "labelled_illustration", "photo", "table", "chem_equation", "molecule_2d",
    "circuit", "force_diagram", "apparatus_illustration", "process_diagram",
    "timeline", "map", "source_document", "micrograph",
}


class ReadinessPolicy(BaseModel):
    id: str
    languages: list[str]
    min_words: int
    min_worked_examples: dict[str, int]
    min_bank_maturity: str
    require_figures_rendered: bool = True
    thumbnail_warning_ok: bool = True
    notes: str = ""

    @field_validator("languages")
    @classmethod
    def _en_first(cls, v):
        if not v or v[0] != "en":
            raise ValueError("English must be the first required language")
        return v

    @classmethod
    def load(cls, path: Path | None = None) -> "ReadinessPolicy":
        p = path or ROOT / "config" / "readiness_policy.json"
        return cls.model_validate(json.loads(p.read_text(encoding="utf-8")))


class Directives(BaseModel):
    """Founder scope decisions for one book (Gate B review). Binding on the
    model, and checked deterministically after it replies."""
    decided: str
    notes: list[str] = []
    must_include: list[str] = []             # objective codes that must be taught
    must_exclude: list[str] = []             # objective codes that must be excluded
    required_new_topics: list[str] = []      # exact proposed titles (e.g. a topic shared by two books)
    forbidden_new_topics: list[str] = []     # titles no proposal may resemble
    blocked_until: str | None = None         # the book may not be blueprinted until this is true


class BookBrief(BaseModel):
    """Stage A input: what the founder wants the book to be."""
    slug: str
    working_title: str
    intent: str
    subject_strand: str                      # e.g. "mathematics/algebra"
    profile: Literal["maths", "science"]     # selects depth thresholds only
    primary_curriculum: str                  # curricula.code
    crosswalk_curricula: list[str] = []
    grades: list[str]                        # e.g. ["7","8","9"]
    age_band: str
    strand_prefixes: list[str]               # node-code strand letters, e.g. ["Ae","As"]
    crosswalk_prefixes: list[str] = []       # e.g. ["ALG"]
    extra_node_codes: list[str] = []         # single objectives from another strand
    catalogue_subjects: list[str]            # topics.subject values offered to the model
    directives: Directives | None = None


class RequiredTopic(BaseModel):
    topic_slug: str | None = None            # an existing catalogue topic
    proposed_title: str | None = None        # a topic that must be created
    role: Literal["core", "supporting", "prerequisite_review"]
    node_codes: list[str] = []
    rationale: str


class BlueprintChapter(BaseModel):
    number: int
    title: str
    purpose: str
    sections: list[str]
    node_codes: list[str]
    required_topics: list[RequiredTopic]
    prerequisite_chapters: list[int] = []
    illustration_needs: list[str] = []       # capability kinds
    assessment: str


class ExcludedNode(BaseModel):
    code: str
    reason: str


class ProposedNode(BaseModel):
    """A curriculum objective the seed lacks. Always needs human verification
    against the official framework before it is loaded."""
    code: str
    title: str
    reason: str


class Blueprint(BaseModel):
    title: str
    subtitle: str
    audience: str
    curriculum_coverage: str
    consolidation_rationale: str
    prerequisite_knowledge: list[str]
    chapters: list[BlueprintChapter]
    excluded_nodes: list[ExcludedNode] = []
    proposed_curriculum_nodes: list[ProposedNode] = []
    duplicate_overlap_analysis: list[str] = []
    content_gaps: list[str] = []
    assessment_plan: str
    illustration_requirements: list[str]
    estimated_pages: int
    estimated_words: int
    complexity: Literal["low", "low-medium", "medium", "medium-high", "high"]
    learning_progression: str
    publishing_considerations: list[str]
    localization_considerations: list[str]
    interactive_units: list[str]
    capability_gaps: list[str] = []


CheckCode = Literal["B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8", "B9", "B10", "B11"]
Verdict = Literal["pass", "fail", "warn", "n/a"]

TopicState = Literal[
    "complete", "needs_improvement", "kit_or_publication_incomplete",
    "missing_article", "missing_topic", "missing_mapping",
    "missing_translation", "missing_video_publication", "blocked",
]


class CheckResult(BaseModel):
    code: CheckCode
    language: str | None = None
    verdict: Verdict
    sub_status: str | None = None
    detail: str = ""
    evidence: dict = Field(default_factory=dict)


class TopicAudit(BaseModel):
    required_index: int                      # position in the blueprint's flat topic list
    topic_id: str | None
    topic_slug: str | None
    title: str
    role: str
    chapter: int
    checks: list[CheckResult]
    state: TopicState
    failing: list[str]
    warnings: list[str] = []


class DependencyStep(BaseModel):
    order: int
    step: str
    job_type: str | None
    language: str | None = None
    depends_on: list[int] = []
    est_cost_usd: float | None
    cost_basis: str
    est_review_min: int
    throughput_note: str = ""
