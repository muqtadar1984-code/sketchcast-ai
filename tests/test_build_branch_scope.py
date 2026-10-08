"""The kind branches of worker.process._build_from_analysis share one Python
scope, and Python binds a name for the WHOLE function the moment any branch
assigns it. A branch that reads a name only a sibling branch assigns does not
get a NameError at import time; it gets UnboundLocalError at run time, on the
first job of that kind.

That is exactly what #167 shipped (2026-10-08): the subject ``profile`` is
resolved inside the presentation branch, and the deck branch gained a read of
``profile.worked_examples`` — every deck job after the deploy failed with
"cannot access local variable 'profile' where it is not associated with a
value", book and catalogue alike, while the video and the documents of the
same kit were fine.

This test walks the function's AST and refuses any name the deck branch (or
any other non-presentation branch) LOADS that is bound ONLY inside the
presentation branch. Names bound at function level — before or after the
if/elif chain — are shared by design and allowed.
"""
from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from worker import process


def _kind_chain(fn: ast.FunctionDef) -> ast.If:
    """The ``if kind == "presentation": ... elif kind == ...`` chain."""
    for node in ast.walk(fn):
        if isinstance(node, ast.If) and _kind_literal(node.test) == "presentation":
            return node
    raise AssertionError("no `if kind == \"presentation\":` chain in _build_from_analysis")


def _kind_literal(test: ast.expr) -> str | None:
    if (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name)
            and test.left.id == "kind" and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)):
        return str(test.comparators[0].value)
    return None


def _branches(chain: ast.If) -> dict[str, list[ast.stmt]]:
    """{kind literal (or "else"): the branch's statements} for the whole chain."""
    out: dict[str, list[ast.stmt]] = {}
    node: ast.If | None = chain
    while node is not None:
        out[_kind_literal(node.test) or "<unknown>"] = node.body
        if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
            node = node.orelse[0]
        else:
            if node.orelse:
                out["else"] = node.orelse
            node = None
    return out


def _stored(stmts: list[ast.stmt]) -> set[str]:
    names: set[str] = set()
    for stmt in stmts:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                names.add(node.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    names.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
    return names


def _loaded(stmts: list[ast.stmt]) -> set[str]:
    return {node.id for stmt in stmts for node in ast.walk(stmt)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}


def _function_level_names(fn: ast.FunctionDef, chain: ast.If) -> set[str]:
    """Names bound anywhere in the function OUTSIDE the kind chain, plus the
    parameters — these are legitimately visible to every branch."""
    params = {a.arg for a in fn.args.args + fn.args.kwonlyargs + fn.args.posonlyargs}
    if fn.args.vararg:
        params.add(fn.args.vararg.arg)
    if fn.args.kwarg:
        params.add(fn.args.kwarg.arg)
    outside = [s for s in fn.body if s is not chain]
    return params | _stored(outside)


@pytest.fixture(scope="module")
def build_fn() -> ast.FunctionDef:
    src = textwrap.dedent(inspect.getsource(process._build_from_analysis))
    mod = ast.parse(src)
    fn = mod.body[0]
    assert isinstance(fn, ast.FunctionDef) and fn.name == "_build_from_analysis"
    return fn


def test_no_branch_reads_a_name_only_the_presentation_branch_binds(build_fn):
    chain = _kind_chain(build_fn)
    branches = _branches(chain)
    assert "presentation" in branches and "deck" in branches, sorted(branches)
    shared = _function_level_names(build_fn, chain)
    presentation_only = _stored(branches["presentation"]) - shared

    offenders: dict[str, set[str]] = {}
    for kind, body in branches.items():
        if kind == "presentation":
            continue
        # a name the branch binds ITSELF before reading is its own, not the
        # presentation's (the document branch's `_doc_profile` pattern)
        leaked = (_loaded(body) & presentation_only) - _stored(body)
        if leaked:
            offenders[kind] = leaked
    assert not offenders, (
        "these kind branches read names that only the presentation branch "
        f"assigns — UnboundLocalError on the first such job: {offenders}")


def test_the_deck_branch_resolves_its_own_subject_profile(build_fn):
    """The positive pin for #167's seam: the deck's maths_lesson decision reads
    a profile the DECK resolved, the way the documents resolve `_doc_profile`."""
    deck = _branches(_kind_chain(build_fn))["deck"]
    assert "_deck_profile" in _stored(deck), "the deck branch must resolve a profile of its own"
    assert "profile" not in _loaded(deck), "the presentation's `profile` is not in scope here"
