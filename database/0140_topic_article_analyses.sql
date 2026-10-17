-- 0140_topic_article_analyses — one analysis per catalogue article (2026-10-17)
--
-- A catalogue kit's pieces (presentation, deck, activity, case study, lesson
-- plan) each ran the agent2 combined analysis of the SAME article before
-- building, because an article has no books row for the chapter_grounding
-- cache (decision 14). On the Arabic Cells kit 427aa1ac (2026-10-17) that
-- was four analyses of 248 s, 566 s, 540 s and 578 s, ~$0.3–0.6 each; the
-- deck, activity and case study were claimed within one second of each
-- other, so a plain write-after-first cache would have saved only the
-- lesson plan's.
--
-- This table is the cache AND the lease. One row per article:
--   status 'analysing' — holder_generation_id is running the analysis; a
--                        sibling kit piece that arrives meanwhile DEFERS
--                        (jobs.params.deferred_until, as a deck waits for
--                        its video) until the row turns 'ready', or takes
--                        the lease over once lease_started_at is older than
--                        ARTICLE_ANALYSIS_LEASE_S (a crashed holder).
--   status 'ready'     — `analysis` is the dict every later piece reads
--                        instead of calling the model.
-- Reuse requires the same language, analyzer_version and source_hash (the
-- article's chunked text and level) and an article not updated since
-- (article_updated_at). Any mismatch re-analyses and overwrites.
--
-- Service-role only: the worker (catalogue/analysis_cache.py) is the sole
-- caller. RLS is on with no policies. A worker running before this file
-- is applied falls back to analysing per piece, as before.

create table if not exists public.topic_article_analyses (
  article_id            uuid primary key references public.topic_articles(id) on delete cascade,
  language              text not null,
  analyzer_version      integer not null,
  source_hash           text not null,
  status                text not null check (status in ('analysing', 'ready')),
  holder_generation_id  uuid,
  lease_started_at      timestamptz,
  analysis              jsonb,
  analysed_at           timestamptz,
  article_updated_at    timestamptz,
  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now()
);
alter table public.topic_article_analyses enable row level security;

comment on table public.topic_article_analyses is
  'One agent2 analysis per catalogue article, shared by every piece of its kits; status analysing = a lease held by holder_generation_id (worker catalogue/analysis_cache.py, 0140).';
