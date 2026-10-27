# SK B3 — package builder: report

Status 2026-10-27: **built and run on the whole of Biology Chapter 2 (6 units, 0 failures).**
Branch `feat/sk-b3-packager`, draft PR sketchcast-ai#330. Runs as a CLI with the
worker venv; no job-queue wiring, no database writes, no Polly yet.

## What it does (one chapter draft → a teaching graph of prepared segments)

| Step | How | Guard |
|---|---|---|
| Units | one per `plan.sections[]` of `chNN-draft.json` (`sid`, heading, blocks, figure refs, node codes) | — |
| Figures | `fig:` → `article_figures` → `visual_assets`; `asset:` → `visual_assets`; approved ink/colour assets only | photos (`deck_validated`) and `book:` local assets are skipped (Workstream A stage 2 brings book pictures into the library) |
| Author | Claude Sonnet 5.5 (Bedrock) writes narration **from the unit's own blocks** plus ≤ 7 board items (one illustration, labels for the asset's named parts, written lines ≤ 42 chars, circles), each cued to a verbatim phrase | `verify_plan`: every sentence needs content-token overlap ≥ 0.72 with a block or an adjacent block pair; cues must be substrings of the narration; labels only for parts the asset has; one repair round, then whatever fails is **dropped, never shipped** |
| Voice | `shared.tts.synthesize` (Edge today) with word boundaries | the segment records `voice_gender` so the player picks SK character A (female) or B (male) |
| Cues | phrase → first matching word sequence in the marks → ms | unmatched cues fall back to a default offset (logged) |
| Scene | the production scene engine: the illustration is bound once to learn its box (fit in 520×560, left), labels sit in one column right of it in region order with ≥ 48 px gaps, leaders to the part; written lines start below the labels (or at the top when there is no picture) and shrink their font to fit the board | the renderer's own overlap mover is never triggered; the S1 lesson ("the column must start past the labels' measured extent") is built in |
| Render | `SceneRenderer` → `clip.mp4` (1280×720, 24 fps) + `hitmap.json` (element boxes, illustration regions in world px, action timeline) | — |
| Manifest | `segment.json` (contracts §3.2) per unit, `graph.json` (§3.1) per chapter; exercises attached to units by stem↔unit-text overlap, marked `heuristic` | per-unit checkpoint by draft hash (`--resume`); `--dry` skips frames |

## Chapter 2 build (2026-10-27)

| Unit | Title | Narration | Board | Grounding | Clip | Wall |
|---|---|---|---|---|---|---|
| s1 | Too small to see: why we need a microscope | 9 sentences, 56.6 s | 6 lines + 1 circle | 9/9 | 1.12 MB | 68 s |
| s2 | Parts of a light microscope | 9, 46.8 s | microscope illustration + 5 labels + 1 line | 9/9 | 1.07 MB | 92 s |
| s3 | Using the microscope safely and focusing | 8, 51.3 s | 6 lines + 2 circles | 8/8 | 1.05 MB | 69 s |
| s4 | Making a temporary slide | 11, 48.4 s | slide illustration + 3 labels + 2 lines + 1 circle | 11/11 | 1.20 MB | 151 s |
| s5 | Magnification and actual size | 8, ~40 s (rebuilt after the pair-grounding fix) | 6 lines + 1 circle, incl. both formulas and the worked example | 8/8 | 0.7 MB | 74 s |
| s6 | Drawing what you see | 10, 50.1 s | 6 lines + 1 circle | 10/10 | 1.04 MB | 55 s |

Totals: ≈ 4.8 min of prepared teaching, 6.2 MB of clips, ≈ 8.5 min wall on one
laptop, ≈ $0.06 of model spend (Edge voice is free). Exercises attached:
2/2/3/2/5/1 per unit, auto-markable 1/1/0/2/2/0 — **the ≥ 3-per-unit check
requirement is not met by the draft's own exercises** (B1 reports the same).

## What the first build taught (fixed in this branch)

1. **Stop-words inflated grounding**: "of the cell" could ground a sentence. Content tokens only now.
2. **Spoken sentences join neighbouring blocks** ("Measure the image with a ruler, then use: magnification = …"): grounding also checks adjacent block pairs, which brought the magnification formulas back into s5.
3. **The renderer moves overlapping text**: s2's labels floated 200 px above their parts because two labels were 40 px apart. Labels now keep ≥ 48 px gaps in one tidy column.
4. **Landscape pictures ate the board**: s4's slide figure scaled to 560 px tall was ~900 px wide, pushing the labels under the written lines. Pictures fit 520×560 and lines start below the labels.
5. **`ClaudeClient.analyze` returns `{data, usage}`**, not the JSON itself.

## Open / next

- Leader lines render as short stubs at the part rather than a full line from the label (the renderer's arrow treatment); labels are in the right order and column, so this is cosmetic — lift the stub length or draw the leader as a shape.

- Science review beyond grounding (plan §14): a vision pass over sample frames and a reviewer sign-off per unit before `status: live`.
- Polly (word + viseme marks) behind the `SpeechProvider` seam; Edge until then.
- Alternative assets per unit (`alt`, `demo`, `misconception`, `practice`, `check`) — only `explain:main` is built now.
- Job-queue wiring (own lane, off-peak) and S3 + CloudFront output when approved.
- `book:` pictures once the image registry (Workstream A stage 2) publishes them.
