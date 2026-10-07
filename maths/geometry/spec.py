"""geometry.figure.v1 — the record a model writes for a figure-bearing question.

The model describes INTENT: points, objects built by named constructions,
angles named by their rays, measures, claimed relations, marks, and the
steps of the working. It never writes a coordinate. Construction parameters
vary by construction, so objects stay plain dicts here and are validated by
the construction registry (maths.geometry.constructions) when compiled;
everything else is fixed shape and validated now.

Everything in here is model output and therefore hostile: ids are bounded
and unique, lists are bounded, and the schema version is exact.
"""

from __future__ import annotations

import re
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from maths.geometry.errors import GeometryRefusal

SCHEMA_VERSION = "geometry.figure.v1"

FigureRole = Literal["evidence", "reasoning", "illustration"]
StepKind = Literal["deduce", "transform", "check", "setup"]
AnswerKind = Literal["number", "values", "label_set", "label_map", "value_set"]
MeasureRole = Literal["given", "unknown", "derived"]

_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.]{0,47}$")
_MAX_ITEMS = 40


def _check_id(v: str) -> str:
    if not isinstance(v, str) or not _ID_RE.match(v):
        raise ValueError(f"{v!r} is not a valid id")
    return v


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PointSpec(_Strict):
    id: str
    label: Optional[str] = Field(default=None, max_length=4)

    _id = field_validator("id")(classmethod(lambda cls, v: _check_id(v)))


class AngleSpec(_Strict):
    id: str
    rays: list[list[str]]
    region: Literal["interior", "reflex"] = "interior"

    _id = field_validator("id")(classmethod(lambda cls, v: _check_id(v)))

    @field_validator("rays")
    @classmethod
    def _two_rays(cls, v):
        if len(v) != 2 or any(len(r) != 2 for r in v):
            raise ValueError("an angle is two rays, each [vertex, point]")
        if v[0][0] != v[1][0]:
            raise ValueError("both rays of an angle start at the same vertex")
        if v[0][1] == v[1][1]:
            raise ValueError("the two rays of an angle go to different points")
        return v

    @property
    def vertex(self) -> str:
        return self.rays[0][0]

    @property
    def arms(self) -> tuple[str, str]:
        return self.rays[0][1], self.rays[1][1]


class SegmentSpec(_Strict):
    id: str
    points: list[str]

    _id = field_validator("id")(classmethod(lambda cls, v: _check_id(v)))

    @field_validator("points")
    @classmethod
    def _two(cls, v):
        if len(v) != 2 or v[0] == v[1]:
            raise ValueError("a segment joins two different points")
        return v


class MeasureSpec(_Strict):
    target: str
    value: Union[str, int, float]
    unit: Optional[Literal["deg", "cm", "mm", "m", "units"]] = None
    role: MeasureRole = "given"


class RelationSpec(BaseModel):
    """A claim. Its fields depend on its kind (points / lines / segments /
    angles / point+segment / point+circle)."""
    model_config = ConfigDict(extra="allow")
    id: Optional[str] = None
    kind: str
    given: bool = False


class MarkSpec(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: Optional[str] = None
    kind: str


class FigureSpec(_Strict):
    units: Optional[Literal["cm", "mm", "m", "units"]] = None
    orientation: float = 0.0
    bind: dict[str, Union[int, float, str]] = Field(default_factory=dict)
    points: list[PointSpec] = Field(default_factory=list, max_length=_MAX_ITEMS)
    objects: list[dict[str, Any]] = Field(default_factory=list, max_length=_MAX_ITEMS)
    angles: list[AngleSpec] = Field(default_factory=list, max_length=_MAX_ITEMS)
    segments: list[SegmentSpec] = Field(default_factory=list, max_length=_MAX_ITEMS)
    measures: list[MeasureSpec] = Field(default_factory=list, max_length=_MAX_ITEMS)
    relations: list[RelationSpec] = Field(default_factory=list, max_length=_MAX_ITEMS)
    marks: list[MarkSpec] = Field(default_factory=list, max_length=_MAX_ITEMS)

    @field_validator("objects")
    @classmethod
    def _objects_named(cls, v):
        for o in v:
            if not isinstance(o, dict) or "make" not in o:
                raise ValueError("every object has a `make`")
            if "id" in o:
                _check_id(o["id"])
        return v

    def angle(self, angle_id: str) -> Optional[AngleSpec]:
        return next((a for a in self.angles if a.id == angle_id), None)

    def measure(self, target: str) -> Optional[MeasureSpec]:
        return next((m for m in self.measures if m.target == target), None)


class FigureRef(_Strict):
    id: str
    label: Optional[str] = Field(default=None, max_length=4)
    figure: FigureSpec


class Asks(_Strict):
    property: str
    over: list[str]
    select: Optional[Any] = None
    fill: Optional[str] = None
    equals: Optional[int] = None
    greater_than: Optional[int] = None
    less_than: Optional[int] = None


class Answer(_Strict):
    kind: AnswerKind
    value: Any
    unit: Optional[str] = None


class StepSpec(_Strict):
    kind: StepKind
    theorem: Optional[str] = None
    uses: list[str] = Field(default_factory=list, max_length=12)
    before: Optional[list[str]] = None
    after: list[str] = Field(default_factory=list, max_length=6)
    speech: str = Field(default="", max_length=600)
    figure_ops: list[dict[str, Any]] = Field(default_factory=list, max_length=12)


class Part(_Strict):
    asks: Asks
    answer: Answer


class QuestionSpec(_Strict):
    schema_version: str
    id: str = "q"
    figure_role: FigureRole
    prompt: str = Field(default="", max_length=600)
    figure: Optional[FigureSpec] = None
    figures: list[FigureRef] = Field(default_factory=list, max_length=8)
    asks: Optional[Asks] = None
    parts: list[Part] = Field(default_factory=list, max_length=6)
    steps: list[StepSpec] = Field(default_factory=list, max_length=20)
    answer: Optional[Answer] = None

    @field_validator("schema_version")
    @classmethod
    def _version(cls, v):
        if v != SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {SCHEMA_VERSION!r}, got {v!r}")
        return v

    @model_validator(mode="after")
    def _one_figure_form(self):
        if self.figure is not None and self.figures:
            raise ValueError("give `figure` or `figures`, not both")
        if self.figure is not None:
            self.figures = [FigureRef(id="fig", figure=self.figure)]
            self.figure = None
        if self.asks is not None:
            self.parts = [Part(asks=self.asks, answer=self.answer)] if self.answer else self.parts
        return self

    def figure_by_id(self, fid: str) -> Optional[FigureRef]:
        return next((f for f in self.figures if f.id == fid), None)


def parse_question(raw: dict) -> QuestionSpec:
    """The question record, or GeometryRefusal('bad_schema'). The message
    keeps pydantic's location so the model is told which field."""
    if not isinstance(raw, dict):
        raise GeometryRefusal("bad_schema", "a question is a JSON object")
    try:
        q = QuestionSpec.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(x) for x in first.get("loc", ()))
        raise GeometryRefusal("bad_schema", f"{loc}: {first.get('msg')}") from exc
    ids = [f.id for f in q.figures]
    if len(set(ids)) != len(ids):
        raise GeometryRefusal("bad_reference", "two figures share an id")
    return q
