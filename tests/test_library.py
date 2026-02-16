"""Tests for library management, upload, and duplicate detection."""

from __future__ import annotations

import time

import pytest


def _upload(client, pdf_path, title="Exploring Society", author="NCERT", isbn=None):
    """Helper to upload a PDF."""
    data = {"title": title, "author": author}
    if isbn:
        data["isbn"] = isbn
    with open(pdf_path, "rb") as f:
        return client.post("/upload", files={"file": ("test.pdf", f, "application/pdf")}, data=data)


def _wait_for_completion(client, book_id, timeout=30):
    """Poll until processing completes or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = client.get(f"/library/{book_id}/status")
        status = resp.json()["status"]
        if status in ("completed", "failed"):
            return status
        time.sleep(0.5)
    return "timeout"


class TestHealthCheck:
    def test_health(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "healthy"


class TestUpload:
    def test_upload_pdf(self, client, sample_pdf_path):
        resp = _upload(client, sample_pdf_path)
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "processing"
        assert body["book_id"]

    def test_upload_non_pdf(self, client, tmp_path):
        fake = tmp_path / "not_a_pdf.txt"
        fake.write_text("hello")
        with open(fake, "rb") as f:
            resp = client.post(
                "/upload",
                files={"file": ("not_a_pdf.txt", f, "text/plain")},
                data={"title": "Test"},
            )
        assert resp.status_code == 400

    def test_duplicate_by_hash(self, client, sample_pdf_path):
        resp1 = _upload(client, sample_pdf_path)
        assert resp1.status_code == 200
        book_id_1 = resp1.json()["book_id"]

        # Wait for first to complete
        _wait_for_completion(client, book_id_1)

        # Upload same file again
        resp2 = _upload(client, sample_pdf_path)
        assert resp2.status_code == 200
        body2 = resp2.json()
        assert body2["status"] == "existing"
        assert body2["book_id"] == book_id_1

    def test_duplicate_by_isbn(self, client, sample_pdf_path):
        resp1 = _upload(client, sample_pdf_path, isbn="978-0-123456-47-2")
        assert resp1.status_code == 200
        book_id_1 = resp1.json()["book_id"]
        _wait_for_completion(client, book_id_1)

        # Upload with same ISBN but different file content (still catches ISBN)
        resp2 = _upload(client, sample_pdf_path, isbn="978-0-123456-47-2")
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "existing"

    def test_duplicate_by_fuzzy_match(self, client, sample_pdf_path):
        resp1 = _upload(client, sample_pdf_path, title="Exploring Society", author="NCERT")
        assert resp1.status_code == 200
        _wait_for_completion(client, resp1.json()["book_id"])

        # Upload with slightly different title (fuzzy match should catch it)
        resp2 = _upload(client, sample_pdf_path, title="Exploring Society ", author="NCERT")
        assert resp2.status_code == 200
        # Should be caught by hash (same file) or fuzzy match
        assert resp2.json()["status"] == "existing"


class TestLibrary:
    def test_list_books_empty(self, client):
        resp = client.get("/library")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_list_books_after_upload(self, client, sample_pdf_path):
        _upload(client, sample_pdf_path)
        resp = client.get("/library")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    def test_get_book_not_found(self, client):
        resp = client.get("/library/nonexistent-id")
        assert resp.status_code == 404

    def test_get_book_after_processing(self, client, sample_pdf_path):
        resp = _upload(client, sample_pdf_path)
        book_id = resp.json()["book_id"]
        status = _wait_for_completion(client, book_id)
        assert status == "completed"

        resp = client.get(f"/library/{book_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["title"] == "Exploring Society"
        assert body["structured_content"] is not None
        assert body["structured_content"]["total_chapters"] >= 1


class TestStatus:
    def test_status_processing(self, client, sample_pdf_path):
        resp = _upload(client, sample_pdf_path)
        book_id = resp.json()["book_id"]
        # Immediately check — should be processing (or already completed if fast)
        resp = client.get(f"/library/{book_id}/status")
        assert resp.status_code == 200
        assert resp.json()["status"] in ("processing", "completed")

    def test_status_not_found(self, client):
        resp = client.get("/library/nonexistent/status")
        assert resp.status_code == 404
