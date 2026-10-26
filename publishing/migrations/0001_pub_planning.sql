-- Publishing engine, Sprint 1: planning, catalogue audit and readiness.
--
-- LOCAL ISOLATED DATABASE ONLY (D4). This file is a draft. It is not a Supabase
-- migration and must never be applied to production without Gate F.
--
-- Two schemas:
--   cat  read-only SNAPSHOT copies of production catalogue rows, taken with
--        GET-only requests (publishing/snapshot.py). Nothing here is ever
--        written back.
--   pub  the publishing engine's own planning tables.

create schema if not exists cat;
create schema if not exists pub;

-- ---------------------------------------------------------------- snapshot --
create table if not exists cat.snapshots (
  id          bigserial primary key,
  taken_at    timestamptz not null default now(),
  source_url  text not null,
  row_counts  jsonb not null default '{}'::jsonb
);

-- Every snapshot table carries the snapshot id it came from; a reload replaces
-- the rows wholesale (truncate + insert in one transaction).
create table if not exists cat.curricula (
  snapshot_id bigint not null, id uuid primary key, code text, name text, kind text,
  country text, edition text, source_url text);
create table if not exists cat.curriculum_nodes (
  snapshot_id bigint not null, id uuid primary key, curriculum_id uuid, code text,
  grade text, strand text, sub_strand text, title text, description text, kind text,
  parent_id uuid, sort int);
create table if not exists cat.topics (
  snapshot_id bigint not null, id uuid primary key, canonical_key text, slug text,
  title text, subject text, summary text, status text, bank_maturity text,
  prerequisites uuid[]);
create table if not exists cat.topic_aliases (
  snapshot_id bigint not null, row jsonb not null);
create table if not exists cat.topic_curriculum_map (
  snapshot_id bigint not null, id uuid primary key, topic_id uuid, node_id uuid,
  coverage text, notes text);
create table if not exists cat.topic_articles (
  snapshot_id bigint not null, id uuid primary key, topic_id uuid, version int,
  language text, source_article_id uuid, title text, status text, word_count int,
  objectives jsonb, sections jsonb, glossary jsonb, misconceptions jsonb,
  worked_examples jsonb, claims jsonb, reviewed_at timestamptz, updated_at timestamptz);
create table if not exists cat.article_figures (
  snapshot_id bigint not null, id uuid primary key, article_id uuid, figure_key text,
  status text, visual_asset_id uuid);
create table if not exists cat.topic_questions (
  snapshot_id bigint not null, id uuid primary key, topic_id uuid, language text,
  status text, item_type text);
create table if not exists cat.topic_kits (
  snapshot_id bigint not null, id uuid primary key, topic_id uuid, article_id uuid,
  language text, source_kit_id uuid, status text, translation_status text,
  part_plan jsonb, reviewed_at timestamptz, updated_at timestamptz,
  created_at timestamptz);
create table if not exists cat.topic_publications (
  snapshot_id bigint not null, id uuid primary key, topic_kit_id uuid, part int,
  channel_language text, youtube_video_id text, privacy text,
  youtube_upload_status text, error text, superseded_by uuid, published_at timestamptz);
create table if not exists cat.youtube_publish_queue (
  snapshot_id bigint not null, id uuid primary key, kit_id uuid, topic_id uuid,
  language text, status text, attempts int, last_error text, updated_at timestamptz);
create table if not exists cat.jobs (
  snapshot_id bigint not null, id uuid primary key, type text, status text,
  attempts int, usage jsonb, params jsonb, error text, created_at timestamptz,
  updated_at timestamptz);
-- The live answer of public.catalogue_current_kits(), kept so the audit's own
-- current-kit rule can be checked against production's (parity test).
create table if not exists cat.current_kits_live (
  snapshot_id bigint not null, topic_id uuid, language text, topic_kit_id uuid,
  article_id uuid, approved_at timestamptz);

-- --------------------------------------------------------------- planning --
create table if not exists pub.readiness_policy (
  id          text primary key,
  policy      jsonb not null,
  created_at  timestamptz not null default now()
);

create table if not exists pub.works (
  uid            text primary key,              -- ULID
  slug           text not null unique,
  title          text not null,
  subject_strand text not null,
  primary_curriculum_code text not null,
  grade_band     text not null,
  age_band       text,
  status         text not null default 'scoping'
    check (status in ('scoping','scope_approved','completing_catalogue','ready',
                      'drafting','in_review','approved','retired')),
  brief          jsonb not null,
  created_at     timestamptz not null default now()
);

create table if not exists pub.blueprints (
  id             bigserial primary key,
  work_uid       text not null references pub.works(uid),
  revision       int not null,
  document       jsonb not null,
  checks         jsonb not null,               -- deterministic check results
  prompt_versions jsonb not null,
  model          text not null,
  provider       text not null,
  cost_usd       numeric(10,4),
  input_tokens   int, output_tokens int,
  elapsed_s      numeric(10,2),
  snapshot_id    bigint not null,
  status         text not null default 'proposed'
    check (status in ('proposed','approved','rejected','superseded')),
  approved_by    text, approved_at timestamptz,
  created_at     timestamptz not null default now(),
  unique (work_uid, revision)
);

create table if not exists pub.blueprint_chapters (
  chapter_uid    text primary key,             -- ULID, carried into the manuscript
  blueprint_id   bigint not null references pub.blueprints(id),
  sort           int not null,
  title          text not null,
  node_codes     text[] not null default '{}'
);

create table if not exists pub.required_topics (
  id             bigserial primary key,
  blueprint_id   bigint not null references pub.blueprints(id),
  chapter_uid    text not null references pub.blueprint_chapters(chapter_uid),
  topic_id       uuid,                         -- null when proposed
  proposed_title text,
  role           text not null check (role in ('core','supporting','prerequisite_review')),
  node_codes     text[] not null default '{}',
  rationale      text
);

create table if not exists pub.proposed_topics (
  id             bigserial primary key,
  blueprint_id   bigint not null references pub.blueprints(id),
  canonical_title text not null,
  node_codes     text[] not null default '{}',
  subject_strand text not null,
  rationale      text,
  approved_by    text, approved_at timestamptz,
  created_topic_id uuid                         -- filled only after Gate B + Stage C
);

create table if not exists pub.audit_runs (
  id             bigserial primary key,
  blueprint_id   bigint not null references pub.blueprints(id),
  snapshot_id    bigint not null,
  policy_id      text not null references pub.readiness_policy(id),
  created_at     timestamptz not null default now()
);

create table if not exists pub.audit_items (
  id             bigserial primary key,
  run_id         bigint not null references pub.audit_runs(id),
  required_topic_id bigint not null references pub.required_topics(id),
  topic_id       uuid,
  check_code     text not null,                -- B1..B11
  language       text,
  verdict        text not null check (verdict in ('pass','fail','warn','n/a')),
  sub_status     text,                         -- carried verbatim from source rows
  evidence       jsonb not null default '{}'::jsonb
);

create table if not exists pub.topic_states (
  run_id         bigint not null references pub.audit_runs(id),
  required_topic_id bigint not null references pub.required_topics(id),
  state          text not null,
  failing_checks text[] not null default '{}',
  primary key (run_id, required_topic_id)
);

create table if not exists pub.dependency_steps (
  id             bigserial primary key,
  run_id         bigint not null references pub.audit_runs(id),
  required_topic_id bigint not null references pub.required_topics(id),
  step_order     int not null,
  step           text not null,
  job_type       text,
  language       text,
  status         text not null default 'planned',
  depends_on     int[] not null default '{}',
  est_cost_usd   numeric(10,4),
  cost_basis     text,                         -- 'measured:<n> jobs' | 'unmeasured'
  est_review_min int,
  throughput_note text
);

create table if not exists pub.readiness_snapshots (
  id             bigserial primary key,
  run_id         bigint not null references pub.audit_runs(id),
  work_uid       text not null references pub.works(uid),
  pct            numeric(5,1) not null,
  ready          boolean not null,
  outstanding    jsonb not null,
  evaluated_at   timestamptz not null default now()
);

create table if not exists pub.source_pins (
  id             bigserial primary key,
  work_uid       text not null references pub.works(uid),
  run_id         bigint not null references pub.audit_runs(id),
  dry_run        boolean not null default true,
  topic_id       uuid not null,
  language       text not null,
  article_id     uuid, article_version int,
  kit_id         uuid,
  publication_ids uuid[] not null default '{}',
  youtube_video_ids text[] not null default '{}',
  item_ids_count int,
  figure_ids     uuid[] not null default '{}',
  pinned_at      timestamptz not null default now()
);

create table if not exists pub.cost_events (
  id             bigserial primary key,
  work_uid       text references pub.works(uid),
  stage          text not null,
  provider       text, model text,
  input_tokens   int, output_tokens int,
  usd            numeric(10,4),
  created_at     timestamptz not null default now()
);
