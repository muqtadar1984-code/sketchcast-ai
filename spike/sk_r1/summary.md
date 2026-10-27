Run 2026-10-26T23:49:00Z against https://grxsibudgjgepecxbyke.supabase.co (k=5, 3 repeats; metrics from repeat 1, latency from all repeats)

### Quality (recall@5 and MRR, block level and unit level)

| group | n | method | recall@5 block | MRR block | recall@5 unit | MRR unit | empty results |
|---|---|---|---|---|---|---|---|
| in_chapter | 30 | fts_and | 0.33 | 0.30 | 0.40 | 0.38 | 18 |
| in_chapter | 30 | fts_or | 0.90 | 0.68 | 1.00 | 0.83 | 0 |
| in_chapter | 30 | vec | 1.00 | 0.94 | 1.00 | 1.00 | 0 |
| in_chapter | 30 | hybrid_and | 1.00 | 0.93 | 1.00 | 1.00 | 0 |
| in_chapter | 30 | hybrid_or | 1.00 | 0.93 | 1.00 | 1.00 | 0 |
| alt_example | 6 | fts_and | 0.00 | 0.00 | 0.00 | 0.00 | 6 |
| alt_example | 6 | fts_or | 0.67 | 0.47 | 0.83 | 0.72 | 0 |
| alt_example | 6 | vec | 1.00 | 0.89 | 1.00 | 1.00 | 0 |
| alt_example | 6 | hybrid_and | 1.00 | 0.89 | 1.00 | 1.00 | 0 |
| alt_example | 6 | hybrid_or | 1.00 | 0.81 | 1.00 | 0.92 | 0 |
| injection | 4 | fts_and | 0.00 | 0.00 | 0.00 | 0.00 | 4 |
| injection | 4 | fts_or | 1.00 | 0.71 | 1.00 | 0.75 | 0 |
| injection | 4 | vec | 1.00 | 0.75 | 1.00 | 1.00 | 0 |
| injection | 4 | hybrid_and | 1.00 | 0.75 | 1.00 | 1.00 | 0 |
| injection | 4 | hybrid_or | 1.00 | 0.80 | 1.00 | 1.00 | 0 |

### Off-chapter questions (8): top-1 score vs threshold

| method | threshold (fixed) | off-chapter false positives | in-chapter wrongly rejected | best threshold on this data | FP / rejected at best | in-chapter top-1 min | off-chapter top-1 max |
|---|---|---|---|---|---|---|---|
| fts_and | 0.0 | 0/8 | 24/36 | 0.0 | 0/8 / 24/36 | 0.0 | 0.0 |
| fts_or | 0.2 | 3/8 | 0/36 | 0.2 | 3/8 / 0/36 | 0.2 | 0.9 |
| vec | 0.6 | 0/8 | 10/36 | 0.4391 | 0/8 / 0/36 | 0.4391 | 0.2017 |
| hybrid_and | 0.6 | 0/8 | 10/36 | 0.4391 | 0/8 / 0/36 | 0.4391 | 0.2017 |
| hybrid_or | 0.6 | 0/8 | 11/36 | 0.2565 | 0/8 / 0/36 | 0.2565 | 0.2017 |

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
| fts_and | 0.26 | 0.39 | 394 | 410 |
| fts_or | 0.70 | 1.53 | 395 | 411 |
| vec | 0.99 | 1.10 | 405 | 423 |
| hybrid_and | 1.08 | 1.20 | 406 | 426 |
| hybrid_or | 1.52 | 2.59 | 407 | 423 |

Query embedding (amazon.titan-embed-text-v2:0, from this machine): p50 401.5 ms, p95 487.5 ms over 50 calls; vec and hybrid pay this once per turn on top of the RPC.

### Per-question detail (repeat 1): first gold rank per method (0 = none in top 5)

| id | group | fts_and | fts_or | vec | hybrid_and | hybrid_or |
|---|---|---|---|---|---|---|
| q01 | in_chapter | 0 (0.00) | 2 (0.50) | 1 (0.65) | 1 (0.65) | 1 (0.65) |
| q02 | in_chapter | 0 (0.00) | 1 (0.90) | 1 (0.80) | 1 (0.80) | 1 (0.80) |
| q03 | in_chapter | 0 (0.00) | 1 (0.90) | 1 (0.74) | 1 (0.74) | 1 (0.74) |
| q04 | in_chapter | 0 (0.00) | 2 (1.20) | 3 (0.73) | 3 (0.73) | 2 (0.73) |
| q05 | in_chapter | 0 (0.00) | 2 (1.20) | 1 (0.68) | 1 (0.68) | 1 (0.68) |
| q06 | in_chapter | 1 (0.00) | 1 (0.60) | 1 (0.69) | 1 (0.69) | 1 (0.69) |
| q07 | in_chapter | 0 (0.00) | 3 (1.00) | 1 (0.44) | 1 (0.44) | 1 (0.44) |
| q08 | in_chapter | 1 (0.20) | 1 (0.20) | 1 (0.91) | 1 (0.91) | 1 (0.91) |
| q09 | in_chapter | 0 (0.00) | 1 (0.50) | 1 (0.56) | 1 (0.56) | 1 (0.56) |
| q10 | in_chapter | 0 (0.00) | 1 (0.60) | 1 (0.69) | 1 (0.69) | 1 (0.69) |
| q11 | in_chapter | 0 (0.15) | 3 (0.70) | 1 (0.79) | 2 (0.79) | 1 (0.79) |
| q12 | in_chapter | 1 (0.00) | 1 (0.50) | 1 (0.62) | 1 (0.62) | 1 (0.62) |
| q13 | in_chapter | 1 (0.00) | 2 (1.00) | 1 (0.86) | 1 (0.86) | 1 (0.86) |
| q14 | in_chapter | 0 (0.00) | 1 (0.50) | 1 (0.69) | 1 (0.69) | 1 (0.69) |
| q15 | in_chapter | 2 (0.10) | 1 (0.50) | 1 (0.72) | 1 (0.72) | 1 (0.72) |
| q16 | in_chapter | 2 (0.06) | 2 (0.40) | 1 (0.56) | 1 (0.56) | 1 (0.56) |
| q17 | in_chapter | 0 (0.00) | 4 (0.90) | 1 (0.68) | 1 (0.68) | 2 (0.68) |
| q18 | in_chapter | 0 (0.00) | 1 (1.30) | 1 (0.50) | 1 (0.50) | 1 (0.50) |
| q19 | in_chapter | 1 (0.02) | 1 (1.10) | 1 (0.69) | 1 (0.69) | 1 (0.69) |
| q20 | in_chapter | 0 (0.00) | 0 (0.30) | 1 (0.83) | 1 (0.83) | 1 (0.83) |
| q21 | in_chapter | 0 (0.00) | 5 (0.90) | 1 (0.66) | 1 (0.66) | 1 (0.66) |
| q22 | in_chapter | 0 (0.00) | 0 (0.90) | 1 (0.52) | 1 (0.52) | 1 (0.52) |
| q23 | in_chapter | 1 (0.03) | 1 (0.40) | 1 (0.72) | 1 (0.72) | 1 (0.72) |
| q24 | in_chapter | 0 (0.01) | 0 (0.50) | 1 (0.72) | 2 (0.72) | 2 (0.26) |
| q25 | in_chapter | 0 (0.00) | 1 (1.20) | 1 (0.76) | 1 (0.76) | 1 (0.76) |
| q26 | in_chapter | 0 (0.00) | 1 (0.90) | 2 (0.70) | 2 (0.70) | 1 (0.70) |
| q27 | in_chapter | 1 (0.01) | 3 (1.10) | 3 (0.58) | 1 (0.58) | 2 (0.58) |
| q28 | in_chapter | 0 (0.00) | 1 (0.50) | 1 (0.62) | 1 (0.62) | 1 (0.62) |
| q29 | in_chapter | 0 (0.00) | 2 (0.50) | 1 (0.65) | 1 (0.65) | 1 (0.65) |
| q30 | in_chapter | 1 (0.01) | 1 (1.10) | 1 (0.81) | 1 (0.81) | 1 (0.81) |
| q31 | off_chapter | - (0.00) | - (0.90) | - (0.17) | - (0.17) | - (0.15) |
| q32 | off_chapter | - (0.00) | - (0.00) | - (0.11) | - (0.11) | - (0.11) |
| q33 | off_chapter | - (0.00) | - (0.20) | - (0.19) | - (0.19) | - (0.19) |
| q34 | off_chapter | - (0.00) | - (0.10) | - (0.13) | - (0.13) | - (0.13) |
| q35 | off_chapter | - (0.00) | - (0.10) | - (0.12) | - (0.12) | - (0.12) |
| q36 | off_chapter | - (0.00) | - (0.10) | - (0.13) | - (0.13) | - (0.13) |
| q37 | off_chapter | - (0.00) | - (0.40) | - (0.20) | - (0.20) | - (0.20) |
| q38 | off_chapter | - (0.00) | - (0.00) | - (0.13) | - (0.13) | - (0.13) |
| q39 | alt_example | 0 (0.00) | 0 (1.00) | 1 (0.59) | 1 (0.59) | 2 (0.59) |
| q40 | alt_example | 0 (0.00) | 2 (1.00) | 1 (0.73) | 1 (0.73) | 1 (0.73) |
| q41 | alt_example | 0 (0.00) | 0 (0.50) | 3 (0.50) | 3 (0.50) | 3 (0.50) |
| q42 | alt_example | 0 (0.00) | 3 (0.60) | 1 (0.54) | 1 (0.54) | 1 (0.54) |
| q43 | alt_example | 0 (0.00) | 1 (0.30) | 1 (0.53) | 1 (0.53) | 1 (0.53) |
| q44 | alt_example | 0 (0.00) | 1 (0.40) | 1 (0.76) | 1 (0.76) | 1 (0.76) |
| q45 | injection | 0 (0.00) | 1 (0.40) | 1 (0.65) | 1 (0.65) | 1 (0.65) |
| q46 | injection | 0 (0.00) | 2 (0.60) | 2 (0.44) | 2 (0.44) | 1 (0.39) |
| q47 | injection | 0 (0.00) | 1 (0.30) | 1 (0.72) | 1 (0.72) | 1 (0.72) |
| q48 | injection | - (0.00) | - (0.00) | - (0.22) | - (0.22) | - (0.22) |
| q49 | injection | - (0.00) | - (0.10) | - (0.14) | - (0.14) | - (0.14) |
| q50 | injection | 0 (0.00) | 3 (0.40) | 2 (0.46) | 2 (0.46) | 5 (0.46) |
