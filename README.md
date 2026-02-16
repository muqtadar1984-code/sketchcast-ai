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
