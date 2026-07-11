"""FastAPI entrypoint for the constrained SymPy math microservice.

Run locally from the repo root:
    uvicorn mathsvc.app:app --port 8090

The Next.js AI-tutor orchestrator is the only caller: the LLM emits a
structured tool call (op name + expression strings) and this service parses
and computes with SymPy. No model-generated code ever executes here — only
the whitelisted operations in mathsvc/ops.py on inputs vetted by
mathsvc/safety.py. Handled requests ALWAYS return HTTP 200 with
{"ok": true|false, ...}; error strings are child-safe and never contain
tracebacks, because they may be read out loud to a student.
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Optional

from fastapi import Body, FastAPI, Header, HTTPException

from mathsvc.ops import OPS
from mathsvc.safety import MathError, run_with_timeout

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mathsvc")

app = FastAPI(title="SketchCast Math Service", docs_url=None, redoc_url=None)

# Generic fallback for bugs on our side. Deliberately vague: the true error is
# in the server log, and a child should never see internals.
_FALLBACK_ERROR = "Sorry — I could not compute that. Please try rephrasing the math."


@app.get("/health")
def health() -> dict:
    """Unauthenticated liveness probe (Railway health checks)."""
    return {"ok": True}


@app.post("/math")
def compute(
    payload: dict = Body(...),
    x_math_token: Optional[str] = Header(default=None),
) -> dict:
    """Run one whitelisted math operation.

    Body: {"op": "<name>", ...op-specific fields}. See mathsvc/README.md.
    """
    expected = os.getenv("MATH_SVC_TOKEN", "")
    if not expected:
        # Fail closed and loudly if the deployment is missing its secret —
        # better an obvious 503 than an accidentally open math endpoint.
        raise HTTPException(status_code=503, detail="math service is not configured")
    provided = x_math_token or ""
    # compare_digest on bytes: constant-time, and immune to non-ASCII header noise.
    if not hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail="unauthorized")

    op = payload.get("op")
    if not isinstance(op, str) or op not in OPS:
        return {
            "ok": False,
            "error": "I don't know that operation. I can do: " + ", ".join(sorted(OPS)) + ".",
        }

    try:
        result, steps = run_with_timeout(OPS[op], payload)
    except MathError as exc:
        # Handled failure: message is child-safe by contract (see safety.py).
        log.info("op %s rejected: %s", op, exc)
        return {"ok": False, "op": op, "error": str(exc)}
    except Exception:
        # Unhandled bug: full traceback to the server log ONLY.
        log.exception("op %s failed unexpectedly", op)
        return {"ok": False, "op": op, "error": _FALLBACK_ERROR}

    return {"ok": True, "op": op, "result": result, "steps": steps}
