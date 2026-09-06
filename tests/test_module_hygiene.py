"""No module in the worker defines the same top-level name twice.

Python allows it — the LAST definition silently wins — which is exactly why it
is dangerous. On 2026-09-03 a merge rebuilt worker/client.py as "HEAD + a
block" and appended a second, byte-identical copy of the file's whole tail
(514 lines, 27 definitions: the storage transfer retry, every chapter helper,
the sketch queue). Production ran it without complaint. The hazard was the
next edit: anyone changing the FIRST copy of `_transfer_with_retry` would have
shipped a no-op, because the second copy was the one being called.

This pins uniqueness with `ast`, so a duplicate fails a test rather than
waiting to be noticed. Kept module-wide rather than per-symbol so a future
duplicate of any name is caught, not just the ones we already know about.

Scope: every module the worker process imports at runtime — worker/,
support_agent/, shared/ (recursively, so the TTS providers are in), and the
compose/render stages. It looks at MODULE-BODY statements only: a definition
nested in an `if` / `try` at module level is a deliberate conditional, not a
paste error, and is left alone.
"""

from __future__ import annotations

import ast
import collections
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

MODULES = sorted(
    p for p in [
        *ROOT.glob("worker/*.py"),
        *ROOT.glob("support_agent/*.py"),
        *ROOT.rglob("shared/**/*.py"),
        *ROOT.rglob("catalogue/**/*.py"),
        *ROOT.glob("agent6_animation/*.py"),
        *ROOT.glob("agent8_render/*.py"),
    ]
    if p.name != "__init__.py" or p.stat().st_size > 0
)


def _top_level_names(tree: ast.Module) -> collections.Counter:
    names: collections.Counter = collections.Counter()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names[node.name] += 1
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names[target.id] += 1
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            names[node.target.id] += 1
    return names


@pytest.mark.parametrize("path", MODULES, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_top_level_name_is_defined_twice(path: Path):
    if not path.exists():
        pytest.skip(f"{path} absent")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    dupes = {name: n for name, n in _top_level_names(tree).items() if n > 1}
    assert not dupes, f"{path.relative_to(ROOT)} defines these names more than once: {dupes}"


def test_the_checker_sees_a_duplicate():
    """The guard must itself be proven to fire, or a green run means nothing."""
    tree = ast.parse("X = 1\ndef f():\n    pass\nX = 2\ndef f():\n    pass\n")
    assert {k: v for k, v in _top_level_names(tree).items() if v > 1} == {"X": 2, "f": 2}


def test_the_scope_reaches_the_tts_providers():
    """The providers live one directory deeper than shared/tts; a non-recursive
    glob missed them (adversarial review, 2026-09-04)."""
    names = {str(p.relative_to(ROOT)).replace("\\", "/") for p in MODULES}
    assert "worker/client.py" in names
    assert any(n.startswith("shared/tts/providers/") for n in names), sorted(names)
