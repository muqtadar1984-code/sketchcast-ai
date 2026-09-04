# Visual-library catalogue — build report

**Output:** `catalogue.json` — 492 entries, one JSON array.
**Inputs:** `biology.json`, `physics.json`, `chemistry.json`, `history.json`, `geography.json` (100 each = 500).
**Canonicalisers imported live** from the worktree `wt-worker-labels` (master `6ccc63e`):
`spike.scene_engine.raster_assets.canonical_key` and `shared.visual_library.canonical_key`.
**Live library checked read-only:** 116 `visual_assets` rows (101 distinct `asset_key`, 87 distinct `canonical_key`).

## Totals per subject

| Subject | Supplied | Final | Removed |
|---|---:|---:|---:|
| biology | 100 | 100 | 0 |
| chemistry | 100 | 96 | 4 |
| physics | 100 | 98 | 2 |
| geography | 100 | 98 | 2 |
| history | 100 | 100 | 0 |
| **Total** | **500** | **492** | **8** |

Grade bands: `14-16` 224, `11-16` 195, `11-13` 73. Every entry carries `subject`, `topic`, `grade_band`.

## Key normalisation — 61 keys rewritten

The two canonicalisers disagree on 19 tokens
(`_KEY_NOISE ^ _STOP` = cell, cells, figure, figures, graphic, graphics, drawn, educational,
for, hand, in, on, show, showing, simple, style, to, whiteboard, with).
A key containing any of them canonicalises one way for the cache reader and another way for the
remote hydrate, so the downloaded PNG lands in a directory the renderer never opens. 61 supplied
keys carried one, and all 61 were rewritten.

* **46 were pure filler** — prepositions and "simple" that neither reader nor writer needs:
  `balanced_forces_on_a_box` → `balanced_forces_box`, `test_for_hydrogen_lit_splint` →
  `test_hydrogen_lit_splint`, `resistors_in_series_and_in_parallel` → `resistors_series_parallel`,
  and so on. Meaning unchanged.
* **15 needed a real rename**, because deleting the offending token would have destroyed the
  concept (`bacterial_cell` → `bacterial` is not a picture):

| Supplied | Catalogue key | Reason |
|---|---|---|
| bacterial_cell | bacterium_structure | `cell` disagrees |
| cell_membrane_bilayer | membrane_bilayer | `cell` disagrees |
| nerve_cell | neurone | `cell` disagrees |
| muscle_cell | muscle_fibre | `cell` disagrees |
| sperm_cell | spermatozoon | `cell` disagrees |
| egg_cell | ovum | `cell` disagrees |
| white_blood_cell | leucocyte | `cell` disagrees |
| guard_cells_stoma | stomatal_guard_pair | `cells` disagrees |
| turgid_and_plasmolysed_cell | turgid_versus_plasmolysed | `cell` disagrees |
| brownian_motion_in_a_smoke_cell | brownian_motion_smoke_chamber | `cell` + `in` disagree |
| flemings_left_hand_rule | flemings_lefthand_rule | `hand` disagrees; joined rather than dropped |
| simple_series_circuit | series_circuit_basic | `simple` disagrees; "basic" preserves the contrast with parallel |
| simple_ac_generator | ac_generator | `simple` disagrees |
| simple_distillation_apparatus | distillation_apparatus_basic | `simple` disagrees; must stay distinct from `fractional_distillation_column` |
| simple_molecular_versus_giant_structure | molecular_versus_giant_structure | `simple` disagrees |
| back_to_back_housing_cross_section | backtoback_housing_cross_section | `to` disagrees; joined so the term survives |

**Verified after the rewrite:** for all 492 entries
`raster_assets.canonical_key(key) == visual_library.canonical_key(key) == entry["canonical"]`,
re-checked in a second independent pass. No key begins with `avatar` (such rows are filtered out of
educational retrieval by key as well as by column).

## Dropped duplicates — 8

**Identical key supplied by two subjects (6).** Kept the first-listed subject's copy; the prompts and
part lists were the same picture in each case.

| Dropped | Kept |
|---|---|
| chemistry / greenhouse_effect | biology |
| geography / greenhouse_effect | biology |
| chemistry / carbon_cycle | biology |
| geography / water_cycle | biology |
| chemistry / changes_of_state_arrows | physics |
| chemistry / rutherford_gold_foil_experiment | physics |

**Same picture under a different key (2).** Found by token-overlap comparison, not by key equality;
in both cases the biology version is a strict superset of the physics one, so the physics entry was
dropped and biology's kept.

| Dropped | Kept | Why |
|---|---|---|
| physics / cross_section_of_the_human_eye (6 parts) | biology / human_eye (7 parts) | biology adds *ciliary muscle* |
| physics / structure_of_the_human_ear (6 parts) | biology / human_ear (7 parts) | biology adds *semicircular canals* |

**Against the live 116 rows: 0 dropped.** No catalogue key, and no catalogue canonical under either
canonicaliser, collides with an existing `asset_key`, its two canonicalisations, or a stored
`canonical_key`. The live library is one cell-biology chapter plus the `sk_*` sketch primitives; the
catalogue's "cell" keys were renamed away from that space for the round-trip rule, which
incidentally clears it too.

**Near-duplicates deliberately kept** (distinct pictures, flagged so nobody re-merges them later):

* `composite_volcano_cross_section` and `shield_volcano_cross_section` vs the live
  `volcano_cross_section`. The standard K12 contrast pair; the live row is a single generic cone.
* chemistry `blast_furnace_cross_section` (charge hopper, refractory lining, tap holes) vs history
  `blast_furnace_ironmaking` (charge layers, casting channel) — a modern furnace and an
  industrial-revolution one, different part lists, different eras.
* `balanced_forces_box` / `unbalanced_forces_box`, `free_body_diagram_block_ground` /
  `..._block_slope`, the three `covalent_bonding_dot_and_cross_*` molecules, and
  `egyptian_pyramid_cross_section` / `egyptian_social_pyramid`. All genuinely different art.

## Canonical collisions

**0 remaining.** 492 entries fold to 492 distinct canonical keys, so no entry can silently overwrite
another's `cache/<canonical>/asset.png`. Six collisions existed in the supplied files and all six
were exact-key duplicates, resolved by the drops above — no two *different* pictures ever folded
together, so no third-party rename was needed.

## Prompt / parts verification

All 492 entries pass, checked mechanically:

* the prompt contains the literal tail `Name the layer groups exactly:`;
* the comma-separated names after that tail number **2–8** (observed range 3–8);
* every name in the tail appears in the entry's `parts` array **and** every `parts` entry appears in
  the tail — the two lists are exact set-equal, so `part_names_from_prompt` and the renderer's
  region map cannot disagree.

0 entries failed and none were dropped for this reason.

## Estimated generation cost

492 images × US$0.04 ≈ **US$19.68** one-off, at one generation per entry and no retries.

Two costs sit outside that figure and are worth budgeting for:

* **Region annotation.** The library has no `regions` column, so a hydrated asset re-runs
  `annotate_regions()` against the vision model on every fresh container — 1–2 paid vision calls per
  asset. Across 492 assets that recurs, per container, unless regions are stored with the row.
* **The canonicaliser split.** It is fixed *for this catalogue* by the naming rules above, but any
  key a director invents at lesson time that contains one of the 19 disagreeing tokens still
  downloads into an unread directory and pays for a fresh image. Unifying the two functions is the
  durable fix; this catalogue only routes around them.
