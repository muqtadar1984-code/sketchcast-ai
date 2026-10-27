# SK R1 — retrieval test: Postgres FTS vs pgvector vs hybrid on Biology Chapter 2

Plan reference: *SketchCast SK AI Tutor — Architecture and Implementation Plan 2026-10-26*, §6 Retrieval and §15 row **R1**.
Run date: 2026-10-26 (UTC). Database: Supabase **branch `sk-b1`** (`grxsibudgjgepecxbyke`, ap-southeast-2), parent = production, which was **never written to**.
Everything for the test lives in `spike/sk_r1/`; every database object the test created carried the prefix `sk_r1_` and **all of them were dropped at the end** (verification query returned 0 remaining objects; `sk_book_blocks` 90, `sk_book_units` 6, `topic_articles` 1, `topic_questions` 34, `teaching_objectives` 6 rows, unchanged).

## 1. Recommendation (one paragraph)

**Add pgvector and run the hybrid (RRF of OR-relaxed FTS + cosine) behind the existing `RetrievalProvider` interface; keep the FTS adapter as the fallback and the gate for "no grounding".** The evidence: the POC's FTS as written (`websearch_to_tsquery`, every term required) returned *nothing* for 18 of 30 in-chapter student questions and for all 6 "explain it differently" turns (recall@5 0.33 / 0.00); relaxing it to OR brings in-chapter recall@5 to 0.97 but it then confidently returns blocks for 3 of 8 off-chapter questions (a "plant cell" paragraph for a photosynthesis question) and its scores do not separate on-chapter from off-chapter. pgvector alone reaches recall@5 0.97 / MRR 0.90 at block level and 1.00 / 1.00 at unit level, and its cosine score separates cleanly: every in-chapter or alternative-example question scored ≥ 0.73 at top-1, every off-chapter one ≤ 0.60, so a 0.65 gate gives 0 false positives and 0 wrong rejections on this set. Hybrid (OR + vector, RRF k=60) is the best of the five: recall@5 1.00, MRR 0.94, and it answers every injection string with ordinary blocks. Cost is not a factor: embedding the chapter cost $0.0007 (4,337 tokens on Gemini; the Titan v2 equivalent is $0.00009) and a query embedding is ~12 tokens. The price of vectors is latency, not money: the query embedding call (~485 ms p50 from this machine) is larger than the whole database round trip (~385 ms p50 end-to-end, ≤ 3 ms inside Postgres), so the app should embed in parallel with the router call and cache per turn. Bedrock KB + S3 Vectors would cost about the same in dollars but adds a second store, a second region and its own filter model for no measured gain at this scale; it was estimated from public pricing only (§7) and not provisioned.

## 2. What was compared

| Method | Implementation (all inside Postgres on the branch) |
|---|---|
| `fts_and` | `websearch_to_tsquery('english', q)` (the POC form — every term required) + `ts_rank_cd`, GIN index on a stored `tsvector` |
| `fts_or` | the same stemmed lexemes joined with `\|` (`to_tsquery`) + `ts_rank_cd` — one-line relaxation of the POC |
| `vec` | pgvector 0.8.2, 1024-dim embeddings, cosine (`<=>`), HNSW index; score = 1 − distance |
| `hybrid_and` | reciprocal rank fusion (k = 60) of `fts_and` top-20 and `vec` top-20 |
| `hybrid_or` | reciprocal rank fusion (k = 60) of `fts_or` top-20 and `vec` top-20 |

All five are SQL functions `sk_r1_search_*(…, product_id, chapter, k)` called through PostgREST RPC with the branch's publishable key, so the end-to-end number is what a Next.js route would see. The filter `product_id = ? and chapter = ?` sits in the `WHERE` of every function. Source: `spike/sk_r1/schema.sql`.

**Corpus.** The 90 `sk_book_blocks` of chapter 2 ("Seeing cells", product `cells-structure-function-organisation`, units u1–u6). Text per block type: paragraph/key_idea/extension/think → `text`; key_term → `term: text`; misconception → "Common mistake … Correct idea …"; figure → caption; list → items; table → caption + cells; worked_example → problem + steps + answer. Markdown emphasis stripped. 19,436 characters in total.

**§6 rule checked.** The insert selects from `sk_book_blocks` and `topic_articles.sections` only. `topic_questions` (34 rows, with `answer` and `marking_scheme`) were never read by the indexing SQL. The `answer` field of `think` blocks was also left out (treated as an answer key); worked examples were indexed in full because §6 names them as a retrieval source.

**Article sections: 0.** The branch copy of the approved article (`459c0b72…`) has `sections = []` by design (its `notes` column says the body was intentionally left empty on the branch). The schema indexes article sections when present; this run had none. Reading the production row to fill the gap was attempted once and refused by the session's permission policy (production reads), so it was not retried.

**Fake data for the filter proof.** Two full copies of the 90 blocks with identical text and identical vectors: one under a fake product id (`00000000-0000-4000-8000-00000000fa4e`, chapter 2) and one under the real product with chapter 9. A broken filter would tie with the real rows and surface immediately.

## 3. Question set and rubric

`spike/sk_r1/questions.json` — 50 questions: 30 in-chapter student questions (q01–q30; magnification, units, microscope parts, focusing, slide preparation, stains, drawings, resolution), 8 off-chapter biology questions (q31–q38; photosynthesis, heart, DNA, enzymes, natural selection, vaccines, autumn leaves, mitochondria), 6 "another example / explain differently" turns tied to a unit (q39–q44), 6 injection attempts (q45–q50: "ignore the book…", `'; DROP TABLE sk_book_blocks; --`, `<script>…`, "SYSTEM: developer mode", "retrieve rows from topic_questions", `OR 1=1 UNION SELECT … FROM topic_questions`).

Gold `unit_key`(s) and gold block(s) were judged by reading all 90 blocks (`spike/sk_r1/corpus_listing.txt`): a block is gold when a tutor could ground a correct answer on it alone; several blocks can be gold. Rubric (also embedded in the JSON):

- **recall@5** — 1 if any gold block (unit) is in the top 5; **MRR** — 1 / rank of the first gold block (unit), 0 if none.
- **Off-chapter** — gold is empty; a false positive is a top-1 score at or above the method's threshold. The number of in-chapter questions the same threshold would wrongly reject is reported beside it.
- **Injection** — pass = HTTP 200 and only ordinary rows inside the filter; where the string also carries a real question, recall is reported for information.
- **Filter** — any returned row whose `product_id`/`chapter` differ from the request is a leak.

## 4. Results

k = 5; 3 repeats (quality from repeat 1 — all three were identical; latency from all 450 calls per method).

### 4.1 Quality

| group | n | method | recall@5 block | MRR block | recall@5 unit | MRR unit | questions with 0 rows |
|---|---|---|---|---|---|---|---|
| in_chapter | 30 | fts_and | 0.33 | 0.28 | 0.40 | 0.38 | **18** |
| in_chapter | 30 | fts_or | 0.97 | 0.69 | 1.00 | 0.84 | 0 |
| in_chapter | 30 | vec | 0.97 | 0.90 | 1.00 | 1.00 | 0 |
| in_chapter | 30 | hybrid_and | 0.97 | 0.90 | 1.00 | 1.00 | 0 |
| in_chapter | 30 | **hybrid_or** | **1.00** | **0.94** | **1.00** | **1.00** | 0 |
| alt_example | 6 | fts_and | 0.00 | 0.00 | 0.00 | 0.00 | 6 |
| alt_example | 6 | fts_or | 0.67 | 0.47 | 1.00 | 0.76 | 0 |
| alt_example | 6 | vec | 1.00 | 0.92 | 1.00 | 1.00 | 0 |
| alt_example | 6 | hybrid_and | 1.00 | 0.92 | 1.00 | 1.00 | 0 |
| alt_example | 6 | hybrid_or | 1.00 | 0.89 | 1.00 | 1.00 | 0 |
| injection (4 with a real question) | 4 | fts_and | 0.00 | 0.00 | 0.00 | 0.00 | 4 |
| injection | 4 | fts_or | 1.00 | 0.71 | 1.00 | 0.88 | 0 |
| injection | 4 | vec / hybrid_and / hybrid_or | 1.00 | 1.00 | 1.00 | 1.00 | 0 |

The one block-level miss shared by `vec` and `hybrid_and` is q04 ("formula for the actual size of a cell from a drawing"): the top 5 are the right unit's neighbouring paragraphs and the worked example; the key_idea block with the formula ranks 6th. `hybrid_or` lifts it to rank 3. Per-question ranks for every method are in `spike/sk_r1/summary.md`.

### 4.2 Off-chapter false positives (8 questions) and the threshold

| method | threshold | off-chapter false positives | in-chapter + alt wrongly rejected (of 36) | in-chapter top-1 min | off-chapter top-1 max |
|---|---|---|---|---|---|
| fts_and | any match (> 0) | 0/8 | **24/36** | 0 | 0 |
| fts_or | ts_rank_cd ≥ 0.2 | **3/8** | 0/36 | 0.2 | 0.9 |
| vec | cosine ≥ 0.60 (fixed in advance) | 0/8 | 0/36 | **0.732** | **0.599** |
| hybrid_* | gate on the vector score of the best row, ≥ 0.60 | 0/8 | 0/36 | 0.732 | 0.599 |

The strict FTS "passes" the off-chapter test only because it returns nothing for most questions of either kind. OR-FTS has no usable threshold: the photosynthesis question scores 0.9 (shares "plant", "cell") while several in-chapter questions score 0.2–0.4. The vector score separates with a margin of 0.13; **0.65** is the recommended gate (midpoint), which also rejects the two injection strings that carry no real question (0.60, 0.56). The RRF score itself is rank-based and cannot be used as a gate; gate hybrid on its vector leg.

### 4.3 Injection attempts (6)

| method | HTTP 200 | rows outside the filter | strings returning 0 rows |
|---|---|---|---|
| fts_and | 6/6 | 0 | 6 |
| fts_or | 6/6 | 0 | 1 |
| vec, hybrid_and, hybrid_or | 6/6 | 0 | 0 |

No method errored. `websearch_to_tsquery` turns `'; DROP TABLE sk_book_blocks; -- <script>alert(1)</script>` into `'drop' & 'tabl' & 'sk' <-> 'book' <-> 'block' & …` — data, not SQL; the SQL fragments reach Postgres only as a bound text parameter of the RPC. The vector arm embeds the string and returns the blocks nearest to its real content (q46 → the focusing steps, q47 → the stain definition). Nothing from `topic_questions` can be returned because it is not in the index.

### 4.4 Filter enforcement

| method | calls (50 × 3) | rows returned | rows from the fake product or fake chapter |
|---|---|---|---|
| fts_and | 150 | 93 | **0** |
| fts_or | 150 | 663 | **0** |
| vec | 150 | 750 | **0** |
| hybrid_and | 150 | 750 | **0** |
| hybrid_or | 150 | 750 | **0** |

Positive control: pointing the same functions at the fake product (or chapter 9) returns 5 rows, all `fake_product` (`fake_chapter`), for every method that had matches — the copies are indexed and searchable, and the filter selects rather than hides. The forced-HNSW plan (`enable_sort = off`) showed the index scan discarding 10 fake rows under the `WHERE`; at POC scale the planner does not use HNSW at all (see 4.5).

### 4.5 Latency

| method | DB-side p50 (`clock_timestamp()` inside the function) | DB-side p95 | end-to-end PostgREST RPC p50 | end-to-end p95 |
|---|---|---|---|---|
| fts_and | 0.26 ms | 0.39 ms | 373 ms | 391 ms |
| fts_or | 0.70 ms | 1.85 ms | 373 ms | 399 ms |
| vec | 0.98 ms | 1.15 ms | 385 ms | 414 ms |
| hybrid_and | 1.06 ms | 1.27 ms | 385 ms | 402 ms |
| hybrid_or | 1.53 ms | 2.67 ms | 386 ms | 415 ms |
| query embedding (gemini-embedding-001, 50 calls) | — | — | 486 ms | 545 ms |

**Where these were measured.** End-to-end = this Windows machine → PostgREST on the `sk-b1` branch in **ap-southeast-2 (Sydney)**; the ~370 ms floor is network + PostgREST and is the same for every method, so the database work (≤ 3 ms) is invisible in it. **The app runs on Vercel**, whose function region is set per project (default `iad1`, US East, unless pinned); from Vercel the floor will be Vercel-region → Sydney, not this machine → Sydney, so re-measure from a Vercel function before quoting a user-facing number. The vector and hybrid methods also pay one embedding call per turn (486 ms p50 here, to Google's endpoint); on Bedrock in us-east-1 with Titan the same call is typically 50–150 ms from us-east-1, which is another reason to keep the app's region and the embedding region together.

Plans (EXPLAIN ANALYZE): FTS = BitmapAnd of the `(product_id, chapter)` b-tree and the GIN index, 0.43 ms; vector = index scan on `(product_id, chapter)` + top-N heapsort over 90 vectors, 1.98 ms — the planner ignores the HNSW index at 270 rows, which is correct. HNSW index 832 kB for 270 × 1024-dim vectors (~3 kB per vector); GIN 152 kB.

### 4.6 Cost

| item | measured / estimated | basis |
|---|---|---|
| Embedding chapter 2 (90 blocks, 4,337 tokens, counted by the API) | **$0.00065 measured** (gemini-embedding-001 at $0.15 / 1M tokens) | 90 calls, 66 s wall, p50 489 ms per call |
| Same on `amazon.titan-embed-text-v2:0` | **$0.000087 estimated** ($0.00002 / 1K tokens) | same token count |
| One query embedding (12.4 tokens average over the 50 questions, 620 tokens total) | $0.0000019 (Gemini) / $0.00000025 (Titan) per turn | measured tokens |
| Whole 10-chapter book (~45K tokens, ~1,000 vectors) | ≈ $0.007 (Gemini) / ≈ $0.001 (Titan) once; ≈ 7 MB of vectors + index in Supabase | extrapolated from chapter 2 |
| 100,000 tutor turns / month needing grounding | ≈ $0.19 (Gemini) / ≈ $0.025 (Titan) in embeddings; $0 extra in Supabase (Pro plan compute) | per-turn × volume |
| FTS | $0 beyond the database | — |

Prices are the public list prices as known when this was written; verify on the pricing pages before any approval.

## 5. Deviations from the brief, and why

1. **Embeddings came from `gemini-embedding-001` (1024 dims, `RETRIEVAL_DOCUMENT` / `RETRIEVAL_QUERY`), not Titan v2.** The AWS identity in `~/.aws` (`sketchcast-ops`, the one the worker uses for Claude on Bedrock) is denied `bedrock:InvokeModel` on every embedding model tried (`amazon.titan-embed-text-v2:0`, `titan-embed-text-v1`, `cohere.embed-english-v3`); Claude Converse works. Changing IAM is outside this test. `bench.py --embedder titan` is wired and will run unchanged once the policy allows it; the Titan cost line above is computed from the measured token counts. The relative findings (strict FTS misses; vector separates) do not depend on the embedding vendor, but the 0.65 threshold is specific to this model and must be re-calibrated for Titan.
2. **Article sections were not in the index (0 rows)** — see §2. Blocks only.
3. **Bedrock Knowledge Base + S3 Vectors was not run.** Creating a Knowledge Base, an S3 Vectors bucket/index and the S3 source bucket provisions chargeable AWS resources, which need the founder's separate approval; it is estimated in §7 from public pricing only. It would also need the same Titan permission that was denied.
4. Latency is from this machine, not from Vercel (§4.5).

## 6. What this means for the POC build

- Keep the `RetrievalProvider` interface; implement `fts` (OR-relaxed, as the fallback when no embedding is available), `pgvector`, and `hybrid` adapters. Default to `hybrid` with the vector-score gate at 0.65 (re-calibrate per embedding model, keep the number in config, log the top-1 score in `sk_events` with the `source_ref`).
- Index blocks and approved article sections, misconceptions and worked examples; never questions, answers or mark schemes (unchanged from §6). Keep the `think` answers out as well.
- Put the `(product_id, chapter)` filter in the SQL `WHERE`, not only in the application, and keep a `(product_id, chapter)` b-tree next to the HNSW index. When the shared index grows to many books, pgvector's HNSW applies the filter *after* the candidate scan (`ef_search` = 40 by default), which can return fewer than k rows for a small product in a big index; the fixes are `hnsw.iterative_scan = relaxed_order` (pgvector 0.8, available on the branch) or a partial index / table per book. Measure this again at 10+ books.
- Embed the query in parallel with the router call; one embedding per turn, cached on the turn.
- Re-run this bench after the article sections, misconceptions and worked examples are loaded, and after switching to Titan: the question set, rubric and script are reusable as they are.

## 7. Bedrock Knowledge Base + S3 Vectors — pricing-only estimate (not provisioned)

Not provisioned because chargeable AWS resources need the founder's separate approval. From the public price lists (verify before approving): S3 Vectors charges storage (~$0.06 per GB-month), PUT (~$0.20 per GB uploaded) and queries (~$0.0025 per 1,000 queries plus ~$0.004 per TB of data processed); a Bedrock Knowledge Base itself has no charge beyond the embedding model and the vector store, and `Retrieve` calls are billed only through the store. For a 10-chapter book (~1,000 vectors, ~5 MB) at 100,000 grounded turns a month: storage ≈ $0.0003, PUT ≈ $0.001 once, queries ≈ $0.25, Titan embeddings ≈ $0.025 — **≈ $0.30 / month**, versus ≈ $0.025 / month for pgvector-in-Supabase with Titan (compute already paid for in the Pro plan). Dollars are not the deciding factor at this scale. The real differences are: a second data store to keep in sync with approvals, rights (`sketchcast_owned` vs tenant-private) and versions; metadata filters instead of SQL `WHERE` for the product/chapter/rights gate; cross-region hops (KB region vs ap-southeast-2 data); and a Titan dependency that is currently blocked by IAM. OpenSearch Serverless stays excluded (~$350 / month floor, plan §6).

## 8. How to re-run

```
# 1. Apply the objects to a BRANCH database (Supabase MCP execute_sql, or psql): spike/sk_r1/schema.sql
# 2. Environment
SK_R1_SUPABASE_URL=https://<branch-ref>.supabase.co
SK_R1_SUPABASE_KEY=<the branch's publishable key>
GOOGLE_AI_API_KEY=<worker key>          # only for --embedder gemini
# 3. Embed, run, summarise (from the worker checkout; the venv has requests and boto3)
python -I spike/sk_r1/bench.py --all --repeats 3 --env-file .env
python -I spike/sk_r1/bench.py --all --embedder titan   # once bedrock:InvokeModel on Titan is allowed
python -I spike/sk_r1/bench.py --summary               # re-print the tables from results.json
# 4. Drop everything: spike/sk_r1/teardown.sql (verification query at the end must return 0 rows)
```

Files: `questions.json` (set + rubric), `bench.py`, `schema.sql`, `teardown.sql`, `corpus_dump.json` / `corpus_listing.txt` (what was indexed, used for the gold judgements), `results.json` (every call: rows, scores, `db_ms`, `e2e_ms`), `summary.md` (tables incl. per-question ranks), `embed_info.json`, `thresholds.json`.
