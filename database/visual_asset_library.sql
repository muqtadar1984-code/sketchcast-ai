-- SketchCast Visual Knowledge Library
--
-- Binary artwork belongs in Supabase Storage, not Postgres. This table stores
-- the searchable identity, curriculum alignment, provenance and validation
-- metadata. The worker uses the service-role client server-side.
--
-- Apply once to the production Supabase project. The application remains
-- functional without this migration: local caching and normal AI generation
-- are the fallback.

create table if not exists public.visual_assets (
  id uuid primary key default gen_random_uuid(),
  asset_key text not null,
  canonical_key text not null,
  description text not null default '',
  curriculum text not null default 'generic',
  subject text not null default 'general',
  grade text not null default 'k12',
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

-- The worker is the only writer in this first implementation, using the
-- service-role key. Client-side RLS is therefore deliberately deny-by-default.
alter table public.visual_assets enable row level security;

-- Supabase Storage bucket. Keep it private: artwork is an internal generation
-- asset, not a user-uploaded public file. The worker downloads it with the
-- service-role client.
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
      coalesce(grade,''))
  );

comment on table public.visual_assets is
  'Reusable SketchCast K-12 visual assets; binaries live in Supabase Storage.';
comment on column public.visual_assets.status is
  'approved assets are eligible for automatic reuse; candidates are retained for review/analytics.';
comment on column public.visual_assets.content_hash is
  'SHA-256 of the normalized asset; prevents duplicate storage when the same generated image is encountered again.';
