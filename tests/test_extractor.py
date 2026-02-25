"""Tests for PDF extraction, structuring, and chapter retrieval."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent1_ingestion.extractor import extract_pdf
from agent1_ingestion.image_extractor import extract_images
from agent1_ingestion.structurer import structure_book


def _upload(client, pdf_path, title="Exploring Society", author="NCERT"):
    data = {"title": title, "author": author}
    with open(pdf_path, "rb") as f:
        return client.post("/upload", files={"file": ("test.pdf", f, "application/pdf")}, data=data)


def _wait_for_completion(client, book_id, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = client.get(f"/library/{book_id}/status")
        status = resp.json()["status"]
        if status in ("completed", "failed"):
            return status
        time.sleep(0.5)
    return "timeout"


class TestExtractor:
    def test_extract_pages(self, sample_pdf_path):
        result = extract_pdf(sample_pdf_path)
        assert result.total_pages == 6
        assert result.readability_score > 0

    def test_heading_detection(self, sample_pdf_path):
        result = extract_pdf(sample_pdf_path)
        # Should detect at least one heading (level >= 1)
        headings = [i for i in result.items if i.level >= 1]
        assert len(headings) >= 1

    def test_toc_extraction(self, sample_pdf_path):
        result = extract_pdf(sample_pdf_path)
        # Our test PDF has bookmarks
        assert len(result.toc) >= 2

    def test_page_text(self, sample_pdf_path):
        result = extract_pdf(sample_pdf_path)
        # Page 0 (title page) should have "Exploring Society"
        page0_text = " ".join(i.text for i in result.items if i.page_num == 0)
        assert "Exploring" in page0_text or "Society" in page0_text


class TestImageExtractor:
    def test_extract_images(self, sample_pdf_path):
        images = extract_images(sample_pdf_path, "test-book-id")
        # Our test PDF has at least 2 inserted pixmaps
        assert len(images) >= 1
        for img in images:
            assert img.filename
            assert img.page_num >= 0
            assert Path(img.file_path).exists()


class TestStructurer:
    def test_structure_book(self, sample_pdf_path):
        extraction = extract_pdf(sample_pdf_path)
        images = extract_images(sample_pdf_path, "test-struct-id")
        structured = structure_book(
            book_id="test-struct-id",
            title="Exploring Society",
            author="NCERT",
            isbn=None,
            extraction=extraction,
            images=images,
        )
        assert structured.total_chapters >= 1
        assert structured.total_pages == 6
        assert len(structured.chapters) >= 1
        # First chapter should have some sections
        ch0 = structured.chapters[0]
        assert ch0.title

    def test_key_boxes_detected(self, sample_pdf_path):
        extraction = extract_pdf(sample_pdf_path)
        images = extract_images(sample_pdf_path, "test-boxes-id")
        structured = structure_book(
            book_id="test-boxes-id",
            title="Exploring Society",
            author="NCERT",
            isbn=None,
            extraction=extraction,
            images=images,
        )
        # Collect all key boxes across chapters
        all_boxes = []
        for ch in structured.chapters:
            all_boxes.extend(ch.key_boxes)
        # Our PDF has "DID YOU KNOW?", "LET'S EXPLORE", "KEY TERM", "EXERCISE"
        box_types = {b.type for b in all_boxes}
        assert len(all_boxes) >= 1, f"Expected key boxes but found: {all_boxes}"

    def test_json_hierarchy(self, sample_pdf_path):
        extraction = extract_pdf(sample_pdf_path)
        images = extract_images(sample_pdf_path, "test-json-id")
        structured = structure_book(
            book_id="test-json-id",
            title="Exploring Society",
            author="NCERT",
            isbn=None,
            extraction=extraction,
            images=images,
        )
        data = structured.model_dump()
        assert "chapters" in data
        assert "table_of_contents" in data
        for ch in data["chapters"]:
            assert "sections" in ch
            assert "images" in ch
            assert "key_boxes" in ch


class TestChapterEndpoint:
    def test_get_chapter(self, client, sample_pdf_path):
        resp = _upload(client, sample_pdf_path)
        book_id = resp.json()["book_id"]
        status = _wait_for_completion(client, book_id)
        assert status == "completed"

        resp = client.get(f"/library/{book_id}/chapter/0")
        assert resp.status_code == 200
        body = resp.json()
        assert body["chapter"]["chapter_num"] == 0
        assert body["chapter"]["title"]

    def test_get_chapter_not_found(self, client, sample_pdf_path):
        resp = _upload(client, sample_pdf_path)
        book_id = resp.json()["book_id"]
        status = _wait_for_completion(client, book_id)
        assert status == "completed"

        resp = client.get(f"/library/{book_id}/chapter/999")
        assert resp.status_code == 404
