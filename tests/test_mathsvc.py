"""Tests for the constrained SymPy math microservice (mathsvc/).

Run from the repo root: python -m pytest tests/test_mathsvc.py -q
"""

from __future__ import annotations

import os

# Must be set before the first authenticated request; the app reads it
# per-request, so setting it at import time keeps every test deterministic.
os.environ.setdefault("MATH_SVC_TOKEN", "test-math-token")

from fastapi.testclient import TestClient

from mathsvc.app import app

TOKEN = os.environ["MATH_SVC_TOKEN"]

svc = TestClient(app)


def post(body: dict, token: str | None = TOKEN):
    headers = {"x-math-token": token} if token is not None else {}
    return svc.post("/math", json=body, headers=headers)


def ok_result(body: dict):
    """POST and assert the happy path shape; returns (result, steps)."""
    resp = post(body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True, f"expected ok:true, got {data}"
    assert data["op"] == body["op"]
    assert isinstance(data["steps"], list)
    return data["result"], data["steps"]


def clean_error(body: dict) -> str:
    """POST and assert the handled-failure shape; returns the error string."""
    resp = post(body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False, f"expected ok:false, got {data}"
    error = data["error"]
    assert isinstance(error, str) and error
    # Child-safe contract: no internals ever leak.
    assert "Traceback" not in error
    assert "sympy" not in error.lower()
    return error


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


def test_health_is_open():
    resp = svc.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_missing_token_rejected():
    resp = post({"op": "solve", "expr": "x - 1"}, token=None)
    assert resp.status_code == 401


def test_wrong_token_rejected():
    resp = post({"op": "solve", "expr": "x - 1"}, token="not-the-token")
    assert resp.status_code == 401


def test_unconfigured_service_returns_503(monkeypatch):
    monkeypatch.delenv("MATH_SVC_TOKEN", raising=False)
    resp = post({"op": "solve", "expr": "x - 1"})
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# grade-10 algebra
# ---------------------------------------------------------------------------


def test_solve_quadratic():
    result, steps = ok_result({"op": "solve", "expr": "x**2 - 5*x + 6 = 0"})
    assert set(result) == {"2", "3"}
    assert any("Factor" in s for s in steps)


def test_solve_quadratic_without_equals_means_equals_zero():
    result, _ = ok_result({"op": "solve", "expr": "x**2 - 5*x + 6"})
    assert set(result) == {"2", "3"}


def test_solve_linear():
    result, _ = ok_result({"op": "solve", "expr": "2*x + 4 = 10"})
    assert result == ["3"]


def test_solve_ambiguous_variable_is_rejected():
    error = clean_error({"op": "solve", "expr": "x + y = 2"})
    assert "which variable" in error


def test_solve_system_two_equations():
    result, _ = ok_result({"op": "solve_system", "exprs": ["x + y = 5", "x - y = 1"]})
    assert len(result) == 1
    assert "x = 3" in result[0]
    assert "y = 2" in result[0]


# ---------------------------------------------------------------------------
# calculus
# ---------------------------------------------------------------------------


def test_differentiate_cubic():
    result, _ = ok_result({"op": "differentiate", "expr": "x**3"})
    assert result == "3*x**2"


def test_integrate_linear():
    result, _ = ok_result({"op": "integrate", "expr": "2*x"})
    assert result == "x**2"


def test_definite_integral():
    result, _ = ok_result(
        {"op": "integrate", "expr": "2*x", "definite": {"from": 0, "to": 3}}
    )
    assert result == "9"


# ---------------------------------------------------------------------------
# numeric ops
# ---------------------------------------------------------------------------


def test_evaluate_fraction():
    result, _ = ok_result({"op": "evaluate", "expr": "2/7"})
    assert result == "0.285714"


def test_substitute_values():
    result, _ = ok_result(
        {"op": "substitute", "expr": "x**2 + y", "values": {"x": 3, "y": 1}}
    )
    assert result == "10"


# ---------------------------------------------------------------------------
# physics with units
# ---------------------------------------------------------------------------


def test_physics_force_from_mass_and_acceleration():
    result, steps = ok_result(
        {
            "op": "physics_eval",
            "expr": "F = m*a",
            "values": {"m": "1500 kg", "a": "2 m/s**2"},
        }
    )
    assert "3000" in result
    assert "newton" in result
    assert any("Substitute" in s for s in steps)


def test_physics_unit_conversion_km_to_m():
    result, _ = ok_result(
        {"op": "physics_eval", "expr": "d", "values": {"d": "2 km"}, "target_unit": "m"}
    )
    assert "2000" in result
    assert "meter" in result


def test_physics_unknown_unit_rejected():
    error = clean_error(
        {"op": "physics_eval", "expr": "m*a", "values": {"m": "5 blorg", "a": 2}}
    )
    assert "blorg" in error


def test_physics_incompatible_conversion_rejected():
    clean_error(
        {"op": "physics_eval", "expr": "d", "values": {"d": "2 kg"}, "target_unit": "m"}
    )


# ---------------------------------------------------------------------------
# safety: whitelist + validation
# ---------------------------------------------------------------------------


def test_unknown_op_rejected():
    error = clean_error({"op": "os_system", "expr": "x"})
    assert "operation" in error


def test_dunder_rejected():
    clean_error({"op": "simplify", "expr": "x.__class__"})


def test_import_rejected():
    clean_error({"op": "simplify", "expr": "import os"})


def test_bracket_indexing_rejected():
    clean_error({"op": "simplify", "expr": "x[0]"})


def test_lambda_rejected():
    clean_error({"op": "simplify", "expr": "lambda x: x"})


def test_quotes_rejected():
    clean_error({"op": "simplify", "expr": "'abc' + x"})


def test_attribute_dot_rejected_but_decimals_pass():
    clean_error({"op": "simplify", "expr": "x.imag"})
    result, _ = ok_result({"op": "evaluate", "expr": "1.5 + 2.5"})
    assert result.startswith("4")


def test_absurdly_long_expression_rejected():
    error = clean_error({"op": "simplify", "expr": "x+" * 300 + "x"})
    assert "too long" in error


def test_double_equals_rejected():
    error = clean_error({"op": "solve", "expr": "x = 2 = 3"})
    assert "=" in error


# ---------------------------------------------------------------------------
# fallback: never a guess, never a traceback
# ---------------------------------------------------------------------------


def test_unparseable_expression_falls_back_cleanly():
    error = clean_error({"op": "solve", "expr": ")(x +"})
    assert "could not read" in error


def test_unsolvable_equation_falls_back_cleanly():
    # sympy.solve raises NotImplementedError for mixed transcendental equations;
    # the student must see a clean message, not a guess.
    clean_error({"op": "solve", "expr": "tan(x) = x"})
