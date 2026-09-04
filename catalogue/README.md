# Visual catalogue

492 pre-generated teaching diagrams — 100 curated concepts each for biology, physics,
chemistry, history and geography, minus 8 duplicates. They are generated once, published to
the visual library, and reused by every lesson, so a lesson does not wait on an image model
and cannot lose a board to a rate limit.

- `catalogue.json` — the entries. Each carries `key`, `prompt`, `parts`, `subject`, `topic`,
  `grade_band` and the `canonical` fold of its key.
- `<subject>.json` — the five curated source lists.
- `catalogue_report.md` — how the list was built, what was dropped, and why 61 keys were renamed.
- `generate_catalogue.py` — the paced, resumable generator.

## Every prompt names its parts

Each prompt ends with `Name the layer groups exactly: a, b, c`. The scene engine reads that tail
to learn what the parts of the picture are called, which is what lets it attach a label to the
right part. A diagram generated without it arrives unlabellable, which is exactly how a plant
cell shipped with no labels on 2026-09-04.

## Running it

    python catalogue/generate_catalogue.py --confirm --order round-robin --deadline-minutes 150

`--confirm` is required before any money is spent. The run is resumable through its state file
and never regenerates a key that is already published. `--dry-run` shows the plan for free.
Round-robin order fills all five subjects evenly, so an interrupted run still leaves a balanced
library. Roughly 53 seconds and four cents per diagram.

It competes with live lessons for the same image quota, so prefer a quiet window.
