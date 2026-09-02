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

## Storage

- **Postgres:** `public.visual_assets` stores identity, curriculum metadata,
  provenance, validation status, content hash, usage telemetry and Storage
  path.
- **Supabase Storage:** private `visual-assets` bucket stores PNG binaries.
- **Worker cache:** the existing `storage/scene_assets` directory remains the
  hot local cache used by the renderer.

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
