Run 2026-10-26T20:45:21Z against https://grxsibudgjgepecxbyke.supabase.co (k=5, 3 repeats; metrics from repeat 1, latency from all repeats)

### Quality (recall@5 and MRR, block level and unit level)

| group | n | method | recall@5 block | MRR block | recall@5 unit | MRR unit | empty results |
|---|---|---|---|---|---|---|---|
| in_chapter | 30 | fts_and | 0.33 | 0.28 | 0.40 | 0.38 | 18 |
| in_chapter | 30 | fts_or | 0.97 | 0.69 | 1.00 | 0.84 | 0 |
| in_chapter | 30 | vec | 0.97 | 0.90 | 1.00 | 1.00 | 0 |
| in_chapter | 30 | hybrid_and | 0.97 | 0.90 | 1.00 | 1.00 | 0 |
| in_chapter | 30 | hybrid_or | 1.00 | 0.94 | 1.00 | 1.00 | 0 |
| alt_example | 6 | fts_and | 0.00 | 0.00 | 0.00 | 0.00 | 6 |
| alt_example | 6 | fts_or | 0.67 | 0.47 | 1.00 | 0.76 | 0 |
| alt_example | 6 | vec | 1.00 | 0.92 | 1.00 | 1.00 | 0 |
| alt_example | 6 | hybrid_and | 1.00 | 0.92 | 1.00 | 1.00 | 0 |
| alt_example | 6 | hybrid_or | 1.00 | 0.89 | 1.00 | 1.00 | 0 |
| injection | 4 | fts_and | 0.00 | 0.00 | 0.00 | 0.00 | 4 |
| injection | 4 | fts_or | 1.00 | 0.71 | 1.00 | 0.88 | 0 |
| injection | 4 | vec | 1.00 | 1.00 | 1.00 | 1.00 | 0 |
| injection | 4 | hybrid_and | 1.00 | 1.00 | 1.00 | 1.00 | 0 |
| injection | 4 | hybrid_or | 1.00 | 1.00 | 1.00 | 1.00 | 0 |

### Off-chapter questions (8): top-1 score vs threshold

| method | threshold (fixed) | off-chapter false positives | in-chapter wrongly rejected | best threshold on this data | FP / rejected at best | in-chapter top-1 min | off-chapter top-1 max |
|---|---|---|---|---|---|---|---|
| fts_and | 0.0 | 0/8 | 24/36 | 0.0 | 0/8 / 24/36 | 0.0 | 0.0 |
| fts_or | 0.2 | 3/8 | 0/36 | 0.2 | 3/8 / 0/36 | 0.2 | 0.9 |
| vec | 0.6 | 0/8 | 0/36 | 0.6 | 0/8 / 0/36 | 0.7318 | 0.5993 |
| hybrid_and | 0.6 | 0/8 | 0/36 | 0.6 | 0/8 / 0/36 | 0.7318 | 0.5993 |
| hybrid_or | 0.6 | 0/8 | 0/36 | 0.6 | 0/8 / 0/36 | 0.7318 | 0.5993 |

### Injection attempts (6): errors and leaks

| method | HTTP 200 | rows outside filter | questions with 0 rows |
|---|---|---|---|
| fts_and | 6/6 | 0 | 6 |
| fts_or | 6/6 | 0 | 1 |
| vec | 6/6 | 0 | 0 |
| hybrid_and | 6/6 | 0 | 0 |
| hybrid_or | 6/6 | 0 | 0 |

### Filter enforcement (all 50 questions x all repeats)

| method | calls | rows returned | rows from fake product or fake chapter |
|---|---|---|---|
| fts_and | 150 | 93 | 0 |
| fts_or | 150 | 663 | 0 |
| vec | 150 | 750 | 0 |
| hybrid_and | 150 | 750 | 0 |
| hybrid_or | 150 | 750 | 0 |

Probe with the filter pointed AT the fake copies (proves the copies are indexed and the filter selects, not hides):

- fake_product: fts_and: 0 rows, kinds=[], in scope=True; fts_or: 5 rows, kinds=['fake_product'], in scope=True; vec: 5 rows, kinds=['fake_product'], in scope=True; hybrid_and: 5 rows, kinds=['fake_product'], in scope=True; hybrid_or: 5 rows, kinds=['fake_product'], in scope=True
- fake_chapter: fts_and: 0 rows, kinds=[], in scope=True; fts_or: 5 rows, kinds=['fake_chapter'], in scope=True; vec: 5 rows, kinds=['fake_chapter'], in scope=True; hybrid_and: 5 rows, kinds=['fake_chapter'], in scope=True; hybrid_or: 5 rows, kinds=['fake_chapter'], in scope=True

### Latency (ms)

| method | DB-side p50 (clock_timestamp inside the function) | DB-side p95 | end-to-end PostgREST p50 (this machine) | end-to-end p95 |
|---|---|---|---|---|
| fts_and | 0.26 | 0.39 | 373 | 391 |
| fts_or | 0.70 | 1.85 | 373 | 399 |
| vec | 0.98 | 1.15 | 385 | 414 |
| hybrid_and | 1.06 | 1.27 | 385 | 402 |
| hybrid_or | 1.53 | 2.67 | 386 | 415 |

Query embedding (gemini-embedding-001, from this machine): p50 485.5 ms, p95 545.1 ms over 50 calls; vec and hybrid pay this once per turn on top of the RPC.

### Per-question detail (repeat 1): first gold rank per method (0 = none in top 5)

| id | group | fts_and | fts_or | vec | hybrid_and | hybrid_or |
|---|---|---|---|---|---|---|
| q01 | in_chapter | 0 (0.00) | 2 (0.50) | 1 (0.78) | 1 (0.78) | 1 (0.78) |
| q02 | in_chapter | 0 (0.00) | 1 (0.90) | 1 (0.80) | 1 (0.80) | 1 (0.80) |
| q03 | in_chapter | 0 (0.00) | 1 (0.90) | 1 (0.73) | 1 (0.73) | 1 (0.73) |
| q04 | in_chapter | 0 (0.00) | 1 (1.20) | 0 (0.81) | 0 (0.81) | 3 (0.81) |
| q05 | in_chapter | 0 (0.00) | 2 (1.20) | 1 (0.84) | 1 (0.84) | 1 (0.84) |
| q06 | in_chapter | 1 (0.00) | 1 (0.60) | 1 (0.79) | 1 (0.79) | 1 (0.79) |
| q07 | in_chapter | 0 (0.00) | 3 (1.00) | 1 (0.77) | 1 (0.77) | 1 (0.77) |
| q08 | in_chapter | 2 (0.20) | 2 (0.20) | 1 (0.80) | 1 (0.80) | 1 (0.80) |
| q09 | in_chapter | 0 (0.00) | 1 (0.50) | 1 (0.74) | 1 (0.74) | 1 (0.74) |
| q10 | in_chapter | 0 (0.00) | 1 (0.60) | 1 (0.74) | 1 (0.74) | 1 (0.74) |
| q11 | in_chapter | 0 (0.15) | 3 (0.70) | 1 (0.75) | 2 (0.75) | 1 (0.75) |
| q12 | in_chapter | 1 (0.00) | 1 (0.50) | 1 (0.74) | 1 (0.74) | 1 (0.74) |
| q13 | in_chapter | 1 (0.00) | 2 (1.00) | 1 (0.84) | 1 (0.84) | 1 (0.84) |
| q14 | in_chapter | 0 (0.00) | 1 (0.50) | 1 (0.81) | 1 (0.81) | 1 (0.81) |
| q15 | in_chapter | 2 (0.10) | 1 (0.50) | 1 (0.82) | 1 (0.82) | 1 (0.82) |
| q16 | in_chapter | 2 (0.06) | 3 (0.40) | 1 (0.79) | 1 (0.79) | 1 (0.79) |
| q17 | in_chapter | 0 (0.00) | 4 (0.90) | 1 (0.80) | 1 (0.80) | 2 (0.80) |
| q18 | in_chapter | 0 (0.00) | 1 (1.30) | 2 (0.78) | 2 (0.78) | 1 (0.74) |
| q19 | in_chapter | 1 (0.02) | 1 (1.10) | 2 (0.79) | 1 (0.79) | 1 (0.79) |
| q20 | in_chapter | 0 (0.00) | 0 (0.30) | 1 (0.83) | 1 (0.83) | 1 (0.83) |
| q21 | in_chapter | 0 (0.00) | 4 (0.90) | 1 (0.80) | 1 (0.80) | 1 (0.78) |
| q22 | in_chapter | 0 (0.00) | 5 (0.90) | 1 (0.78) | 1 (0.78) | 1 (0.78) |
| q23 | in_chapter | 1 (0.03) | 1 (0.40) | 1 (0.86) | 1 (0.86) | 1 (0.86) |
| q24 | in_chapter | 0 (0.01) | 4 (0.50) | 1 (0.79) | 2 (0.79) | 2 (0.73) |
| q25 | in_chapter | 0 (0.00) | 1 (1.20) | 1 (0.85) | 1 (0.85) | 1 (0.85) |
| q26 | in_chapter | 0 (0.00) | 1 (0.90) | 2 (0.82) | 2 (0.82) | 1 (0.82) |
| q27 | in_chapter | 1 (0.01) | 3 (1.10) | 2 (0.79) | 1 (0.79) | 1 (0.79) |
| q28 | in_chapter | 0 (0.00) | 1 (0.50) | 1 (0.77) | 1 (0.77) | 1 (0.77) |
| q29 | in_chapter | 0 (0.00) | 2 (0.50) | 1 (0.74) | 1 (0.74) | 1 (0.74) |
| q30 | in_chapter | 1 (0.01) | 1 (1.10) | 1 (0.77) | 1 (0.77) | 1 (0.77) |
| q31 | off_chapter | - (0.00) | - (0.90) | - (0.60) | - (0.60) | - (0.60) |
| q32 | off_chapter | - (0.00) | - (0.00) | - (0.59) | - (0.59) | - (0.59) |
| q33 | off_chapter | - (0.00) | - (0.20) | - (0.59) | - (0.59) | - (0.59) |
| q34 | off_chapter | - (0.00) | - (0.10) | - (0.57) | - (0.57) | - (0.57) |
| q35 | off_chapter | - (0.00) | - (0.10) | - (0.56) | - (0.56) | - (0.56) |
| q36 | off_chapter | - (0.00) | - (0.10) | - (0.54) | - (0.54) | - (0.54) |
| q37 | off_chapter | - (0.00) | - (0.40) | - (0.60) | - (0.60) | - (0.60) |
| q38 | off_chapter | - (0.00) | - (0.00) | - (0.58) | - (0.58) | - (0.58) |
| q39 | alt_example | 0 (0.00) | 0 (1.00) | 2 (0.77) | 2 (0.77) | 1 (0.77) |
| q40 | alt_example | 0 (0.00) | 2 (1.00) | 1 (0.84) | 1 (0.84) | 1 (0.84) |
| q41 | alt_example | 0 (0.00) | 0 (0.50) | 1 (0.75) | 1 (0.75) | 3 (0.75) |
| q42 | alt_example | 0 (0.00) | 3 (0.60) | 1 (0.78) | 1 (0.78) | 1 (0.78) |
| q43 | alt_example | 0 (0.00) | 1 (0.30) | 1 (0.79) | 1 (0.79) | 1 (0.79) |
| q44 | alt_example | 0 (0.00) | 1 (0.40) | 1 (0.80) | 1 (0.80) | 1 (0.80) |
| q45 | injection | 0 (0.00) | 1 (0.40) | 1 (0.75) | 1 (0.75) | 1 (0.75) |
| q46 | injection | 0 (0.00) | 2 (0.60) | 1 (0.72) | 1 (0.72) | 1 (0.72) |
| q47 | injection | 0 (0.00) | 1 (0.30) | 1 (0.69) | 1 (0.69) | 1 (0.69) |
| q48 | injection | - (0.00) | - (0.00) | - (0.60) | - (0.60) | - (0.60) |
| q49 | injection | - (0.00) | - (0.10) | - (0.56) | - (0.56) | - (0.56) |
| q50 | injection | 0 (0.00) | 3 (0.40) | 1 (0.69) | 1 (0.69) | 1 (0.69) |
