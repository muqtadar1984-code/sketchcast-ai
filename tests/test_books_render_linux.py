"""Rendering on the books runner (Linux, R1 S3): the browser, the pictures and the font trial."""
import json

import pytest

from publishing.manuscript import render, theme
from publishing.runner import stages


def test_browser_prefers_publish_browser_and_adds_no_sandbox_off_windows(monkeypatch):
    monkeypatch.setenv("PUBLISH_BROWSER", "/usr/bin/chromium")
    import os
    monkeypatch.setattr(os, "name", "posix")
    assert render.browser() == ["/usr/bin/chromium", "--no-sandbox"]


def test_browser_finds_chromium_on_the_path(monkeypatch):
    import os
    import shutil
    monkeypatch.delenv("PUBLISH_BROWSER", raising=False)
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/chromium" if n == "chromium" else None)
    assert render.browser() == ["/usr/bin/chromium", "--no-sandbox"]


def test_browser_refuses_when_none(monkeypatch):
    import os
    import shutil
    monkeypatch.delenv("PUBLISH_BROWSER", raising=False)
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(shutil, "which", lambda n: None)
    with pytest.raises(RuntimeError, match="no headless browser"):
        render.browser()


@pytest.mark.parametrize("stored", [
    r"..\study-guide-sprint1\book-assets\zz_runner_only_picture.png",
    r"C:\Users\Arieb\OneDrive\Desktop\Arieb folder\Edtech\study-guide-sprint1\book-assets\zz_runner_only_picture.png",
    "/somewhere/else/book-assets/zz_runner_only_picture.png",
])
def test_a_stored_picture_path_is_found_by_name_in_the_assets_folder(tmp_path, monkeypatch, stored):
    (tmp_path / "zz_runner_only_picture.png").write_bytes(b"png")
    monkeypatch.setenv("PUBLISH_ASSETS", str(tmp_path))
    assert render.local_asset(stored) == tmp_path / "zz_runner_only_picture.png"
    assert render.asset_bytes(None, "", {"local_path": stored}) == b"png"


def test_an_existing_path_is_used_as_is(tmp_path, monkeypatch):
    f = tmp_path / "x.png"
    f.write_bytes(b"1")
    monkeypatch.setenv("PUBLISH_ASSETS", str(tmp_path / "elsewhere"))
    assert render.local_asset(str(f)) == f


def test_the_opener_art_is_found_without_a_database(tmp_path, monkeypatch):
    from PIL import Image
    Image.new("RGB", (64, 48), (250, 246, 236)).save(tmp_path / "hero_ch03.png")
    monkeypatch.setenv("PUBLISH_ASSETS", str(tmp_path))
    monkeypatch.delenv("PUBDB_URL", raising=False)          # localdb.connect() raises without it
    assert render.hero_image("any", 3).startswith("data:image/png;base64,")
    assert render.hero_image("any", 4) is None


def test_a_font_trial_overrides_the_theme_without_changing_it(monkeypatch):
    base = theme.for_work("cells-structure-function-organisation")
    monkeypatch.setenv("PUBLISH_HEADING_FONT", "'Literata', Georgia, serif")
    t = theme.for_work("cells-structure-function-organisation")
    assert t["heading_font"] == "'Literata', Georgia, serif" and t["body_font"] == base["body_font"]
    monkeypatch.delenv("PUBLISH_HEADING_FONT")
    assert theme.for_work("cells-structure-function-organisation")["heading_font"] == base["heading_font"]


def test_the_render_proof_stage(tmp_path):
    stage, params = stages.validate("render_proof", {})
    assert params == {"chapter": 1} and stage.needs_assets and not stage.uses_models
    assert stage.argv("b", params, "runs/run-5")[-3:] == ["b", "runs/run-5", "1"]
    with pytest.raises(ValueError):
        stages.validate("render_proof", {"chapter": 0})
    (tmp_path / "proof.json").write_text(json.dumps({"chapter": 1, "renders": [
        {"font": "Literata", "folder": "literata", "pages": 9, "hard_defects": 0, "embedded_fonts": ["x"]}]}))
    assert stage.collect(tmp_path) == (0.0, [], {"chapter": 1, "renders": [{"font": "Literata", "pages": 9, "hard_defects": 0}]})


def test_source_serif_4_follows_every_windows_only_serif():
    """Founder 2026-10-29 (D3): Source Serif 4 replaces Sitka (and Georgia) on the books runner; this PC keeps
    rendering the approved Biology in Sitka, because Sitka stays first in the stack."""
    import json
    from publishing.manuscript.theme import THEMES
    cfg = json.loads(THEMES.read_text(encoding="utf-8"))
    life = cfg["themes"]["life"]
    assert life["heading_font"].startswith("'Sitka Heading'")
    for t in cfg["themes"].values():
        stacks = [t["heading_font"], ((t.get("page") or {}).get("folio") or {}).get("font", "")]
        for stack in stacks:
            if any(f in stack for f in ("Sitka", "Georgia")):
                assert "'Source Serif 4'" in stack, stack
                first_open = stack.index("'Source Serif 4'")
                assert all(stack.index(f) < first_open for f in ("Sitka",) if f in stack), stack


def test_overlap_check_ignores_slivers_but_not_collisions(tmp_path):
    import fitz
    from publishing.manuscript.layout_check import diagnose
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((100, 100), "In the expression 5x", fontsize=11)
    page.insert_text((214, 100), "- 3x + 7", fontsize=11)          # a neighbour's box nudged ~1-2 pt in
    p = tmp_path / "sliver.pdf"
    doc.save(p)
    assert not [h for h in diagnose(str(p))["hard"] if h["check"] == "text overlaps text"]
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((100, 200), "Left label text", fontsize=11)
    page.insert_text((120, 202), "Right label text", fontsize=11)   # drawn over each other
    p = tmp_path / "collision.pdf"
    doc.save(p)
    assert [h for h in diagnose(str(p))["hard"] if h["check"] == "text overlaps text"]


def test_compile_runs_the_layout_search_for_a_draft_without_a_saved_choice(monkeypatch, tmp_path):
    from publishing.manuscript import book, render
    import publishing.manuscript.layout_check as lc
    printed = []
    monkeypatch.setattr(render, "_figures", lambda *a, **k: {})
    monkeypatch.setattr(render, "annotations_for", lambda slug: {})
    monkeypatch.setattr(render, "chapter_html", lambda cand, figs, edition, first: cand.get("_density") or "authored")
    monkeypatch.setattr(render, "_print", lambda page, pdf: (printed.append(page.read_text()), pdf)[1])
    monkeypatch.setattr(render, "_underlay", lambda *a: None)
    scores = {"authored": {"hard": [], "pages": [{"unused_share": 0.9}, {"unused_share": 0.1}], "page_count": 2},
              "snug": {"hard": [], "pages": [{"unused_share": 0.1}], "page_count": 1},
              "tight": {"hard": [1], "pages": [{"unused_share": 0.0}], "page_count": 1}}
    monkeypatch.setattr(lc, "diagnose", lambda path: scores[printed[-1]])
    doc = {"chapter": {"number": 1}, "work": {"slug": "lin"}, "sections": []}
    book._chapter_pdf(doc, {}, tmp_path / "e.env", tmp_path, 1, "digital")
    assert printed[-1] == "snug"              # the fullest layout without defects is the one printed
