"""The SUBJECT PROFILE a generation is built under — resolved once, passed down.

SketchCast was built for science: a lesson is a narrative, the board is an
illustration the teacher labels, the sketch lexicon draws what the narration
names. Mathematics is taught differently — a concept, then a ladder of worked
examples, with the working itself as the board — and several science habits
work AGAINST it (2026-09-24 survey): the five-word label rule drops an equation
from the board, "root" sketches a plant, an image model cannot draw a triangle
whose letters survive.

The founder's direction: do NOT fork the pipeline. One shared pipeline, one
profile object resolved here, and exactly three things switch on it — the
lesson shape (agent3 vs maths.lesson), the board grammar (illustration vs the
algebra board) and the vocabulary/speech layer (sketch lexicon off, notation
spoken). Ingestion, analysis, TTS, encoding, publishing and the document
builders stay shared.

Gated twice, because it changes what a teacher receives:
  * FEATURE_MATHS_LESSONS=1 turns the maths profile on for every generation
    whose book/topic subject reads as mathematics.
  * params.subject_profile = "maths" | "science" pins ONE generation either
    way, flag or no flag — the console's demo and rollback lever.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional

SCIENCE = "science"
MATHS = "maths"
KINDS = (SCIENCE, MATHS)

FEATURE_FLAG = "FEATURE_MATHS_LESSONS"
PARAM_KEY = "subject_profile"

# Lesson modes within the maths profile. The first cut builds worked_example
# only; proof_reasoning and construction are named so the schema is complete
# before they ship (founder direction 2026-09-24, decision 3).
MODE_WORKED_EXAMPLE = "worked_example"
MODE_PROOF = "proof_reasoning"
MODE_CONSTRUCTION = "construction"
MODES = (MODE_WORKED_EXAMPLE, MODE_PROOF, MODE_CONSTRUCTION)

# A subject string that means mathematics, in the languages the app serves.
# Word-bounded on the Latin side so "aftermath" is not maths; the Arabic stem
# رياض (riyāḍ) covers رياضيات / الرياضيات.
_MATHS_RE = re.compile(
    r"\b(?:math|maths|mathematics|mathematik|mathématiques|matematika|matematik|"
    r"algebra|arithmetic|calculus|geometry|trigonometry|statistics|precalculus)\b|رياض",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Profile:
    """What the pipeline switches on. Immutable: it is resolved once per
    generation and read in several places, and a reader must never find a
    different answer than the writer did."""

    kind: str
    lesson_mode: Optional[str] = None
    #: the narration-driven auto-sketches (whiteboard.sketch_elements) — off
    #: for maths, where "root", "plane", "table" and "scale" are not objects
    sketch_lexicon: bool = True
    #: notation is turned into spoken words before it reaches a voice
    math_speech: bool = False

    @property
    def maths(self) -> bool:
        return self.kind == MATHS

    @property
    def worked_examples(self) -> bool:
        """The one mode the first cut renders."""
        return self.kind == MATHS and self.lesson_mode == MODE_WORKED_EXAMPLE

    def as_dict(self) -> dict:
        return {"kind": self.kind, "lesson_mode": self.lesson_mode,
                "sketch_lexicon": self.sketch_lexicon, "math_speech": self.math_speech}


SCIENCE_PROFILE = Profile(kind=SCIENCE)
MATHS_PROFILE = Profile(kind=MATHS, lesson_mode=MODE_WORKED_EXAMPLE,
                        sketch_lexicon=False, math_speech=True)


def is_maths_subject(subject) -> bool:
    return bool(subject) and bool(_MATHS_RE.search(str(subject)))


def enabled() -> bool:
    return os.getenv(FEATURE_FLAG, "").strip().lower() in ("1", "true", "yes", "on")


def resolve(subject, *, params: dict | None = None) -> Profile:
    """The profile for a generation with this book/topic ``subject`` and these
    job ``params``. An explicit params.subject_profile wins either way; the
    flag decides for everyone else; the default is science, the well-tested
    majority path, exactly as an unknown language falls to the default
    provider."""
    explicit = str((params or {}).get(PARAM_KEY) or "").strip().lower() if isinstance(params, dict) else ""
    if explicit == MATHS:
        return MATHS_PROFILE
    if explicit == SCIENCE:
        return SCIENCE_PROFILE
    if enabled() and is_maths_subject(subject):
        return MATHS_PROFILE
    return SCIENCE_PROFILE


__all__ = ["SCIENCE", "MATHS", "KINDS", "FEATURE_FLAG", "PARAM_KEY", "MODE_WORKED_EXAMPLE",
           "MODE_PROOF", "MODE_CONSTRUCTION", "MODES", "Profile", "SCIENCE_PROFILE",
           "MATHS_PROFILE", "is_maths_subject", "enabled", "resolve"]
