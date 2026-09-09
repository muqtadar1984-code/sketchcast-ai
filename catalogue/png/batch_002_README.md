# PNG Catalogue Batch 002 — Remaining 278 Targets

This batch contains the remaining **278 of the 378 catalogue targets** defined in `svg_delivery_spec.md`.

## Purpose

Use the existing production raster/image-generation pipeline to generate these educational visuals as high-quality PNGs and ingest them through the existing Visual Library path.

The first 100 targets are handed off separately in PR #29. Together, Batch 001 + Batch 002 cover all 378 catalogue targets.

## Source of truth

`svg_delivery_spec.md` remains the source of truth for:

- asset key
- subject
- visual description
- named semantic parts
- intended educational content

The `.svg` extension in the source specification does **not** mean these assets should now be generated as SVG. For this batch, the target delivery format is **PNG**.

## Generation / ingestion

For every key in `batch_002_keys.txt`:

1. Read the corresponding entry in `svg_delivery_spec.md`.
2. Generate a high-quality educational PNG using the existing production image-generation path.
3. Reuse an existing Visual Library asset when the library resolver determines it is a suitable/confident match.
4. Validate the generated/reused asset using the existing validation path.
5. Publish validated newly generated assets into the Visual Library with the correct provenance and metadata.
6. Confirm the resulting asset can be consumed by the existing renderer.

Do **not** commit PNG binaries to Git. Production binaries belong in Supabase asset storage / the existing Visual Library storage path.

## Report back

Please report:

- 278 targets attempted / succeeded / failed
- Visual Library reuses vs new AI generations
- validation failures and reasons
- generation/ingestion elapsed time
- total storage added
- any renderer compatibility issues
- any rate-limit or provider failures
- keys requiring manual review

This is intentionally a catalogue/data handoff. Do not rewrite the renderer or Visual Library unless a concrete compatibility defect is found during ingestion.
