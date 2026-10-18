-- 0144_book_unit_analyses — one analysis per book chapter PART, shared by
-- every piece of a teacher's kit (2026-10-18)
--
-- A teacher's kit on a chapter part (params.part = k — most chapters are
-- split, e.g. "Part 1 of 6") ran the agent2 combined analysis once PER PIECE:
-- worker/process.py never reads or writes the chapter_grounding cache for a
-- part job, because that row describes the WHOLE chapter and is also the AI
-- Tutor's fence. Measured on the Bedrock cut-over sample kit (founder's book
-- 8fce6e4c ch1 part 1, Arabic, 2026-10-18): the worksheet, activity, case
-- study, lesson plan and deck each analysed the same part — $0.43–0.54 each,
-- all claimed within one second of each other. Over 60 days, part-job
-- documents averaged $0.076 on Claude against $0.034 for whole-chapter ones
-- (which do reuse), and 428 of 634 book jobs were part jobs.
--
-- Same protocol as 0140 (topic_article_analyses): the row is the cache AND
-- the lease. unit_key = '<book_id>:<chapter_num>:<part|all>:<language>'.
--   status 'analysing' — holder_generation_id is analysing; a sibling defers
--                        (jobs.params.deferred_until) until 'ready', or takes
--                        over once lease_started_at is older than
--                        ARTICLE_ANALYSIS_LEASE_S.
--   status 'ready'     — `analysis` is what every later piece reads.
-- Reuse also requires the same analyzer_version and source_hash (the text
-- handed to the analyzer), so a re-indexed or relocated chapter re-analyses.
--
-- Deliberately NOT chapter_grounding: a part's analysis must never become
-- the whole chapter's cache or the tutor's curriculum fence.
--
-- Service-role only (worker catalogue/analysis_cache.py). RLS on, no
-- policies. A worker running before this is applied analyses per piece, as
-- before. Rollback lever: BOOK_SHARED_ANALYSIS=0.

create table if not exists public.book_unit_analyses (
  unit_key              text primary key,
  book_id               uuid not null references public.books(id) on delete cascade,
  chapter_num           integer not null,
  part                  integer,
  language              text not null,
  analyzer_version      integer not null,
  source_hash           text not null,
  status                text not null check (status in ('analysing', 'ready')),
  holder_generation_id  uuid,
  lease_started_at      timestamptz,
  analysis              jsonb,
  analysed_at           timestamptz,
  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now()
);
create index if not exists book_unit_analyses_book_idx on public.book_unit_analyses (book_id);
alter table public.book_unit_analyses enable row level security;

comment on table public.book_unit_analyses is
  'One agent2 analysis per book chapter part (or whole chapter) and language, shared by every piece of a kit; status analysing = a lease held by holder_generation_id (worker catalogue/analysis_cache.py, 0144).';
