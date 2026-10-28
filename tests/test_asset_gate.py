"""The asset-correctness gate (shared/asset_gate.py): G1 reuse, G2 content,
G3 publish — with a fake model throughout, plus one live Haiku call behind
ASSET_GATE_LIVE=1.

The case every fixture here is cut from: kit eef426bd asked `two_cells` for
a plant cell beside an animal cell; the colour shelf served `two_cells__colour`,
drawn for a bacterium beside a eukaryote; the lesson labelled the bacterium
"Plant cell". Nothing between the key match and the finished video asked what
the picture WAS.
"""

from __future__ import annotations

import inspect
import json
import os
from pathlib import Path

import pytest
from PIL import Image

from shared import asset_gate as ag
from shared import phase_clock, usage_ledger
from shared import visual_library as vl
from spike.scene_engine import raster_assets as ra

PLANT_ANIMAL = (
    "Two cells side by side: on the left a rectangular plant cell with a thick "
    "wall, green chloroplasts and one large central vacuole; on the right a "
    "rounded irregular animal cell with a nucleus and two small centrioles. "
    "Name the layer groups exactly: plant_cell_left, animal_cell_right, "
    "plant_wall, plant_vacuole, animal_centrioles.")
BACTERIUM = (
    "Two cells side by side: on the left a small bacterium with a loose tangle "
    "of DNA in its middle and no inner compartments; on the right a larger "
    "cell with a round nucleus enclosed by a double envelope. Name the layer "
    "groups exactly: bacterium_left, eukaryote_right, nucleus.")
PARTS = ["plant_cell_left", "animal_cell_right", "plant_wall", "plant_vacuole",
         "animal_centrioles"]


class FakeModel:
    """Answers the gate's two questions the way a real model would for the
    fixtures above: a stored bacterium is not a requested plant cell."""
    model = "claude-haiku-5-5"
    backend = "bedrock"

    def __init__(self):
        self.calls: list[tuple[str, str, str | None]] = []
        self.text = None      # override: prompt -> data dict
        self.vision = None    # override: (path, prompt) -> data dict
        self.raise_text = None

    # the two ClaudeClient methods the gate uses
    def analyze(self, prompt, system="", max_tokens=0, retries=0, **_):
        self.calls.append(("text", prompt, None))
        if self.raise_text:
            raise self.raise_text
        data = self.text(prompt) if self.text else self._text_default(prompt)
        return {"data": data, "usage": {"input_tokens": 400, "output_tokens": 30,
                                        "estimated_cost_usd": 0.000055}}

    def analyze_image(self, path, prompt, max_tokens=0, retries=0, **_):
        self.calls.append(("vision", prompt, str(path)))
        data = self.vision(path, prompt) if self.vision else {"ok": True, "parts": {},
                                                               "reason": "as requested"}
        return {"data": data, "usage": {"input_tokens": 1600, "output_tokens": 80,
                                        "estimated_cost_usd": 0.0002}}

    @staticmethod
    def _text_default(prompt):
        req = prompt.split("REQUESTED:", 1)[1].split("\n", 1)[0].lower()
        stored = prompt.split("STORED DRAWING WAS MADE FROM:", 1)[1].split("\n", 1)[0].lower()
        if "bacterium" in stored and "bacterium" not in req:
            return {"verdict": "different",
                    "reason": "the stored drawing shows a bacterium beside a eukaryote, "
                              "not a plant cell beside an animal cell"}
        return {"verdict": "same", "reason": "same subject"}

    def n(self, kind):
        return sum(1 for k, _, _ in self.calls if k == kind)


def _gate(monkeypatch, mode="strict"):
    fake = FakeModel()
    monkeypatch.setenv(ag.GATE_ENV, mode)
    monkeypatch.setattr(ag, "_CLIENT_FACTORY", lambda: fake)
    ag.reset_caches()
    return fake


def _png(path: Path, shade: int = 0) -> Path:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    for x in range(8, 56):
        img.putpixel((x, 20), (shade, shade, shade, 255))
        img.putpixel((x, 40), (shade, shade, shade, 255))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return path


ROW_BACTERIUM = {"id": "row-bact", "asset_key": "two_cells", "canonical_key": "cells_two",
                 "description": BACTERIUM, "concepts": ["cell"], "status": "approved",
                 "content_hash": "h-bact", "asset_format": "png",
                 "storage_path": "generated/cells_two/bact.png"}
ROW_PLANT = {"id": "row-plant", "asset_key": "two_cells", "canonical_key": "cells_two",
             "description": PLANT_ANIMAL, "concepts": ["cell"], "status": "approved",
             "content_hash": "h-plant", "asset_format": "png",
             "storage_path": "generated/cells_two/plant.png"}


# ── switches ─────────────────────────────────────────────────────────────────


class TestTheSwitch:
    def test_warn_is_the_default(self, monkeypatch):
        # 2026-10-28: strict refused a correct lesson on two false positives;
        # warn logs and pays for the verdicts so precision can be measured first.
        monkeypatch.delenv(ag.GATE_ENV, raising=False)
        assert ag.gate_mode() == "warn"

    @pytest.mark.parametrize("raw,mode", [("0", "off"), ("off", "off"), ("false", "off"),
                                          ("warn", "warn"), ("1", "strict"),
                                          ("strict", "strict"), ("nonsense", "warn")])
    def test_the_spellings(self, monkeypatch, raw, mode):
        monkeypatch.setenv(ag.GATE_ENV, raw)
        assert ag.gate_mode() == mode

    def test_a_non_claude_model_is_refused_for_the_default(self, monkeypatch):
        monkeypatch.setenv(ag.MODEL_ENV, "gemini-2.5-flash")
        assert ag.gate_model() == ag.DEFAULT_MODEL
        monkeypatch.setenv(ag.MODEL_ENV, "claude-haiku-4-5")
        assert ag.gate_model() == "claude-haiku-4-5"


# ── G1: the reuse check ─────────────────────────────────────────────────────


class TestG1SameSubject:
    def test_the_two_cells_case_is_different(self, monkeypatch):
        """The regression: the stub answers as the model does, and the verdict
        names what the stored picture actually shows."""
        fake = _gate(monkeypatch)
        v = ag.same_subject(PLANT_ANIMAL, BACTERIUM, PARTS, row=ROW_BACTERIUM, key="two_cells")
        assert v["verdict"] == "different"
        assert "bacterium" in v["reason"]
        assert v["source"] == "model" and fake.n("text") == 1
        prompt = fake.calls[0][1]
        assert ag.REUSE_PROMPT_VERSION in prompt
        assert "plant_cell_left" in prompt and "bacterium_left" in prompt

    def test_an_identical_prompt_costs_nothing(self, monkeypatch):
        fake = _gate(monkeypatch)
        v = ag.same_subject(PLANT_ANIMAL, PLANT_ANIMAL, row=ROW_PLANT)
        assert v["verdict"] == "same" and v["source"] == "identical"
        assert fake.calls == []

    def test_the_layer_tail_does_not_make_two_prompts_differ(self, monkeypatch):
        fake = _gate(monkeypatch)
        trimmed = vl.strip_layer_tail(PLANT_ANIMAL) + " Name the layer groups exactly: x, y."
        v = ag.same_subject(PLANT_ANIMAL, trimmed)
        assert v["source"] == "identical" and fake.calls == []

    def test_no_shared_subject_word_is_different_without_a_call(self, monkeypatch):
        fake = _gate(monkeypatch)
        v = ag.same_subject("A volcano cross-section with magma chamber and vent",
                            {"description": "A red blood cell, biconcave disc"}["description"],
                            row={"asset_key": "red_blood_cell", "concepts": []})
        assert v["verdict"] == "different" and v["source"] == "precheck"
        assert fake.calls == []

    def test_layout_words_alone_do_not_pass_the_precheck(self, monkeypatch):
        fake = _gate(monkeypatch)
        v = ag.same_subject("Two shapes side by side, left large, right small",
                            "Two boxes side by side, left small, right large",
                            row={"asset_key": "boxes", "concepts": []})
        assert v["source"] == "precheck" and fake.calls == []

    def test_a_pair_is_judged_once(self, monkeypatch):
        fake = _gate(monkeypatch)
        ag.same_subject(PLANT_ANIMAL, BACTERIUM, row=ROW_BACTERIUM)
        v = ag.same_subject(PLANT_ANIMAL, BACTERIUM, row=ROW_BACTERIUM)
        assert v["source"] == "cache" and v["verdict"] == "different"
        assert fake.n("text") == 1

    def test_the_cache_is_per_request_prompt(self, monkeypatch):
        fake = _gate(monkeypatch)
        ag.same_subject(PLANT_ANIMAL, BACTERIUM, row=ROW_BACTERIUM)
        ag.same_subject("Two cells: a bacterium and a plant cell. Name the layer groups "
                        "exactly: a.", BACTERIUM, row=ROW_BACTERIUM)
        assert fake.n("text") == 2

    def test_off_bypasses_without_a_call(self, monkeypatch):
        fake = _gate(monkeypatch, "0")
        v = ag.same_subject(PLANT_ANIMAL, BACTERIUM, row=ROW_BACTERIUM)
        assert v == {"verdict": "same", "reason": "asset gate off", "source": "off"}
        assert fake.calls == []

    def test_an_unknown_verdict_is_unsure(self, monkeypatch):
        fake = _gate(monkeypatch)
        fake.text = lambda p: {"verdict": "maybe", "reason": "?"}
        assert ag.same_subject(PLANT_ANIMAL, BACTERIUM, row=ROW_BACTERIUM)["verdict"] == "unsure"

    def test_a_model_failure_is_unsure_not_a_refusal_and_not_cached(self, monkeypatch):
        fake = _gate(monkeypatch)
        fake.raise_text = RuntimeError("bedrock down")
        v = ag.same_subject(PLANT_ANIMAL, BACTERIUM, row=ROW_BACTERIUM)
        assert v["verdict"] == "unsure" and v["source"] == "error"
        fake.raise_text = None
        assert ag.same_subject(PLANT_ANIMAL, BACTERIUM, row=ROW_BACTERIUM)["source"] == "model"

    def test_a_stored_verdict_on_the_row_is_read_before_the_model(self, monkeypatch):
        fake = _gate(monkeypatch)
        phash = ag.prompt_hash(PLANT_ANIMAL)
        monkeypatch.setattr(vl, "read_gate", lambda rid: {
            "reuse": {phash: {"verdict": "different", "reason": "stored", "model": "m"}}})
        v = ag.same_subject(PLANT_ANIMAL, BACTERIUM, row=ROW_BACTERIUM)
        assert v["verdict"] == "different" and v["source"] == "cache" and fake.calls == []

    def test_the_verdict_is_recorded_on_the_row(self, monkeypatch):
        _gate(monkeypatch)
        written = []
        monkeypatch.setattr(vl, "record_gate", lambda *a: written.append(a) or True)
        ag.same_subject(PLANT_ANIMAL, BACTERIUM, row=ROW_BACTERIUM)
        (rid, section, phash, entry), = written
        assert (rid, section, phash) == ("row-bact", "reuse", ag.prompt_hash(PLANT_ANIMAL))
        assert entry["verdict"] == "different" and entry["prompt_version"] == ag.REUSE_PROMPT_VERSION


class TestG1Decisions:
    """verdict -> what the lookup does: same reuses, different skips to the
    next candidate (else generates), unsure reuses and flags needs_g2."""

    def _candidates(self, monkeypatch, *rows):
        def best_match(key, prompt, context=None, *, _ctx=None, guarded=False,
                       asset_format=None, exclude=None):
            for r in rows:
                if not exclude or vl._candidate_identity(r) not in exclude:
                    return r, 0.95, "local"
            return None, 0.0, "none"
        monkeypatch.setattr(vl, "best_match", best_match)
        monkeypatch.setattr(vl, "_sb", lambda: None)

    def test_same_reuses(self, monkeypatch):
        _gate(monkeypatch)
        self._candidates(monkeypatch, ROW_PLANT)
        hit = vl.find("two_cells", PLANT_ANIMAL)
        assert hit["id"] == "row-plant" and hit["gate"]["verdict"] == "same"
        assert hit["gate"]["needs_g2"] is False

    def test_different_skips_to_the_next_candidate(self, monkeypatch):
        fake = _gate(monkeypatch)
        self._candidates(monkeypatch, ROW_BACTERIUM, ROW_PLANT)
        hit = vl.find("two_cells", PLANT_ANIMAL)
        assert hit["id"] == "row-plant"
        trail = ag.g1_trail("two_cells")
        assert [r["id"] for r in trail["refused"]] == ["row-bact"]
        assert fake.n("text") == 1          # the plant row is the request itself

    def test_different_everywhere_means_generate(self, monkeypatch):
        _gate(monkeypatch)
        self._candidates(monkeypatch, ROW_BACTERIUM)
        assert vl.find("two_cells", PLANT_ANIMAL) is None
        assert [r["id"] for r in ag.g1_trail("two_cells")["refused"]] == ["row-bact"]

    def test_unsure_reuses_but_flags_needs_g2(self, monkeypatch):
        fake = _gate(monkeypatch)
        fake.text = lambda p: {"verdict": "unsure", "reason": "cannot tell"}
        self._candidates(monkeypatch, ROW_BACTERIUM)
        hit = vl.find("two_cells", PLANT_ANIMAL)
        assert hit["id"] == "row-bact" and hit["gate"]["needs_g2"] is True

    def test_warn_refuses_a_different_row_too(self, monkeypatch, caplog):
        """G1 acts in warn mode as well: a refusal here never fails a job, the
        worker just draws fresh — so the cheap text check always protects reuse."""
        _gate(monkeypatch, "warn")
        self._candidates(monkeypatch, ROW_BACTERIUM)
        with caplog.at_level("WARNING", logger="shared.asset_gate"):
            hit = vl.find("two_cells", PLANT_ANIMAL)
        assert hit is None
        assert [r["id"] for r in ag.g1_trail("two_cells")["refused"]] == ["row-bact"]
        assert "reuse refused" in caplog.text

    def test_off_leaves_the_hit_untouched(self, monkeypatch):
        fake = _gate(monkeypatch, "0")
        self._candidates(monkeypatch, ROW_BACTERIUM)
        hit = vl.find("two_cells", PLANT_ANIMAL)
        assert hit["id"] == "row-bact" and "gate" not in hit and fake.calls == []

    def test_the_rounds_are_bounded(self, monkeypatch):
        fake = _gate(monkeypatch)
        rows = [{**ROW_BACTERIUM, "id": f"b{i}", "content_hash": f"h{i}"} for i in range(10)]
        self._candidates(monkeypatch, *rows)
        assert vl.find("two_cells", PLANT_ANIMAL) is None
        assert fake.n("text") == ag.CANDIDATE_ROUNDS
        assert len(ag.g1_trail("two_cells")["refused"]) == ag.CANDIDATE_ROUNDS


class TestG1OnTheColourShelf:
    """The production path: find_colour is exact-key, and an exact key was
    exactly the problem."""

    def _shelf(self, monkeypatch, rows):
        class Q:
            def __init__(self):
                self.filters = []

            def select(self, *a, **k): return self
            def eq(self, c, v): self.filters.append((c, v)); return self
            def order(self, *a, **k): return self
            def limit(self, *a, **k): return self
            def execute(self): return type("R", (), {"data": [dict(r) for r in rows]})()

        class SB:
            def table(self, _): return Q()
        monkeypatch.setattr(vl, "_sb", lambda: SB())
        monkeypatch.setenv(vl.COLOUR_SHELF_ENV, "1")

    def test_the_bacterium_row_is_passed_over_for_the_plant_request(self, monkeypatch):
        _gate(monkeypatch)
        bact = {**ROW_BACTERIUM, "asset_key": "two_cells__colour"}
        plant = {**ROW_PLANT, "asset_key": "two_cells__colour"}
        self._shelf(monkeypatch, [bact, plant])
        hit = vl.find_colour("two_cells__colour", prompt=PLANT_ANIMAL)
        assert hit["id"] == "row-plant" and hit["gate"]["verdict"] == "same"

    def test_a_shelf_of_only_wrong_pictures_draws_fresh(self, monkeypatch):
        _gate(monkeypatch)
        self._shelf(monkeypatch, [{**ROW_BACTERIUM, "asset_key": "two_cells__colour"}])
        assert vl.find_colour("two_cells__colour", prompt=PLANT_ANIMAL) is None

    def test_without_a_prompt_the_lookup_is_the_old_exact_one(self, monkeypatch):
        fake = _gate(monkeypatch)
        self._shelf(monkeypatch, [{**ROW_BACTERIUM, "asset_key": "two_cells__colour"}])
        hit = vl.find_colour("two_cells__colour")
        assert hit["id"] == "row-bact" and "gate" not in hit and fake.calls == []

    def test_hydrate_colour_hands_the_request_over(self, monkeypatch, tmp_path):
        _gate(monkeypatch)
        asked = {}
        monkeypatch.setattr(vl, "find_colour", lambda key, **kw: asked.update(kw) or None)
        assert vl.hydrate_colour("two_cells__colour", PLANT_ANIMAL, tmp_path) is None
        assert asked == {"prompt": PLANT_ANIMAL}


class TestRejectedRowsAreNeverCandidates:
    def test_the_local_index_skips_a_rejected_row(self, monkeypatch, tmp_path):
        monkeypatch.setattr(vl, "LIBRARY_DIR", tmp_path)
        vl.register_local({**ROW_PLANT, "status": "rejected", "local_cache_path": "x"})
        assert vl._local_candidates() == []

    def test_best_match_refuses_a_rejected_row_it_is_handed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(vl, "LIBRARY_DIR", tmp_path)
        monkeypatch.setattr(vl, "_sb", lambda: None)
        vl._write_local_index([{**ROW_PLANT, "status": "rejected", "local_cache_path": "x"}])
        best, _, _ = vl.best_match("two_cells", PLANT_ANIMAL)
        assert best is None
        vl._write_local_index([{**ROW_PLANT, "local_cache_path": "x"}])
        best, _, _ = vl.best_match("two_cells", PLANT_ANIMAL)
        assert best["id"] == "row-plant"

    def test_reject_asset_drops_the_local_row_and_demotes_the_remote_one(self, monkeypatch, tmp_path):
        monkeypatch.setattr(vl, "LIBRARY_DIR", tmp_path)
        vl._write_local_index([{**ROW_PLANT, "local_cache_path": "x"}])
        updates = []

        class Q:
            def update(self, payload): updates.append(payload); return self
            def select(self, *a): return self
            def eq(self, *a): return self
            def limit(self, *a): return self
            def execute(self): return type("R", (), {"data": [dict(ROW_PLANT)]})()

        class SB:
            def table(self, _): return Q()
        monkeypatch.setattr(vl, "_sb", lambda: SB())
        assert vl.reject_asset("row-plant", "gate_g2: shows a bacterium") is True
        assert vl._local_candidates() == []
        assert updates[0] == {"status": "rejected"}
        assert updates[1]["vision"]["gate"]["rejected"]["reason"]["reason"].startswith("gate_g2")


# ── G2: the content check ───────────────────────────────────────────────────


def _cache_entry(cache: Path, key: str, md: dict, shade: int = 0) -> Path:
    d = cache / ra.canonical_key(key)
    _png(d / "asset.png", shade)
    (d / "meta.json").write_text(json.dumps(md), encoding="utf-8")
    return d


class TestG2VerifyAsset:
    def test_ok(self, monkeypatch, tmp_path):
        fake = _gate(monkeypatch)
        png = _png(tmp_path / "a.png")
        v = ag.verify_asset(png, PLANT_ANIMAL, PARTS, [("Plant cell", "plant_cell_left")], key="two_cells")
        assert v["ok"] is True and v["source"] == "model"
        prompt = fake.calls[0][1]
        assert ag.CONTENT_PROMPT_VERSION in prompt
        assert '"Plant cell" -> plant_cell_left' in prompt
        assert all(p in prompt for p in PARTS)

    def test_wrong_names_the_picture(self, monkeypatch, tmp_path):
        fake = _gate(monkeypatch)
        fake.vision = lambda path, prompt: {"ok": False, "parts": {"plant_cell_left": "wrong"},
                                            "reason": "a bacterium, not a plant cell"}
        v = ag.verify_asset(_png(tmp_path / "a.png"), PLANT_ANIMAL, PARTS)
        assert v["ok"] is False and "bacterium" in v["reason"]
        assert v["parts"]["plant_cell_left"] == "wrong"
        assert v["parts"]["animal_centrioles"] == "missing"     # unmentioned = missing

    def test_a_wrong_part_fails_even_when_the_model_says_ok_overall(self, monkeypatch, tmp_path):
        fake = _gate(monkeypatch)
        fake.vision = lambda p, q: {"ok": True, "parts": {"plant_wall": "wrong"}, "reason": "x"}
        assert ag.verify_asset(_png(tmp_path / "a.png"), PLANT_ANIMAL, PARTS)["ok"] is False

    def test_missing_parts_alone_do_not_fail(self, monkeypatch, tmp_path):
        fake = _gate(monkeypatch)
        fake.vision = lambda p, q: {"ok": True, "parts": {"plant_wall": "missing"}, "reason": "x"}
        assert ag.verify_asset(_png(tmp_path / "a.png"), PLANT_ANIMAL, PARTS)["ok"] is True

    # ── v2 verdicts (2026-10-28): the SUBJECT decides, nit-picks never fail ──
    def test_v2_the_prompt_says_what_not_to_judge(self):
        prompt = ag._content_prompt(PLANT_ANIMAL, PARTS, [("Plant cell", "plant_cell_left")])
        assert "gate-content-v2" in prompt
        assert "DO NOT fail the picture for" in prompt
        assert '"subject": "same" | "different"' in prompt

    def test_v2_a_different_subject_fails(self, monkeypatch, tmp_path):
        fake = _gate(monkeypatch)
        fake.vision = lambda p, q: {"subject": "different", "parts": {},
                                    "reason": "a bacterium beside a eukaryote"}
        assert ag.verify_asset(_png(tmp_path / "a.png"), PLANT_ANIMAL, PARTS)["ok"] is False

    def test_v2_same_subject_with_missing_parts_and_a_fussy_reason_passes(self, monkeypatch, tmp_path):
        """The live false positive: a plant cell whose vacuole was blue and showed
        no turgid/wilted state must not be refused."""
        fake = _gate(monkeypatch)
        fake.vision = lambda p, q: {"subject": "same",
                                    "parts": {"plant_wall": "ok", "plant_vacuole": "missing"},
                                    "reason": "a plant cell; the interior is blue rather than a "
                                              "central vacuole and nothing depicts a turgid state"}
        v = ag.verify_asset(_png(tmp_path / "a.png"), PLANT_ANIMAL, PARTS)
        assert v["ok"] is True and v["parts"]["plant_vacuole"] == "missing"

    def test_v2_same_subject_but_a_wrong_region_fails(self, monkeypatch, tmp_path):
        fake = _gate(monkeypatch)
        fake.vision = lambda p, q: {"subject": "same", "parts": {"plant_cell_left": "wrong"},
                                    "reason": "x"}
        assert ag.verify_asset(_png(tmp_path / "a.png"), PLANT_ANIMAL, PARTS)["ok"] is False

    def test_v2_layer_group_names_alone_cannot_fail(self, monkeypatch, tmp_path):
        """The other live false positive: a prokaryote/eukaryote picture that
        'matches the requested layout' but whose layer-group names differ."""
        fake = _gate(monkeypatch)
        fake.vision = lambda p, q: {"subject": "same",
                                    "parts": {"prokaryote_cell": "missing", "eukaryote_cell": "missing"},
                                    "reason": "a small cell with a DNA tangle and a larger cell with "
                                              "a nucleus, matching the requested layout"}
        v = ag.verify_asset(_png(tmp_path / "b.png"), "Two cells: a prokaryote and a eukaryote. "
                            "Name the layer groups exactly: prokaryote_cell, eukaryote_cell",
                            ["prokaryote_cell", "eukaryote_cell"])
        assert v["ok"] is True

    def test_a_pair_is_judged_once(self, monkeypatch, tmp_path):
        fake = _gate(monkeypatch)
        png = _png(tmp_path / "a.png")
        ag.verify_asset(png, PLANT_ANIMAL, PARTS)
        v = ag.verify_asset(png, PLANT_ANIMAL, PARTS)
        assert v["source"] == "cache" and fake.n("vision") == 1

    def test_different_bytes_are_a_different_question(self, monkeypatch, tmp_path):
        fake = _gate(monkeypatch)
        ag.verify_asset(_png(tmp_path / "a.png", 0), PLANT_ANIMAL, PARTS)
        ag.verify_asset(_png(tmp_path / "b.png", 40), PLANT_ANIMAL, PARTS)
        assert fake.n("vision") == 2

    def test_the_rows_cached_verdict_is_used_only_for_the_same_bytes(self, monkeypatch, tmp_path):
        fake = _gate(monkeypatch)
        png = _png(tmp_path / "a.png")
        phash = ag.prompt_hash(PLANT_ANIMAL)
        stored = {"ok": False, "parts": {}, "reason": "stored", "content_hash": "other"}
        monkeypatch.setattr(vl, "read_gate", lambda rid: {"content": {phash: dict(stored)}})
        assert ag.verify_asset(png, PLANT_ANIMAL, PARTS, row_id="r")["source"] == "model"
        ag.reset_caches()
        stored["content_hash"] = ag.content_hash_of(png)
        v = ag.verify_asset(png, PLANT_ANIMAL, PARTS, row_id="r")
        assert v["source"] == "cache" and v["ok"] is False and fake.n("vision") == 1

    def test_the_budget_is_respected_and_force_ignores_it(self, monkeypatch, tmp_path):
        fake = _gate(monkeypatch)
        monkeypatch.setenv(ag.BUDGET_ENV, "1")
        ag.verify_asset(_png(tmp_path / "a.png", 0), PLANT_ANIMAL, PARTS)
        v = ag.verify_asset(_png(tmp_path / "b.png", 40), PLANT_ANIMAL, PARTS)
        assert v["ok"] is None and v["source"] == "budget"
        v = ag.verify_asset(_png(tmp_path / "b.png", 40), PLANT_ANIMAL, PARTS, force=True)
        assert v["ok"] is True and fake.n("vision") == 2

    def test_off_and_errors_are_unchecked_not_failures(self, monkeypatch, tmp_path):
        fake = _gate(monkeypatch, "0")
        assert ag.verify_asset(_png(tmp_path / "a.png"), PLANT_ANIMAL)["ok"] is None
        _gate(monkeypatch)
        assert ag.verify_asset(tmp_path / "missing.png", PLANT_ANIMAL)["ok"] is None
        fake2 = _gate(monkeypatch)
        fake2.vision = lambda p, q: (_ for _ in ()).throw(RuntimeError("down"))
        v = ag.verify_asset(_png(tmp_path / "a.png"), PLANT_ANIMAL)
        assert v["ok"] is None and v["source"] == "error"


class TestG2AtTheSeam:
    KEY = "two_cells"

    def _lesson(self, monkeypatch, tmp_path, *, row_id="row-bact", needs_g2=False):
        cache = tmp_path / "cache"
        md = {"key": self.KEY, "prompt": BACTERIUM, "provenance": "visual_library",
              "library_asset_id": row_id, "regions": {}, "annotated_for": []}
        if needs_g2:
            md["gate"] = {"verdict": "unsure", "needs_g2": True}
        _cache_entry(cache, self.KEY, md, shade=0)
        monkeypatch.setattr(ra, "colour_key", lambda k: k)
        rejected = []
        monkeypatch.setattr(vl, "reject_asset", lambda rid, why: rejected.append((rid, why)) or True)
        regen = []

        def regenerate(key, prompt, shade=90):
            regen.append((key, ag.reuse_suspended(key)))
            _cache_entry(cache, key, {"key": key, "prompt": prompt, "provenance": "generated"},
                         shade=shade)
            return object()
        return cache, rejected, regen, regenerate

    @staticmethod
    def _vision_by_shade(verdicts):
        """first bytes (shade 0) -> verdicts[0], the redraw (shade 90) -> verdicts[1]."""
        def vision(path, prompt):
            dark = Image.open(path).convert("RGBA").getpixel((10, 20))[0] == 0
            ok = verdicts[0] if dark else verdicts[1]
            return {"ok": ok, "parts": {}, "reason": "as requested" if ok
                    else "a bacterium beside a eukaryote, not a plant and an animal cell"}
        return vision

    def test_ok_leaves_everything_untouched(self, monkeypatch, tmp_path):
        fake = _gate(monkeypatch)
        cache, rejected, regen, regenerate = self._lesson(monkeypatch, tmp_path)
        out = ag.verify_lesson_assets([(self.KEY, PLANT_ANIMAL)], cache_dir=cache,
                                      regenerate=regenerate)
        assert out["ok"] == [self.KEY] and out["checked"] == 1
        assert rejected == [] and regen == [] and out["refused"] == {}
        assert fake.n("vision") == 1

    def test_wrong_rejects_the_row_redraws_once_and_passes(self, monkeypatch, tmp_path):
        fake = _gate(monkeypatch)
        fake.vision = self._vision_by_shade([False, True])
        cache, rejected, regen, regenerate = self._lesson(monkeypatch, tmp_path)
        out = ag.verify_lesson_assets([(self.KEY, PLANT_ANIMAL)], cache_dir=cache,
                                      regenerate=regenerate)
        assert rejected and rejected[0][0] == "row-bact" and "bacterium" in rejected[0][1]
        assert regen == [(self.KEY, True)], "redrawn once, with library reuse suspended"
        assert out["regenerated"] == [self.KEY] and out["ok"] == [self.KEY]
        assert out["refused"] == {}
        assert fake.n("vision") == 2
        md = json.loads((cache / ra.canonical_key(self.KEY) / "meta.json").read_text())
        assert md["provenance"] == "generated", "the cache holds the redraw"

    def test_wrong_twice_is_refused_with_the_reason(self, monkeypatch, tmp_path):
        fake = _gate(monkeypatch)
        fake.vision = self._vision_by_shade([False, False])
        cache, rejected, regen, regenerate = self._lesson(monkeypatch, tmp_path)
        out = ag.verify_lesson_assets([(self.KEY, PLANT_ANIMAL)], cache_dir=cache,
                                      regenerate=regenerate)
        assert len(regen) == 1, "regenerated ONCE, not until it passes"
        assert out["refused"][self.KEY].startswith("content_wrong: a bacterium")

    def test_a_redraw_that_never_arrives_is_refused_too(self, monkeypatch, tmp_path):
        fake = _gate(monkeypatch)
        fake.vision = self._vision_by_shade([False, True])
        cache, rejected, regen, _ = self._lesson(monkeypatch, tmp_path)
        out = ag.verify_lesson_assets([(self.KEY, PLANT_ANIMAL)], cache_dir=cache,
                                      regenerate=lambda k, p: None)
        assert "regeneration produced no picture" in out["refused"][self.KEY]
        assert not (cache / ra.canonical_key(self.KEY) / "asset.png").exists(), "evicted"

    def test_a_local_picture_without_a_row_is_redrawn_but_nothing_is_rejected(self, monkeypatch, tmp_path):
        fake = _gate(monkeypatch)
        fake.vision = self._vision_by_shade([False, True])
        cache, rejected, regen, regenerate = self._lesson(monkeypatch, tmp_path, row_id=None)
        ag.verify_lesson_assets([(self.KEY, PLANT_ANIMAL)], cache_dir=cache, regenerate=regenerate)
        assert rejected == [] and len(regen) == 1

    def test_warn_only_warns(self, monkeypatch, tmp_path, caplog):
        fake = _gate(monkeypatch, "warn")
        fake.vision = self._vision_by_shade([False, True])
        cache, rejected, regen, regenerate = self._lesson(monkeypatch, tmp_path)
        with caplog.at_level("WARNING", logger="shared.asset_gate"):
            out = ag.verify_lesson_assets([(self.KEY, PLANT_ANIMAL)], cache_dir=cache,
                                          regenerate=regenerate)
        assert rejected == [] and regen == [] and out["refused"] == {}
        assert self.KEY in out["warned"] and "ASSET_GATE=warn" in caplog.text

    def test_off_checks_nothing(self, monkeypatch, tmp_path):
        fake = _gate(monkeypatch, "0")
        cache, *_ = self._lesson(monkeypatch, tmp_path)
        out = ag.verify_lesson_assets([(self.KEY, PLANT_ANIMAL)], cache_dir=cache)
        assert out["checked"] == 0 and fake.calls == []

    def test_avatars_sk_assets_and_pending_keys_are_skipped(self, monkeypatch, tmp_path):
        fake = _gate(monkeypatch)
        cache, *_ = self._lesson(monkeypatch, tmp_path)
        for k in ("avatar_teacher_female", "sk_cone"):
            _cache_entry(cache, k, {"key": k, "prompt": "x", "provenance": "generated"})
        out = ag.verify_lesson_assets(
            [("avatar_teacher_female", "a teacher"), ("sk_cone", "a cone"),
             ("red_blood_cell", "a red blood cell"), (self.KEY, PLANT_ANIMAL)],
            cache_dir=cache, pending_keys=[self.KEY])
        assert out["skipped"] == {"avatar_teacher_female": "avatar", "sk_cone": "sk_asset",
                                  "red_blood_cell": "no_picture", self.KEY: "pending"}
        assert fake.calls == []

    def test_the_budget_binds_but_an_unsure_reuse_is_always_checked(self, monkeypatch, tmp_path):
        fake = _gate(monkeypatch)
        monkeypatch.setenv(ag.BUDGET_ENV, "0")
        cache, *_ = self._lesson(monkeypatch, tmp_path)
        out = ag.verify_lesson_assets([(self.KEY, PLANT_ANIMAL)], cache_dir=cache)
        assert out["skipped"] == {self.KEY: "budget"} and fake.calls == []
        cache, *_ = self._lesson(monkeypatch, tmp_path / "second", needs_g2=True)
        out = ag.verify_lesson_assets([(self.KEY, PLANT_ANIMAL)], cache_dir=cache)
        assert out["checked"] == 1 and fake.n("vision") == 1

    def test_the_labels_the_plan_will_write_reach_the_model(self, monkeypatch, tmp_path):
        fake = _gate(monkeypatch)
        cache, *_ = self._lesson(monkeypatch, tmp_path)
        scene = {"elements": [
            {"id": "pic", "type": "illustration", "asset": self.KEY},
            {"id": "lbl", "type": "text", "text": "Plant cell", "at": [10, 10]},
            {"id": "arr", "type": "arrow", "tail": {"el": "lbl"},
             "head": {"el": "pic", "layer": "plant_cell_left"}}]}
        assert ag.label_layer_pairs([scene]) == {self.KEY: [("Plant cell", "plant_cell_left")]}
        ag.verify_lesson_assets([(self.KEY, PLANT_ANIMAL)], scenes=[scene], cache_dir=cache)
        assert '"Plant cell" -> plant_cell_left' in fake.calls[0][1]


class TestTheComposerWiring:
    def _src(self):
        from agent6_animation import video_composer as vc
        return inspect.getsource(vc.compose_episode_videos)

    def test_the_content_check_runs_at_the_seam_before_the_anchor_repair(self):
        src = self._src()
        assert src.index("verify_lesson_assets(") < src.index("_repair_anchor_regions(slide_segments")
        assert src.index("missing_pictures(_entries, _warm)") < src.index("verify_lesson_assets(")

    def test_the_refusal_is_raised_outside_the_swallowing_except(self):
        src = self._src()
        assert src.index("lesson image warm pass skipped") < src.index("raise ContentRefused(_content_refused")
        assert 'if _content_refused and _gate_mode == "strict":' in src

    def test_the_error_is_an_images_incomplete_that_names_key_and_reason(self):
        from spike.scene_engine.asset_warm import ContentRefused, ImagesIncomplete
        exc = ContentRefused({"two_cells": "content_wrong: a bacterium beside a eukaryote"}, 7)
        assert isinstance(exc, ImagesIncomplete)
        msg = str(exc)
        assert "two_cells" in msg and "bacterium" in msg and "1/7" in msg
        assert "ASSET_GATE=warn" in msg


# ── G3: the publish check ───────────────────────────────────────────────────


class TestG3Publish:
    def _sb(self, monkeypatch):
        inserted, updated = [], []

        class Q:
            def select(self, *a, **k): return self
            def insert(self, payload): inserted.append(payload); return self
            def update(self, payload): updated.append(payload); return self
            def eq(self, *a): return self
            def neq(self, *a): return self
            def or_(self, *a): return self
            def order(self, *a, **k): return self
            def range(self, *a): return self
            def limit(self, *a): return self
            def execute(self): return type("R", (), {"data": []})()

        class Storage:
            def from_(self, _): return self
            def upload(self, *a, **k): return None

        class SB:
            storage = Storage()
            def table(self, _): return Q()
        monkeypatch.setattr(vl, "_sb", lambda: SB())
        return inserted

    def _publish(self, monkeypatch, tmp_path):
        monkeypatch.setattr(vl, "LIBRARY_DIR", tmp_path / "idx")
        inserted = self._sb(monkeypatch)
        png = _png(tmp_path / "two_cells" / "asset.png")
        md = {"provenance": "generated", "baked_text": False,
              "regions": {"plant_cell_left": [[1, 1, 20, 20]]},
              "annotated_for": PARTS}
        assert vl.publish_generated("two_cells", PLANT_ANIMAL, png, md) is True
        (row,) = inserted
        return row

    def test_a_wrong_picture_is_published_rejected_and_never_a_candidate(self, monkeypatch, tmp_path):
        fake = _gate(monkeypatch)
        fake.vision = lambda p, q: {"ok": False, "parts": {}, "reason": "a bacterium"}
        row = self._publish(monkeypatch, tmp_path)
        assert row["status"] == "rejected" and row["quality"] == "gate_content_rejected"
        entry = row["vision"]["gate"]["content"][ag.prompt_hash(PLANT_ANIMAL)]
        assert entry["ok"] is False and entry["content_hash"] == row["content_hash"]
        assert vl._local_candidates() == [], "not registered locally either"

    def test_a_right_picture_is_approved_and_carries_its_verdict(self, monkeypatch, tmp_path):
        fake = _gate(monkeypatch)
        row = self._publish(monkeypatch, tmp_path)
        assert row["status"] == "approved" and row["quality"] == "renderer_validated"
        assert row["vision"]["gate"]["content"][ag.prompt_hash(PLANT_ANIMAL)]["ok"] is True
        assert row["vision"]["regions"] == {"plant_cell_left": [[1.0, 1.0, 20.0, 20.0]]}
        assert len(vl._local_candidates()) == 1
        assert fake.n("vision") == 1

    def test_warn_publishes_approved_but_says_so(self, monkeypatch, tmp_path, caplog):
        fake = _gate(monkeypatch, "warn")
        fake.vision = lambda p, q: {"ok": False, "parts": {}, "reason": "a bacterium"}
        with caplog.at_level("WARNING", logger="shared.visual_library"):
            row = self._publish(monkeypatch, tmp_path)
        assert row["status"] == "approved" and "ASSET_GATE=warn" in caplog.text

    def test_off_publishes_as_before(self, monkeypatch, tmp_path):
        fake = _gate(monkeypatch, "0")
        row = self._publish(monkeypatch, tmp_path)
        assert row["status"] == "approved" and "gate" not in row.get("vision", {})
        assert fake.calls == []

    def test_the_seam_then_asks_nothing_for_a_picture_published_this_lesson(self, monkeypatch, tmp_path):
        """G3 and G2 share the (bytes, prompt) cache: one call per picture."""
        fake = _gate(monkeypatch)
        self._publish(monkeypatch, tmp_path)
        v = ag.verify_asset(tmp_path / "two_cells" / "asset.png", PLANT_ANIMAL, PARTS)
        assert v["source"] == "cache" and fake.n("vision") == 1


# ── the row keeps the gate's verdicts ──────────────────────────────────────


class TestTheRowKeepsItsVerdicts:
    def test_record_vision_carries_the_gate_document_through(self, monkeypatch):
        updates = []
        stored = {"regions": {"a": [[0, 0, 1, 1]]}, "annotated_for": ["a"], "baked_text": False,
                  "w": 64, "h": 64, "gate": {"reuse": {"p": {"verdict": "same"}}}}

        class Q:
            def select(self, *a): return self
            def eq(self, *a): return self
            def limit(self, *a): return self
            def update(self, payload): updates.append(payload); return self
            def execute(self): return type("R", (), {"data": [{"vision": stored, "group_ids": ["a"]}]})()

        class SB:
            def table(self, _): return Q()
        monkeypatch.setattr(vl, "_sb", lambda: SB())
        assert vl.record_vision("r", vl.vision_payload({"b": [[2, 2, 3, 3]]}, ["b"], False, 64, 64))
        assert updates[0]["vision"]["gate"] == stored["gate"]
        assert set(updates[0]["vision"]["regions"]) == {"a", "b"}

    def test_a_gate_only_document_is_not_an_annotation(self):
        assert vl.row_vision({"vision": {"gate": {"content": {}}}}) == {}

    def test_record_gate_is_a_read_modify_write(self, monkeypatch):
        updates = []

        class Q:
            def select(self, *a): return self
            def eq(self, *a): return self
            def limit(self, *a): return self
            def update(self, payload): updates.append(payload); return self
            def execute(self): return type("R", (), {"data": [{"vision": {"regions": {}, "gate": {"reuse": {"x": 1}}}}]})()

        class SB:
            def table(self, _): return Q()
        monkeypatch.setattr(vl, "_sb", lambda: SB())
        assert vl.record_gate("r", "content", "p1", {"ok": True}) is True
        assert updates[0]["vision"] == {"regions": {}, "gate": {"reuse": {"x": 1},
                                                                 "content": {"p1": {"ok": True}}}}


# ── spend ───────────────────────────────────────────────────────────────────


class TestTheLedger:
    def test_a_gate_call_shows_on_the_generations_cost_line(self, monkeypatch):
        _gate(monkeypatch)
        usage_ledger.reset()
        usage_ledger.start("gen-gate")
        tok = phase_clock.bind("gen-gate")
        try:
            ag.same_subject(PLANT_ANIMAL, BACTERIUM, row=ROW_BACTERIUM, key="two_cells")
        finally:
            phase_clock._CURRENT.reset(tok)
        tot = usage_ledger.total("gen-gate")
        assert tot["gate_calls"] == 1
        assert tot["gate_by_check"] == {"reuse": {"calls": 1, "usd": 0.000055}}
        assert tot["gate_usd"] == 0.000055

    def test_the_gate_line_is_a_split_not_a_second_charge(self):
        """The text buckets are fed by ClaudeClient.track_tokens; the
        asset_gate line must not count the same call again."""
        usage_ledger.reset()
        usage_ledger.start("g")
        usage_ledger.add({"model": "claude-haiku-5-5", "backend": "bedrock", "input_tokens": 400,
                          "output_tokens": 30, "estimated_cost_usd": 0.000055}, "g")
        usage_ledger.add({"service": "asset_gate", "check": "content", "model": "claude-haiku-5-5",
                          "backend": "bedrock", "usd": 0.000055}, "g")
        tot = usage_ledger.total("g")
        assert tot["calls"] == 1 and tot["cost_usd"] == 0.000055
        assert tot["gate_calls"] == 1 and tot["gate_usd"] == 0.000055
        assert list(tot["by_model"]) == ["bedrock:claude-haiku-5-5"]


# ── one real call ───────────────────────────────────────────────────────────


@pytest.mark.live
@pytest.mark.skipif(os.getenv("ASSET_GATE_LIVE") != "1",
                    reason="spends on Haiku via Bedrock; ASSET_GATE_LIVE=1 to run")
def test_live_haiku_says_the_bacterium_is_not_a_plant_cell(monkeypatch):
    monkeypatch.setenv(ag.GATE_ENV, "strict")
    monkeypatch.setenv("LLM_BACKEND", "bedrock")
    monkeypatch.setattr(ag, "_CLIENT_FACTORY", ag._default_client)
    ag.reset_caches()
    v = ag.same_subject(PLANT_ANIMAL, BACTERIUM, PARTS,
                        row={"asset_key": "two_cells__colour", "concepts": ["cell"]},
                        key="two_cells")
    print("\nLIVE G1 verdict:", json.dumps(v, indent=2))
    assert v["source"] == "model"
    assert v["verdict"] == "different", v
