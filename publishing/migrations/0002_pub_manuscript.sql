-- Stage E manuscript store (architecture §6.2), LOCAL ONLY. Idempotent: the
-- migrate command re-runs every file.
--
-- An edition is one language's snapshot of unit revisions. A unit has a stable
-- ULID carried from the blueprint (chapter_uid) down to sections, exercises and
-- figures; every change is a new immutable revision with its source_refs into
-- the pinned dossier, the prompt version, the model and its cost.

create table if not exists pub.editions (
  id             bigserial primary key,
  work_uid       text not null references pub.works(uid),
  language       text not null,
  edition_no     int not null default 1,
  status         text not null default 'drafting',      -- drafting | sample_review | approved
  pin_run_id     bigint references pub.audit_runs(id),  -- the Gate R pins it was authored from
  source_edition_id bigint references pub.editions(id), -- a Malay edition points at its EN edition
  created_at     timestamptz not null default now(),
  unique (work_uid, language, edition_no)
);

create table if not exists pub.units (
  uid            text primary key,                      -- ULID; a chapter unit reuses the blueprint chapter_uid
  edition_id     bigint not null references pub.editions(id),
  parent_uid     text references pub.units(uid),
  kind           text not null,                         -- chapter | section | exercise | figure
  sort           int not null default 0,
  created_at     timestamptz not null default now()
);

create table if not exists pub.unit_revisions (
  id             bigserial primary key,
  unit_uid       text not null references pub.units(uid),
  revision       int not null,
  body           jsonb not null,                        -- typed blocks / exercise record / plan
  source_refs    text[] not null default '{}',          -- dossier ids: claim:, section:, item:, figure:, gloss:, misc:, we:
  prompt_version text, provider text, model text,
  usd            numeric(10,4),
  status         text not null default 'draft',         -- draft | validated | flagged | approved
  created_at     timestamptz not null default now(),
  unique (unit_uid, revision)
);

create table if not exists pub.validation_results (
  id             bigserial primary key,
  unit_uid       text not null references pub.units(uid),
  revision_id    bigint references pub.unit_revisions(id),
  check_name     text not null,                         -- e.g. source_refs, figure_exists, deictic, coverage, review:source_gap
  verdict        text not null,                         -- pass | fail | warn
  detail         jsonb not null default '{}',
  created_at     timestamptz not null default now()
);

alter table pub.cost_events add column if not exists edition_id bigint references pub.editions(id);
alter table pub.cost_events add column if not exists unit_uid text;
