"""The fixed operation set for the math service.

WHY a hand-written registry: the LLM orchestrator picks an operation BY NAME
from `OPS` — nothing outside this dict is ever callable, so a hallucinated
op name fails closed instead of reaching Python. Every op takes the raw
request payload (already authenticated) and returns `(result, steps)` where
`steps` is a short list of human-readable method strings the tutor model
narrates from. All failures raise MathError subclasses whose messages are
child-safe; the app layer converts them to `{"ok": false, "error": ...}`.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import sympy as sp
from sympy.physics import units as u
from sympy.physics.units import convert_to

from mathsvc.safety import (
    MAX_EXPRS,
    MAX_SUBSTITUTIONS,
    MathComputeError,
    MathInputError,
    parse_equation_or_expression,
    parse_expression,
    parse_symbol,
    parse_value_with_units,
)

log = logging.getLogger("mathsvc")

Result = Union[str, List[str]]
OpReturn = Tuple[Result, List[str]]

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _fmt(expr: sp.Basic) -> str:
    """Single formatting choke point — sstr gives stable 'x**2 + 1' style output."""
    return sp.sstr(expr)


def _require_expr(payload: dict) -> str:
    expr = payload.get("expr")
    if not isinstance(expr, str):
        raise MathInputError("Missing 'expr' — send the expression as text.")
    return expr


def _pick_var(expr: sp.Basic, requested: Any) -> sp.Symbol:
    """Resolve the working variable, refusing to guess when it is ambiguous.

    WHY strict: silently picking x when the student meant t produces a
    confidently wrong answer — the worst failure mode for a tutor.
    """
    if requested is not None:
        var = parse_symbol(requested)
        if var not in expr.free_symbols:
            raise MathInputError(f"The variable '{var}' does not appear in the expression.")
        return var
    free = sorted(expr.free_symbols, key=lambda s: s.name)
    if len(free) == 1:
        return free[0]
    if not free:
        raise MathInputError("There is no variable in that expression — tell me which one to use.")
    names = ", ".join(s.name for s in free)
    raise MathInputError(f"Please tell me which variable to use — I see: {names}.")


def _as_equation(parsed: Union[sp.Expr, sp.Eq]) -> sp.Eq:
    """An expression without '=' is treated as `expr = 0` (standard convention)."""
    if isinstance(parsed, sp.Eq):
        return parsed
    equation = sp.Eq(parsed, 0)
    if not isinstance(equation, sp.Eq):
        raise MathComputeError("That expression is a constant — there is nothing to solve.")
    return equation


def _parse_substitutions(payload: dict) -> Dict[sp.Symbol, sp.Expr]:
    values = payload.get("values")
    if not isinstance(values, dict) or not values:
        raise MathInputError("Missing 'values' — send them like {\"x\": 3}.")
    if len(values) > MAX_SUBSTITUTIONS:
        raise MathInputError(f"Too many values — the limit is {MAX_SUBSTITUTIONS}.")
    mapping: Dict[sp.Symbol, sp.Expr] = {}
    for name, value in values.items():
        symbol = parse_symbol(name)
        if isinstance(value, bool):
            raise MathInputError(f"The value for '{symbol}' must be a number or expression.")
        if isinstance(value, (int, float)):
            mapping[symbol] = sp.sympify(value)
        else:
            mapping[symbol] = parse_expression(value, field=f"value for '{symbol}'")
    return mapping


# ---------------------------------------------------------------------------
# algebra
# ---------------------------------------------------------------------------


def op_solve(payload: dict) -> OpReturn:
    """Solve one equation for one variable."""
    equation = _as_equation(parse_equation_or_expression(_require_expr(payload)))
    var = _pick_var(equation, payload.get("var"))
    steps = [f"Equation: {_fmt(equation.lhs)} = {_fmt(equation.rhs)}"]

    # A factoring step for polynomials — the narration the tutor model wants.
    residual = sp.expand(equation.lhs - equation.rhs)
    if residual.is_polynomial(var):
        factored = sp.factor(residual)
        if factored != residual and factored.is_Mul:
            steps.append(f"Factor: {_fmt(factored)} = 0")

    try:
        solutions = sp.solve(equation, var)
    except Exception as exc:
        log.info("solve failed for %s: %s", _fmt(equation), type(exc).__name__)
        raise MathComputeError("I could not solve this equation.") from exc
    if not isinstance(solutions, list):
        solutions = [solutions]
    if not solutions:
        raise MathComputeError("I could not find any solutions for this equation.")
    solutions = sorted(solutions, key=sp.default_sort_key)
    steps.append("Solutions: " + ", ".join(f"{var} = {_fmt(s)}" for s in solutions))
    return [_fmt(s) for s in solutions], steps


def op_solve_system(payload: dict) -> OpReturn:
    """Solve a system of equations."""
    exprs = payload.get("exprs")
    if not isinstance(exprs, list) or len(exprs) < 2:
        raise MathInputError("Send 'exprs' as a list of at least two equations.")
    if len(exprs) > MAX_EXPRS:
        raise MathInputError(f"Too many equations — the limit is {MAX_EXPRS}.")
    equations = [_as_equation(parse_equation_or_expression(e)) for e in exprs]

    requested = payload.get("vars")
    if requested is not None:
        if not isinstance(requested, list) or not requested:
            raise MathInputError("Send 'vars' as a list of variable names.")
        variables = [parse_symbol(v) for v in requested]
    else:
        seen: set = set()
        for eq in equations:
            seen |= eq.free_symbols
        variables = sorted(seen, key=lambda s: s.name)
    if not variables:
        raise MathInputError("There are no variables in those equations.")

    try:
        solutions = sp.solve(equations, variables, dict=True)
    except Exception as exc:
        log.info("solve_system failed: %s", type(exc).__name__)
        raise MathComputeError("I could not solve this system of equations.") from exc
    if not solutions:
        raise MathComputeError("I could not find a solution for this system of equations.")

    rendered = [
        ", ".join(f"{v} = {_fmt(sol[v])}" for v in variables if v in sol) for sol in solutions
    ]
    steps = [
        f"System of {len(equations)} equations with unknowns: "
        + ", ".join(v.name for v in variables)
    ]
    for line in rendered:
        steps.append(f"Solution: {line}")
    return rendered, steps


def _rewrite_op(verb: str, fn: Callable[[sp.Expr], sp.Expr]) -> Callable[[dict], OpReturn]:
    """simplify/factor/expand share one shape: parse, rewrite, report."""

    def _op(payload: dict) -> OpReturn:
        expr = parse_expression(_require_expr(payload))
        rewritten = fn(expr)
        steps = [f"{verb}: {_fmt(expr)}  ->  {_fmt(rewritten)}"]
        return _fmt(rewritten), steps

    _op.__doc__ = f"{verb} an expression."
    return _op


op_simplify = _rewrite_op("Simplify", sp.simplify)
op_factor = _rewrite_op("Factor", sp.factor)
op_expand = _rewrite_op("Expand", sp.expand)


# ---------------------------------------------------------------------------
# calculus
# ---------------------------------------------------------------------------


def op_differentiate(payload: dict) -> OpReturn:
    """Differentiate an expression, optionally to a higher order."""
    expr = parse_expression(_require_expr(payload))
    var = _pick_var(expr, payload.get("var"))
    order = payload.get("order", 1)
    if isinstance(order, bool) or not isinstance(order, int) or not 1 <= order <= 8:
        raise MathInputError("The 'order' must be a whole number from 1 to 8.")
    derivative = sp.diff(expr, var, order)
    times = "" if order == 1 else f" ({order} times)"
    steps = [
        f"Differentiate {_fmt(expr)} with respect to {var}{times}",
        f"Result: {_fmt(derivative)}",
    ]
    return _fmt(derivative), steps


def op_integrate(payload: dict) -> OpReturn:
    """Integrate an expression, indefinitely or over given bounds."""
    expr = parse_expression(_require_expr(payload))
    var = _pick_var(expr, payload.get("var"))

    definite = payload.get("definite")
    if definite is not None:
        if not isinstance(definite, dict) or "from" not in definite or "to" not in definite:
            raise MathInputError("Send 'definite' as {\"from\": ..., \"to\": ...}.")
        bounds = []
        for key in ("from", "to"):
            raw = definite[key]
            if isinstance(raw, bool):
                raise MathInputError(f"The '{key}' bound must be a number.")
            bound = sp.sympify(raw) if isinstance(raw, (int, float)) else parse_expression(raw, field=f"'{key}' bound")
            if bound.free_symbols:
                raise MathInputError(f"The '{key}' bound must be a constant number.")
            bounds.append(bound)
        result = sp.integrate(expr, (var, bounds[0], bounds[1]))
        steps = [f"Integrate {_fmt(expr)} with respect to {var} from {_fmt(bounds[0])} to {_fmt(bounds[1])}"]
    else:
        result = sp.integrate(expr, var)
        steps = [f"Integrate {_fmt(expr)} with respect to {var}"]

    if result.atoms(sp.Integral):
        raise MathComputeError("I could not work out this integral.")
    if definite is not None:
        steps.append(f"Result: {_fmt(result)}")
    else:
        steps.append(f"Antiderivative: {_fmt(result)} + C")
    return _fmt(result), steps


# ---------------------------------------------------------------------------
# numeric
# ---------------------------------------------------------------------------


def op_evaluate(payload: dict) -> OpReturn:
    """Numerically evaluate a constant expression."""
    expr = parse_expression(_require_expr(payload))
    precision = payload.get("precision", 6)
    if isinstance(precision, bool) or not isinstance(precision, int) or not 1 <= precision <= 50:
        raise MathInputError("The 'precision' must be a whole number from 1 to 50.")
    if expr.free_symbols:
        names = ", ".join(sorted(s.name for s in expr.free_symbols))
        raise MathComputeError(
            f"That expression still has unknowns ({names}) — give me their values, or use 'substitute'."
        )
    value = expr.evalf(precision)
    if value.has(sp.nan, sp.zoo, sp.oo, -sp.oo):
        raise MathComputeError("That value is not a normal number — it may be undefined or infinite.")
    steps = [f"Evaluate {_fmt(expr)} to {precision} significant digits", f"Value: {_fmt(value)}"]
    return _fmt(value), steps


def op_substitute(payload: dict) -> OpReturn:
    """Substitute values into an expression, then simplify (and approximate)."""
    expr = parse_expression(_require_expr(payload))
    mapping = _parse_substitutions(payload)
    # simultaneous=True substitutes all values in one pass — sequential subs
    # can corrupt later replacement values (see physics_eval for the unit case).
    substituted = expr.subs(mapping, simultaneous=True)
    simplified = sp.simplify(substituted)
    sub_text = ", ".join(f"{k} = {_fmt(v)}" for k, v in sorted(mapping.items(), key=lambda kv: kv[0].name))
    steps = [f"Substitute {sub_text} into {_fmt(expr)}", f"Result: {_fmt(simplified)}"]
    if not simplified.free_symbols:
        if simplified.has(sp.nan, sp.zoo, sp.oo, -sp.oo):
            raise MathComputeError("That value is not a normal number — it may be undefined or infinite.")
        approx = simplified.evalf(6)
        if _fmt(approx) != _fmt(simplified):
            steps.append(f"Approximately: {_fmt(approx)}")
    return _fmt(simplified), steps


# ---------------------------------------------------------------------------
# physics (unit-aware)
# ---------------------------------------------------------------------------

# Fixed name -> unit map. ONLY these resolve; an unknown unit is a clean
# rejection, never a guess (a wrong unit is a wrong physical answer).
UNITS: Dict[str, Any] = {
    # mass
    "kg": u.kilogram, "kilogram": u.kilogram, "kilograms": u.kilogram,
    "g": u.gram, "gram": u.gram, "grams": u.gram, "mg": u.milligram,
    # length
    "m": u.meter, "meter": u.meter, "meters": u.meter, "metre": u.meter, "metres": u.meter,
    "cm": u.centimeter, "mm": u.millimeter, "km": u.kilometer,
    # time
    "s": u.second, "second": u.second, "seconds": u.second,
    "min": u.minute, "minute": u.minute, "minutes": u.minute,
    "hour": u.hour, "hours": u.hour, "h": u.hour,
    # force / energy / power / pressure
    "N": u.newton, "newton": u.newton, "newtons": u.newton,
    "J": u.joule, "joule": u.joule, "joules": u.joule,
    "W": u.watt, "watt": u.watt, "watts": u.watt,
    "Pa": u.pascal, "pascal": u.pascal, "pascals": u.pascal,
    # electricity
    "V": u.volt, "volt": u.volt, "volts": u.volt,
    "A": u.ampere, "ampere": u.ampere, "amperes": u.ampere, "amp": u.ampere, "amps": u.ampere,
    "ohm": u.ohm, "ohms": u.ohm,
    "C": u.coulomb, "coulomb": u.coulomb, "coulombs": u.coulomb,
    # frequency / amount / temperature
    "Hz": u.hertz, "hertz": u.hertz,
    "mol": u.mole, "mole": u.mole, "moles": u.mole,
    "K": u.kelvin, "kelvin": u.kelvin,
}

# When no target_unit is given, try to express the answer in a named unit a
# student recognizes ("3000 newton", not "3000 kilogram*meter/second**2").
_PREFERRED_UNITS = (u.newton, u.joule, u.watt, u.pascal, u.volt, u.ohm, u.coulomb, u.hertz)
_SI_BASE = (u.kilogram, u.meter, u.second, u.ampere, u.kelvin, u.mole, u.candela)


def _format_quantity(qty: sp.Expr) -> str:
    """Render '3000*newton' as '3000 newton' (numbers rounded to 6 sig figs)."""
    quantities = qty.atoms(u.Quantity)
    if not quantities:
        number = qty if qty.is_Integer or qty.is_Rational else qty.evalf(6)
        return _fmt(number)
    number, unit_part = qty.as_independent(*quantities)
    if not number.is_Integer:
        number = sp.Float(number.evalf(15), 6)
    return f"{_fmt(number)} {_fmt(unit_part)}".strip()


def _auto_convert(qty: sp.Expr, target: Optional[sp.Expr]) -> sp.Expr:
    """Convert to the requested unit, or the friendliest named unit we can find."""
    if not qty.atoms(u.Quantity):
        if target is not None:
            raise MathComputeError("That result has no units, so I cannot convert it.")
        return qty
    if target is not None:
        converted = convert_to(qty, target)
        # convert_to silently returns leftovers when dimensions don't match
        # (kg -> m stays kg). Failing loud beats a subtly wrong unit.
        if converted.atoms(u.Quantity) - target.atoms(u.Quantity):
            raise MathComputeError("I cannot convert this result to that unit — the types don't match.")
        return converted
    for candidate in _PREFERRED_UNITS:
        converted = convert_to(qty, candidate)
        if not (converted / candidate).atoms(u.Quantity):
            return converted
    return convert_to(qty, list(_SI_BASE))


def op_physics_eval(payload: dict) -> OpReturn:
    """Unit-aware evaluation: plug quantities into a formula, mind the units.

    Accepts either a bare expression ("m*a" with every symbol given a value)
    or a formula with one unknown ("F = m*a" with m and a given -> solves F).
    """
    values = payload.get("values")
    if not isinstance(values, dict) or not values:
        raise MathInputError("Missing 'values' — send them like {\"m\": \"1500 kg\"}.")
    if len(values) > MAX_SUBSTITUTIONS:
        raise MathInputError(f"Too many values — the limit is {MAX_SUBSTITUTIONS}.")
    mapping: Dict[sp.Symbol, sp.Expr] = {}
    for name, raw in values.items():
        symbol = parse_symbol(name)
        mapping[symbol] = parse_value_with_units(raw, UNITS, field=f"value for '{symbol}'")

    target: Optional[sp.Expr] = None
    if payload.get("target_unit") is not None:
        target = parse_value_with_units(payload["target_unit"], UNITS, field="target unit")

    parsed = parse_equation_or_expression(_require_expr(payload))
    # simultaneous=True is load-bearing: sequential subs walks INTO already
    # inserted Quantity objects and can rewrite their internals when a user
    # symbol collides with a unit abbreviation (m/meter, s/second) — that
    # silently breaks unit conversion afterwards.
    substituted = parsed.subs(mapping, simultaneous=True)
    sub_text = ", ".join(
        f"{k} = {_format_quantity(v)}" for k, v in sorted(mapping.items(), key=lambda kv: kv[0].name)
    )

    if isinstance(parsed, sp.Eq):
        unknowns = sorted(substituted.free_symbols, key=lambda s: s.name)
        if len(unknowns) != 1:
            if not unknowns:
                raise MathInputError(
                    "Everything in that formula already has a value — leave exactly one unknown, "
                    "or send the expression without '='."
                )
            names = ", ".join(s.name for s in unknowns)
            raise MathInputError(f"Give me values for all but one unknown — still missing: {names}.")
        unknown = unknowns[0]
        try:
            solutions = sp.solve(substituted, unknown)
        except Exception as exc:
            log.info("physics_eval solve failed: %s", type(exc).__name__)
            raise MathComputeError("I could not solve this formula.") from exc
        if not solutions:
            raise MathComputeError("I could not solve this formula.")
        quantity = _auto_convert(solutions[0], target)
        rendered = _format_quantity(quantity)
        steps = [
            f"Formula: {_fmt(parsed.lhs)} = {_fmt(parsed.rhs)}",
            f"Substitute: {sub_text}",
            f"{unknown} = {rendered}",
        ]
        return rendered, steps

    missing = sorted(substituted.free_symbols, key=lambda s: s.name)
    if missing:
        names = ", ".join(s.name for s in missing)
        raise MathInputError(f"I still need values for: {names}.")
    quantity = _auto_convert(substituted, target)
    rendered = _format_quantity(quantity)
    steps = [
        f"Expression: {_fmt(parsed)}",
        f"Substitute: {sub_text}",
        f"Result: {rendered}",
    ]
    return rendered, steps


# ---------------------------------------------------------------------------
# registry — the ONLY operations this service will ever run
# ---------------------------------------------------------------------------

OPS: Dict[str, Callable[[dict], OpReturn]] = {
    "solve": op_solve,
    "solve_system": op_solve_system,
    "simplify": op_simplify,
    "factor": op_factor,
    "expand": op_expand,
    "differentiate": op_differentiate,
    "integrate": op_integrate,
    "evaluate": op_evaluate,
    "substitute": op_substitute,
    "physics_eval": op_physics_eval,
}
