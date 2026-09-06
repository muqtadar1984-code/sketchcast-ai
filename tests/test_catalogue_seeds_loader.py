"""The curriculum seed loader: two passes for parents, leaf-only candidates,
idempotent, and a dry run that never touches the client.

Against the fake Supabase in tests/catalogue_fakes.py, which honours the
unique keys the real tables carry and records every write. The scripted
entry point (scripts/seed_curricula.py) is exercised through its ``main``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from catalogue.key import canonical_key
from catalogue.seeds import loader
from catalogue.seeds.loader import (
    SeedError, candidate_for_node, leaf_codes, load_seed, plan_seed, validate_seed,
)
from tests.catalogue_fakes import ExplodingSB, FakeSB

ROOT = Path(__file__).resolve().parents[1]

# Children BEFORE their parents, on purpose — the two-pass case.
SEED = {
    "curriculum": {"code": "test_sci", "name": "Test Science", "kind": "syllabus",
                   "country": "XX", "edition": "2026", "source_url": "https://example.test"},
    "nodes": [
        {"code": "7Bs.01", "grade": "7", "strand": "Biology", "sub_strand": "Structure",
         "title": "Cells", "description": "Know that cells are the basic unit of life.",
         "parent_code": "7/Bio", "sort": 3},
        {"code": "7Bs.02", "grade": "7", "strand": "Biology", "sub_strand": "Structure",
         "title": "Diffusion and osmosis", "description": None, "parent_code": "7/Bio", "sort": 4},
        {"code": "7Cm.01", "grade": "7", "strand": "Chemistry", "sub_strand": None,
         "title": "Acids, Bases & Salts", "description": None, "parent_code": "7/Chem", "sort": 6},
        {"code": "7/Bio", "grade": "7", "strand": "Biology", "sub_strand": None,
         "title": "Biology", "description": None, "parent_code": None, "sort": 1},
        {"code": "7/Chem", "grade": "7", "strand": "Chemistry", "sub_strand": None,
         "title": "Chemistry", "description": None, "parent_code": "7", "sort": 5},
        {"code": "7", "grade": "7", "strand": None, "sub_strand": None,
         "title": "Stage 7", "description": None, "sort": 0},
        {"code": "7Ps.01", "grade": "7", "strand": "Physics", "sub_strand": None,
         "title": "T" * 150, "description": None, "parent_code": None, "sort": 7},
    ],
}


def _write(tmp_path, seed=SEED, name="test_sci.json"):
    p = tmp_path / name
    p.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")
    return p


def _nodes_by_code(sb):
    return {r["code"]: r for r in sb.tables["curriculum_nodes"]}


# ── pure half ───────────────────────────────────────────────────────────


class TestPureHalf:
    def test_leaves_are_the_nodes_nobody_names_as_parent(self):
        assert leaf_codes(SEED["nodes"]) == ["7Bs.01", "7Bs.02", "7Cm.01", "7Ps.01"]

    def test_candidate_payload(self):
        c = candidate_for_node(SEED["nodes"][2])
        assert c == {"source_kind": "curriculum", "raw_title": "Acids, Bases & Salts",
                     "normalized": "acid_base_and_salt"}
        long = candidate_for_node(SEED["nodes"][6])
        assert len(long["raw_title"]) == 120
        assert long["normalized"] == canonical_key(long["raw_title"]) == "t" * 120
        assert candidate_for_node({"title": "الخلية"}) is None

    def test_plan(self):
        assert plan_seed(SEED) == {"curriculum": "test_sci", "nodes": 7, "roots": 3,
                                   "leaves": 4, "candidates": 4}

    @pytest.mark.parametrize("mutate,message", [
        (lambda s: s["nodes"].append({"code": "9X.01", "title": "Dangling", "parent_code": "9/Nope"}),
         "not in the file"),
        (lambda s: s["nodes"].append({"code": "7Bs.01", "title": "Again"}), "appears twice"),
        (lambda s: s["nodes"].append({"title": "No code"}), "has no code"),
        (lambda s: s["nodes"].append({"code": "7Z.01"}), "has no title"),
        (lambda s: s["nodes"].append({"code": "7Z.02", "title": "Self", "parent_code": "7Z.02"}),
         "its own parent"),
        (lambda s: s["curriculum"].pop("code"), "curriculum.code"),
        (lambda s: s["curriculum"].pop("name"), "curriculum.name"),
        (lambda s: s.update(nodes={"a": 1}), "must be a list"),
    ])
    def test_validation_names_the_fault(self, mutate, message):
        seed = json.loads(json.dumps(SEED))
        mutate(seed)
        with pytest.raises(SeedError, match=message):
            validate_seed(seed)

    def test_the_shipped_seed_files_validate(self):
        files = sorted((ROOT / "catalogue" / "seeds").glob("*.json"))
        if not files:
            pytest.skip("no seed files present in this checkout")
        for f in files:
            seed = loader.read_seed(f)
            validate_seed(seed, where=f.name)
            plan = plan_seed(seed)
            assert plan["nodes"] > 0 and plan["leaves"] > 0, f.name
            # Cambridge's leaves are objective SENTENCES, so its file says
            # candidates: none; every other file queues its leaves.
            if loader.candidate_mode(seed) == "none":
                assert plan["candidates"] == 0, f.name
            else:
                assert plan["candidates"] > 0, f.name


# ── the load ────────────────────────────────────────────────────────────


class TestLoad:
    def test_parents_are_resolved_even_when_they_come_after_their_children(self, tmp_path):
        sb = FakeSB()
        out = load_seed(sb, _write(tmp_path))

        cur = sb.tables["curricula"]
        assert len(cur) == 1 and cur[0]["code"] == "test_sci" and cur[0]["kind"] == "syllabus"
        nodes = _nodes_by_code(sb)
        assert set(nodes) == {n["code"] for n in SEED["nodes"]}
        assert all(n["curriculum_id"] == cur[0]["id"] for n in nodes.values())
        assert nodes["7Bs.01"]["parent_id"] == nodes["7/Bio"]["id"]
        assert nodes["7Bs.02"]["parent_id"] == nodes["7/Bio"]["id"]
        assert nodes["7Cm.01"]["parent_id"] == nodes["7/Chem"]["id"]
        assert nodes["7/Chem"]["parent_id"] == nodes["7"]["id"]
        # Roots: the fake has no column until something writes it; a real row
        # has NULL. Either way, no parent.
        assert nodes["7/Bio"].get("parent_id") is None and nodes["7"].get("parent_id") is None
        assert nodes["7Ps.01"].get("parent_id") is None
        assert nodes["7Bs.01"]["sub_strand"] == "Structure" and nodes["7Bs.01"]["sort"] == 3

        assert out["curriculum_created"] == 1
        assert out["nodes_created"] == 7 and out["nodes_updated"] == 0
        assert out["parents_set"] == 4
        assert out["candidates_created"] == 4 and out["candidates_existing"] == 0

    def test_the_first_pass_never_writes_a_parent_id(self, tmp_path):
        """Pass 1 must not carry parent_id at all — a null there would wipe a
        correct parent on every re-run, and the id it needs does not exist
        yet for a parent that comes later in the file."""
        sb = FakeSB()
        load_seed(sb, _write(tmp_path))
        upserts = [e for e in sb.writes("curriculum_nodes") if e[0] == "upsert"]
        assert upserts, "nodes must be upserted"
        for _, _, rows, kw in upserts:
            assert kw.get("on_conflict") == "curriculum_id,code"
            assert all("parent_id" not in r for r in rows)
        # parent_id arrives only through pass-2 UPDATEs, one per node that needs one.
        updates = [e for e in sb.writes("curriculum_nodes") if e[0] == "update"]
        assert len(updates) == 4 and all(set(e[2]) == {"parent_id"} for e in updates)

    def test_leaf_only_candidates(self, tmp_path):
        sb = FakeSB()
        load_seed(sb, _write(tmp_path))
        nodes = _nodes_by_code(sb)
        cands = sb.tables["topic_candidates"]
        assert {c["node_id"] for c in cands} == {nodes[c]["id"] for c in ["7Bs.01", "7Bs.02", "7Cm.01", "7Ps.01"]}
        for c in cands:
            assert c["source_kind"] == "curriculum"
            assert c.get("book_id") is None
            assert c["normalized"] == canonical_key(c["raw_title"])
            assert len(c["raw_title"]) <= 120
        by_key = {c["normalized"]: c for c in cands}
        assert by_key["cell"]["raw_title"] == "Cells"
        assert by_key["acid_base_and_salt"]["node_id"] == nodes["7Cm.01"]["id"]
        # A node's DESCRIPTION is never a candidate — only its title.
        assert not any("basic unit" in c["raw_title"] for c in cands)

    def test_a_second_run_changes_nothing(self, tmp_path):
        """Reads, compares, writes nothing — and SAYS so. The loader used to
        re-upsert every node with identical values and report nodes_updated: 7,
        which read as seven changed rows to anyone checking the run."""
        sb = FakeSB()
        path = _write(tmp_path)
        load_seed(sb, path)
        before = sb.snapshot()
        n_log = len(sb.log)

        out = load_seed(sb, path)

        assert sb.tables == before, "the second run must leave every table as it found it"
        assert out["curriculum_created"] == 0
        assert out["nodes_created"] == 0 and out["nodes_updated"] == 0
        assert out["parents_set"] == 0
        assert out["candidates_created"] == 0 and out["candidates_existing"] == 4
        assert sb.log[n_log:] == [], "a second run of an unchanged file writes nothing: no upsert, insert or update"

    def test_nodes_updated_counts_only_the_rows_that_changed(self, tmp_path):
        sb = FakeSB()
        load_seed(sb, _write(tmp_path))
        seed = json.loads(json.dumps(SEED))
        seed["nodes"][1]["title"] = "Diffusion, osmosis and active transport"
        seed["nodes"][4]["sort"] = 9
        n_log = len(sb.log)
        out = load_seed(sb, _write(tmp_path, seed))
        assert out["nodes_created"] == 0 and out["nodes_updated"] == 2
        upserted = [r["code"] for e in sb.log[n_log:] if e[0] == "upsert" and e[1] == "curriculum_nodes"
                    for r in e[2]]
        assert sorted(upserted) == ["7/Chem", "7Bs.02"], "only the two changed rows are upserted"
        assert not [e for e in sb.log[n_log:] if e[1] == "curricula"], "the unchanged curriculum row is not"
        nodes = _nodes_by_code(sb)
        assert nodes["7Bs.02"]["title"] == "Diffusion, osmosis and active transport"
        assert nodes["7/Chem"]["sort"] == 9

    def test_a_changed_curriculum_field_is_written_once(self, tmp_path):
        sb = FakeSB()
        load_seed(sb, _write(tmp_path))
        seed = json.loads(json.dumps(SEED))
        seed["curriculum"]["edition"] = "2027"
        n_log = len(sb.log)
        out = load_seed(sb, _write(tmp_path, seed))
        assert out["curriculum_created"] == 0 and out["nodes_updated"] == 0
        assert [e[1] for e in sb.log[n_log:] if e[0] == "upsert"] == ["curricula"]
        assert sb.tables["curricula"][0]["edition"] == "2027"

    def test_an_edited_title_updates_the_node_and_opens_a_new_candidate(self, tmp_path):
        """The old candidate stays (a curator may have acted on it); the new
        spelling gets its own row under the same node."""
        sb = FakeSB()
        load_seed(sb, _write(tmp_path))
        seed = json.loads(json.dumps(SEED))
        seed["nodes"][0]["title"] = "The Cell"
        seed["nodes"][0]["parent_code"] = "7/Chem"  # moved, too
        out = load_seed(sb, _write(tmp_path, seed))
        nodes = _nodes_by_code(sb)
        assert nodes["7Bs.01"]["title"] == "The Cell"
        assert nodes["7Bs.01"]["parent_id"] == nodes["7/Chem"]["id"]
        assert out["parents_set"] == 1 and out["nodes_updated"] == 1
        # "The Cell" keys to "cell" — the same key the old title carried under
        # this node, so no new candidate row is needed.
        assert out["candidates_created"] == 0
        seed["nodes"][0]["title"] = "Cell structure"
        out = load_seed(sb, _write(tmp_path, seed))
        assert out["candidates_created"] == 1
        assert sorted(c["raw_title"] for c in sb.tables["topic_candidates"]
                      if c["node_id"] == nodes["7Bs.01"]["id"]) == ["Cell structure", "Cells"]

    def test_the_curriculum_row_carries_only_table_columns(self, tmp_path):
        """A seed may carry bookkeeping the table has no column for (the
        shipped files have "partial"); PostgREST would 400 on it."""
        seed = json.loads(json.dumps(SEED))
        seed["curriculum"]["partial"] = True
        sb = FakeSB()
        load_seed(sb, _write(tmp_path, seed))
        row = sb.tables["curricula"][0]
        assert "partial" not in row
        assert set(row) <= {"id", "code", "name", "kind", "country", "edition", "source_url"}
        for n in sb.tables["curriculum_nodes"]:
            assert set(n) <= {"id", "curriculum_id", "code", "grade", "strand", "sub_strand",
                              "title", "description", "parent_id", "sort"}

    def test_kind_defaults_to_syllabus(self, tmp_path):
        seed = json.loads(json.dumps(SEED))
        seed["curriculum"].pop("kind")
        sb = FakeSB()
        load_seed(sb, _write(tmp_path, seed))
        assert sb.tables["curricula"][0]["kind"] == "syllabus"

    def test_a_malformed_file_writes_nothing(self, tmp_path):
        seed = json.loads(json.dumps(SEED))
        seed["nodes"].append({"code": "9X.01", "title": "Dangling", "parent_code": "9/Nope"})
        sb = FakeSB()
        with pytest.raises(SeedError):
            load_seed(sb, _write(tmp_path, seed))
        assert sb.log == [] and sb.calls == []


class TestDryRun:
    def test_dry_run_reports_the_plan_and_never_touches_the_client(self, tmp_path):
        out = load_seed(ExplodingSB(), _write(tmp_path), dry_run=True)
        assert out["dry_run"] is True
        assert out["nodes"] == 7 and out["leaves"] == 4 and out["candidates"] == 4
        assert "nodes_created" not in out
        # And with no client at all.
        assert load_seed(None, _write(tmp_path), dry_run=True)["curriculum"] == "test_sci"

    def test_a_real_run_without_a_client_refuses(self, tmp_path):
        with pytest.raises(RuntimeError, match="client"):
            load_seed(None, _write(tmp_path))


# ── the script ──────────────────────────────────────────────────────────


def _script():
    spec = importlib.util.spec_from_file_location("seed_curricula", ROOT / "scripts" / "seed_curricula.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.pop("seed_curricula", None)
    spec.loader.exec_module(mod)
    return mod


class TestScript:
    def test_dry_run_prints_counts_builds_no_client_and_exits_0(self, tmp_path, capsys):
        script = _script()
        path = _write(tmp_path)

        def never():
            raise AssertionError("--dry-run must not build a client")

        assert script.main(["--file", str(path), "--dry-run"], make_client=never) == 0
        out = capsys.readouterr().out.strip().splitlines()
        assert len(out) == 1
        printed = json.loads(out[0])
        assert printed["dry_run"] is True and printed["candidates"] == 4

    def test_a_real_run_uses_the_client_and_writes(self, tmp_path, capsys):
        script = _script()
        sb = FakeSB()
        assert script.main(["--file", str(_write(tmp_path))], make_client=lambda: sb) == 0
        assert len(sb.tables["curriculum_nodes"]) == 7
        assert json.loads(capsys.readouterr().out)["candidates_created"] == 4

    def test_a_bad_file_exits_1_and_names_it(self, tmp_path, capsys):
        script = _script()
        good = _write(tmp_path)
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"curriculum": {"code": "x", "name": "X"},
                                   "nodes": [{"code": "a", "title": "A", "parent_code": "zz"}]}),
                       encoding="utf-8")
        sb = FakeSB()
        assert script.main(["--file", str(good), "--file", str(bad)], make_client=lambda: sb) == 1
        err = capsys.readouterr().err
        assert "bad.json" in err and "not in the file" in err
        assert len(sb.tables["curricula"]) == 1, "the good file still loaded"

    def test_missing_credentials_exit_1(self, tmp_path, capsys):
        script = _script()

        def no_env():
            raise KeyError("SUPABASE_URL")

        assert script.main(["--file", str(_write(tmp_path))], make_client=no_env) == 1
        assert "SUPABASE_URL" in capsys.readouterr().err

    def test_all_reads_the_seeds_directory(self, tmp_path, monkeypatch, capsys):
        script = _script()
        monkeypatch.setattr(script, "SEEDS_DIR", tmp_path)
        _write(tmp_path, name="a.json")
        seed_b = json.loads(json.dumps(SEED))
        seed_b["curriculum"]["code"] = "test_sci_b"
        _write(tmp_path, seed_b, name="b.json")
        (tmp_path / "notes.txt").write_text("ignored", encoding="utf-8")
        sb = FakeSB()
        assert script.main(["--all"], make_client=lambda: sb) == 0
        assert sorted(c["code"] for c in sb.tables["curricula"]) == ["test_sci", "test_sci_b"]
        assert len(capsys.readouterr().out.strip().splitlines()) == 2

    def test_all_with_an_empty_directory_exits_1(self, tmp_path, monkeypatch, capsys):
        script = _script()
        monkeypatch.setattr(script, "SEEDS_DIR", tmp_path)
        assert script.main(["--all", "--dry-run"]) == 1
        assert "no seed files" in capsys.readouterr().err
