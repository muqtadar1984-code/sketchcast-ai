# Burst control — admission to the shared model pool

Since 2026-10-14: worker PR #213 (the gates) and the hardening PR that
followed it (migration `database/0126_provider_gate_hardening.sql`), which
made every decision deployment-wide. Companion to `docs/phase-timings.md`,
which measures, and to the root-cause note
`SketchCast Vertex 429 Root Cause 2026-10-14.md`, which explains why.

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
or `gate("vertex_text")`. Three controls, every one of them decided **once
for the whole deployment** when the store is configured:

| Control | Image | Text | How it moves | Where it is decided |
|---|---|---|---|---|
| In flight | 3 | 4 | fixed (env) | a **lease** in `provider_leases`, plus a per-process semaphore |
| Pace, per minute | start 4 · floor 2 · ceiling 12 | unpaced | **learned**: +1 after 5 clean calls, halved on a refusal | `provider_permit`: the send window lives in `provider_sends`, under the provider's advisory lock |
| Cooldown after a refusal | 30 s | 20 s | or the server's Retry-After if longer, **capped at 120 s** in both languages | `provider_outcome` writes it, `provider_permit` enforces it |

The pace is additive-increase / multiplicative-decrease: the worker climbs
until it is refused, halves, and climbs again, so nobody has to know what
the pool gives at a particular hour. A refusal silences *every* caller on
the provider — on every replica — for the cooldown; the retry wait on top
keeps the measured half-spread jitter (`backoff_seconds`) and honours
Retry-After.

**The admission sequence** (`ProviderGate.acquire`): take a lease (the
deployment-wide in-flight cap; wait with jitter while it is full), then ask
the store for a permit (cooldown first, then the one-minute window). A
denied permit names the wait; the gate sleeps it in 5 s chunks and asks
again. A wait longer than 10 s hands the lease back meanwhile, so a slot is
never held by someone who may not send. Only then is the process semaphore
taken and the call made. Every wait lands on the generation's phase clock:
`lease_wait`, `cooldown_wait`, `pacing_wait`, `rate_limit_wait`, with the
counts `rate_limit_refusals_image`, `rate_limit_refusals_text`,
`rate_limit_retries`.

**Leases are short and renewed.** A lease lasts 180 s; a background thread
per gate renews every lease this process holds every 45 s
(`renew_provider_lease`), so a ten-minute script call never outlives its
lease and a crashed holder frees its slot within three minutes. A lease the
store has already swept is forgotten; the call it guarded runs on,
uncounted, and the event is logged.

**A lost store degrades, never bursts.** When the database does not answer,
the gate runs DEGRADED: one call in flight per process
(`PROVIDER_<X>_DEGRADED_IN_FLIGHT`), the floor rate locally, the local
cooldown — and says so once a minute. It returns to the shared rules on the
first answer. Without any store at all (`PROVIDER_GATE_GLOBAL=0`, tools,
tests) the gate is local with the normal per-process settings.

Vision calls keep their own per-minute window and `_model_gate()` slot; they
have never been throttled in a measured lesson.

### 2. Catalogue admission is one transaction (`worker/run.py`, `worker/client.py`)

Both catalogue lanes claim through the database, under one advisory lock,
with the admission rule and the claim in the same transaction — two workers
polling together cannot both start a kit, or both take the cap's last slot:

- **`claim_catalogue_kit_job(exclude_types, MAX_CATALOGUE_KITS)`** — while
  `MAX_CATALOGUE_KITS` (default 1) kits have a processing job anywhere, only
  *their* remaining jobs are candidates (the deck waits for its video, the
  documents take a minute each); otherwise the oldest queued kit job starts.
  Whatever `CATALOGUE_WINDOW_UTC` says.
- **`claim_catalogue_observer_job(types, MAX_CATALOGUE_JOBS)`** — figure
  renders, harvests and the rest, at most `MAX_CATALOGUE_JOBS` (default 2)
  processing across the deployment.

Both **fail closed**: when the database does not answer, no catalogue job is
claimed (logged once a minute). Teachers are never under either rule — the
user lanes claim through the ordinary query and never consult them — and a
kit's jobs, once claimed, are processed exactly as before.

### 3. Recoverable refusals (`worker/client.py`)

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

### 4. A deferral is not an outcome (`worker/process.py`)

A job that raises `DeferredJob` — a deck waiting for its video, a kit waiting
out a busy pool — records no `params.timings`; it is clocked afresh when it
runs. Before this, every deferred deck carried a false `outcome: error`.

### 6. Text requests stream (`shared/gemini_client.py`)

The maths lesson is one request of up to 20,000 output tokens. As a
single-shot `:generateContent` call Vertex closed it with HTTP 503 at the
ten-minute mark on every attempt of every maths video on 2026-10-14, and the
client retried the identical request — thirty to forty minutes to a failure.
No gate shortens a request that cannot finish. Text requests therefore go
to `:streamGenerateContent?alt=sse` and the reply is reassembled into the
single-shot shape, so nothing downstream changes; the only timeout that
applies is the gap between two chunks (`VERTEX_STREAM_READ_TIMEOUT_S`,
120 s), not a total. A dropped stream is a transient 504 that says how far
the reply got and is retried like any other; after a slow drop the
remaining attempts go to the lighter model (`VERTEX_SLOW_DROP_MODEL`,
#217). `VERTEX_STREAM=0` is the kill switch. The log line
`Vertex text stream: N chunk(s), M chars, S s, finish=…` is one per call.

## Knobs

```
PROVIDER_VERTEX_IMAGE_IN_FLIGHT=3          PROVIDER_VERTEX_TEXT_IN_FLIGHT=4
PROVIDER_VERTEX_IMAGE_GLOBAL_IN_FLIGHT=3   PROVIDER_VERTEX_TEXT_GLOBAL_IN_FLIGHT=4
PROVIDER_VERTEX_IMAGE_DEGRADED_IN_FLIGHT=1 PROVIDER_VERTEX_TEXT_DEGRADED_IN_FLIGHT=1
PROVIDER_VERTEX_IMAGE_START_PER_MIN=4      PROVIDER_VERTEX_IMAGE_FLOOR_PER_MIN=2
PROVIDER_VERTEX_IMAGE_CEILING_PER_MIN=12   PROVIDER_VERTEX_IMAGE_STEP_AFTER=5
PROVIDER_VERTEX_IMAGE_COOLDOWN_SECS=30     PROVIDER_VERTEX_TEXT_COOLDOWN_SECS=20
PROVIDER_VERTEX_IMAGE_MAX_COOLDOWN_SECS=120
PROVIDER_GATE_GLOBAL=1
VERTEX_STREAM=1  VERTEX_STREAM_CONNECT_TIMEOUT_S=30  VERTEX_STREAM_READ_TIMEOUT_S=120
VERTEX_SLOW_DROP_S=300  VERTEX_SLOW_DROP_MODEL=gemini-3.5-flash-lite
MAX_CATALOGUE_JOBS=2        MAX_CATALOGUE_KITS=1
CATALOGUE_RATE_LIMIT_DEFER_SECONDS=300     CATALOGUE_RATE_LIMIT_MAX_WAIT_SECONDS=10800
```

The old `MODEL_CALL_CONCURRENCY` (image in-flight cap) and
`IMAGE_CALLS_PER_MINUTE` (pace ceiling) still count when the new names are
unset. Changing a knob is a Railway variable edit; the gates read the
environment when first built, so it takes effect on the next deploy or
restart. The lease TTL (180 s), the renewal period (45 s) and the longest a
full cap is waited for (900 s, then degraded) are constants in
`shared/provider_gate.py`.

## Rollback

No code change needed for any of these:

| Symptom | Lever |
|---|---|
| the shared store misbehaves | `PROVIDER_GATE_GLOBAL=0` — gates go local (per-process settings) |
| kits too slow, one at a time is too strict | `MAX_CATALOGUE_KITS=99`, `MAX_CATALOGUE_JOBS=99` — the old scheduling |
| pacing too timid | raise `PROVIDER_VERTEX_IMAGE_START_PER_MIN` / `_FLOOR_PER_MIN` |
| a kit paused on a spent budget | the console's Retry re-runs it; `CATALOGUE_RATE_LIMIT_MAX_WAIT_SECONDS` lengthens the budget |

The migrations are additive (three tables, eight functions, service-role
only, RLS on with no policies); the worker runs without them, locally.

## Reading the result

The learned pace right now, per provider, and whether a refusal is silencing it:

```sql
select provider, rate_per_min, cooldown_until, successes, refusals, updated_at
from public.provider_pacing;
```

Sends in the last minute and leases in flight (expired ones are swept on the
next permit or acquire):

```sql
select provider, count(*) as sends_last_minute from public.provider_sends
where sent_at > now() - interval '60 seconds' group by provider;

select provider, count(*) as in_flight, min(acquired_at) as oldest, max(expires_at) as latest_expiry
from public.provider_leases where expires_at > now() group by provider;
```

Per day, from the timings every generation records:

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

A worker running degraded shows up in the Railway log as
`gate DEGRADED: the shared store is unreachable` and `no catalogue job
claimed until the database answers`, once a minute while it lasts.

Success looks like: refusals per video falling towards zero, no kit failing
on a 429, and waiting that shows up as `cooldown_wait` on a job that finished
rather than as a job that did not. The measure that matters is completed
kits per hour, not requests per minute.

## Validation after a deploy

1. One kit alone, in the off-peak window: confirm `provider_pacing` gains a
   row per provider, `provider_sends` shows the sends, and the kit's
   `timings` carry the new counters.
2. Two kits queued together: confirm the second kit's jobs stay `queued`
   until the first kit's are done (one distinct `kit_id` among processing
   catalogue jobs at any moment).
3. A figure batch of more than two jobs: confirm at most two are
   `processing` at any moment.
4. A teacher's worksheet queued while a kit generates: confirm it is claimed
   and finished at once.
5. Then the daily query above against the 2026-10-13 baseline (150 image
   refusals in 23 minutes; two kits lost to text refusals).
