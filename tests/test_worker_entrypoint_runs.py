"""The worker entry point must actually RUN, not merely contain the right text.

A NameError shipped to production because every new worker-facing test was an
`inspect.getsource(...)` substring assertion. Those pass happily against code
that cannot execute: `os` was used on three lines of worker/process.py and
imported nowhere at module level, so process_generation — the entry point for
presentations, documents, exams and revision papers — raised on its 8th line,
before it touched the database. Every claimed job went straight to the failure
handler.

These tests call things.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_every_module_attribute_used_is_actually_imported():
    """Repo-wide: catch the next missing import before it reaches a worker.

    Walks each production module and checks that every `NAME.attr` load whose
    NAME looks like a module is bound at module level or locally in scope.
    """
    offenders = []
    for pkg in ("worker", "shared", "agent3_scripts", "agent6_animation",
                "agent8_render", "spike/scene_engine"):
        for f in (ROOT / pkg).rglob("*.py"):
            src = f.read_text(encoding="utf-8", errors="replace")
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            top = set()
            for node in tree.body:          # module level only
                if isinstance(node, ast.Import):
                    top |= {(a.asname or a.name.split(".")[0]) for a in node.names}
                elif isinstance(node, ast.ImportFrom):
                    top |= {(a.asname or a.name) for a in node.names}
                elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                    for t in ast.walk(node):
                        if isinstance(t, ast.Name):
                            top.add(t.id)
            # Walk with the ENCLOSING scopes carried down: a nested function
            # closes over its parent's imports. A first version of this test
            # flagged continuity.py's `_questions` for using `re`, which its
            # enclosing `seed_moment` imports — a false positive, and a
            # cry-wolf check is worse than no check.
            watched = {"os", "sys", "json", "re", "time", "math", "shutil",
                       "logging", "tempfile", "uuid"}

            def _bound(node) -> set:
                names = set()
                for n in ast.walk(node):
                    if isinstance(n, ast.Import):
                        names |= {(a.asname or a.name.split(".")[0])
                                  for a in n.names}
                    elif isinstance(n, ast.ImportFrom):
                        names |= {(a.asname or a.name) for a in n.names}
                    elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                        names.add(n.id)
                    elif isinstance(n, ast.arg):
                        names.add(n.arg)
                return names

            def _check(node, scope: set) -> None:
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        _check(child, scope | _bound(child))
                        continue
                    if (isinstance(child, ast.Attribute)
                            and isinstance(child.value, ast.Name)
                            and child.value.id in watched
                            and child.value.id not in scope):
                        offenders.append(
                            f"{f.relative_to(ROOT)}:{child.lineno} uses "
                            f"{child.value.id}.{child.attr} with no import "
                            "in scope")
                    _check(child, scope)

            _check(tree, top)
    assert not offenders, "\n".join(offenders)


def test_process_generation_gets_past_its_setup():
    """CALL it. The label block at the top used os.getenv with os unimported;
    a source-substring test could never see that."""
    from worker import process

    calls = {}

    class _DB:
        def __getattr__(self, name):
            def _rec(*a, **k):
                calls.setdefault(name, 0)
                calls[name] += 1
                if name == "get_generation":
                    raise _Stop("reached the database")
                return {} if name.startswith("get") else None
            return _rec

    class _Stop(Exception):
        pass

    process.db = _DB()
    with pytest.raises(Exception) as exc:
        process.process_generation(object(), {"id": "j1"}, "g1")
    # It must NOT be a NameError/AttributeError from module setup — reaching
    # the DB (or any later failure) means the entry point itself executes.
    assert not isinstance(exc.value, NameError), exc.value
    assert calls, "process_generation never reached its first db call"


def test_acceptance_report_is_callable_without_a_scene_engine():
    """Its own guard clause used os.getenv, above its try/except — so the
    'a validator bug must never destroy a lesson' net did not cover it."""
    from worker.process import _acceptance_report
    assert _acceptance_report({}, {"segments": []}) is None
