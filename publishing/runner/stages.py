"""The stage registry: what a run of each stage executes and what it leaves behind.

Data, not a pipeline: the cost plan merges and drops stages, so the app and the
runner read this list instead of hard-coding the order. The app keeps the same
keys in src/utils/books/stages.ts (S2 moves both onto one table).

A stage's command runs with the book folder at
    <PUBLISH_OUT>/manuscripts/<slug>/
and writes its own outputs into
    <PUBLISH_OUT>/manuscripts/<slug>/runs/run-<id>/
which the runner uploads and lists as the run's files.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class Stage:
    key: str
    title: str
    # argv for the stage, from (slug, params, run_rel) — run_rel is "runs/run-<id>"
    argv: Callable[[str, dict, str], list[str]]
    # extra environment for the command
    env: Callable[[str, dict, str], dict] = lambda slug, params, run_rel: {}
    # (usd, cost lines, summary) read from the run's output folder
    collect: Callable[[Path], tuple[float, list[dict], dict]] = lambda out: (0.0, [], {})
    uses_models: bool = False
    writes_source: bool = False        # S1 stages only read the book; a stage that edits it comes in S3
    needs_assets: bool = False         # pulls the shared book-assets/ (the book's own pictures) as well
    needs_source: bool = True          # pulls the book's source set (a book not drafted yet has none)
    uses_pubdb: bool = False           # restores the shared publishing database (publishing/runner/pubdb.py)
    writes_pubdb: bool = False         # … and pushes it back afterwards; one such run at a time
    step: str = ""
    params: dict = field(default_factory=dict)   # name → default, validated before the run starts


def _chapters(params: dict) -> tuple[int, int]:
    first, last = int(params.get("first", 1)), int(params.get("last", 10))
    if not (1 <= first <= last <= 99):
        raise ValueError(f"chapters must satisfy 1 ≤ first ≤ last ≤ 99, got {first}–{last}")
    return first, last


def _coverage_collect(out: Path) -> tuple[float, list[dict], dict]:
    chapters = json.loads((out / "coverage.json").read_text(encoding="utf-8"))
    matrix = json.loads((out / "coverage-matrix.json").read_text(encoding="utf-8"))
    lines = [{"chapter": int(n), "role": "validators", "model": ", ".join(c.get("validators") or {}),
              "usd": round(float(c.get("usd") or 0), 4)} for n, c in chapters.items()]
    counts: dict[str, int] = {}
    for m in matrix.values():
        counts[m["status"]] = counts.get(m["status"], 0) + 1
    return round(sum(x["usd"] for x in lines), 4), lines, {"counts": counts, "objectives": len(matrix)}


def _audit_collect(out: Path) -> tuple[float, list[dict], dict]:
    """One cost line per chapter and validator; the summary counts findings by severity and agreement."""
    lines, totals = [], {"P0": 0, "P1": 0, "P2": 0, "confirmed": 0, "single": 0}
    for f in sorted((out / "audit").glob("ch[0-9][0-9]-audit.json")):
        rec = json.loads(f.read_text(encoding="utf-8"))
        for model, c in (rec.get("validators") or {}).items():
            lines.append({"chapter": int(rec["chapter"]), "role": "validator", "model": model,
                          "usd": round(float(c.get("usd") or 0), 4),
                          "input_tokens": c.get("in"), "output_tokens": c.get("out")})
        for fd in rec.get("findings") or []:
            if fd.get("severity") in ("P0", "P1", "P2"):
                totals[fd["severity"]] += 1
            totals["confirmed" if fd.get("agreement") == "CONFIRMED" else "single"] += 1
    chapters = len({ln["chapter"] for ln in lines})
    return round(sum(ln["usd"] for ln in lines), 4), lines, {"chapters": chapters, "findings": totals}


def _proof_collect(out: Path) -> tuple[float, list[dict], dict]:
    proof = json.loads((out / "proof.json").read_text(encoding="utf-8"))
    return 0.0, [], {"chapter": proof["chapter"],
                     "renders": [{k: r[k] for k in ("font", "pages", "hard_defects")} for r in proof["renders"]]}


def _readiness_collect(out: Path) -> tuple[float, list[dict], dict]:
    r = json.loads((out / "readiness.json").read_text(encoding="utf-8"))
    return 0.0, [], {k: r.get(k) for k in ("pct", "ready", "outstanding_by_state", "gaps", "curriculum")}


def _gate_r_collect(out: Path) -> tuple[float, list[dict], dict]:
    r = json.loads((out / "gate_r.json").read_text(encoding="utf-8"))
    keep = ("passed", "pct", "pins", "edition_id", "policy", "gaps", "curriculum")
    return 0.0, [], {k: r[k] for k in keep if k in r}


def _draft_collect(out: Path) -> tuple[float, list[dict], dict]:
    """One cost line per model and step kind, from the draft's own ledger (`costs` in chNN-draft.json)."""
    summary = json.loads((out / "draft.json").read_text(encoding="utf-8"))
    doc = json.loads((out / f"ch{int(summary['chapter']):02d}-draft.json").read_text(encoding="utf-8"))
    by: dict[tuple[str, str], dict] = {}
    for c in doc.get("costs") or []:
        k = (str(c.get("stage") or "?"), str(c.get("model") or "?"))
        ln = by.setdefault(k, {"chapter": int(summary["chapter"]), "role": k[0], "model": k[1], "usd": 0.0,
                               "input_tokens": 0, "output_tokens": 0})
        ln["usd"] += float(c.get("usd") or 0)
        ln["input_tokens"] += int(c.get("in") or 0)
        ln["output_tokens"] += int(c.get("out") or 0)
    lines = [{**ln, "usd": round(ln["usd"], 4)} for ln in by.values()]
    return round(sum(ln["usd"] for ln in lines), 4), lines, summary


def _check_collect(out: Path) -> tuple[float, list[dict], dict]:
    return 0.0, [], json.loads((out / "check.json").read_text(encoding="utf-8"))["summary"]


STAGES: dict[str, Stage] = {s.key: s for s in [
    Stage(
        key="check_files", title="Check the book's files", step="check",
        # free: proves the source set came back whole and every approved chapter still matches its stamp
        argv=lambda slug, p, rel: [sys.executable, "-m", "publishing.runner.check", slug, rel],
        collect=_check_collect,
    ),
    Stage(
        key="coverage", title="Coverage re-check", step="coverage", uses_models=True,
        params={"first": 1, "last": 10},
        argv=lambda slug, p, rel: [sys.executable, "-m", "publishing.tools.coverage", slug,
                                   *map(str, _chapters(p)), "--env-file", "__RUNNER_EMPTY_ENV__"],
        # the tool writes into <book>/<PUBLISH_REV>/ — point it at this run's folder
        env=lambda slug, p, rel: {"PUBLISH_REV": rel},
        collect=_coverage_collect,
    ),
    Stage(
        key="academic_audit", title="Academic audit", step="audit", uses_models=True,
        params={"first": 1, "last": 10},
        # reads the book and the curriculum (production, GET only); writes <run>/audit/chNN-audit.json + summary.json
        argv=lambda slug, p, rel: [sys.executable, "-m", "publishing.tools.academic_audit", slug,
                                   *map(str, _chapters(p)), "--env-file", "__RUNNER_EMPTY_ENV__"],
        env=lambda slug, p, rel: {"PUBLISH_REV": rel},
        collect=_audit_collect,
    ),
    Stage(
        key="render_proof", title="Font proof (render one chapter)", step="render", needs_assets=True,
        params={"chapter": 1},
        # free: renders the chapter on Linux once per candidate heading font (founder D3: Sitka is Windows-only)
        argv=lambda slug, p, rel: [sys.executable, "-m", "publishing.runner.render_proof", slug, rel, str(int(p["chapter"]))],
        collect=_proof_collect,
    ),
    Stage(
        key="readiness", title="Readiness re-check", step="audit", needs_source=False, uses_pubdb=True,
        writes_pubdb=True,
        # free: a fresh catalogue snapshot (GET-only) + the readiness audit, recorded in the shared publishing database
        argv=lambda slug, p, rel: [sys.executable, "-m", "publishing.runner.readiness", slug, rel],
        collect=_readiness_collect,
    ),
    Stage(
        key="gate_r", title="Gate R (pin the sources)", step="gate_r", needs_source=False, uses_pubdb=True,
        writes_pubdb=True,
        # free: fresh snapshot + Gate R; pins the sources and opens the edition ONLY at 100% ready, else changes nothing
        argv=lambda slug, p, rel: [sys.executable, "-m", "publishing.runner.gate_r", slug, rel],
        collect=_gate_r_collect,
    ),
    Stage(
        key="draft_trial", title="Draft one chapter (trial, into the run folder)", step="drafting", uses_models=True,
        needs_assets=True, uses_pubdb=True, params={"chapter": 1},
        # paid: the engine's one-pass draft + render, written to the run folder only; no new pictures; the publishing
        # database is read (dossier, Gate R pins) but not pushed back
        argv=lambda slug, p, rel: [sys.executable, "-m", "publishing.runner.draft", slug, rel, str(int(p["chapter"]))],
        collect=_draft_collect,
    ),
]}

WRITES_PUBDB = [k for k, s in STAGES.items() if s.writes_pubdb]


def validate(stage_key: str, params: dict) -> tuple[Stage, dict]:
    """The stage and its parameters with defaults filled; unknown stages and keys are refused."""
    stage = STAGES.get(stage_key)
    if stage is None:
        raise ValueError(f"unknown stage {stage_key!r}")
    extra = set(params) - set(stage.params)
    if extra:
        raise ValueError(f"{stage_key}: unknown parameters {sorted(extra)}")
    merged = {**stage.params, **params}
    if "first" in stage.params:
        _chapters(merged)
    if "chapter" in stage.params and not (1 <= int(merged["chapter"]) <= 99):
        raise ValueError(f"chapter must be 1–99, got {merged['chapter']}")
    return stage, merged
