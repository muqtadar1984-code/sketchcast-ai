# Scanned-book chapter self-heal

## The failure this fixes

A teacher uploaded a **scanned** (image-only, no text layer) textbook. The chapter
stored as *"Unit 3: Computer storage"* pointed at pages that actually teach
networking / IP addresses, so the generated lesson was about the wrong topic.

Root cause (confirmed against the real PDF — Cambridge Primary Computing Learner's
Book 5, 88 pages): `detect_chapters_vision` copied the **printed** page number off
the contents page (*"Unit 3 Computer storage … 34"*) and used it as a **physical**
PDF page index. Because a scan's physical:printed page ratio is not 1:1, page 34
landed 15 pages too deep — inside Unit 5. Two safety nets also failed: the
index-time audit was blind on scans (its snippets come from the empty text layer),
and the generation guard skipped descriptive `Unit N: <topic>` titles.

## The three layers

1. **Detection returns physical positions** (`detect_chapters_vision`).
   The model reports each unit opener by its **image position** within the batch
   (1..N); we compute `physical_page = batch_start + position − 1`. A number
   outside 1..N (a leaked printed page number) is rejected. Positions come from
   the image order we control, never from text on the page.

2. **Index-time audit + self-heal** (`heal_chapter_boundaries`, wired into
   `index_book`). For a scanned book it builds a one-image-per-chapter **vision**
   opening snippet so the audit can actually judge title↔content. Any flagged
   chapter is re-matched to a freshly detected unit (`match_title_to_units`),
   preserving `chapter_num`; the result is **validated (or reverted)** so a heal
   can never corrupt `books.chapters`.

3. **Generation-time self-heal** (`process_generation` + `relocate_chapter_for_generation`).
   A per-chapter override is read **before** `books.chapters`. When the guard
   still catches a mismatch, it finds the pages that match the title, OCRs them,
   **strict-confirms** the primary topic, generates from those pages, and persists
   `heal_status='ok'` (paid once). If the topic genuinely isn't in the book it
   persists `heal_status='not_found'` and fails loud (and fast next time).

Every heal path is best-effort: any failure degrades to the pre-existing loud
error, never crashes a job.

## Deploy checklist

Layers A + B (physical-position detection + index-time audit/heal) are always on —
no flag, no migration — because they only improve detection and write `books.chapters`.
Layer C (generation-time override) needs the migration and is behind a flag:

1. **Deploy the worker** (Railway, `master`). New scanned uploads are now detected
   and index-healed correctly.
2. **App migration `0040_chapter_heal.sql`** — adds `heal_status`,
   `heal_start_page`, `heal_end_page` to `chapter_grounding`. Additive + idempotent.
3. **Set `FEATURE_CHAPTER_HEAL=true`** on the worker (Railway) — but only AFTER
   step 2. This turns on generation-time self-heal. If it were on before the columns
   exist, every override read/write would be a best-effort no-op and the expensive
   relocation would re-run on every generation instead of being paid once; with the
   flag off, a mismatch simply fails loud (the pre-existing behavior).
4. **Re-index books already stored wrong** (see below).

## Remediating a book already indexed wrong

Two options (either works; re-index is the thorough one):

- **Re-generate the affected chapter.** Generation-time self-heal relocates on the
  fly and persists the fix. Fixes the lesson content immediately, per chapter.

- **Re-index the whole book** (also fixes the dashboard chapter pages + AI-tutor
  grounding). Indexing is fired by a DB trigger on book insert; to re-run it for an
  existing book, enqueue one job (service-role / SQL editor):

  ```sql
  insert into jobs (book_id, type, status)
  values ('<BOOK_ID>', 'index_book', 'queued');
  ```

  Re-index re-detects with the fixed detector, re-heals, and clears any stale
  generation-time overrides and OCR cache for moved chapters.

## Verifying on a real scanned PDF

The unit tests (`tests/test_chapter_heal.py`) mock the model. To prove the fix
against a real book with a live key:

```bash
ANTHROPIC_API_KEY=… python scripts/acceptance_chapter_relocation.py \
  "textbook/Grade 5 Textbook - Hoders (3).pdf" \
  --expect-title "Computer storage" --expect-page 18 --tol 3 --forbid-page 33
```

Passes when Unit 3 "Computer storage" is detected near physical page 18 and **not**
near the old wrong page 33.

## Cost

- Text books: no new vision (audit keeps the free text-layer snippet).
- Clean scanned book: one cheap opening-snippet call at index time.
- Scanned book with a flagged chapter (index time, once): a fresh detection pass +
  a one-page strict-confirm per relocation + small text-only match calls. (Known
  minor: this detection is separate from the one `structure_book` already ran — a
  ~2x on that call for flagged scanned books; acceptable one-time, off the user's
  wait. Threading a single detection through both is a future optimization.)
  Generation-time relocation fires only after the guard fails (a rare error path)
  and its OCR becomes the lesson's source text (not wasted).
- `heal_status='not_found'` stops a genuinely-absent topic from re-running a
  minutes-long search on every click.
