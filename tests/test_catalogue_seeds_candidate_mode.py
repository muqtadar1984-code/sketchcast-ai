"""``curriculum.candidates`` — the seed file decides whether its leaves are
queued as topic candidates.

Cambridge 0893's leaves are learning-objective sentences ("Understand that all
organisms are made of cells…"); 200 of them in the candidates queue would bury
the real names, and those nodes are covered from the portal's Curricula screen
instead. Its file says "none". CBSE's leaves are chapter and topic names; its
file keeps the default "leaves". Against the fake Supabase in
tests/catalogue_fakes.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from catalogue.seeds import loader
from catalogue.seeds.loader import SeedError, candidate_mode, load_seed, plan_seed
from tests.catalogue_fakes import FakeSB

ROOT = Path(__file__).resolve().parents[1]

SEED = {
    "curriculum": {"code": "mode_sci", "name": "Mode Science", "kind": "syllabus"},
    "nodes": [
        {"code": "7/Bio", "grade": "7", "title": "Biology", "parent_code": None, "sort": 1},
        {"code": "7Bs.01", "grade": "7", "title": "Understand that all organisms are made of cells.",
         "parent_code": "7/Bio", "sort": 2},
        {"code": "7Bs.02", "grade": "7", "title": "Cells", "parent_code": "7/Bio", "sort": 3},
    ],
}


def _seed(mode):
    s = json.loads(json.dumps(SEED))
    if mode is not None:
        s["curriculum"]["candidates"] = mode
    return s


def _write(tmp_path, seed):
    p = tmp_path / "mode_sci.json"
    p.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")
    return p


class TestMode:
    def test_default_is_leaves(self):
        assert candidate_mode(_seed(None)) == "leaves"
        assert plan_seed(_seed(None))["candidates"] == 2

    def test_none_plans_no_candidates_but_still_loads_every_node(self, tmp_path):
        assert candidate_mode(_seed("none")) == "none"
        assert plan_seed(_seed("none"))["candidates"] == 0
        sb = FakeSB()
        out = load_seed(sb, _write(tmp_path, _seed("none")))
        assert out["nodes_created"] == 3 and out["parents_set"] == 2
        assert out["candidates_created"] == 0 and out["candidates_existing"] == 0
        assert sb.tables.get("topic_candidates", []) == []
        # The switch is a file instruction, never a column.
        assert "candidates" not in sb.tables["curricula"][0]

    def test_leaves_queues_each_leaf(self, tmp_path):
        sb = FakeSB()
        out = load_seed(sb, _write(tmp_path, _seed("leaves")))
        assert out["candidates_created"] == 2
        titles = sorted(c["raw_title"] for c in sb.tables["topic_candidates"])
        assert titles == ["Cells", "Understand that all organisms are made of cells."]

    def test_unknown_mode_is_refused(self):
        with pytest.raises(SeedError, match="candidates must be one of"):
            candidate_mode(_seed("everything"))
        assert candidate_mode(_seed("  LEAVES ")) == "leaves"


class TestShippedFiles:
    def test_cambridge_says_none_and_cbse_queues_names(self):
        seeds_dir = ROOT / "catalogue" / "seeds"
        cam = seeds_dir / "cambridge_ls_science_0893.json"
        cbse = seeds_dir / "cbse_science_086.json"
        if not (cam.exists() and cbse.exists()):
            pytest.skip("seed files not present in this checkout")
        cam_seed = loader.read_seed(cam)
        cbse_seed = loader.read_seed(cbse)
        assert candidate_mode(cam_seed) == "none"
        assert plan_seed(cam_seed)["candidates"] == 0
        assert plan_seed(cam_seed)["leaves"] == 200
        assert candidate_mode(cbse_seed) == "leaves"
        assert plan_seed(cbse_seed)["candidates"] == plan_seed(cbse_seed)["leaves"] > 0
