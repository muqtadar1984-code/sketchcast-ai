"""The board-colour calibration relay: a sample of the ink library shipped
to a scratch table, once per boot, only while the variable is set."""

from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from tools import colour_calibration_sample as ccs


class _Query:
    def __init__(self, rows):
        self.rows = rows
        self.not_ = self

    def __getattr__(self, name):
        return lambda *a, **k: self

    def execute(self):
        return type("R", (), {"data": self.rows})()


class FakeSB:
    def __init__(self, board, sketches, blobs):
        self.board, self.sketches, self.blobs = board, sketches, blobs
        self.upserts: list[dict] = []
        self._calls = 0

    def table(self, name):
        sb = self

        class T:
            def select(self, *a, **k):
                sb._calls += 1
                return _Query(sb.board if sb._calls == 1 else sb.sketches)

            def upsert(self, row):
                sb.upserts.append(row)
                return _Query([])
        return T()

    @property
    def storage(self):
        sb = self

        class S:
            def from_(self, bucket):
                class B:
                    def download(self, path):
                        if path not in sb.blobs:
                            raise RuntimeError("missing")
                        return sb.blobs[path]
                return B()
        return S()


def _png():
    buf = io.BytesIO()
    Image.new("RGBA", (30, 20), (0, 0, 0, 0)).save(buf, "PNG")
    return buf.getvalue()


def test_dark_without_the_variable(monkeypatch):
    monkeypatch.delenv(ccs.ENV, raising=False)
    ccs._done = False
    assert ccs.maybe_ship(FakeSB([], [], {})) is None


def test_ships_board_pictures_and_sketches_once_per_boot(monkeypatch):
    monkeypatch.setenv(ccs.ENV, "4")
    ccs._done = False
    blob = _png()
    sb = FakeSB(
        board=[{"asset_key": "plant_cell", "storage_path": "generated/plant/a.png", "description": "A plant cell",
                "vision": {"regions": {"nucleus": [[1, 2, 3, 4]]}}, "group_count": 11},
               {"asset_key": "gone", "storage_path": "generated/gone.png", "description": "", "vision": None}],
        sketches=[{"asset_key": "sk_cone", "storage_path": "generated/sk_cone/b.png", "description": "A cone", "vision": {}}],
        blobs={"generated/plant/a.png": blob, "generated/sk_cone/b.png": blob})
    shipped = ccs.maybe_ship(sb)
    assert shipped == ["plant_cell", "sk_cone"], "the missing blob is skipped, not fatal"
    row = sb.upserts[0]
    assert row["asset_key"] == "plant_cell" and row["width"] == 30 and row["height"] == 20
    assert row["regions"] == {"nucleus": [[1, 2, 3, 4]]}
    assert base64.b64decode(row["png_b64"]) == blob
    assert ccs.maybe_ship(sb) is None, "once per process"


def test_the_worker_runs_it_from_the_reaper_tick():
    from pathlib import Path
    import worker.run as R
    src = Path(R.__file__).read_text(encoding="utf-8")
    i = src.index("from tools.colour_calibration_sample import maybe_ship")
    assert "maybe_ship(sb)" in src[i:i + 200]
