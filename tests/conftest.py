"""Shared fixtures for the test suite."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Force PyMuPDF backend in tests — avoids downloading heavy Docling ML models
os.environ.setdefault("DOCLING_BACKEND", "pymupdf")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Ensure the project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent1_ingestion.main import app
from database.db import Base, get_db

# Use a file-based test database (shared across endpoint + background tasks)
TEST_DB_PATH = PROJECT_ROOT / "test_sketchcast.db"
TEST_DB_URL = f"sqlite:///{TEST_DB_PATH}"

test_engine = create_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
TestSession = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


def override_get_db():
    db = TestSession()
    try:
        yield db
    finally:
        db.close()


# Override both the dependency and the background-task session factory
app.dependency_overrides[get_db] = override_get_db
app._session_factory = TestSession


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "slow: spawns real processes / runs ffmpeg; still part of the suite")


@pytest.fixture(autouse=True)
def setup_db():
    """Create all tables before each test and drop them after."""
    Base.metadata.create_all(bind=test_engine)
    yield
    # Close all connections, then drop tables
    test_engine.dispose()
    # Re-connect to drop
    Base.metadata.drop_all(bind=test_engine)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def sample_pdf_path():
    """Path to the test sample PDF. Generates it if it doesn't exist."""
    pdf_path = Path(__file__).parent / "test_sample.pdf"
    if not pdf_path.exists():
        from tests.generate_test_pdf import create_test_pdf
        create_test_pdf(str(pdf_path))
    return pdf_path


@pytest.fixture(autouse=True)
def _fresh_model_call_limiters(monkeypatch):
    """The image/vision per-minute windows are process-wide; without a reset a
    test that spends tokens makes a later test sleep for real (54 s seen).
    Tests of the pacing itself install their own windows."""
    from shared.ratelimit import RateLimiter
    from spike.scene_engine import raster_assets as ra
    monkeypatch.setattr(ra, "_LIMITERS",
                        {"image": RateLimiter(10**6), "vision": RateLimiter(10**6)})
    yield
