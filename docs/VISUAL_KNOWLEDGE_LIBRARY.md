# SketchCast Visual Knowledge Library

## Why this exists

The Scene Engine already caches generated raster assets locally. The visual
knowledge library turns that cache into a durable, curriculum-aware asset
system so SketchCast does not pay an image model to redraw the same educational
visual over and over.

The target lifecycle is:

```text
visual request
     |
     v
local cache ---- hit ----> render
     |
     miss
     v
Supabase visual library ---- confident hit ----> local cache -> render
     |
     miss
     v
AI image generation
     |
     v
existing renderer validation
     |
     +---- failed ----> normal fallback
     |
     +---- passed ----> publish asset + metadata -> future reuse
```

Image generation remains the fallback. The library never becomes a hard
dependency for producing a lesson.

## Curriculum model

Every library record carries:

- `curriculum`: `generic`, `cbse`, `cambridge`, `igcse`, `icse`, etc.
- `subject`: biology, chemistry, physics, mathematics, geography, history,
  computer science, and so on.
- `grade`: a grade/year/stage or range.
- `topic`: the teaching topic.
- `concepts`: searchable concept vocabulary.

The checked-in `visual_library/catalog.json` is the initial K-12 concept map.
It is metadata, not a collection of third-party copyrighted images.

Curriculum alignment should be treated as metadata supplied by the generation
context where possible. If it is not available, the library conservatively
uses `generic/k12` and may infer subject from the visual request. It must not
pretend that a free-text prompt proves syllabus alignment.

## Two formats, one library

`asset_format` is `png` or `svg`. It is a SECOND axis, independent of
`asset_type`: `asset_type` says what an asset is FOR (educational visual vs
persistent avatar), `asset_format` says what its bytes ARE. Neither implies
the other, and there is exactly one library — an SVG is a row with a different
format in the same bucket, never a parallel store and never rasterised to fit
the older path. The markup is the canonical asset.

An SVG row also carries `group_ids` (the exact `<g id>` values, in drawing
order) and `group_count`. The group ids are the labelling contract, so the
library can answer "does this asset contain the part the lesson wants to
label?" without downloading anything.

Group ids do three different jobs and conflating them is a bug:

| job | behaviour | where |
| --- | --- | --- |
| storage | exact — preserved verbatim | `svg_group_ids`, `parse_svg_asset` |
| validation | exact — a bad id is rejected, never repaired | `validate_svg_document` |
| matching | tolerant — "chloroplast" finds "chloroplasts" | `match_layer_ids` |

### Two validation philosophies

- **Publish is strict.** `spike/scene_engine/svg_validate.validate_svg_document`
  enforces `svg > g > path` with no exceptions: a valid viewBox, unique
  lowercase_snake_case group ids, no text/rect/circle/image/use/defs/marker/
  style elements, no transforms, stylesheets, CSS geometry, gradients, fills,
  embedded raster data, arcs or path commands outside `M L H V C Q Z`. A row
  is served to other lessons on other machines for months, so a defect stored
  there is handed out rather than costing one board. `publish_generated` is
  the gate and every publisher goes through it.
- **Runtime is forgiving.** `parse_svg_asset` degrades — an arc becomes a
  chord, a malformed tail is dropped, an unreadable document returns `None`
  and the ladder continues svg -> raster -> authored vector. A bad generation
  must never blank a board.

`tools/validate_svg_batch.py` runs the strict validator over a delivered
folder offline and additionally cross-checks each file's group ids against
that key's `parts` in the delivery catalogue.

### The SVG tier makes no vision call

`annotate_regions` is a paid vision request that guesses where the named parts
of a flat image are. An SVG's groups ARE the regions, named by the model that
drew them, so the SVG path never calls it — for a generation or for a library
hit. That is the tier's largest single saving and it is pinned by a test.

## Storage

- **Postgres:** `public.visual_assets` stores identity, curriculum metadata,
  format, group metadata, provenance, validation status, content hash, usage
  telemetry and Storage path.
- **Supabase Storage:** private `visual-assets` bucket stores the binaries at
  `generated/<canonical_key>/<hash>.<png|svg>`.
- **Worker cache:** the existing `storage/scene_assets` directory remains the
  hot local cache used by the renderer — `<canonical>/asset.png` for raster,
  `svg_<canonical>/asset.svg` for markup. Both are keyed by CANONICAL
  identity, and a hydrated download is filed under the key that was REQUESTED,
  because that is the only path the renderer will look at.

Supabase recommends storing media outside Postgres; the library follows that
model. The worker already uses a server-side service-role client, so the
library does not expose credentials to the browser.

## Automatic learning loop

A generated image that passes the existing Scene Engine validation is published
with a SHA-256 content hash. The same image cannot create another database
record. A future version can add explicit human review without changing the
lookup contract.

The one-shot migration script can publish the already-generated Scene Engine
cache without regenerating anything:

```bash
python scripts/migrate_scene_assets_to_visual_library.py
```

This is intentionally not run at every worker startup. New assets are
published automatically by the Scene Engine integration.

## Retrieval

The first implementation is deliberately simple and deterministic:

1. canonicalise the asset request;
2. compare concept/description tokens;
3. add bounded bonuses for curriculum, subject and grade compatibility;
4. require a confidence threshold (`VISUAL_LIBRARY_MIN_SCORE`, default `0.58`);
5. hydrate the winner into the existing renderer cache.

This avoids introducing a second AI call merely to decide whether an asset is
reusable. Once the library is large enough, the intended next step is a Postgres
full-text/vector index rather than loading hundreds or thousands of rows into
Python.

## Important quality rule

**Generated does not mean blindly trusted.** The current integration only
publishes assets after the Scene Engine has accepted the raster output and found
no remaining baked text. A future review workflow can move an asset from
`approved` to `retired` without deleting its provenance/history.

## Curriculum sources

Curriculum mappings should be maintained from the actual curriculum selected
by a school rather than assuming that one K-12 taxonomy is universal. Cambridge,
for example, structures its pathway across Early Years, Primary, Lower
Secondary, Upper Secondary and Advanced, while CBSE publishes its own class-
based curriculum. The library therefore stores curriculum as a first-class
field instead of making `grade` alone the identity.
