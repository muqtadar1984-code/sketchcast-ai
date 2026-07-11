# mathsvc — constrained SymPy math microservice

A tiny FastAPI service the Next.js AI-tutor orchestrator calls to **compute and
verify math for students**. The LLM emits a structured tool call (`op` +
expression strings); this service parses and computes with SymPy.

**No model-generated code is ever executed.** Only the whitelisted operations
in `ops.py` run, on expressions vetted by `safety.py` (character whitelist, no
attribute access / indexing / lambdas / quotes, restricted parse dict, blanked
builtins, 500-char cap, 5s hard timeout). A wrong answer to a child is the
worst failure mode: when in doubt the service returns a clean
`{"ok": false, "error": "<child-safe reason>"}` — never a guess, never a
traceback.

## API

- `GET /health` → `{"ok": true}` (no auth — Railway health checks)
- `POST /math` — header `x-math-token: $MATH_SVC_TOKEN` required
  (401 if wrong, 503 if the env var is unset on the server).
  Handled requests always return HTTP 200:

```json
{"ok": true,  "op": "solve", "result": ["2", "3"], "steps": ["Equation: ...", "Factor: ...", "Solutions: ..."]}
{"ok": false, "op": "solve", "error": "I could not solve this equation."}
```

`steps` is a short list of human-readable method strings — the tutor model
narrates from these.

### Operations

| op | fields | example |
|---|---|---|
| `solve` | `expr`, `var?` | `{"op":"solve","expr":"x**2 - 5*x + 6 = 0"}` → `["2","3"]` |
| `solve_system` | `exprs: []`, `vars?: []` | `{"op":"solve_system","exprs":["x + y = 5","x - y = 1"]}` |
| `simplify` / `factor` / `expand` | `expr` | `{"op":"factor","expr":"x**2 - 4"}` |
| `differentiate` | `expr`, `var?`, `order?=1` | `{"op":"differentiate","expr":"x**3"}` → `3*x**2` |
| `integrate` | `expr`, `var?`, `definite? {"from","to"}` | `{"op":"integrate","expr":"2*x"}` → `x**2` |
| `evaluate` | `expr`, `precision?=6` | `{"op":"evaluate","expr":"2/7"}` → `0.285714` |
| `substitute` | `expr`, `values: {sym: value}` | `{"op":"substitute","expr":"x**2 + y","values":{"x":3,"y":1}}` |
| `physics_eval` | `expr`, `values: {sym: "1500 kg"}`, `target_unit?` | `{"op":"physics_eval","expr":"F = m*a","values":{"m":"1500 kg","a":"2 m/s**2"}}` → `"3000 newton"` |

Notation: `=` (single) for equations, `**` or `^` for powers, implicit
multiplication (`2x`) accepted. `physics_eval` accepts a bare expression
(every symbol valued) or a formula with exactly one unknown ("F = m*a" with
`m`, `a` given solves for `F`). Supported units: kg g mg, m cm mm km, s min
hour, N J W Pa, V A ohm C, Hz mol K (plus full names) — unknown units are
rejected, never guessed. `target_unit` converts the result (e.g. `"m"` turns
`2 km` into `2000 meter`).

## Run locally

From the **repo root** (imports are `mathsvc.*`):

```bash
pip install -r mathsvc/requirements.txt
set MATH_SVC_TOKEN=dev-secret        # PowerShell: $env:MATH_SVC_TOKEN = "dev-secret"
uvicorn mathsvc.app:app --port 8090
```

```bash
curl -X POST http://localhost:8090/math -H "content-type: application/json" \
  -H "x-math-token: dev-secret" \
  -d '{"op":"solve","expr":"x**2 - 5*x + 6 = 0"}'
```

Tests (from the repo root): `python -m pytest tests/test_mathsvc.py -q`

## Deploy — second Railway service from this repo

Same pattern the worker already uses: `nixpacks.toml`'s `[start]` cmd is a
default that each Railway service **overrides in the dashboard** — do not
edit it.

1. Railway project → **New service → GitHub repo** → pick this repo
   (leave Root Directory as the repo root — the code imports `mathsvc.*`).
2. Service → **Settings → Deploy → Custom Start Command**:

   ```
   pip install -r mathsvc/requirements.txt && uvicorn mathsvc.app:app --host 0.0.0.0 --port $PORT
   ```

   (The build still installs the repo-root `requirements.txt`; the extra
   `pip install` adds sympy, which is deliberately NOT in the worker's
   requirements.)
3. Service → **Variables**: set `MATH_SVC_TOKEN` to a long random secret
   (e.g. `openssl rand -hex 32`). The service returns 503 until it is set.
4. Settings → **Networking → Generate Domain** (or a private domain if only
   the Next.js app calls it), and optionally point the health check at
   `/health`.
5. In the Next.js app (Vercel), set the matching env vars, e.g.
   `MATH_SVC_URL=https://<service>.up.railway.app` and
   `MATH_SVC_TOKEN=<same secret>`, and send the token as the `x-math-token`
   header on every `POST /math`.

### Env

| var | required | purpose |
|---|---|---|
| `MATH_SVC_TOKEN` | yes | shared secret; requests without it are rejected (constant-time compare) |
| `PORT` | set by Railway | listen port for uvicorn |
