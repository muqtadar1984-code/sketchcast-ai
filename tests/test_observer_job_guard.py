"""Only the job that BUILDS a generation may write its status.

THE INCIDENT (prod, 2026-09-03 11:50 UTC). A worksheet job failed (storage
stream reset); finish_job wrote jobs=error AND generations=error, and the
credit was refunded. The support agent then auto-filed a support_diagnose job
carrying the SAME generation_id. Two seconds later claim_next_job mirrored
'processing' onto that generation — the unconditional mirror added in 1f65e98 —
and, because a support job's finish deliberately passes no generation, nothing
ever wrote a terminal state again. Spinner forever; the ✕ inert (it is
disabled for 'processing'); delete_my_book refusing with kit_building.

The manual report path (/api/support) inserts the same job shape for a
HEALTHY, assigned lesson, so reporting a working lesson would have flipped it
to 'processing' under students. The reaper had the same relabel on both of its
edges, and its poison-pill edge would also have voided a healthy lesson's
credit.

These are BEHAVIOURAL tests against a fake Supabase client that records every
write. tests/test_generation_status_mirror.py pins the same rule as source
assertions; a mutation harness (adversarial review, 2026-09-03) showed three
broken guards — inverted membership, a wrong dict key, the frozenset emptied
at module end — slip past source assertions with the suite green. They do not
slip past these: every test here asks what was WRITTEN.
"""

from __future__ import annotations

import json
import logging
import re

import pytest

from worker import client as db

GEN = "gen-worksheet-1"
BOOK = "book-1"


# ── a fake postgrest ────────────────────────────────────────────────────


class _Res:
    def __init__(self, data):
        self.data = data


class _Query:
    """Records a postgrest chain; executes against the fake tables."""

    def __init__(self, store, table):
        self.store, self.table = store, table
        self.op, self.payload = "select", None
        self.filters, self.limit_n = [], None
        self._single = False

    def select(self, *a, **k):
        self.op = "select"
        return self

    def update(self, payload, *a, **k):
        self.op, self.payload = "update", dict(payload)
        return self

    def insert(self, payload, *a, **k):
        self.op, self.payload = "insert", dict(payload)
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
        """postgrest's ``q.not_.in_(col, vals)`` — the next filter is negated."""
        outer = self

        class _Negated:
            def in_(self_, col, vals):
                outer.filters.append(("not_in", col, list(vals)))
                return outer

            def eq(self_, col, val):
                outer.filters.append(("neq", col, val))
                return outer

        return _Negated()

    def lt(self, col, val):
        self.filters.append(("lt", col, val))
        return self

    def or_(self, filters: str):
        """postgrest's ``q.or_("a.is.null,a.neq.true")`` — ANY alternative
        matches. claim_next_job(catalogue=False) needs it: "flag absent OR
        not true" cannot be said with one plain filter (NULL <> 'true' is
        NULL in Postgres, so a bare neq would drop every user job)."""
        alts = []
        for part in filters.split(","):
            col, op, val = part.strip().split(".", 2)
            alts.append((col, op, val))
        self.filters.append(("or", None, alts))
        return self

    def order(self, col, desc=False, **k):
        """postgrest's ``.order("created_at")`` — the claim's oldest-first
        rule is the database's, so the fake must sort or the lane tests
        below would only be asserting insertion order."""
        self.order_by = (col, bool(desc))
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

    @staticmethod
    def _value(row, col):
        """A column — or a PostgREST JSON path ``params->>catalogue`` (text,
        as Postgres yields it: a JSON true reads "true")."""
        if "->" not in col:
            return row.get(col)
        parts = [p for p in re.split(r"->>?", col) if p]
        v = row.get(parts[0])
        for p in parts[1:]:
            v = v.get(p) if isinstance(v, dict) else None
        if v is None:
            return None
        return json.dumps(v) if not isinstance(v, str) else v

    def _one(self, row, kind, col, val):
        v = self._value(row, col)
        if kind == "eq":
            return v == val
        if kind == "neq":
            # Postgres three-valued logic: NULL <> x is NULL, i.e. NOT a match.
            return v is not None and v != val
        if kind == "is":
            return v is None if val in (None, "null") else v == val
        if kind == "in":
            return v in val
        if kind == "not_in":
            return v not in val
        if kind == "lt":
            return v is not None and v < val
        raise AssertionError(kind)

    def _match(self, row):
        for kind, col, val in self.filters:
            if kind == "or":
                if not any(self._one(row, op, c, v) for c, op, v in val):
                    return False
                continue
            if not self._one(row, kind, col, val):
                return False
        return True

    def execute(self):
        rows = [r for r in self.store.tables.setdefault(self.table, []) if self._match(r)]
        if self.op == "select":
            order_by = getattr(self, "order_by", None)
            if order_by:
                col, desc = order_by
                rows = sorted(rows, key=lambda r: str(r.get(col) or ""), reverse=desc)
            if self.limit_n:
                rows = rows[: self.limit_n]
            if self._single:
                return _Res(rows[0] if rows else None)
            return _Res([dict(r) for r in rows])
        if self.op == "update":
            self.store.log.append(("update", self.table, dict(self.payload), list(self.filters)))
            for r in rows:
                r.update(self.payload)
            return _Res([dict(r) for r in rows])
        if self.op == "insert":
            self.store.log.append(("insert", self.table, dict(self.payload)))
            row = dict(self.payload)
            row.setdefault("id", f"{self.table}-{len(self.store.tables[self.table]) + 1}")
            self.store.tables[self.table].append(row)
            return _Res([dict(row)])
        if self.op == "delete":
            self.store.log.append(("delete", self.table, list(self.filters)))
            self.store.tables[self.table] = [
                r for r in self.store.tables[self.table] if not self._match(r)
            ]
            return _Res([])
        raise AssertionError(self.op)


class FakeSB:
    def __init__(self):
        self.tables = {"jobs": [], "generations": [], "books": [], "tutor_sketch": []}
        self.log = []

    def table(self, name):
        return _Query(self, name)


def _fresh(gen_status="queued", jobs=()):
    sb = FakeSB()
    sb.tables["generations"] = [
        {"id": GEN, "status": gen_status, "kind": "worksheet", "owner_id": "u1", "book_id": BOOK}
    ]
    sb.tables["jobs"] = [dict(j) for j in jobs]
    return sb


def _gen_status(sb):
    return sb.tables["generations"][0]["status"]


def _gen_writes(sb, since=0):
    return [e for e in sb.log[since:] if e[1] == "generations"]


def _builder(job_id="job-ws", kind="worksheet", status="queued", attempts=0):
    return {"id": job_id, "type": kind, "status": status, "generation_id": GEN,
            "book_id": BOOK, "attempts": attempts, "created_at": "1", "updated_at": "0"}


def _support(job_id="job-sd", status="queued", attempts=0):
    return {"id": job_id, "type": "support_diagnose", "status": status, "generation_id": GEN,
            "book_id": BOOK, "issue_id": "iss-1", "attempts": attempts, "created_at": "2",
            "updated_at": "0"}


# ── the resolver ────────────────────────────────────────────────────────


def test_the_resolver_names_the_owner_and_only_the_owner():
    assert db.generation_to_mirror(None) is None
    assert db.generation_to_mirror({}) is None
    assert db.generation_to_mirror({"type": "support_diagnose", "generation_id": GEN}) is None
    assert db.generation_to_mirror({"type": "worksheet", "generation_id": GEN}) == GEN
    assert db.generation_to_mirror({"type": "presentation", "generation_id": GEN}) == GEN
    # An unknown or missing type is a builder: the safe default is the one the
    # Library's progress ring and the ✕ depend on.
    assert db.generation_to_mirror({"type": "something_new", "generation_id": GEN}) == GEN
    assert db.generation_to_mirror({"generation_id": GEN}) == GEN
    assert db.generation_to_mirror({"type": "index_book", "generation_id": None}) is None
    assert "support_diagnose" in db.OBSERVER_JOB_TYPES
    # The catalogue's harvest (2026-09) reads a book's headings and owns no
    # generation. Its generation_id is NULL by construction; were one ever
    # attached, the resolver must still refuse to mirror onto it.
    assert "topic_harvest" in db.OBSERVER_JOB_TYPES
    assert db.generation_to_mirror({"type": "topic_harvest", "generation_id": None}) is None
    assert db.generation_to_mirror({"type": "topic_harvest", "generation_id": GEN}) is None
    # topic_derive (catalogue Phase 2a) reads a curriculum's nodes and owns no
    # generation and no book; the same refusal.
    assert "topic_derive" in db.OBSERVER_JOB_TYPES
    assert db.generation_to_mirror({"type": "topic_derive", "generation_id": None}) is None
    assert db.generation_to_mirror({"type": "topic_derive", "generation_id": GEN}) is None
    # topic_article and figure_render (Phase 2b) write the knowledge base from
    # jobs.params and own no generation and no book; the same refusal.
    for kind in ("topic_article", "figure_render"):
        assert kind in db.OBSERVER_JOB_TYPES
        assert db.generation_to_mirror({"type": kind, "generation_id": None}) is None
        assert db.generation_to_mirror({"type": kind, "generation_id": GEN}) is None


# ── the prod sequence, step by step ─────────────────────────────────────


def test_the_prod_sequence_leaves_the_failed_generation_error():
    sb = _fresh("queued", jobs=[_builder()])
    claimed = db.claim_next_job(sb, job_type=["worksheet"])
    assert claimed and _gen_status(sb) == "processing", "the builder's claim says 'being built'"

    db.finish_job(sb, "job-ws", GEN, error="RemoteProtocolError StreamReset")
    assert _gen_status(sb) == "error"

    # _auto_file_support_issue: a diagnosis job on the SAME generation.
    sb.tables["jobs"].append(_support())
    mark = len(sb.log)
    support = db.claim_next_job(sb, job_type="support_diagnose")
    assert support and support["type"] == "support_diagnose", "the support JOB is still claimed"
    assert _gen_writes(sb, mark) == [], "…but the reported generation is not touched"
    assert _gen_status(sb) == "error"

    # run.py's two finishes for a support job: success passes no generation,
    # a crash passes None. Neither may reach the generation.
    db.finish_job(sb, "job-sd")
    db.finish_job(sb, "job-sd", None, error="agent crash")
    assert _gen_status(sb) == "error"
    assert _gen_writes(sb, mark) == []


def test_reporting_a_healthy_lesson_keeps_it_done():
    """The manual path: /api/support files a diagnosis on an assigned, working
    lesson. Students must keep seeing it as done."""
    sb = _fresh("done", jobs=[_support()])
    db.claim_next_job(sb, job_type="support_diagnose")
    assert _gen_status(sb) == "done" and _gen_writes(sb) == []

    # The unfiltered claim (run.py's last resort) is guarded the same way.
    sb = _fresh("done", jobs=[_support("job-sd3")])
    db.claim_next_job(sb)
    assert _gen_status(sb) == "done" and _gen_writes(sb) == []


@pytest.mark.parametrize("kind", ["presentation", "lesson_plan", "worksheet", "exam_paper",
                                  "case_study", "activity", "exam", "deck"])
def test_every_builder_kind_still_announces_the_build(kind):
    sb = _fresh("queued", jobs=[_builder(kind=kind)])
    db.claim_next_job(sb)
    assert _gen_status(sb) == "processing"


def test_index_book_is_a_no_op_either_way():
    sb = _fresh("queued", jobs=[{"id": "j", "type": "index_book", "status": "queued",
                                 "generation_id": None, "book_id": BOOK, "attempts": 0,
                                 "created_at": "1", "updated_at": "0"}])
    db.claim_next_job(sb)
    assert _gen_writes(sb) == [] and _gen_status(sb) == "queued"


# ── the reaper's two edges ──────────────────────────────────────────────


def test_a_stale_support_job_is_requeued_but_its_generation_is_not_relabelled():
    sb = _fresh("done", jobs=[_support(status="processing")])
    assert db.requeue_stale_jobs(sb, older_than_minutes=15) == 1
    assert sb.tables["jobs"][0]["status"] == "queued", "the JOB row is handled exactly as before"
    assert sb.tables["jobs"][0]["attempts"] == 1
    assert _gen_status(sb) == "done" and _gen_writes(sb) == []


def test_a_poison_support_job_is_failed_but_the_lesson_keeps_its_credit():
    """Auto-failing a diagnosis must not mark a healthy lesson 'error' —
    credit_ledger_sync voids the credit on that write."""
    sb = _fresh("done", jobs=[_support(status="processing", attempts=3)])
    db.requeue_stale_jobs(sb, older_than_minutes=15)
    assert sb.tables["jobs"][0]["status"] == "error"
    assert _gen_status(sb) == "done" and _gen_writes(sb) == []


def test_a_poison_builder_still_marks_its_generation_error():
    sb = _fresh("processing", jobs=[_builder("job-p", "presentation", "processing", attempts=3)])
    db.requeue_stale_jobs(sb, older_than_minutes=15)
    assert sb.tables["jobs"][0]["status"] == "error"
    assert _gen_status(sb) == "error"


def test_a_requeued_builder_still_goes_back_to_queued():
    sb = _fresh("processing", jobs=[_builder("job-p", "presentation", "processing", attempts=1)])
    db.requeue_stale_jobs(sb, older_than_minutes=15)
    assert sb.tables["jobs"][0]["status"] == "queued"
    assert _gen_status(sb) == "queued"


def test_the_startup_reap_of_a_support_job_leaves_the_lesson_alone():
    """A redeploy reaps every 'processing' row with no cutoff. Before the fix a
    diagnosis in flight at deploy time flipped its lesson to 'queued'."""
    sb = _fresh("done", jobs=[_support(status="processing")])
    db.requeue_stale_jobs(sb)
    assert sb.tables["jobs"][0]["status"] == "queued"
    assert _gen_status(sb) == "done" and _gen_writes(sb) == []


def test_an_index_book_poison_pill_still_fails_the_book():
    sb = _fresh("queued", jobs=[{"id": "j", "type": "index_book", "status": "processing",
                                 "generation_id": None, "book_id": BOOK, "attempts": 3,
                                 "created_at": "1", "updated_at": "0"}])
    sb.tables["books"] = [{"id": BOOK, "status": "indexing"}]
    db.requeue_stale_jobs(sb, older_than_minutes=15)
    assert sb.tables["books"][0]["status"] == "error"
    assert _gen_writes(sb) == []


# ── run.py's half of the rule ───────────────────────────────────────────
#
# The guard is complete only because worker/run.py finishes a support job
# WITHOUT a generation, on success and on crash. A one-token regression there
# (`db.finish_job(sb, job["id"], gen_id)`) would mark the reported lesson
# 'done' — or 'error' on a crash, voiding its credit — with every client.py
# test green. So run_once itself is driven here, with the agent stubbed.


def _run_once_with(monkeypatch, sb, *, support_raises=None, builder_raises=None):
    import worker.run as run

    import support_agent.agent as agent

    def fake_support(sb_, job):
        if support_raises:
            raise support_raises
        return None

    def fake_builder(sb_, job, gen_id):
        if builder_raises:
            raise builder_raises
        db.finish_job(sb_, job["id"], gen_id)  # process_generation finishes itself

    monkeypatch.setattr(agent, "run_support_job", fake_support)
    monkeypatch.setattr(run, "process_generation", fake_builder)
    monkeypatch.delenv("SUPPORT_AGENT_ENABLED", raising=False)
    return run.run_once(sb)


def test_run_once_finishing_a_support_job_never_writes_the_generation(monkeypatch):
    sb = _fresh("done", jobs=[_support()])
    assert _run_once_with(monkeypatch, sb) is True
    assert sb.tables["jobs"][0]["status"] == "done"
    assert _gen_status(sb) == "done" and _gen_writes(sb) == []


def test_run_once_crashing_a_support_job_never_writes_the_generation(monkeypatch):
    sb = _fresh("done", jobs=[_support()])
    assert _run_once_with(monkeypatch, sb, support_raises=RuntimeError("agent exploded")) is True
    assert sb.tables["jobs"][0]["status"] == "error"
    assert _gen_status(sb) == "done" and _gen_writes(sb) == [], (
        "a crashed diagnosis must not turn a working lesson into an error and void its credit"
    )


def test_run_once_crashing_a_builder_still_writes_error(monkeypatch):
    """The control: the terminal write for a BUILDER is untouched by the guard."""
    sb = _fresh("queued", jobs=[_builder()])
    assert _run_once_with(monkeypatch, sb, builder_raises=RuntimeError("render died")) is True
    assert sb.tables["jobs"][0]["status"] == "error"
    assert _gen_status(sb) == "error"


def test_run_once_finishing_a_builder_still_writes_done(monkeypatch):
    sb = _fresh("queued", jobs=[_builder()])
    assert _run_once_with(monkeypatch, sb) is True
    assert sb.tables["jobs"][0]["status"] == "done"
    assert _gen_status(sb) == "done"


# ── the catalogue's harvest is dispatched in the LAST lane ──────────────
#
# A harvest is cheap on quota but heavy on CPU and egress (it re-downloads and
# re-extracts the PDF), so run_once claims it only when no builder is queued:
# support_diagnose, then documents, then every builder (the generic lane with
# the observer types EXCLUDED), then topic_harvest.


def _harvest(job_id="job-th", status="queued", generation_id=None, created_at="3"):
    return {"id": job_id, "type": "topic_harvest", "status": status, "generation_id": generation_id,
            "book_id": BOOK, "attempts": 0, "created_at": created_at, "updated_at": "0"}


def test_exclude_types_keeps_the_generic_claim_off_the_observer_types():
    """The generic lane must not be able to pick a harvest up — otherwise the
    ordering below would depend on nothing but created_at."""
    sb = _fresh("queued", jobs=[_harvest(created_at="0"), _builder("job-p", "presentation")])
    claimed = db.claim_next_job(sb, exclude_types=db.OBSERVER_JOB_TYPES)
    assert claimed and claimed["type"] == "presentation", "the OLDER harvest is skipped"
    assert db.claim_next_job(sb, exclude_types=db.OBSERVER_JOB_TYPES) is None, "nothing else queued"
    last = db.claim_next_job(sb, job_type="topic_harvest")
    assert last and last["type"] == "topic_harvest"
    assert _gen_status(sb) == "processing", "the builder's claim mirrored onto its generation"


def test_run_once_dispatches_a_topic_harvest_to_the_catalogue(monkeypatch):
    """Called, not grepped: the claim must pick the job up in its own lane
    and hand it to catalogue.harvest.run_harvest_job, which finishes its own
    row. The generations table is never written."""
    import worker.run as run
    import catalogue.harvest as harvest

    sb = _fresh("done", jobs=[_harvest()])
    seen = []

    def fake_harvest(sb_, job):
        seen.append(job["id"])
        db.finish_job(sb_, job["id"])  # what the real one does on success

    monkeypatch.setattr(harvest, "run_harvest_job", fake_harvest)
    monkeypatch.setattr(run, "process_generation",
                        lambda *a, **k: pytest.fail("a harvest must not reach process_generation"))
    monkeypatch.delenv("SUPPORT_AGENT_ENABLED", raising=False)

    assert run.run_once(sb) is True
    assert seen == ["job-th"]
    assert sb.tables["jobs"][0]["status"] == "done"
    assert _gen_status(sb) == "done" and _gen_writes(sb) == []


@pytest.mark.parametrize("kind", ["presentation", "worksheet", "deck", "exam"])
def test_a_queued_builder_is_claimed_before_an_older_harvest(monkeypatch, kind):
    """A harvest filed BEFORE a presentation still waits for it: the harvest
    is the last lane, and the generic lane cannot see it. And even a harvest
    row that somehow carries a generation_id leaves that generation alone."""
    import worker.run as run
    import catalogue.harvest as harvest

    sb = _fresh("queued", jobs=[_harvest(generation_id=GEN, created_at="0"),
                                _builder(job_id="job-b", kind=kind)])
    order = []
    monkeypatch.setattr(harvest, "run_harvest_job",
                        lambda sb_, job: (order.append(job["type"]), db.finish_job(sb_, job["id"])))
    monkeypatch.setattr(run, "process_generation",
                        lambda sb_, job, gen: (order.append(job["type"]), db.finish_job(sb_, job["id"], gen)))
    monkeypatch.delenv("SUPPORT_AGENT_ENABLED", raising=False)

    assert run.run_once(sb) is True
    assert order == [kind]
    assert _gen_status(sb) == "done", "the builder built and finished its generation"
    mark = len(sb.log)
    assert run.run_once(sb) is True
    assert order == [kind, "topic_harvest"]
    assert _gen_writes(sb, mark) == [] and _gen_status(sb) == "done", "the harvest touched no generation"
    assert run.run_once(sb) is False, "the queue is empty"


def test_a_support_diagnosis_still_goes_first(monkeypatch):
    """Moving the harvest to the back must not move the diagnosis with it: a
    teacher is waiting on that answer, so it is claimed ahead of a builder
    filed before it."""
    import worker.run as run
    import support_agent.agent as agent

    sb = _fresh("done", jobs=[_builder("job-p", "presentation", status="queued"),
                              {**_support(), "created_at": "9"}])
    order = []
    monkeypatch.setattr(agent, "run_support_job", lambda sb_, job: order.append(job["type"]))
    monkeypatch.setattr(run, "process_generation",
                        lambda sb_, job, gen: (order.append(job["type"]), db.finish_job(sb_, job["id"], gen)))
    monkeypatch.delenv("SUPPORT_AGENT_ENABLED", raising=False)

    assert run.run_once(sb) is True and order == ["support_diagnose"]
    assert run.run_once(sb) is True and order == ["support_diagnose", "presentation"]


# ── the catalogue's derive shares the LAST lane ─────────────────────────
#
# topic_derive (Phase 2a) is one sequential text call per sub-strand (48 for
# Cambridge 0893): cheap on image quota, but model time a teacher's document
# would otherwise have.
# It is claimed in the same last lane as the harvest — after every builder —
# and hands off to catalogue.derive.run_derive_job, which finishes its own row.


def _derive(job_id="job-td", status="queued", generation_id=None, created_at="3"):
    return {"id": job_id, "type": "topic_derive", "status": status, "generation_id": generation_id,
            "book_id": None, "params": {"curriculum_id": "cur-1"}, "attempts": 0,
            "created_at": created_at, "updated_at": "0"}


def test_exclude_types_keeps_the_generic_claim_off_a_derive():
    sb = _fresh("queued", jobs=[_derive(created_at="0"), _builder("job-p", "presentation")])
    claimed = db.claim_next_job(sb, exclude_types=db.OBSERVER_JOB_TYPES)
    assert claimed and claimed["type"] == "presentation", "the OLDER derive is skipped"
    assert db.claim_next_job(sb, exclude_types=db.OBSERVER_JOB_TYPES) is None
    import worker.run as run
    last = db.claim_next_job(sb, job_type=run.CATALOGUE_JOB_TYPES)
    assert last and last["type"] == "topic_derive"
    assert _gen_status(sb) == "processing", "only the builder's claim mirrored onto its generation"


def test_run_once_dispatches_a_topic_derive_to_the_catalogue(monkeypatch):
    """Called, not grepped: the last lane must pick the job up and hand it to
    catalogue.derive.run_derive_job, which finishes its own row. The
    generations table is never written."""
    import worker.run as run
    import catalogue.derive as derive

    sb = _fresh("done", jobs=[_derive()])
    seen = []

    def fake_derive(sb_, job):
        seen.append((job["id"], job["params"]["curriculum_id"]))
        db.finish_job(sb_, job["id"])  # what the real one does on success

    monkeypatch.setattr(derive, "run_derive_job", fake_derive)
    monkeypatch.setattr(run, "process_generation",
                        lambda *a, **k: pytest.fail("a derive must not reach process_generation"))
    monkeypatch.delenv("SUPPORT_AGENT_ENABLED", raising=False)

    assert run.run_once(sb) is True
    assert seen == [("job-td", "cur-1")]
    assert sb.tables["jobs"][0]["status"] == "done"
    assert _gen_status(sb) == "done" and _gen_writes(sb) == []


@pytest.mark.parametrize("kind", ["presentation", "worksheet", "deck", "exam"])
def test_a_queued_builder_is_claimed_before_an_older_derive(monkeypatch, kind):
    import worker.run as run
    import catalogue.derive as derive

    sb = _fresh("queued", jobs=[_derive(generation_id=GEN, created_at="0"), _builder(job_id="job-b", kind=kind)])
    order = []
    monkeypatch.setattr(derive, "run_derive_job",
                        lambda sb_, job: (order.append(job["type"]), db.finish_job(sb_, job["id"])))
    monkeypatch.setattr(run, "process_generation",
                        lambda sb_, job, gen: (order.append(job["type"]), db.finish_job(sb_, job["id"], gen)))
    monkeypatch.delenv("SUPPORT_AGENT_ENABLED", raising=False)

    assert run.run_once(sb) is True and order == [kind]
    mark = len(sb.log)
    assert run.run_once(sb) is True and order == [kind, "topic_derive"]
    assert _gen_writes(sb, mark) == [] and _gen_status(sb) == "done", "the derive touched no generation"
    assert run.run_once(sb) is False


def test_the_last_lane_takes_harvest_and_derive_by_age(monkeypatch):
    """Neither catalogue job outranks the other: the older is claimed first,
    whichever kind it is."""
    import worker.run as run
    import catalogue.derive as derive
    import catalogue.harvest as harvest

    sb = _fresh("done", jobs=[_harvest(created_at="5"), _derive(created_at="4")])
    order = []
    monkeypatch.setattr(derive, "run_derive_job",
                        lambda sb_, job: (order.append(job["type"]), db.finish_job(sb_, job["id"])))
    monkeypatch.setattr(harvest, "run_harvest_job",
                        lambda sb_, job: (order.append(job["type"]), db.finish_job(sb_, job["id"])))
    monkeypatch.setattr(run, "process_generation", lambda *a, **k: pytest.fail("no builder is queued"))
    monkeypatch.delenv("SUPPORT_AGENT_ENABLED", raising=False)

    assert run.run_once(sb) is True and run.run_once(sb) is True
    assert order == ["topic_derive", "topic_harvest"]
    assert run.run_once(sb) is False
    assert _gen_writes(sb) == []


def test_a_crashing_derive_dispatch_never_writes_the_generation(monkeypatch):
    """run_derive_job never raises; should it ever, run.py's generic failure
    path resolves the generation through the ownership rule and finds none."""
    import worker.run as run
    import catalogue.derive as derive

    sb = _fresh("done", jobs=[_derive(generation_id=GEN)])
    monkeypatch.setattr(derive, "run_derive_job", lambda sb_, job: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(run, "process_generation", lambda *a, **k: pytest.fail("not a builder"))
    monkeypatch.delenv("SUPPORT_AGENT_ENABLED", raising=False)

    assert run.run_once(sb) is True
    assert sb.tables["jobs"][0]["status"] == "error"
    assert _gen_status(sb) == "done" and _gen_writes(sb) == []


# ── Phase 2b: topic_article and figure_render share the LAST lane ────────
#
# topic_article is one 16k-token text call; figure_render is the one catalogue
# job that may make IMAGE calls (it re-checks the queue itself before each one,
# see tests/test_catalogue_figures.py). Both are claimed after every builder and
# hand off to a run_* function that finishes its own row.


def _article(job_id="job-ta", status="queued", generation_id=None, created_at="3"):
    return {"id": job_id, "type": "topic_article", "status": status, "generation_id": generation_id,
            "book_id": None, "params": {"topic_id": "t-1"}, "attempts": 0, "created_at": created_at, "updated_at": "0"}


def _figure(job_id="job-fr", status="queued", generation_id=None, created_at="3"):
    return {"id": job_id, "type": "figure_render", "status": status, "generation_id": generation_id,
            "book_id": None, "params": {"article_id": "art-1"}, "attempts": 0, "created_at": created_at, "updated_at": "0"}


@pytest.mark.parametrize("make", [_article, _figure])
def test_exclude_types_keeps_the_generic_claim_off_the_phase_2b_jobs(make):
    sb = _fresh("queued", jobs=[make(created_at="0"), _builder("job-p", "presentation")])
    claimed = db.claim_next_job(sb, exclude_types=db.OBSERVER_JOB_TYPES)
    assert claimed and claimed["type"] == "presentation", "the OLDER catalogue job is skipped"
    assert db.claim_next_job(sb, exclude_types=db.OBSERVER_JOB_TYPES) is None
    import worker.run as run
    last = db.claim_next_job(sb, job_type=run.CATALOGUE_JOB_TYPES)
    assert last and last["type"] == make()["type"]
    assert _gen_status(sb) == "processing", "only the builder's claim mirrored onto its generation"


def test_run_once_dispatches_a_topic_article_to_the_catalogue(monkeypatch):
    """Called, not grepped: the last lane must pick the job up and hand it to
    catalogue.article.run_article_job, which finishes its own row."""
    import worker.run as run
    import catalogue.article as article

    sb = _fresh("done", jobs=[_article()])
    seen = []

    def fake_article(sb_, job):
        seen.append((job["id"], job["params"]["topic_id"]))
        db.finish_job(sb_, job["id"])

    monkeypatch.setattr(article, "run_article_job", fake_article)
    monkeypatch.setattr(run, "process_generation",
                        lambda *a, **k: pytest.fail("an article must not reach process_generation"))
    monkeypatch.delenv("SUPPORT_AGENT_ENABLED", raising=False)

    assert run.run_once(sb) is True
    assert seen == [("job-ta", "t-1")]
    assert sb.tables["jobs"][0]["status"] == "done"
    assert _gen_status(sb) == "done" and _gen_writes(sb) == []


def test_run_once_dispatches_a_figure_render_to_the_catalogue(monkeypatch):
    import worker.run as run
    import catalogue.figures as figures

    sb = _fresh("done", jobs=[_figure()])
    seen = []

    def fake_figures(sb_, job):
        seen.append((job["id"], job["params"]["article_id"]))
        db.finish_job(sb_, job["id"])

    monkeypatch.setattr(figures, "run_figure_render_job", fake_figures)
    monkeypatch.setattr(run, "process_generation",
                        lambda *a, **k: pytest.fail("a figure render must not reach process_generation"))
    monkeypatch.delenv("SUPPORT_AGENT_ENABLED", raising=False)

    assert run.run_once(sb) is True
    assert seen == [("job-fr", "art-1")]
    assert sb.tables["jobs"][0]["status"] == "done"
    assert _gen_status(sb) == "done" and _gen_writes(sb) == []


@pytest.mark.parametrize("kind", ["presentation", "worksheet", "deck", "exam"])
@pytest.mark.parametrize("make,module_name,fn", [(_article, "catalogue.article", "run_article_job"),
                                                 (_figure, "catalogue.figures", "run_figure_render_job")])
def test_a_queued_builder_is_claimed_before_an_older_phase_2b_job(monkeypatch, kind, make, module_name, fn):
    import importlib
    import worker.run as run

    module = importlib.import_module(module_name)
    sb = _fresh("queued", jobs=[make(generation_id=GEN, created_at="0"), _builder(job_id="job-b", kind=kind)])
    order = []
    monkeypatch.setattr(module, fn, lambda sb_, job: (order.append(job["type"]), db.finish_job(sb_, job["id"])))
    monkeypatch.setattr(run, "process_generation",
                        lambda sb_, job, gen: (order.append(job["type"]), db.finish_job(sb_, job["id"], gen)))
    monkeypatch.delenv("SUPPORT_AGENT_ENABLED", raising=False)

    assert run.run_once(sb) is True and order == [kind]
    mark = len(sb.log)
    assert run.run_once(sb) is True and order == [kind, make()["type"]]
    assert _gen_writes(sb, mark) == [] and _gen_status(sb) == "done", "the catalogue job touched no generation"
    assert run.run_once(sb) is False


def test_the_last_lane_takes_all_four_catalogue_jobs_by_age(monkeypatch):
    import worker.run as run
    import catalogue.article as article
    import catalogue.derive as derive
    import catalogue.figures as figures
    import catalogue.harvest as harvest

    sb = _fresh("done", jobs=[_harvest(created_at="5"), _figure(created_at="2"), _derive(created_at="4"),
                              _article(created_at="3")])
    order = []
    for module, fn in ((derive, "run_derive_job"), (harvest, "run_harvest_job"), (article, "run_article_job"),
                       (figures, "run_figure_render_job")):
        monkeypatch.setattr(module, fn, lambda sb_, job: (order.append(job["type"]), db.finish_job(sb_, job["id"])))
    monkeypatch.setattr(run, "process_generation", lambda *a, **k: pytest.fail("no builder is queued"))
    monkeypatch.delenv("SUPPORT_AGENT_ENABLED", raising=False)

    assert all(run.run_once(sb) for _ in range(4))
    assert order == ["figure_render", "topic_article", "topic_derive", "topic_harvest"]
    assert run.run_once(sb) is False and _gen_writes(sb) == []


@pytest.mark.parametrize("make,module_name,fn", [(_article, "catalogue.article", "run_article_job"),
                                                 (_figure, "catalogue.figures", "run_figure_render_job")])
def test_a_crashing_phase_2b_dispatch_never_writes_the_generation(monkeypatch, make, module_name, fn):
    import importlib
    import worker.run as run

    module = importlib.import_module(module_name)
    sb = _fresh("done", jobs=[make(generation_id=GEN)])
    monkeypatch.setattr(module, fn, lambda sb_, job: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(run, "process_generation", lambda *a, **k: pytest.fail("not a builder"))
    monkeypatch.setenv("SUPPORT_AGENT_ENABLED", "1")   # even with the agent on, no issue is filed for an observer

    assert run.run_once(sb) is True
    assert sb.tables["jobs"][0]["status"] == "error"
    assert _gen_status(sb) == "done" and _gen_writes(sb) == []
    assert sb.tables.get("platform_issues", []) == []


# ── Phase 3: catalogue KIT generations take the LAST lane, by FLAG ──────
#
# A kit's rows are ordinary builder types (presentation, deck, documents)
# owned by the system account; migration 0115 stamps params.catalogue = true
# on their job rows. The user lanes pass catalogue=False and can never see
# them; run.py's last lane claims them only when no user builder is live
# (catalogue.figures.builder_queued, which ignores the flagged ones) and the
# off-peak window is open (catalogue.kit.catalogue_window_open).


def _kit_job(job_id="job-kit", kind="presentation", status="queued", created_at="0", gen_id="gen-kit"):
    return {"id": job_id, "type": kind, "status": status, "generation_id": gen_id, "book_id": None,
            "params": {"catalogue": True, "topic_id": "t-1", "kit_id": "k-1"}, "attempts": 0,
            "created_at": created_at, "updated_at": "0"}


def _kit_sb(*jobs, gen_status="queued"):
    sb = _fresh(gen_status, jobs=jobs)
    sb.tables["generations"].append({"id": "gen-kit", "status": "queued", "kind": "presentation",
                                     "owner_id": "sys", "book_id": None})
    return sb


def _kit_gen(sb):
    return next(g for g in sb.tables["generations"] if g["id"] == "gen-kit")


def test_user_lanes_never_claim_a_catalogue_flagged_job():
    import worker.run as run

    for kind, lane in (("activity", {"job_type": run.DOC_JOB_TYPES}),
                       ("presentation", {"exclude_types": db.OBSERVER_JOB_TYPES})):
        sb = _kit_sb(_kit_job(kind=kind))
        assert db.claim_next_job(sb, catalogue=False, **lane) is None
        assert db.claim_next_job(sb, job_type="support_diagnose", catalogue=False) is None
        assert db.claim_next_job(sb, job_type=run.CATALOGUE_JOB_TYPES) is None, "not an observer type either"
        assert sb.tables["jobs"][0]["status"] == "queued" and _kit_gen(sb)["status"] == "queued"
        claimed = db.claim_next_job(sb, exclude_types=db.OBSERVER_JOB_TYPES, catalogue=True)
        assert claimed and claimed["id"] == "job-kit"
        assert _kit_gen(sb)["status"] == "processing", "a kit row is a BUILDER's: its claim is mirrored"


def test_the_flag_filter_keeps_every_user_job_however_its_params_look():
    # params NULL, params without the key, params.catalogue false: all users'
    sb = _fresh("queued", jobs=[{**_builder("j1"), "params": None, "created_at": "1"},
                                {**_builder("j2", "activity"), "params": {"part": 2}, "created_at": "2"},
                                {**_builder("j3", "deck"), "params": {"catalogue": False}, "created_at": "3"}])
    seen = []
    while True:
        j = db.claim_next_job(sb, catalogue=False)
        if not j:
            break
        seen.append(j["id"])
    assert seen == ["j1", "j2", "j3"]
    assert db.claim_next_job(sb, catalogue=True) is None


def test_a_user_builder_is_claimed_before_an_older_kit_job(monkeypatch):
    import worker.run as run
    import catalogue.kit as ck

    sb = _kit_sb(_kit_job(created_at="0"), _builder("job-w", "worksheet"))
    order = []
    monkeypatch.setattr(run, "process_generation",
                        lambda sb_, job, gen: (order.append(job["id"]), db.finish_job(sb_, job["id"], gen)))
    monkeypatch.setattr(ck, "catalogue_window_open", lambda now=None, **kw: True)
    monkeypatch.delenv("SUPPORT_AGENT_ENABLED", raising=False)
    assert run.run_once(sb) is True and order == ["job-w"], "the teacher's worksheet first, though the kit is older"
    assert run.run_once(sb) is True and order == ["job-w", "job-kit"]
    assert run.run_once(sb) is False


def test_the_kit_lane_needs_no_live_user_builder_and_an_open_window(monkeypatch):
    import worker.run as run
    import catalogue.kit as ck

    sb = _kit_sb(_kit_job(), _builder("job-live", "presentation", status="processing"))
    order = []
    monkeypatch.setattr(run, "process_generation",
                        lambda sb_, job, gen: (order.append(job["id"]), db.finish_job(sb_, job["id"], gen)))
    monkeypatch.delenv("SUPPORT_AGENT_ENABLED", raising=False)
    monkeypatch.setattr(ck, "catalogue_window_open", lambda now=None, **kw: True)
    assert run.run_once(sb) is False, "a teacher's presentation is being built: the kit waits"
    sb.tables["jobs"] = [j for j in sb.tables["jobs"] if j["id"] != "job-live"]
    monkeypatch.setattr(ck, "catalogue_window_open", lambda now=None, **kw: False)
    assert run.run_once(sb) is False, "outside the window the kit waits"
    monkeypatch.setattr(ck, "catalogue_window_open", lambda now=None, **kw: True)
    assert run.run_once(sb) is True and order == ["job-kit"]
    assert _kit_gen(sb)["status"] == "done"


def test_a_queued_kit_job_does_not_hold_another_kit_job_back(monkeypatch):
    """builder_queued ignores the flag: two queued kit rows drain in order."""
    import worker.run as run
    import catalogue.kit as ck

    sb = _kit_sb(_kit_job("job-k1", created_at="1"), _kit_job("job-k2", kind="activity", created_at="2"))
    order = []
    monkeypatch.setattr(run, "process_generation",
                        lambda sb_, job, gen: (order.append(job["id"]), db.finish_job(sb_, job["id"], gen)))
    monkeypatch.setattr(ck, "catalogue_window_open", lambda now=None, **kw: True)
    monkeypatch.delenv("SUPPORT_AGENT_ENABLED", raising=False)
    assert run.run_once(sb) is True and run.run_once(sb) is True and order == ["job-k1", "job-k2"]


def test_builder_queued_ignores_catalogue_jobs_and_observers():
    from catalogue.figures import builder_queued

    sb = _kit_sb(_kit_job())
    assert builder_queued(sb) is False, "a queued kit row is not a user waiting"
    sb.tables["jobs"].append(_kit_job("job-k2", status="processing", created_at="2"))
    assert builder_queued(sb) is False
    sb.tables["jobs"].append(_harvest())
    assert builder_queued(sb) is False, "observers never counted"
    sb.tables["jobs"].append(_builder("job-u", "worksheet"))
    assert builder_queued(sb) is True
    sb.tables["jobs"][-1]["status"] = "processing"
    assert builder_queued(sb) is True
    sb.tables["jobs"][-1]["status"] = "done"
    assert builder_queued(sb) is False


def test_topic_questions_is_an_observer_dispatched_to_the_catalogue(monkeypatch):
    """Called, not grepped: the last lane picks the job up and hands it to
    catalogue.questions.run_questions_job (W2's module; a stand-in is
    installed so this test does not depend on its presence)."""
    import sys
    import types
    import worker.run as run

    assert "topic_questions" in db.OBSERVER_JOB_TYPES
    assert "topic_questions" in run.OBSERVER_JOB_TYPES and "topic_questions" in run.CATALOGUE_JOB_TYPES
    assert db.generation_to_mirror({"type": "topic_questions", "generation_id": GEN}) is None
    seen = []
    mod = types.ModuleType("catalogue.questions")

    def run_questions_job(sb_, job):
        seen.append((job["id"], job["params"]["topic_id"]))
        db.finish_job(sb_, job["id"])

    mod.run_questions_job = run_questions_job
    monkeypatch.setitem(sys.modules, "catalogue.questions", mod)
    sb = _fresh("done", jobs=[{"id": "job-tq", "type": "topic_questions", "status": "queued", "generation_id": GEN,
                               "book_id": None, "params": {"topic_id": "t-1", "language": "en"}, "attempts": 0,
                               "created_at": "1", "updated_at": "0"}])
    monkeypatch.setattr(run, "process_generation", lambda *a, **k: pytest.fail("not a builder"))
    monkeypatch.delenv("SUPPORT_AGENT_ENABLED", raising=False)
    assert run.run_once(sb) is True and seen == [("job-tq", "t-1")]
    assert sb.tables["jobs"][0]["status"] == "done"
    assert _gen_status(sb) == "done" and _gen_writes(sb) == [], "an observer: the generation it names is untouched"


def test_a_failed_kit_job_files_no_support_issue(monkeypatch):
    import worker.run as run
    import catalogue.kit as ck

    sb = _kit_sb(_kit_job())
    monkeypatch.setenv("SUPPORT_AGENT_ENABLED", "1")
    monkeypatch.setattr(ck, "catalogue_window_open", lambda now=None, **kw: True)

    def _die(*a, **k):
        raise RuntimeError("kit died")

    monkeypatch.setattr(run, "process_generation", _die)
    assert run.run_once(sb) is True
    assert sb.tables["jobs"][0]["status"] == "error" and _kit_gen(sb)["status"] == "error"
    assert sb.tables.get("platform_issues", []) == [], "the reviewer sees the kit row; no console issue for nobody"


# ── Phase 3 review fixes: the window margin, and a kit failed from anywhere ──


def _kit_row(status="generating"):
    return {"id": "k-1", "topic_id": "t-1", "status": status, "notes": None,
            "presentation_generation_id": "gen-kit", "doc_generation_ids": {}}


def test_a_presentation_kit_waits_for_the_margin_but_a_document_does_not(monkeypatch):
    """The lane asks the window twice: open NOW (any kind) and open for the
    presentation margin too (a 30-60 minute render must not run into users'
    hours). Near the window's end only documents and decks are claimed."""
    import worker.run as run
    import catalogue.kit as ck

    sb = _kit_sb(_kit_job("job-pres", kind="presentation", created_at="1"),
                 _kit_job("job-act", kind="activity", created_at="2", gen_id="gen-act"))
    sb.tables["generations"].append({"id": "gen-act", "status": "queued", "kind": "activity", "owner_id": "sys",
                                     "book_id": None})
    order = []
    monkeypatch.setattr(run, "process_generation",
                        lambda sb_, job, gen: (order.append(job["id"]), db.finish_job(sb_, job["id"], gen)))
    monkeypatch.delenv("SUPPORT_AGENT_ENABLED", raising=False)
    # open now, but not for the presentation margin
    monkeypatch.setattr(ck, "catalogue_window_open", lambda now=None, margin_minutes=0: margin_minutes == 0)
    assert run.run_once(sb) is True and order == ["job-act"], "the older presentation is skipped near the window's end"
    assert run.run_once(sb) is False, "and stays queued"
    assert sb.tables["jobs"][0]["status"] == "queued" and _kit_gen(sb)["status"] == "queued"
    # the margin fits: the presentation is claimed
    monkeypatch.setattr(ck, "catalogue_window_open", lambda now=None, margin_minutes=0: True)
    assert run.run_once(sb) is True and order == ["job-act", "job-pres"]


def test_the_lane_passes_the_configured_presentation_margin(monkeypatch):
    import worker.run as run
    import catalogue.kit as ck

    sb = _kit_sb(_kit_job())
    asked = []
    monkeypatch.setattr(ck, "catalogue_window_open",
                        lambda now=None, margin_minutes=0: (asked.append(margin_minutes), True)[1])
    monkeypatch.setenv("CATALOGUE_PRESENTATION_MARGIN_MIN", "45")
    assert run._claim_catalogue_generation(sb) is not None
    assert asked == [0, 45]


def test_the_reaper_fails_the_kit_of_a_poison_pill_kit_job():
    """A kit's presentation that kept crashing the worker is auto-failed after
    three requeues, like any builder — and its kit goes 'failed' with it, or
    the portal keeps Generate disabled on a kit that reads 'generating' and
    offers no Retry."""
    sb = _kit_sb(_kit_job(status="processing"))
    sb.tables["jobs"][0]["attempts"] = 3
    sb.tables["topic_kits"] = [_kit_row()]
    assert db.requeue_stale_jobs(sb) == 0
    assert sb.tables["jobs"][0]["status"] == "error" and _kit_gen(sb)["status"] == "error"
    kit = sb.tables["topic_kits"][0]
    assert kit["status"] == "failed" and "Auto-failed after 3 attempts" in kit["notes"] and kit["notes"].startswith("presentation:")


def test_the_reaper_leaves_a_reviewed_kit_and_a_users_job_alone():
    sb = _kit_sb(_kit_job(status="processing"), _builder("job-u", status="processing", attempts=3))
    sb.tables["jobs"][0]["attempts"] = 3
    sb.tables["topic_kits"] = [_kit_row(status="in_review")]
    db.requeue_stale_jobs(sb)
    assert sb.tables["topic_kits"][0]["status"] == "in_review", "guarded from generating only"
    assert [j["status"] for j in sb.tables["jobs"]] == ["error", "error"]
    assert db.kit_id_of({"catalogue": True, "kit_id": "k-9"}) == "k-9"
    assert db.kit_id_of({"kit_id": "k-9"}) is None and db.kit_id_of(None) is None, "a user's row names no kit"


def test_a_kit_job_failing_before_its_prelude_still_fails_its_kit(monkeypatch):
    """process.py marks the kit failed when the build fails; a failure BEFORE
    that (the generation row unreadable, taken down, the tier unresolvable)
    used to leave the kit 'generating' forever. run.py's failure path now
    writes it too — guarded, so a kit process.py already failed is untouched."""
    import worker.run as run
    import catalogue.kit as ck

    sb = _kit_sb(_kit_job())
    sb.tables["topic_kits"] = [_kit_row()]
    monkeypatch.delenv("SUPPORT_AGENT_ENABLED", raising=False)
    monkeypatch.setattr(ck, "catalogue_window_open", lambda now=None, **kw: True)

    def _die(*a, **k):
        raise RuntimeError("content removed")

    monkeypatch.setattr(run, "process_generation", _die)
    assert run.run_once(sb) is True
    kit = sb.tables["topic_kits"][0]
    assert kit["status"] == "failed" and kit["notes"] == "presentation failed: content removed"

    # the tier-exhausted path is the same
    sb = _kit_sb(_kit_job())
    sb.tables["jobs"][0]["attempts"] = 3
    sb.tables["topic_kits"] = [_kit_row()]

    def _tier(*a, **k):
        raise db.TransientTierError("rpc timed out")

    monkeypatch.setattr(run, "process_generation", _tier)
    assert run.run_once(sb) is True
    assert sb.tables["topic_kits"][0]["status"] == "failed"
    assert "plan tier unresolvable" in sb.tables["topic_kits"][0]["notes"]
    assert sb.tables.get("platform_issues", []) == []


def test_the_flag_probe_counts_queued_kit_jobs_that_0115_did_not_stamp(caplog):
    """Deploy order: a kit row inserted before migration 0115 carries no
    jobs.params.catalogue, and the user lanes would claim it. The boot probe
    names those rows loudly; a stamped queue is a quiet OK."""
    sb = _kit_sb(_kit_job())
    assert db.probe_catalogue_job_flags(sb) == 0
    sb.tables["generations"][-1]["params"] = {"catalogue": True, "topic_id": "t-1", "kit_id": "k-1"}
    sb.tables["jobs"][0]["params"] = None                       # never stamped
    sb.tables["jobs"].append(_builder("job-u"))                 # a user's row: params None, generation unflagged
    caplog.set_level(logging.CRITICAL, logger="worker")
    assert db.probe_catalogue_job_flags(sb) == 1
    assert "CATALOGUE FLAG CHECK FAILED" in caplog.text and "job-kit" in caplog.text
