"""A fake Supabase client for the catalogue tests: an in-memory postgrest that
records every write and honours the uniqueness the real tables carry.

Modelled on tests/test_observer_job_guard.py's FakeSB, extended with what the
catalogue code uses: list inserts, ``in_``, ``upsert(on_conflict=...)`` with
PostgREST's merge semantics (only the payload's columns are written), and a
storage shim. Unique keys per table mirror migration 0112:

    topic_candidates  (source_kind, book_id|zero, node_id|zero, normalized)
    curricula         (code)
    curriculum_nodes  (curriculum_id, code)
    topic_aliases     (normalized)
    topic_articles    (topic_id, language, version)          -- Phase 2b
    article_figures   (article_id, figure_key)               -- Phase 2b
    topic_questions   (topic_id, language, content_hash)     -- Phase 3

A violating INSERT raises with Postgres's 23505 text, which is what the real
client surfaces (postgrest.APIError carries {"code": "23505", ...}).
"""

from __future__ import annotations

import itertools
import json
import re

ZERO = "00000000-0000-0000-0000-000000000000"

UNIQUE = {
    "topic_candidates": lambda r: ("book" if r.get("source_kind") == "book" else "curriculum",
                                   r.get("book_id") or ZERO, r.get("node_id") or ZERO,
                                   r.get("normalized")),
    "curricula": lambda r: (r.get("code"),),
    "curriculum_nodes": lambda r: (r.get("curriculum_id"), r.get("code")),
    "topic_aliases": lambda r: (r.get("normalized"),),
    "topic_articles": lambda r: (r.get("topic_id"), r.get("language"), r.get("version")),
    "article_figures": lambda r: (r.get("article_id"), r.get("figure_key")),
    "topic_questions": lambda r: (r.get("topic_id"), r.get("language"), r.get("content_hash")),
}


class DuplicateKey(Exception):
    code = "23505"

    def __init__(self, table, key):
        super().__init__(f'duplicate key value violates unique constraint "{table}_uniq" (23505): {key}')


class _Res:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, store, table):
        self.store, self.table = store, table
        self.op, self.payload, self.kw = "select", None, {}
        self.filters, self.limit_n, self._single = [], None, False

    # ── builders ──
    def select(self, *a, **k):
        self.op = "select"
        return self

    def update(self, payload, *a, **k):
        self.op, self.payload = "update", dict(payload)
        return self

    def insert(self, payload, *a, **k):
        self.op, self.payload = "insert", payload
        return self

    def upsert(self, payload, *a, **k):
        self.op, self.payload, self.kw = "upsert", payload, dict(k)
        return self

    def delete(self, *a, **k):
        self.op = "delete"
        return self

    def eq(self, col, val):
        self.filters.append(("eq", col, val))
        return self

    def neq(self, col, val):
        self.filters.append(("neq", col, val))
        return self

    def in_(self, col, vals):
        self.filters.append(("in", col, list(vals)))
        return self

    @property
    def not_(self):
        """postgrest's ``q.not_.in_(col, vals)`` — the next filter is negated
        (the builder-queued probe of the figure job uses it)."""
        outer = self

        class _Negated:
            def in_(self_, col, vals):
                outer.filters.append(("not_in", col, list(vals)))
                return outer

            def eq(self_, col, val):
                outer.filters.append(("neq", col, val))
                return outer

        return _Negated()

    def order(self, *a, **k):
        return self

    def range(self, *a, **k):
        return self

    def limit(self, n):
        self.limit_n = n
        return self

    def maybe_single(self):
        self.limit_n, self._single = 1, True
        return self

    def single(self):
        self.limit_n, self._single = 1, True
        return self

    # ── execution ──
    @staticmethod
    def _value(row, col):
        """A column — or a PostgREST JSON path: ``params->>curriculum_id``
        (text), ``a->b->>c``. ``->>`` yields text, as Postgres does."""
        if "->" not in col:
            return row.get(col)
        parts = [p for p in re.split(r"->>?", col) if p]
        v = row.get(parts[0])
        for p in parts[1:]:
            v = v.get(p) if isinstance(v, dict) else None
        as_text = re.search(r"->>[^>-]*$", col) is not None  # the LAST operator is ->>
        if as_text and v is not None and not isinstance(v, str):
            return json.dumps(v)
        return v

    def _match(self, row):
        for kind, col, val in self.filters:
            v = self._value(row, col)
            if kind == "eq" and v != val:
                return False
            if kind == "neq" and v == val:
                return False
            if kind == "in" and v not in val:
                return False
            if kind == "not_in" and v in val:
                return False
        return True

    def _rows_in(self, payload):
        return [dict(r) for r in (payload if isinstance(payload, list) else [payload])]

    def _new_id(self):
        return f"{self.table}-{next(self.store.ids)}"

    def _insert_one(self, row, allow_merge=False, on_conflict=None):
        table = self.store.tables.setdefault(self.table, [])
        keyf = UNIQUE.get(self.table)
        if keyf:
            key = keyf(row)
            for existing in table:
                if keyf(existing) == key:
                    if allow_merge:
                        existing.update(row)  # PostgREST: SET only the payload's columns
                        return existing
                    raise DuplicateKey(self.table, key)
        row.setdefault("id", self._new_id())
        table.append(row)
        return row

    def execute(self):
        self.store.calls.append((self.op, self.table))
        table = self.store.tables.setdefault(self.table, [])
        if self.op == "select":
            rows = [r for r in table if self._match(r)]
            if self.limit_n:
                rows = rows[: self.limit_n]
            if self._single:
                return _Res(dict(rows[0]) if rows else None)
            return _Res([dict(r) for r in rows])
        if self.op == "update":
            self.store.log.append(("update", self.table, dict(self.payload), list(self.filters)))
            out = []
            for r in table:
                if self._match(r):
                    r.update(self.payload)
                    out.append(dict(r))
            return _Res(out)
        if self.op == "insert":
            rows = self._rows_in(self.payload)
            self.store.log.append(("insert", self.table, [dict(r) for r in rows]))
            # Postgres: one statement, all-or-nothing.
            keyf = UNIQUE.get(self.table)
            if keyf:
                seen = {keyf(r) for r in table}
                for r in rows:
                    k = keyf(r)
                    if k in seen:
                        raise DuplicateKey(self.table, k)
                    seen.add(k)
            return _Res([dict(self._insert_one(r)) for r in rows])
        if self.op == "upsert":
            rows = self._rows_in(self.payload)
            self.store.log.append(("upsert", self.table, [dict(r) for r in rows], dict(self.kw)))
            return _Res([dict(self._insert_one(r, allow_merge=True)) for r in rows])
        if self.op == "delete":
            self.store.log.append(("delete", self.table, list(self.filters)))
            self.store.tables[self.table] = [r for r in table if not self._match(r)]
            return _Res([])
        raise AssertionError(self.op)


class _Bucket:
    def __init__(self, store, name):
        self.store, self.name = store, name

    def download(self, path):
        self.store.downloads.append((self.name, path))
        try:
            return self.store.files[(self.name, path)]
        except KeyError:
            raise RuntimeError(f"storage object not found: {self.name}/{path}") from None


class _Storage:
    def __init__(self, store):
        self.store = store

    def from_(self, name):
        return _Bucket(self.store, name)


class RpcError(Exception):
    """What postgrest surfaces for a failed function call: ``code`` is the
    SQLSTATE (or PGRST202 for a function the schema does not have)."""

    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(f"{message} ({code})")


class _Rpc:
    """``sb.rpc(name, params).execute()`` — the one function the worker calls
    on a kit, ``repoint_kit_generation`` (app migration 0115), with the SQL
    body's semantics: the kit must exist (P0002) and be ``generating``
    (23514), the pointer for ``p_kind`` must still be ``p_replaces`` (23514)
    and the write is a MERGE of one key, never a replace. Any other name is
    a function this database does not have (PGRST202), which is what a
    pre-0115 database answers."""

    def __init__(self, store, name, params):
        self.store, self.name, self.params = store, name, params

    def execute(self):
        # ``calls`` keeps its (op, table) shape — tests unpack it in pairs;
        # the arguments go to ``rpc_calls``.
        self.store.calls.append(("rpc", self.name))
        self.store.rpc_calls.append((self.name, dict(self.params)))
        if self.name != "repoint_kit_generation":
            raise RpcError("PGRST202", f"Could not find the function public.{self.name} in the schema cache")
        p = self.params
        kits = [k for k in self.store.tables.get("topic_kits", []) if k.get("id") == p.get("p_kit")]
        if not kits:
            raise RpcError("P0002", f"kit {p.get('p_kit')} not found")
        k = kits[0]
        if k.get("status") != "generating":
            raise RpcError("23514", f"kit {k['id']} is {k.get('status')}, not generating — nothing is repointed")
        kind = p.get("p_kind")
        docs = k.get("doc_generation_ids") if isinstance(k.get("doc_generation_ids"), dict) else {}
        current = k.get("presentation_generation_id") if kind == "presentation" else docs.get(kind)
        if (current or None) != (p.get("p_replaces") or None):
            raise RpcError("23514", f"kit {k['id']} points its {kind} at {current or 'nothing'}, not "
                                    f"{p.get('p_replaces') or 'nothing'} — the pointer moved; nothing is repointed")
        if kind == "presentation":
            k["presentation_generation_id"] = p["p_generation"]
        else:
            k["doc_generation_ids"] = {**docs, kind: p["p_generation"]}
        self.store.log.append(("rpc", "topic_kits", dict(k), [("id", k["id"])]))
        return _Res(dict(k))


class FakeSB:
    """``tables`` is the state; ``log`` every write; ``calls`` every execute."""

    def __init__(self):
        self.tables = {"jobs": [], "generations": [], "books": [], "topic_candidates": [],
                       "topic_aliases": [], "curricula": [], "curriculum_nodes": [],
                       "topics": [], "topic_curriculum_map": [], "topic_articles": [],
                       "article_figures": [], "visual_assets": []}
        self.log = []
        self.calls = []
        self.rpc_calls = []
        self.files = {}
        self.downloads = []
        self.ids = itertools.count(1)
        self.storage = _Storage(self)

    def table(self, name):
        return _Query(self, name)

    def rpc(self, name, params=None):
        return _Rpc(self, name, dict(params or {}))

    # helpers for assertions
    def writes(self, table=None):
        return [e for e in self.log if table is None or e[1] == table]

    def snapshot(self):
        import copy
        return copy.deepcopy(self.tables)


class ExplodingSB:
    """Any use is a test failure — for dry runs, which must not touch a client."""

    def __getattr__(self, name):
        raise AssertionError(f"the client was used ({name}) on a dry run")
