# PNG catalogue batch 001

This batch is the first 100 entries of the 378-diagram educational catalogue.

## Purpose

Use this branch to test the existing PNG generation → validation → Visual Library ingestion path at catalogue scale before committing to all 378 assets.

The 100 names are in `batch_001_keys.txt`; `batch_001_manifest.json` is the machine-readable manifest.

## Asset policy

- Format: PNG
- No generated image binaries in Git
- Generated assets should go through the existing raster resolver and Visual Library publication path
- Preserve the catalogue key as the asset key
- Keep the existing conservative Visual Library matching threshold
- Validate the generated image before publication
- Do not replace an existing approved library asset merely because this batch regenerates the same key

## Expected outcome

For each key, generate a production-quality educational illustration matching the catalogue description, publish successful validated assets to the Visual Library, and record generation/reuse/provenance decisions through the existing instrumentation.

This PR is deliberately a catalogue-target handoff rather than a renderer rewrite.
