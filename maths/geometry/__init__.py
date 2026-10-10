"""geometry.figure.v1 — figures for maths questions, built from a model's
intent by a closed construction library, held as an exact facts graph,
proved by theorems + SymPy, and drawn under a render policy.

    construction creates → facts describe truth → relations claim and are
    verified → theorems deduce → SymPy proves → coordinates realise →
    renderer displays

Entry points: ``parse_question`` (spec), ``verify_question`` (the chain,
a QuestionReport), ``realise`` (a model under a policy),
``render_figure`` (SVG/PNG of a model).
"""

from maths.geometry.compiler import compile_figure
from maths.geometry.errors import CODES, GeometryRefusal
from maths.geometry.realise import POLICIES, realise
from maths.geometry.spec import SCHEMA_VERSION, QuestionSpec, parse_question
from maths.geometry.verify import QuestionReport, verify_question

__all__ = ["CODES", "GeometryRefusal", "POLICIES", "QuestionReport", "QuestionSpec", "SCHEMA_VERSION",
           "compile_figure", "parse_question", "realise", "verify_question"]
