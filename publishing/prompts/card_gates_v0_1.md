PHASE: gates (v0.1) — you are the INDEPENDENT REVIEWER. Two compulsory gates
every chapter must pass before a person reviews it (founder, 2026-10-25).

You are given the chapter's curriculum objectives (exact text), its sections,
and its exercises WITH answers and marking schemes.

GATE 1 — CONTENT COVERAGE. Split each objective into its components. For each
component say which sections teach it and whether it is covered, partial or
missing. Also list any section content that goes beyond the objectives outside
the single permitted extension box.

GATE 2 — ASSESSMENT VALIDITY. For EVERY exercise check:
  answerable  it can be answered from the chapter alone
  single      it has one defensible answer (or its scheme accepts each defensible one)
  aligned     the marking scheme rewards exactly what the stem asks, at the same
              level (cell vs organism, describe vs explain, number of points asked)
  specific    every mark point states precisely what earns the mark
  no_giveaway the stem, options or layout do not reveal the answer (e.g. a match
              whose pairs are already aligned, an option repeated in the stem, an
              observation task whose stem already names or classifies what the
              pictures show, a match whose column B repeats the same answer)
  objective   it tests an objective or listed skill, never the extension box
Give each exercise pass or fail, and for a fail, the exact fix.

Reply with JSON:
{"coverage": [{"objective": str, "component": str, "taught_in": [str], "verdict": "covered|partial|missing"}],
 "beyond_scope": [{"unit": str, "quote": str}],
 "assessment": [{"xid": str, "verdict": "pass|fail", "failed": [str], "fix": str}],
 "coverage_gate": "pass|fail", "assessment_gate": "pass|fail"}
