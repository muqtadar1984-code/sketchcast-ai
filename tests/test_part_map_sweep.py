"""worker.client.sweep_orphaned_parts — the worker half of migration 0152.

After a part map shrinks, queued kits for parts the chapter no longer has can
only fail (Sara's Sci 7, 2026-10-22: 7 failed, 17 hand-cancelled). The SQL
function does the work; the client wrapper must call it with the book id,
report the count, and NEVER raise — it runs inside index_book and inside a
paid generation, and a missing migration must not cost either.
"""
from __future__ import annotations

from types import SimpleNamespace

from worker import client as db


class _Rpc:
    def __init__(self, data=None, exc: Exception | None = None):
        self.data, self.exc, self.calls = data, exc, []

    def rpc(self, name, params):
        self.calls.append((name, params))
        return self

    def execute(self):
        if self.exc:
            raise self.exc
        return SimpleNamespace(data=self.data)


def test_calls_the_sql_sweep_with_the_book_and_returns_its_count():
    sb = _Rpc(data=3)
    assert db.sweep_orphaned_parts(sb, "book-1") == 3
    assert sb.calls == [("sweep_orphaned_parts", {"p_book": "book-1"})]


def test_nothing_to_sweep_is_zero():
    assert db.sweep_orphaned_parts(_Rpc(data=0), "book-1") == 0
    assert db.sweep_orphaned_parts(_Rpc(data=None), "book-1") == 0


def test_never_raises_when_the_migration_is_not_applied():
    sb = _Rpc(exc=RuntimeError("PGRST202: function public.sweep_orphaned_parts does not exist"))
    assert db.sweep_orphaned_parts(sb, "book-1") == 0
