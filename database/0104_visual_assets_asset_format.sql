-- 0104_visual_assets_asset_format
--
-- Makes SVG a first-class format of the ONE visual library.
--
-- WHAT IT DOES
--   public.visual_assets gains three columns:
--     asset_format  'png' | 'svg'   default 'png'
--     group_ids     jsonb           default '[]'   (SVG only: exact <g id>s)
--     group_count   integer         default 0      (SVG only: length of the above)
--   plus a check constraint on asset_format and an index on it.
--
-- WHY IT IS SAFE ON THE 230 LIVE ROWS (measured 2026-09-05: 230 rows, 13 of
-- them avatars, every stored object a .png)
--   * Every statement is `if not exists` / `if not found`, so re-running it is
--     a no-op. Applying it twice cannot fail and cannot change data.
--   * asset_format is added WITH a default and NOT NULL, so all 230 existing
--     rows are stamped 'png' by the ALTER itself — which is what they are.
--     Postgres 11+ stores the default in the catalogue rather than rewriting
--     the table, so this is a metadata-only change on a small table.
--   * The check constraint is added only after the backfill, and only if a
--     constraint of that name is not already present.
--   * asset_type is NOT touched. asset_format is a second, independent axis:
--     asset_type says what an asset is FOR (educational visual vs persistent
--     character), asset_format says what its bytes ARE. An avatar PNG and an
--     educational SVG differ on both axes and neither column implies the other.
--   * No storage objects are moved or renamed. Existing rows keep pointing at
--     generated/<canonical_key>/<hash>.png; new SVG rows are written to
--     generated/<canonical_key>/<hash>.svg in the same bucket. There is no
--     second library and nothing is rasterised.
--
-- DOWN
--   alter table public.visual_assets
--     drop column if exists group_count,
--     drop column if exists group_ids,
--     drop column if exists asset_format;
--   (Dropping asset_format loses only the png/svg distinction; every row that
--   predates this migration is a png, which is also the default.)
--
-- NUMBERING
--   0103_deck_kind is the newest migration applied to prod (checked read-only
--   on 2026-09-05). If the app repo has since claimed 0104, renumber this file
--   — nothing inside it depends on the number.
--
-- NOT APPLIED BY ANY AGENT. The founder applies prod schema changes.

begin;

alter table public.visual_assets
  add column if not exists asset_format text not null default 'png';

alter table public.visual_assets
  add column if not exists group_ids jsonb not null default '[]'::jsonb;

alter table public.visual_assets
  add column if not exists group_count integer not null default 0;

-- Belt and braces: the ALTER above already stamps every pre-existing row, but
-- a row inserted by an older client between deploy and migration would carry
-- NULL if the column were ever made nullable by hand.
update public.visual_assets set asset_format = 'png' where asset_format is null;
update public.visual_assets set group_ids = '[]'::jsonb where group_ids is null;
update public.visual_assets set group_count = 0 where group_count is null;

do $$
begin
  if not exists (
    select 1 from pg_constraint
     where conrelid = 'public.visual_assets'::regclass
       and conname = 'visual_assets_asset_format_check'
  ) then
    alter table public.visual_assets
      add constraint visual_assets_asset_format_check
      check (asset_format in ('png', 'svg'));
  end if;
end
$$;

create index if not exists visual_assets_format_idx
  on public.visual_assets (asset_format);

comment on column public.visual_assets.asset_format is
  'png = raster bytes; svg = vector markup. Independent of asset_type; the stored object is generated/<canonical_key>/<hash>.<asset_format>.';
comment on column public.visual_assets.group_ids is
  'SVG assets only: the exact <g id> values in drawing order. They are the labelling contract, so a lesson can ask whether the part it wants exists before downloading.';
comment on column public.visual_assets.group_count is
  'SVG assets only: length of group_ids, denormalised for cheap filtering.';

commit;
