# SketchCast AI — Agent 1: Library & Ingestion

Processes textbook PDFs into structured content for downstream agents that generate interactive podcasts with whiteboard-style sketch animations.

## Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Set up environment
cp .env.example .env

# Run the server
uvicorn agent1_ingestion.main:app --reload --port 8000
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check |
| `POST` | `/upload` | Upload a PDF with optional metadata |
| `GET` | `/library` | List all processed books |
| `GET` | `/library/{book_id}` | Full book details + structured content |
| `GET` | `/library/{book_id}/chapter/{chapter_num}` | Single chapter content |
| `GET` | `/library/{book_id}/status` | Processing status |

## Upload Example

```bash
curl -X POST http://localhost:8000/upload \
  -F "file=@textbook.pdf" \
  -F "title=Exploring Society" \
  -F "author=NCERT"
```

Response:
```json
{
  "status": "processing",
  "book_id": "uuid-here",
  "message": "Upload received. Processing in background."
}
```

Poll status:
```bash
curl http://localhost:8000/library/<book_id>/status
```

## Streamlit UI

Run the API server first, then launch the Streamlit app:

```bash
# Terminal 1: API server
uvicorn agent1_ingestion.main:app --reload --port 8000

# Terminal 2: Streamlit UI
streamlit run streamlit_app.py
```

The UI provides:
- **Upload Book** — Drag-and-drop PDF upload with metadata fields and live progress
- **Library** — Browse all processed books with status and metadata
- **Book Viewer** — Explore structured content chapter by chapter, with sections, key boxes, and images

## Running Tests

```bash
pytest tests/ -v
```

## Worker render tuning

The scene engine renders one MP4 per narration segment. Two variables control
how much of the box a lesson uses (see `worker/.env.example`):

| Variable | Default | Meaning |
|---|---|---|
| `RENDER_WORKERS` | 4 | Segment threads per lesson (TTS + encode overlap). 1 = sequential. |
| `RENDER_PROCESSES` | 0 | Size of the rasterization process pool. 0 = render in-process (the rollback). |

With `RENDER_PROCESSES > 0` each segment thread does its TTS, then submits the
frame rasterization + ffmpeg encode to a module-global `ProcessPoolExecutor`
(explicit `spawn`; the child never calls an image model — the parent warms
the asset cache first) and blocks on the result. Simultaneous renders are
`min(RENDER_WORKERS, RENDER_PROCESSES)`, so set both. One pool per worker
process is shared by every `WORKER_CONCURRENCY` job. A broken pool
(OOM-killed child) is recreated and the affected segment finishes in-process.
`compose_episode_videos` logs `compose: N segments, M rendered, S s wall
(workers=W, processes=P)` per lesson. Railway: `RENDER_PROCESSES=8`,
`RENDER_WORKERS=8` on the 24 vCPU box (~200 MB per child).

## Image reliability

Pictures are the one paid, rate-limited call in a render, and a 429 burst once
cost a lesson two blank boards. Four variables govern how hard a lesson tries
and how it degrades when it cannot get a picture.

| Variable | Default | Meaning |
|---|---|---|
| `IMAGE_CALLS_PER_LESSON` | 24 | Image generations one lesson may pay for. A refused call is refunded; the hard stop is a separate ceiling at 2x this, counted in HTTP REQUESTS. |
| `IMAGE_WARM_BUDGET_SECS` | 180 | Wall clock the up-front warm pass may spend fetching the lesson's pictures before rendering starts. |
| `IMAGE_WARM_RETRY_SECS` | 120 | How long a key still pending when that budget expires is deferred for. It is NEVER abandoned: a rate limit expires, so exactly one later segment retries it. |
| `IMAGE_DEFER_SECONDS` | 45 | Default deferral when a provider 429s without naming a `Retry-After`. Capped at 120 s either way. |

A key that is deferred or abandoned costs no provider call at all, so eight
render threads asking for one refused picture run one ladder, not eight. When
a picture that had a prompt still cannot be got, the board keeps a dashed
placeholder frame rather than collapsing its labels onto a point, and the
renderer reports `ASSET_PLACEHOLDER`; the acceptance gate counts that exactly
as it counts `ASSET_UNRESOLVED`, so a lesson mostly made of frames still
blocks. The frame answers to any layer the scene names — it has no parts to
tell apart — and the two persistent avatars are excluded from it: a teacher we
cannot draw is simply absent, not a dashed rectangle in the corner of every
frame of the lesson. All of this state is per GENERATION, not per process —
with `WORKER_CONCURRENCY > 1` several lessons share one process.

Every unresolved board carries `reason=` on its warning line, and the
acceptance summary counts them per cause:

| Reason | What to do about it |
|---|---|
| `no_prompt` | The director planned an illustration and named no asset prompt. A script bug. |
| `rate_limited` | A provider 429'd and the lesson chose to wait rather than retry harder. Read the logged response body: a QUOTA 429 names a number to raise, a CAPACITY 429 does not. |
| `budget_exhausted` | Our OWN ceiling refused the call, not the provider. Raise `IMAGE_CALLS_PER_LESSON` if the lesson legitimately needs more. |
| `generation_failed` | The provider was asked and produced nothing, for a reason that was not a rate limit. Go and look at the provider. |
| `cache_only_miss` | A render child (`RENDER_PROCESSES > 1`) may only read the cache, and the warm pass did not land this file. |
| `no_vector` | Nothing at any tier, including the authored vectors. |

## Catalogue kits (Phase 3): deploy order and the never-starve rule

A catalogue kit's generations are ordinary builder rows (presentation, deck,
documents) owned by the system account with `params.catalogue = true`. The
worker's lanes tell them from a teacher's by the copy of that flag on
**`jobs.params`**, which only app migration **0115** (`create_job_for_generation()`)
writes at insert. **Apply 0115 before `FEATURE_CATALOGUE_GENERATE` is turned
on**: a kit row inserted before it carries no flag, so the user lanes claim
it (the never-starve gate is bypassed for it), `builder_queued` counts it as a
user waiting, and its failure files a console issue under the system account.
The worker checks at boot (`CATALOGUE FLAG CHECK`) and logs CRITICAL when any
queued catalogue job lacks the flag; requeue or re-create those rows after the
migration.

Kits yield to real users twice: at CLAIM (the last lane runs only with no live
user builder and inside the window) and DURING the render (the presentation
loop polls the queue between parts, and the scene engine asks the same probe
before every image generation; a user who outlasts the wait fails the kit
rather than being rendered over — Retry it inside the window).

| Variable | Default | Meaning |
|---|---|---|
| `CATALOGUE_WINDOW_UTC` | `20:00-05:00` | When kit generations may be claimed (UTC, wraps midnight). `always` disables the window and is ONLY for a supervised pilot. A typo — a zero-width `HH:MM-HH:MM` included — keeps the default. |
| `CATALOGUE_PRESENTATION_MARGIN_MIN` | 60 | A presentation is claimed only while the window stays open this long; documents and decks need no margin. |
| `CATALOGUE_YIELD_POLL_SECONDS` | 20 | How often a rendering kit re-asks whether a user builder is live. |
| `CATALOGUE_YIELD_MAX_SECONDS` | 1800 | How long it waits before giving the contended work up (an image is skipped; a part boundary fails the kit). |

## Project Structure

```
sketchcast/
├── agent1_ingestion/     # Core ingestion logic
│   ├── main.py           # FastAPI endpoints
│   ├── library.py        # Duplicate detection & library queries
│   ├── extractor.py      # PDF text extraction & font analysis
│   ├── image_extractor.py# Image/diagram extraction
│   ├── structurer.py     # Hierarchical content structuring
│   ├── models.py         # Pydantic request/response models
│   └── config.py         # Configuration
├── database/
│   ├── db.py             # SQLAlchemy engine & sessions
│   └── schemas.py        # ORM models (Book, Chapter, Image)
├── storage/              # File storage (PDFs, images, JSON)
├── tests/                # Test suite
├── requirements.txt
└── .env.example
```

## Math Microservice (mathsvc/)

A constrained SymPy service the AI tutor calls to compute/verify math for
students — deployed as a SECOND Railway service from this same repo. The LLM
sends a structured tool call (`op` + expression strings); only whitelisted
operations on validated expressions ever run, and errors are always
child-safe (never a guess, never a traceback).

```bash
# Run locally (from the repo root)
pip install -r mathsvc/requirements.txt
uvicorn mathsvc.app:app --port 8090   # requires MATH_SVC_TOKEN env

# Tests
python -m pytest tests/test_mathsvc.py -q
```

See `mathsvc/README.md` for the API contract and the Railway deploy steps
(custom start command + `MATH_SVC_TOKEN`).
