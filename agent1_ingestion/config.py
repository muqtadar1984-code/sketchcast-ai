"""Configuration & environment variables for Agent 1: Library & Ingestion."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Base paths
BASE_DIR = Path(__file__).resolve().parent.parent
STORAGE_DIR = BASE_DIR / "storage"
BOOKS_DIR = STORAGE_DIR / "books"
EXTRACTED_IMAGES_DIR = STORAGE_DIR / "extracted_images"
PROCESSED_DIR = STORAGE_DIR / "processed"
DOCLING_CACHE_DIR = STORAGE_DIR / "docling_cache"

# Ensure directories exist
for d in [BOOKS_DIR, EXTRACTED_IMAGES_DIR, PROCESSED_DIR, DOCLING_CACHE_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# Database
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'sketchcast.db'}")

# Upload limits
MAX_UPLOAD_SIZE_MB = int(os.getenv("MAX_UPLOAD_SIZE_MB", "200"))
ALLOWED_EXTENSIONS = {".pdf"}

# Duplicate detection
FUZZY_MATCH_THRESHOLD = int(os.getenv("FUZZY_MATCH_THRESHOLD", "85"))

# Image extraction
MIN_IMAGE_WIDTH = int(os.getenv("MIN_IMAGE_WIDTH", "50"))
MIN_IMAGE_HEIGHT = int(os.getenv("MIN_IMAGE_HEIGHT", "50"))
IMAGE_CONTEXT_WORDS = int(os.getenv("IMAGE_CONTEXT_WORDS", "50"))

# Text extraction
LOW_TEXT_PAGE_THRESHOLD = int(os.getenv("LOW_TEXT_PAGE_THRESHOLD", "20"))
