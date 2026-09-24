"""The structured mathematics lesson — the record everything derives from.

Shapes chosen for the four things that have to work: SymPy can verify a step
only when the state before and after it is machine-readable, so states are
lists of relations in linear notation (``maths.notation``), never prose. A
list, not one string, because simultaneous equations carry two lines and a
quadratic splits into two cases — one expression per step could describe
neither. Steps are TYPED because not every step is a transformation: "let x
be the number of tickets" is a setup nobody can prove equivalent, and "check
by substituting" is verified by evaluation, not by equivalence. The speech
lives ON the step, so the board's write cue, the caption and the voice come
from the same sentence.

Everything the model returns is model output and therefore hostile: every
field is bounded, every string trimmed, and the lesson is validated before a
single SymPy call is made.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

StepKind = Literal["transform", "setup", "check"]
Task = Literal["solve", "solve_system", "solve_inequality", "simplify", "expand",
               "factorise", "evaluate"]
TASKS: tuple[str, ...] = ("solve", "solve_system", "solve_inequality", "simplify", "expand",
                          "factorise", "evaluate")
DIFFICULTY_NAMES = {1: "simplest", 2: "medium", 3: "difficult", 4: "extremely difficult"}

_MAX_LINE = 400
_MAX_STATE = 6
_MAX_STEPS = 14


def _clean(s) -> str:
    return " ".join(str(s or "").split())


def _as_list(v):
    """A state field the model wrote as one string instead of a list
    (production, 2026-09-24: `"from_state": "(z - 3)/5 = (z - 5)/3"` with the
    Vertex schema flag off). One line is a one-line state; None is empty."""
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, (list, tuple)):
        return list(v)
    return [str(v)]


def _as_lines(v):
    """Dialogue the model wrote as plain strings becomes teacher lines."""
    if isinstance(v, str):
        return [{"who": "teacher", "line": v}]
    if not isinstance(v, (list, tuple)):
        return []
    out = []
    for item in v:
        if isinstance(item, str):
            out.append({"who": "teacher", "line": item})
        elif isinstance(item, (dict, Line)):
            out.append(item)
    return out


class Line(BaseModel):
    """One spoken line of the two-voice dialogue."""
    model_config = ConfigDict(extra="ignore")

    who: Literal["teacher", "student"] = "teacher"
    line: str = ""

    @field_validator("line")
    @classmethod
    def _trim(cls, v: str) -> str:
        return _clean(v)[:_MAX_LINE * 2]


class Step(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kind: StepKind = "transform"
    #: what was done, in words a board annotation can carry: "subtract 5 from both sides"
    operation: str = ""
    #: the state this step starts from and the state it produces — relations
    #: or expressions in linear notation, one per line of working
    before: list[str] = Field(default_factory=list)
    after: list[str] = Field(default_factory=list)
    #: a short board note beside the new line (defaults to the operation)
    explanation: str = ""
    #: the teacher's words for this step — WORDS, never notation
    speech: str = ""
    #: an optional student reaction or question after the step
    student: str = ""

    @field_validator("operation", "explanation", "speech", "student")
    @classmethod
    def _trim(cls, v: str) -> str:
        return _clean(v)[:_MAX_LINE]

    @field_validator("before", "after", mode="before")
    @classmethod
    def _listify(cls, v):
        return _as_list(v)

    @field_validator("before", "after")
    @classmethod
    def _states(cls, v: list) -> list[str]:
        return [_clean(x)[:_MAX_LINE] for x in (v or []) if _clean(x)][:_MAX_STATE]

    @property
    def note(self) -> str:
        return self.explanation or self.operation


class Mistake(BaseModel):
    """A tempting wrong route: from ``from_state`` the wrong move gives
    ``wrong_state``. Verified the OTHER way round — SymPy must confirm it is
    actually wrong, or a valid method would be taught as an error."""
    model_config = ConfigDict(extra="ignore")

    from_state: list[str] = Field(default_factory=list)
    wrong_state: list[str] = Field(default_factory=list)
    operation: str = ""
    why_wrong: str = ""
    speech: str = ""

    @field_validator("from_state", "wrong_state", mode="before")
    @classmethod
    def _listify(cls, v):
        return _as_list(v)

    @field_validator("from_state", "wrong_state")
    @classmethod
    def _states(cls, v: list) -> list[str]:
        return [_clean(x)[:_MAX_LINE] for x in (v or []) if _clean(x)][:_MAX_STATE]

    @field_validator("operation", "why_wrong", "speech")
    @classmethod
    def _trim(cls, v: str) -> str:
        return _clean(v)[:_MAX_LINE]


class WorkedExample(BaseModel):
    model_config = ConfigDict(extra="ignore")

    label: str = ""
    difficulty: int = Field(default=1, ge=1, le=4)
    task: Task = "solve"
    #: the problem as the student reads it: notation, or a word problem
    problem: str = ""
    #: the mathematics of the problem — the relations the working starts from
    givens: list[str] = Field(default_factory=list)
    #: what is asked for: the variable(s), or "expression"
    target: str = "x"
    intro_speech: str = ""
    student_question: str = ""
    steps: list[Step] = Field(default_factory=list)
    #: one relation per entry: ["x = 5"], ["x = 2", "x = 3"] (either), ["x = 1", "y = 2"] (both)
    final_answer: list[str] = Field(default_factory=list)
    answer_speech: str = ""
    common_mistake: Optional[Mistake] = None

    @field_validator("label", "problem", "target", "intro_speech", "student_question", "answer_speech")
    @classmethod
    def _trim(cls, v: str) -> str:
        return _clean(v)[:_MAX_LINE]

    @field_validator("givens", "final_answer", mode="before")
    @classmethod
    def _listify(cls, v):
        return _as_list(v)

    @field_validator("givens", "final_answer")
    @classmethod
    def _states(cls, v: list) -> list[str]:
        return [_clean(x)[:_MAX_LINE] for x in (v or []) if _clean(x)][:_MAX_STATE]

    @field_validator("difficulty", mode="before")
    @classmethod
    def _difficulty(cls, v):
        try:
            return max(1, min(4, int(str(v).strip()[:1])))
        except (TypeError, ValueError):
            return 1

    @field_validator("common_mistake", mode="before")
    @classmethod
    def _mistake(cls, v):
        # an empty object or a bare string is "no mistake shown"
        if isinstance(v, Mistake):
            return v
        return v if isinstance(v, dict) and (v.get("from_state") or v.get("wrong_state")) else None

    @field_validator("steps", mode="before")
    @classmethod
    def _dict_steps(cls, v):
        return [x for x in (v or []) if isinstance(x, (dict, Step))] if isinstance(v, (list, tuple)) else []

    @field_validator("steps")
    @classmethod
    def _cap(cls, v: list) -> list:
        return list(v or [])[:_MAX_STEPS]

    @property
    def difficulty_name(self) -> str:
        return DIFFICULTY_NAMES.get(int(self.difficulty), "")

    @property
    def variables(self) -> list[str]:
        """The unknowns named by ``target`` ("x", "x, y", "x and y")."""
        import re
        names = re.findall(r"[A-Za-z][A-Za-z0-9_]*", self.target or "")
        return [n for n in names if n.lower() not in ("and", "expression", "value")] or ["x"]


class MethodCard(BaseModel):
    """The method, pinned on the board for the whole lesson: at most five
    short lines, the step being used highlighted as each example proceeds."""
    model_config = ConfigDict(extra="ignore")

    title: str = "METHOD"
    steps: list[str] = Field(default_factory=list)

    @field_validator("steps", mode="before")
    @classmethod
    def _listify(cls, v):
        return _as_list(v)

    @field_validator("steps")
    @classmethod
    def _short(cls, v: list) -> list[str]:
        return [_clean(x)[:48] for x in (v or []) if _clean(x)][:5]


class TryIt(BaseModel):
    model_config = ConfigDict(extra="ignore")

    problem: str = ""
    answer: list[str] = Field(default_factory=list)
    speech: str = ""

    @field_validator("answer", mode="before")
    @classmethod
    def _listify(cls, v):
        return _as_list(v)


class Lesson(BaseModel):
    """One maths lesson: hook -> concept (+ method card) -> examples 1..4 ->
    recap / try it. The blueprint is fixed here, not left to the model."""
    model_config = ConfigDict(extra="ignore")

    topic: str = ""
    level: str = ""
    hook: list[Line] = Field(default_factory=list)
    concept: list[Line] = Field(default_factory=list)
    #: two or three short board points written during the concept
    concept_points: list[str] = Field(default_factory=list)
    method: MethodCard = Field(default_factory=MethodCard)
    examples: list[WorkedExample] = Field(default_factory=list)
    recap: list[Line] = Field(default_factory=list)
    misconceptions: list[str] = Field(default_factory=list)
    try_it: TryIt = Field(default_factory=TryIt)

    @field_validator("hook", "concept", "recap", mode="before")
    @classmethod
    def _lines(cls, v):
        return _as_lines(v)

    @field_validator("concept_points", "misconceptions", mode="before")
    @classmethod
    def _listify(cls, v):
        return _as_list(v)

    @field_validator("concept_points", "misconceptions")
    @classmethod
    def _points(cls, v: list) -> list[str]:
        return [_clean(x)[:90] for x in (v or []) if _clean(x)][:3]

    @field_validator("examples", mode="before")
    @classmethod
    def _dict_examples(cls, v):
        return [x for x in (v or []) if isinstance(x, (dict, WorkedExample))] if isinstance(v, (list, tuple)) else []

    @field_validator("examples")
    @classmethod
    def _four(cls, v: list) -> list:
        return list(v or [])[:4]


# ── the JSON schema the model is asked to fill (Vertex responseSchema subset:
# no additionalProperties, no $ref, enums and required lists only) ──────────

def _str() -> dict:
    return {"type": "string"}


def _strs() -> dict:
    return {"type": "array", "items": {"type": "string"}}


def _lines() -> dict:
    return {"type": "array", "items": {"type": "object", "properties": {
        "who": {"type": "string", "enum": ["teacher", "student"]}, "line": _str()},
        "required": ["who", "line"]}}


STEP_SCHEMA = {"type": "object", "properties": {
    "kind": {"type": "string", "enum": ["transform", "setup", "check"]},
    "operation": _str(), "before": _strs(), "after": _strs(),
    "explanation": _str(), "speech": _str(), "student": _str()},
    "required": ["kind", "operation", "before", "after", "speech"]}

MISTAKE_SCHEMA = {"type": "object", "properties": {
    "from_state": _strs(), "wrong_state": _strs(), "operation": _str(),
    "why_wrong": _str(), "speech": _str()},
    "required": ["from_state", "wrong_state", "why_wrong", "speech"]}

EXAMPLE_SCHEMA = {"type": "object", "properties": {
    "label": _str(), "difficulty": {"type": "integer"},
    "task": {"type": "string", "enum": list(TASKS)},
    "problem": _str(), "givens": _strs(), "target": _str(),
    "intro_speech": _str(), "student_question": _str(),
    "steps": {"type": "array", "items": STEP_SCHEMA},
    "final_answer": _strs(), "answer_speech": _str(),
    "common_mistake": MISTAKE_SCHEMA},
    "required": ["label", "difficulty", "task", "problem", "givens", "target",
                 "intro_speech", "steps", "final_answer", "answer_speech"]}

LESSON_SCHEMA = {"type": "object", "properties": {
    "topic": _str(), "level": _str(),
    "hook": _lines(), "concept": _lines(), "concept_points": _strs(),
    "method": {"type": "object", "properties": {"title": _str(), "steps": _strs()},
               "required": ["title", "steps"]},
    "examples": {"type": "array", "items": EXAMPLE_SCHEMA},
    "recap": _lines(), "misconceptions": _strs(),
    "try_it": {"type": "object", "properties": {"problem": _str(), "answer": _strs(), "speech": _str()},
               "required": ["problem", "answer", "speech"]}},
    "required": ["topic", "hook", "concept", "concept_points", "method", "examples", "recap",
                 "try_it"]}


def parse_lesson(data) -> Lesson:
    """A Lesson out of a model reply. Pydantic ignores unknown keys and
    coerces what it can; a reply that is not even an object is empty."""
    if not isinstance(data, dict):
        return Lesson()
    return Lesson.model_validate(data)


def parse_example(data) -> WorkedExample:
    if not isinstance(data, dict):
        return WorkedExample()
    return WorkedExample.model_validate(data)


__all__ = ["Line", "Step", "Mistake", "WorkedExample", "MethodCard", "TryIt", "Lesson",
           "TASKS", "DIFFICULTY_NAMES", "STEP_SCHEMA", "EXAMPLE_SCHEMA", "LESSON_SCHEMA",
           "parse_lesson", "parse_example"]
