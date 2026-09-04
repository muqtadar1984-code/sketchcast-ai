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
