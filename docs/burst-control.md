# Burst control — admission to the shared model pool

Since 2026-10-14 (worker PR #213, migration `database/0125_provider_gate.sql`).
Companion to `docs/phase-timings.md`, which measures, and to the root-cause
note `SketchCast Vertex 429 Root Cause 2026-10-14.md`, which explains why.

## The problem in one line

Both Gemini models the worker calls on Vertex — `gemini-3.5-flash` for
scripts, `gemini-3-pro-image` for pictures, both on the `global` endpoint —
run on Google's shared pay-as-you-go pool. A 429 means the pool is busy now;
there is no per-project quota to raise; sending more buys refusals. On
2026-10-13 the worker pushed 12–14 image requests a minute at a pool yielding
3–5 and was refused three times in four, and four kits queued together in
peak hours ran four retry ladders against the same busy script model until
two of them failed after 32 minutes.

## What the worker does now

### 1. One gate per provider (`shared/provider_gate.py`)

Every image call (`raster_assets._with_backoff`, kind `image`) and every text
call (`gemini_client._call`) goes through `provider_gate.gate("vertex_image")`
or `gate("vertex_text")`.

| Control | Image | Text | How it moves |
|---|---|---|---|
| In flight, per process | 3 | 4 | fixed (env) |
| In flight, deployment-wide (lease) | 3 | 4 | fixed (env); only with the store configured |
| Pace, per minute | start 4 · floor 2 · ceiling 12 | unpaced | **learned**: +1 after 5 clean calls, halved on a refusal |
| Cooldown after a refusal | 30 s | 20 s | or the server's Retry-After if longer, capped at 120 s |

The pace is additive-increase / multiplicative-decrease: the worker climbs
until it is refused, halves, and climbs again, so nobody has to know what
the pool gives at a particular hour. A refusal silences *every* caller on
that provider for the cooldown, which is what stops threads refused together
from coming back together; the retry wait on top keeps the measured
half-spread jitter (`backoff_seconds`) and honours Retry-After.

Each wait lands on the generation's phase clock: `pacing_wait`,
`cooldown_wait`, `lease_wait`, `rate_limit_wait`, with the counts
`rate_limit_refusals_image`, `rate_limit_refusals_text` and
`rate_limit_retries`.

Vision calls keep their own per-minute window and `_model_gate()` slot; they
have never been throttled in a measured lesson.

### 2. Shared across replicas (migration 0125)

`worker/run.py` calls `provider_gate.configure(sb)` at boot. With the
migration applied:

- the in-flight cap is a **lease** in `provider_leases` (TTL 600 s, so a
  crashed holder frees its slot), taken through `acquire_provider_lease` and
  released through `release_provider_lease`;
- the learned pace and cooldown live in `provider_pacing`, written through
  `provider_outcome` after every call and adopted by every replica, so a
  replica that has never seen a 429 starts at the rate its peers learned and
  goes quiet when a peer is refused.

Without the migration, or with `PROVIDER_GATE_GLOBAL=0`, the gates are local
and behave identically for one process. A store failure is logged once a
minute and the gate carries on locally — nothing in this module can fail a
lesson.

**What is and is not global today** (one replica runs in production):

| Shared across replicas | Per process |
|---|---|
| in-flight cap (lease) | the per-minute send window (the *rate* is shared, the *timestamps* are not — N replicas send N × rate) |
| learned rate, cooldown | |

Before Railway runs more than one worker replica, the send window needs a
database-backed permit or a single dispatcher; the kit admission read
(section 3) needs a transactional claim; and the lease should renew during a
text call, whose HTTP timeout equals the lease TTL. The SQL
`provider_outcome` function also applies no cap to a server-supplied
Retry-After, where the Python side caps it at 120 s; the two must agree.
These are the follow-up hardening items, listed so they are not forgotten.

### 3. Scheduling (`worker/run.py`)

- **`MAX_CATALOGUE_JOBS`** (default 2): catalogue observer jobs — figure
  renders above all — processing at once, counted from the `jobs` table
  (`catalogue_jobs_processing`), so the cap holds across replicas, not per
  process. Nineteen figure jobs now run two at a time instead of sixteen.
- **`MAX_CATALOGUE_KITS`** (default 1): kits generating at once, read from
  `jobs` (`catalogue_kits_processing`), whatever `CATALOGUE_WINDOW_UTC` says.
  While a kit generates only its own remaining jobs are claimed
  (`claim_next_job(kit_ids=…)`): the deck waits for its video, the documents
  take a minute each; the next kit starts when they are done. Teachers are
  never under either rule — both read only `params.catalogue` rows and the
  user lanes never consult them.

### 4. Recoverable refusals (`worker/client.py`)

A quota refusal that outlasts the client's retries defers the job — back in
the queue, invisible for a step, no attempt spent — until it has waited its
whole budget:

| Job | Step | Budget |
|---|---|---|
| a teacher's | `RATE_LIMIT_DEFER_SECONDS` 180 s | `RATE_LIMIT_MAX_WAIT_SECONDS` 1 800 s |
| a catalogue kit's | `CATALOGUE_RATE_LIMIT_DEFER_SECONDS` 300 s | `CATALOGUE_RATE_LIMIT_MAX_WAIT_SECONDS` 10 800 s |

A spent budget goes to `error` with a message that begins
`paused after …s of provider refusals (429): nothing is wrong with this job`
and names Retry as the way back (`rate_limit_exhausted_message`); a kit is
marked failed with the same text so the console shows Retry. Nothing is
deleted: sibling jobs that finished keep their artifacts, and the row keeps
`params.deferred_since` and `deferred_note` so the history is readable.

### 5. A deferral is not an outcome (`worker/process.py`)

A job that raises `DeferredJob` — a deck waiting for its video, a kit waiting
out a busy pool — records no `params.timings`; it is clocked afresh when it
runs. Before this, every deferred deck carried a false `outcome: error`.

## Knobs

```
PROVIDER_VERTEX_IMAGE_IN_FLIGHT=3         PROVIDER_VERTEX_TEXT_IN_FLIGHT=4
PROVIDER_VERTEX_IMAGE_GLOBAL_IN_FLIGHT=3  PROVIDER_VERTEX_TEXT_GLOBAL_IN_FLIGHT=4
PROVIDER_VERTEX_IMAGE_START_PER_MIN=4     PROVIDER_VERTEX_IMAGE_FLOOR_PER_MIN=2
PROVIDER_VERTEX_IMAGE_CEILING_PER_MIN=12  PROVIDER_VERTEX_IMAGE_STEP_AFTER=5
PROVIDER_VERTEX_IMAGE_COOLDOWN_SECS=30    PROVIDER_VERTEX_TEXT_COOLDOWN_SECS=20
PROVIDER_VERTEX_IMAGE_MAX_COOLDOWN_SECS=120
PROVIDER_GATE_GLOBAL=1
MAX_CATALOGUE_JOBS=2        MAX_CATALOGUE_KITS=1
CATALOGUE_RATE_LIMIT_DEFER_SECONDS=300    CATALOGUE_RATE_LIMIT_MAX_WAIT_SECONDS=10800
```

The old `MODEL_CALL_CONCURRENCY` (image in-flight cap) and
`IMAGE_CALLS_PER_MINUTE` (pace ceiling) still count when the new names are
unset. Changing a knob is a Railway variable edit; the gates read the
environment when first built, so it takes effect on the next deploy or
restart.

## Rollback

No code change needed for any of these:

| Symptom | Lever |
|---|---|
| the shared store misbehaves | `PROVIDER_GATE_GLOBAL=0` — gates go local |
| kits too slow, one at a time is too strict | `MAX_CATALOGUE_KITS=99`, `MAX_CATALOGUE_JOBS=99` — the old scheduling |
| pacing too timid | raise `PROVIDER_VERTEX_IMAGE_START_PER_MIN` / `_FLOOR_PER_MIN` |
| a kit paused on a spent budget | the console's Retry re-runs it; `CATALOGUE_RATE_LIMIT_MAX_WAIT_SECONDS` lengthens the budget |

The migration is additive (two tables, three functions, service-role only);
the worker runs without it.

## Reading the result

The learned pace right now, per provider:

```sql
select provider, rate_per_min, cooldown_until, successes, refusals, updated_at
from public.provider_pacing;
```

Leases in flight (expired ones are swept on the next acquire):

```sql
select provider, count(*) as in_flight, min(acquired_at) as oldest
from public.provider_leases where expires_at > now() group by provider;
```

Per day, from the timings every generation now records:

```sql
select created_at::date as day, count(*) as videos,
       sum((params->'timings'->'counts'->>'rate_limit_refusals_image')::int) as image_refusals,
       sum((params->'timings'->'counts'->>'rate_limit_refusals_text')::int)  as text_refusals,
       round(sum((params->'timings'->'phases'->>'cooldown_wait')::numeric) / 60, 1) as cooldown_min,
       round(sum((params->'timings'->'phases'->>'pacing_wait')::numeric) / 60, 1)   as pacing_min,
       round(sum((params->'timings'->'phases'->>'lease_wait')::numeric) / 60, 1)    as lease_min,
       count(*) filter (where params->'timings'->>'outcome' = 'error') as failed
from public.generations
where kind = 'presentation' and params ? 'timings'
group by 1 order by 1 desc;
```

Kits paused on a spent budget (the console shows Retry for these):

```sql
select left(id::text, 8) as job, type, updated_at, left(error, 120) as error
from public.jobs
where status = 'error' and error like 'paused after%'
order by updated_at desc;
```

Success looks like: refusals per video falling towards zero, no kit failing
on a 429, and waiting that shows up as `cooldown_wait` on a job that finished
rather than as a job that did not. The measure that matters is completed
kits per hour, not requests per minute.

## Validation after a deploy

1. One kit alone, in the off-peak window: confirm `provider_pacing` gains a
   row per provider and the kit's `timings` carry the new counters.
2. Two kits queued together: confirm the second kit's jobs stay `queued`
   until the first kit's are done (`catalogue_kits_processing` has one id).
3. A figure batch of more than two jobs: confirm at most two are
   `processing` at any moment.
4. A teacher's worksheet queued while a kit generates: confirm it is claimed
   and finished at once.
5. Then the daily query above against the 2026-10-13 baseline (150 image
   refusals in 23 minutes; two kits lost to text refusals).
