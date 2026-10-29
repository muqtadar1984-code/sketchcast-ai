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
]}


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
