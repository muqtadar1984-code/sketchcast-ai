"""An invalid branding template must degrade to the default style, never fail a job.

Regression test for the 2026-08-10 production failure: a teacher uploaded a JPEG
as their .docx letterhead (stored as `branding/template.docx`, mimetype
image/jpeg, 187 KB). load_branding returned its path, docgen handed it to
python-docx, and `PackageNotFoundError: Package not found at
'/tmp/.../template.docx'` failed 10 generations across a full kit.

Branding is documented as best-effort — "any field may come back None and the
generators fall back to the default style" — so a bad template must yield None.
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from worker.branding import _is_ooxml, _download


# ── _is_ooxml ────────────────────────────────────────────────────────────────

def _write_ooxml(p: Path) -> Path:
    """A minimal but genuine OOXML package."""
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<document/>")
    return p


def test_real_ooxml_accepted(tmp_path):
    assert _is_ooxml(_write_ooxml(tmp_path / "template.docx")) is True


def test_jpeg_named_docx_rejected(tmp_path):
    """The exact production case: JPEG bytes stored under a .docx name."""
    p = tmp_path / "template.docx"
    p.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"\x00" * 4096)
    assert _is_ooxml(p) is False


def test_empty_file_rejected(tmp_path):
    p = tmp_path / "template.docx"
    p.write_bytes(b"")
    assert _is_ooxml(p) is False


def test_zip_without_content_types_rejected(tmp_path):
    """A plain zip is not an OOXML package — python-docx would still fail."""
    p = tmp_path / "template.docx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("hello.txt", "not office")
    assert _is_ooxml(p) is False


def test_missing_file_rejected(tmp_path):
    assert _is_ooxml(tmp_path / "nope.docx") is False


def test_truncated_ooxml_rejected(tmp_path):
    """Half a download is not a template."""
    good = _write_ooxml(tmp_path / "good.docx")
    p = tmp_path / "template.docx"
    p.write_bytes(good.read_bytes()[: len(good.read_bytes()) // 2])
    assert _is_ooxml(p) is False


# ── _download ────────────────────────────────────────────────────────────────

class _FakeStorage:
    def __init__(self, payload: bytes):
        self._payload = payload

    def from_(self, _bucket):
        return self

    def download(self, _path):
        return self._payload


class _FakeSB:
    def __init__(self, payload: bytes):
        self.storage = _FakeStorage(payload)


def test_download_returns_none_for_jpeg(tmp_path):
    """The whole point: a bad template becomes None, so docgen uses its default."""
    sb = _FakeSB(b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 1024)
    assert _download(sb, "owner/branding/template.docx", tmp_path / "template.docx") is None


def test_download_returns_path_for_valid_docx(tmp_path):
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
    sb = _FakeSB(payload.getvalue())
    dest = tmp_path / "template.docx"
    assert _download(sb, "owner/branding/template.docx", dest) == dest


def test_download_returns_none_when_storage_raises(tmp_path):
    class _Boom(_FakeSB):
        def __init__(self):
            super().__init__(b"")
            self.storage.download = self._raise

        @staticmethod
        def _raise(_path):
            raise RuntimeError("404 not found")

    assert _download(_Boom(), "missing.docx", tmp_path / "template.docx") is None
