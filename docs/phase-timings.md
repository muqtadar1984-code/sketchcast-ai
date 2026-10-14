# Phase timings — where a generation's minutes go

Since 2026-10-14 every generation records, on its own row, how long each phase
of the pipeline took. Before this the only timing the worker persisted was
`jobs.updated_at - jobs.created_at` (queue wait and everything else as one
number), and one `compose:` line in a Railway log stream that was running up to
47 minutes behind the worker on 2026-10-13.

## Where it lives

`generations.params.timings` — one object per generation, written when the job
finishes **or fails** (a failed kit reports where its minutes went too):

```json
{
  "total_s": 2583.4,
  "outcome": "done",
  "started_at": "2026-10-13T14:46:09+00:00",
  "phases": {
    "queue_wait": 3.2, "prelude": 11.0, "analysis": 0.0, "script": 1510.5,
    "slides": 2.1, "images_warm": 180.0, "tts": 412.0, "render": 640.3,
    "compose": 190.2, "concat": 7.9, "acceptance": 1.4, "upload": 3.0,
    "pacing_wait": 44.0, "rate_limit_wait": 131.0
  },
  "counts": {"images_ready": 11, "images_pending": 1, "rate_limit_retries": 12}
}
```

The same summary is printed as one INFO line, `phases <gen> (<type>, <outcome>): total=… queue_wait=… …`.

## Reading the phases

| Phase | What it measures | Thread |
|---|---|---|
| `queue_wait` | job row created → claimed by a worker | — |
| `prelude` | book download / OCR / chapter pick, or `kit.prepare` for a catalogue kit | job |
| `analysis` | Agent 2 analysis model call(s); 0 when the cached analysis was reused | job |
| `script` | the script model call(s), maths verification rounds included | job |
| `slides` | Agent 5 slide build | job |
| `images_warm` | the pre-render picture warm pass (its budget is `IMAGE_WARM_BUDGET_SECS`) | job |
| `tts` | **sum** over segments of synthesis time | render threads |
| `render` | **sum** over segments of frame drawing + encode, as the thread waited for it | render threads |
| `compose` | **wall** time of the whole compose pass (TTS + render across threads) | job |
| `concat` | the final ffmpeg concat | job |
| `acceptance` | the acceptance audit | job |
| `document` / `deck` | docgen build for document kinds / the deck build | job |
| `upload` | every artifact upload (video, deck, script json, thumbnails, documents) | any |
| `pacing_wait` | seconds spent waiting for an image/vision rate-limiter token | any |
| `rate_limit_wait` | seconds slept on HTTP 429 backoff ladders | any |
| `deferral_wait` | seconds a patient kit waited out an image deferral | any |

`tts` and `render` are busy-seconds **sums** across the render threads, so they
can exceed `compose`, which is the wall time a teacher waited. The sum says how
much work the phase is; the wall says how long it took.

Phases are wall seconds as seen by the thread that did the work. A render
thread is bound to its lesson through `raster_assets.bind_generation` (the phase
clock travels with the image budget). Code that never opened a clock pays
nothing: every `phase_clock` call is a no-op without one.

## Queries

Median minutes per phase for videos, last 14 days:

```sql
select p.key as phase,
       count(*) as n,
       round((percentile_cont(0.5) within group (order by p.value::numeric) / 60)::numeric, 1) as p50_min,
       round((percentile_cont(0.9) within group (order by p.value::numeric) / 60)::numeric, 1) as p90_min
from public.generations g,
     jsonb_each_text(g.params->'timings'->'phases') p
where g.kind = 'presentation'
  and g.created_at > now() - interval '14 days'
  and g.params ? 'timings'
group by p.key order by p50_min desc;
```

The slowest videos and what they spent it on:

```sql
select left(id::text, 8) as gen, created_at,
       params->'timings'->>'outcome' as outcome,
       (params->'timings'->>'total_s')::numeric / 60 as total_min,
       params->'timings'->'phases' as phases,
       params->'timings'->'counts' as counts
from public.generations
where kind = 'presentation' and params ? 'timings'
order by (params->'timings'->>'total_s')::numeric desc
limit 20;
```

Rate-limit cost per day (how much of the day's video time was spent waiting on
image quota):

```sql
select created_at::date as day, count(*) as videos,
       round(sum((params->'timings'->'phases'->>'rate_limit_wait')::numeric) / 60, 1) as rate_limit_min,
       round(sum((params->'timings'->'phases'->>'images_warm')::numeric) / 60, 1) as warm_min,
       sum((params->'timings'->'counts'->>'rate_limit_retries')::int) as retries
from public.generations
where kind = 'presentation' and params ? 'timings'
group by 1 order by 1 desc;
```
