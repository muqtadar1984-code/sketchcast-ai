-- 0106_visual_assets_vision
--
-- Stops the library re-buying the vision annotation of a PNG it already owns.
--
-- WHAT IT DOES
--   public.visual_assets gains ONE column:
--     vision  jsonb  not null  default '{}'
--   holding the whole annotation payload, self-describing:
--     {"regions": {"<part>": [[x0,y0,x1,y1], ...]},   -- ASSET PIXEL COORDS
--      "annotated_for": ["<part>", ...],               -- the names ASKED for
--      "baked_text": false,
--      "w": 1024, "h": 1024}                           -- the pixel dims the
--                                                      -- boxes belong to
--   Nothing else is added. The EXISTING group_ids / group_count (0104) start
--   carrying the part names vision FOUND on a PNG, which is what those columns
--   already say they are for — "so a lesson can ask whether the part it wants
--   exists before downloading" is a claim about parts, not about markup.
--   Populating them for PNG needs no DDL, only the writer change that ships
--   with this file.
--
-- WHY ONE JSONB COLUMN AND NOT FIVE SCALAR ONES
--   Nothing ever FILTERS on regions. They are read for a row that has already
--   been matched, downloaded and bound, so there is no index to earn and no
--   query to narrow — the whole payload is fetched or none of it is. Keeping
--   w/h INSIDE the payload is the point: a box is only meaningful for the
--   exact bytes it was measured on, so a consumer that rescales can normalise
--   against the dims that were true, and one whose local file is a different
--   size can DETECT the mismatch and refuse the boxes instead of drawing them
--   in the wrong place. Five loose columns would let those five values drift
--   apart; one document cannot.
--
-- WHY IT IS SAFE ON THE 230 LIVE ROWS (measured read-only 2026-09-05: 230
-- rows — 217 approved non-avatar PNGs plus 13 avatars, every one asset_format
-- 'png', and group_count = 0 on every single row)
--   * Every statement is `if not exists`, so re-running it is a no-op.
--     Applying it twice cannot fail and cannot change data.
--   * vision is added WITH a default and NOT NULL, so all 230 existing rows
--     are stamped '{}' by the ALTER itself — which is the truth about them:
--     none of them has ever carried an annotation. Postgres 11+ stores the
--     default in the catalogue rather than rewriting the table, so this is a
--     metadata-only change on a small table.
--   * No constraint and no index. '{}' is a legal, meaningful value (this row
--     has not been annotated), so there is nothing to check; and since no
--     query filters on the column, an index would only cost writes.
--   * asset_format, asset_type, group_ids and group_count are NOT touched.
--     0104 already added them and is applied; this migration only changes
--     which rows the CODE writes group_ids for, and that is a code change.
--   * No storage objects are moved or renamed, and no row's bytes change. The
--     boxes describe the object already at
--     generated/<canonical_key>/<hash>.png.
--
-- WHAT IT DOES NOT DO
--   It does not backfill. The 217 existing PNG rows stay '{}' until a lesson
--   binds one and the renderer writes its annotation back (record_vision), so
--   each of them pays for vision ONCE more, ever, instead of once per deploy.
--   The 378 diagrams still to be commissioned pay nothing extra: they are
--   annotated on the way in and publish with the payload already in hand.
--
-- DOWN
--   alter table public.visual_assets drop column if exists vision;
--   (Dropping it loses only the cached annotation. The next render of each
--   affected asset re-buys one vision call and the library is exactly where
--   it is today — no asset, no row and no stored object is harmed.)
--
-- NUMBERING
--   0106, because 0105 is taken. The app repo's 0105_premium_voices_threshold
--   was applied to prod on 2026-09-05 at 07:45 UTC (supabase_migrations:
--   20260905074545) — exactly the collision an earlier draft of this note
--   predicted, so this file was renumbered 0105 -> 0106. Before it, the newest
--   applied migration was 0104_visual_assets_asset_format (20260905054301).
--   Nothing inside this file depends on its number, but
--   tests/test_library_regions.py reads the filename, so the two move together.
--
-- NOT APPLIED BY ANY AGENT. The founder applies prod schema changes.

begin;

alter table public.visual_assets
  add column if not exists vision jsonb not null default '{}'::jsonb;

-- Belt and braces: the ALTER above already stamps every pre-existing row, but
-- a row inserted by an older client between deploy and migration would carry
-- NULL if the column were ever made nullable by hand.
update public.visual_assets set vision = '{}'::jsonb where vision is null;

comment on column public.visual_assets.vision is
  'The paid vision annotation of a raster asset, cached so it is bought once ever rather than once per worker deploy: {"regions": {"<part>": [[x0,y0,x1,y1], ...]}, "annotated_for": [...], "baked_text": bool, "w": int, "h": int}. Boxes are in the ASSET pixel coordinates of the stored object, and w/h are the dimensions they were measured on — a consumer whose bytes are a different size must refuse them, not rescale blindly. {} means not annotated. SVG assets leave it empty: their <g id> groups are the parts, for free.';

commit;
