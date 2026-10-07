"""Refusals: every way a geometry figure can fail, each with a stable code.

A refusal is the normal outcome for a figure the engine cannot prove, not an
error in the engine. The code goes into the verification report and, with the
message, back to the model through the regenerate-with-reasons loop, so the
message names the object and says what would have been acceptable.
"""

from __future__ import annotations

from typing import Optional

CODES = frozenset({
    "bad_schema",                # the JSON does not parse as geometry.figure.v1
    "bad_reference",             # an id that is not defined (or defined twice)
    "unknown_construction",      # `make` is not in the construction registry
    "unsupported_feature",       # a known later-phase feature (reflection, tessellation, ...)
    "construction_impossible",   # no figure has these measurements
    "construction_ambiguous",    # more than one figure has them (SSA)
    "closure_failed",            # angles at a point / a polygon do not close
    "relation_not_implied",      # a claimed relation the constructions do not produce
    "given_not_realised",        # a given measure the figure does not have
    "theorem_unknown",           # `deduce` names a theorem outside the enum
    "theorem_premise",           # the theorem does not apply to these objects
    "step_not_equivalent",       # an equation that does not follow
    "step_unverifiable",         # SymPy could not decide
    "answer_unproved",           # the steps never establish the answer
    "answer_mismatch",           # the stated answer is not the proved/computed one
    "bind_mismatch",             # the figure's bound value of x is not the proved one
    "undecidable",               # an exact comparison SymPy could not settle
    "not_discernible",           # an evidence figure a student could misread
    "mark_gives_away",           # a mark that answers the question being asked
    "layout_collision",          # labels that cannot be placed without overlapping
    "not_schematisable",         # no distortion hides the unknown's size
})


class GeometryRefusal(ValueError):
    """The figure is refused. ``code`` is one of CODES; ``where`` names the
    object (an id) when there is one."""

    def __init__(self, code: str, message: str, where: Optional[str] = None):
        if code not in CODES:
            raise ValueError(f"unknown refusal code {code!r}")
        self.code = code
        self.message = message
        self.where = where
        super().__init__(f"{code}: {message}" + (f" [{where}]" if where else ""))

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "where": self.where}
