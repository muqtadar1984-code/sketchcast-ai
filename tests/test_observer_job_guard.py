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

    def order(self, *a, **k):
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

    def _match(self, row):
        for kind, col, val in self.filters:
            v = row.get(col)
            if kind == "eq" and v != val:
                return False
            if kind == "neq" and v == val:
                return False
            if kind == "in" and v not in val:
                return False
            if kind == "not_in" and v in val:
                return False
            if kind == "lt" and not (v is not None and v < val):
                return False
        return True

    def execute(self):
        rows = [r for r in self.store.tables.setdefault(self.table, []) if self._match(r)]
        if self.op == "select":
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
