"""Validation and sandboxing for the math service.

WHY this module exists: expressions arrive as strings composed by an LLM on
behalf of school kids, and SymPy's parser ultimately compiles and evaluates
Python code. Nothing model-shaped may reach the parser unvalidated. Defense
in depth, three layers:

1. BEFORE parsing — a character whitelist plus a forbidden-token scan, so
   attribute access, indexing, lambdas, string literals and statements can
   never even tokenize.
2. DURING parsing — a restricted global dict (school-math functions plus the
   constructors SymPy's tokenizer emits) with ``__builtins__`` explicitly
   blanked, because ``eval`` silently injects the full builtins module when
   that key is absent.
3. AROUND every operation — a hard thread-based timeout, because SymPy can
   spin forever on pathological input. A wrong or hung answer to a child is
   the worst failure mode; we always prefer a clean "cannot compute".
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any, Callable, Mapping, Optional, Union

import sympy as sp
from sympy.parsing.sympy_parser import (
    convert_xor,
    implicit_application,
    implicit_multiplication,
    parse_expr,
    standard_transformations,
)

log = logging.getLogger("mathsvc")

# Hard limits — small on purpose; school problems never need more.
MAX_EXPR_LEN = 500
MAX_EXPRS = 8
MAX_SUBSTITUTIONS = 12
OP_TIMEOUT_SECONDS = 5.0


class MathError(Exception):
    """Base for handled failures. str(exc) is relayed to students — keep it child-safe."""


class MathInputError(MathError):
    """The request itself is malformed or disallowed."""


class MathComputeError(MathError):
    """Valid input that SymPy could not (or should not) answer."""


class MathTimeoutError(MathError):
    """Computation exceeded the hard time budget."""


# convert_xor lets kids/LLMs write x^2 for powers; implicit multiplication
# lets them write 2x; implicit application lets them write "sin x". We use
# the two sub-transformations instead of the usual composite
# implicit_multiplication_application because the composite also includes
# split_symbols, which silently rewrites multi-letter names ("speed" ->
# s*p*e*e*d) — a quiet way to give a student a wrong answer.
_TRANSFORMATIONS = standard_transformations + (convert_xor, implicit_multiplication, implicit_application)

# Only the characters school math needs. No quotes, brackets, braces,
# semicolons, newlines, '@', '!', '%', '&', '|', '<', '>' — most Python
# injection vectors die right here.
_ALLOWED_CHARS = re.compile(r"^[A-Za-z0-9_+\-*/^(), .=\t ]+$")

# A dot is only legal as a decimal point. We strip numeric literals first;
# any dot that survives is attribute-access syntax -> reject.
_NUMBER_WITH_DOT = re.compile(r"\d+\.\d+|\d+\.|\.\d+")

# Names that must never appear, even though the restricted parse dict would
# reduce most of them to inert symbols anyway (defense in depth).
_FORBIDDEN_WORDS = re.compile(
    r"\b(lambda|import|exec|eval|compile|open|input|getattr|setattr|delattr|"
    r"globals|locals|vars|dir|type|object|bytes|breakpoint|help|print)\b",
    re.IGNORECASE,
)

_SYMBOL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,15}$")

# Everything parse_expr's compiled code is allowed to resolve by name.
# Symbol/Integer/Float/Rational are required plumbing: the tokenizer rewrites
# numbers and unknown names into calls to them.
_MATH_GLOBALS: dict[str, Any] = {
    "__builtins__": {},
    "Symbol": sp.Symbol,
    "Integer": sp.Integer,
    "Float": sp.Float,
    "Rational": sp.Rational,
    # school functions
    "sqrt": sp.sqrt,
    "root": sp.root,
    "exp": sp.exp,
    "log": sp.log,
    "ln": sp.log,
    "sin": sp.sin,
    "cos": sp.cos,
    "tan": sp.tan,
    "asin": sp.asin,
    "acos": sp.acos,
    "atan": sp.atan,
    "sinh": sp.sinh,
    "cosh": sp.cosh,
    "tanh": sp.tanh,
    "Abs": sp.Abs,
    "abs": sp.Abs,
    "floor": sp.floor,
    "ceiling": sp.ceiling,
    # constants
    "pi": sp.pi,
    "E": sp.E,
}


def validate_text(text: Any, *, field: str = "expression", allow_equals: bool = False) -> str:
    """Reject anything that is not plain school-math text. Returns the stripped string."""
    if not isinstance(text, str):
        raise MathInputError(f"The {field} must be written as text.")
    text = text.strip()
    if not text:
        raise MathInputError(f"The {field} is empty.")
    if len(text) > MAX_EXPR_LEN:
        raise MathInputError(f"That {field} is too long (limit is {MAX_EXPR_LEN} characters).")
    if "__" in text or not _ALLOWED_CHARS.match(text) or _FORBIDDEN_WORDS.search(text):
        raise MathInputError(f"That {field} contains characters or words I can't work with.")
    if "." in _NUMBER_WITH_DOT.sub("", text):
        raise MathInputError("Dots are only allowed inside numbers (like 3.14).")
    equals = text.count("=")
    if equals > 1:
        raise MathInputError("Please use a single '=' sign — I found more than one.")
    if equals and not allow_equals:
        raise MathInputError(f"The {field} must not contain '=' here.")
    return text


def _parse(text: str, local_dict: Optional[Mapping[str, Any]] = None) -> sp.Basic:
    """Parse pre-validated text with the restricted dicts.

    Every parser exception (SyntaxError, TokenError, ...) is collapsed into a
    child-safe MathInputError so tracebacks can never leak to a student.
    """
    try:
        return parse_expr(
            text,
            local_dict=dict(local_dict or {}),
            global_dict=dict(_MATH_GLOBALS),
            transformations=_TRANSFORMATIONS,
            evaluate=True,
        )
    except MathError:
        raise
    except Exception as exc:
        log.info("parse rejected %r: %s", text[:80], type(exc).__name__)
        raise MathInputError("I could not read that expression — please check it and try again.") from exc


def parse_expression(text: Any, *, field: str = "expression") -> sp.Expr:
    """Validate and parse a plain expression (no '=' allowed)."""
    cleaned = validate_text(text, field=field)
    expr = _parse(cleaned)
    if not isinstance(expr, sp.Expr):
        raise MathInputError(f"That {field} is not a mathematical expression I can use.")
    return expr


def parse_equation_or_expression(text: Any) -> Union[sp.Expr, sp.Eq]:
    """Parse "lhs = rhs" into Eq(lhs, rhs), or a bare expression as-is.

    A single '=' is the only equation syntax we accept; '==', '<=' etc. are
    already impossible (multiple '=' rejected, '<' '>' not in the whitelist).
    """
    cleaned = validate_text(text, allow_equals=True)
    if "=" not in cleaned:
        expr = _parse(cleaned)
        if not isinstance(expr, sp.Expr):
            raise MathInputError("That is not a mathematical expression I can use.")
        return expr
    lhs_text, rhs_text = (side.strip() for side in cleaned.split("="))
    if not lhs_text or not rhs_text:
        raise MathInputError("Both sides of the '=' need something on them.")
    lhs, rhs = _parse(lhs_text), _parse(rhs_text)
    if not isinstance(lhs, sp.Expr) or not isinstance(rhs, sp.Expr):
        raise MathInputError("I could not read that equation — please check it.")
    equation = sp.Eq(lhs, rhs)
    # Eq() collapses to a plain boolean for things like "2 = 2" or "1 = 2".
    if equation is sp.S.true:
        raise MathComputeError("That equation is always true — there is nothing to solve.")
    if equation is sp.S.false:
        raise MathComputeError("That equation is never true — there is nothing to solve.")
    return equation


def parse_symbol(name: Any, *, field: str = "variable") -> sp.Symbol:
    """A variable name must be a short plain identifier (x, y, x_1, speed)."""
    if not isinstance(name, str) or "__" in name or not _SYMBOL_NAME.match(name.strip()):
        raise MathInputError(f"That {field} name is not one I can use — try a short name like x.")
    return sp.Symbol(name.strip())


def parse_value_with_units(value: Any, units: Mapping[str, Any], *, field: str = "value") -> sp.Expr:
    """Parse a physics value: a plain number, or text like "1500 kg" / "2 m/s**2".

    Unit names resolve through the fixed `units` map ONLY. Anything left over
    as a free symbol after parsing is an unknown unit -> clean rejection,
    because guessing a unit would mean guessing a physical answer.
    """
    if isinstance(value, bool):
        raise MathInputError(f"The {field} must be a number or a quantity with a unit.")
    if isinstance(value, (int, float)):
        return sp.sympify(value)
    cleaned = validate_text(value, field=field)
    expr = _parse(cleaned, local_dict=units)
    if not isinstance(expr, sp.Expr):
        raise MathInputError(f"That {field} is not a quantity I can use.")
    unknown = sorted(str(s) for s in expr.free_symbols)
    if unknown:
        raise MathInputError(f"I don't know the unit '{unknown[0]}'.")
    return expr


def run_with_timeout(
    fn: Callable[..., Any], *args: Any, timeout: float = OP_TIMEOUT_SECONDS
) -> Any:
    """Run `fn` in a worker thread with a hard deadline.

    WHY threads + abandonment: SymPy has no cooperative cancellation and can
    hang on pathological input (huge polynomial GCDs, hostile integrals).
    CPython cannot kill a thread, so on timeout we abandon the daemon thread
    and answer with a clean failure — a leaked busy thread is a better
    outcome than a hung request or a guessed answer to a child.
    """
    result_box: list[Any] = []
    error_box: list[BaseException] = []

    def _target() -> None:
        try:
            result_box.append(fn(*args))
        except BaseException as exc:  # noqa: BLE001 — re-raised in the caller below
            error_box.append(exc)

    worker = threading.Thread(target=_target, daemon=True, name="mathsvc-op")
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        log.warning("math op timed out after %.1fs; abandoning worker thread", timeout)
        raise MathTimeoutError("That took too long to compute — please try a simpler version.")
    if error_box:
        raise error_box[0]
    return result_box[0]
