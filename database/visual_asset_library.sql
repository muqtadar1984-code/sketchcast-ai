-- SketchCast Visual Knowledge Library
--
-- Binary artwork belongs in Supabase Storage, not Postgres. This table stores
-- searchable identity, curriculum alignment, provenance and validation metadata.
-- Visuals and avatars deliberately share the same library and storage bucket;
-- asset_type/role keeps the two retrieval domains separate.
--
-- asset_format is a SECOND, independent axis. asset_type says what the asset
-- is FOR (educational visual vs persistent character); asset_format says what
-- the bytes ARE (a raster PNG vs SVG markup). There is exactly one library:
-- an SVG is not a different store, it is a different format of the same row,
-- and it is never rasterised to fit the older path — the markup is canonical.

create table if not exists public.visual_assets (
  id uuid primary key default gen_random_uuid(),
  asset_key text not null,
  canonical_key text not null,
  asset_type text not null default 'visual'
    check (asset_type in ('visual', 'avatar')),
  asset_format text not null default 'png'
    check (asset_format in ('png', 'svg')),
  -- For an SVG asset: the EXACT group ids, in drawing order, and how many
  -- there are. The group ids are the labelling contract, so this lets the
  -- library answer "does this asset contain the part the lesson wants to
  -- label?" without downloading the markup.
  group_ids jsonb not null default '[]'::jsonb,
  group_count integer not null default 0,
  role text,
  description text not null default '',
  curriculum text not null default 'generic',
  subject text not null default 'general',
  grade text not null default 'k12',
  age_band text,
  topic text not null default '',
  concepts jsonb not null default '[]'::jsonb,
  status text not null default 'candidate'
    check (status in ('candidate', 'approved', 'rejected', 'retired')),
  provenance text not null default 'generated',
  source text,
  storage_path text,
  content_hash text,
  quality text,
  usage_count integer not null default 0,
  last_used_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- Idempotent migration for installations where the table already existed.
alter table public.visual_assets add column if not exists asset_type text not null default 'visual';
alter table public.visual_assets add column if not exists role text;
alter table public.visual_assets add column if not exists age_band text;
alter table public.visual_assets add column if not exists asset_format text not null default 'png';
alter table public.visual_assets add column if not exists group_ids jsonb not null default '[]'::jsonb;
alter table public.visual_assets add column if not exists group_count integer not null default 0;

create index if not exists visual_assets_type_idx
  on public.visual_assets (asset_type);
create index if not exists visual_assets_format_idx
  on public.visual_assets (asset_format);
create index if not exists visual_assets_role_idx
  on public.visual_assets (asset_type, role);
create index if not exists visual_assets_canonical_idx
  on public.visual_assets (canonical_key);
create index if not exists visual_assets_subject_grade_idx
  on public.visual_assets (subject, grade);
create index if not exists visual_assets_curriculum_subject_idx
  on public.visual_assets (curriculum, subject);
create index if not exists visual_assets_status_idx
  on public.visual_assets (status);
create unique index if not exists visual_assets_hash_idx
  on public.visual_assets (content_hash)
  where content_hash is not null;

alter table public.visual_assets enable row level security;

insert into storage.buckets (id, name, public)
values ('visual-assets', 'visual-assets', false)
on conflict (id) do nothing;

create index if not exists visual_assets_search_idx
  on public.visual_assets using gin (
    to_tsvector('simple',
      coalesce(canonical_key,'') || ' ' ||
      coalesce(description,'') || ' ' ||
      coalesce(topic,'') || ' ' ||
      coalesce(subject,'') || ' ' ||
      coalesce(curriculum,'') || ' ' ||
      coalesce(grade,'') || ' ' ||
      coalesce(role,'') || ' ' ||
      coalesce(age_band,''))
  );

comment on table public.visual_assets is
  'Reusable SketchCast visuals and avatars; binaries live in private Supabase Storage.';
comment on column public.visual_assets.asset_type is
  'visual = educational artwork; avatar = persistent teacher/student character asset.';
comment on column public.visual_assets.asset_format is
  'png = raster bytes; svg = vector markup. Independent of asset_type; the stored object is generated/<canonical_key>/<hash>.<asset_format>.';
comment on column public.visual_assets.group_ids is
  'SVG assets only: the exact <g id> values in drawing order. They are the labelling contract, so a lesson can ask whether the part it wants exists before downloading.';
comment on column public.visual_assets.group_count is
  'SVG assets only: length of group_ids, denormalised for cheap filtering.';
comment on column public.visual_assets.role is
  'Avatar role such as teacher or student; null for ordinary educational visuals.';
comment on column public.visual_assets.age_band is
  'Learner/character age band used for avatar matching; null for ordinary visuals.';
comment on column public.visual_assets.content_hash is
  'SHA-256 of the asset; prevents duplicate storage when the same generated image is encountered again.';
