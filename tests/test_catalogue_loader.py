"""An approved article becomes the chapter dict the pipeline reads
(catalogue.loader, Phase 3). The contract is agent1_ingestion.models
.ChapterContent, and it is pinned by VALIDATING the result through that
pydantic model — a drift in the article row or the chapter shape fails here,
not three agents later. Everything CALLS things; no model, no network."""

from __future__ import annotations

import pytest

from agent1_ingestion.models import ChapterContent
from catalogue.loader import (SYNTHETIC_CHAPTER_NUM, article_to_chapter, key_boxes, plain_text,
                              rendered_captions, section_dicts, section_ids_by_heading)

ARTICLE = {
    "id": "art-1", "topic_id": "t-cell", "title": "Cells: the basic unit of life", "status": "approved",
    "language": "en", "version": 1, "depth_node_id": "n-cb1",
    "objectives": [{"id": "o1", "text": "State that all organisms are made of cells."}],
    "sections": [
        {"id": "s1", "heading": "What a cell is",
         "body_md": "Every living organism is built from **cells**. A cell is the *smallest* unit of life.",
         "figure_keys": [], "covers": ["7Bs.01"]},
        {"id": "s2", "heading": "Structures common to all cells",
         "body_md": "## Parts\n\n- The **cell membrane** controls entry.\n- The `nucleus` holds the DNA.",
         "figure_keys": ["animal_cell", "plant_cell"], "covers": ["7Bs.02"]},
        {"id": "s3", "heading": "", "body_md": "Plant cells add a wall, chloroplasts and a vacuole.",
         "figure_keys": ["plant_cell"], "covers": []},
    ],
    "glossary": [{"term": "cell", "definition": "The smallest unit of a living thing."},
                 {"term": "organelle", "definition": "A structure inside a cell with a **job**."},
                 {"term": "", "definition": "dropped: no term"}],
    "misconceptions": [
        {"id": "m1", "misconception": "Plants are not made of cells.", "correction": "Every plant is made of cells."},
        {"id": "m2", "misconception": "Animal cells have cell walls.", "correction": "Only plant cells have a cell wall."},
    ],
    "worked_examples": [{"id": "w1", "problem": "A cell has a cell wall. Plant or animal?",
                         "solution_md": "Only **plant** cells have a cell wall, so it is a plant cell."}],
    "claims": [
        {"id": "c1", "text": "All organisms are made of cells.", "section_id": "s1"},
        {"id": "c2", "text": "A cell is the smallest unit of life.", "section_id": "s1"},
        {"id": "c3", "text": "The nucleus holds the genetic material.", "section_id": "s2"},
        {"id": "c4", "text": "Orphaned claim with an unknown section.", "section_id": "nope"},
    ],
}
FIGURES = [
    {"id": "f1", "article_id": "art-1", "figure_key": "plant_cell", "caption": "A plant cell in cross-section",
     "status": "rendered", "visual_asset_id": "va-1"},
    {"id": "f2", "article_id": "art-1", "figure_key": "animal_cell", "caption": "An animal cell",
     "status": "draft", "visual_asset_id": None},
]


def test_the_result_validates_as_chapter_content():
    chapter = article_to_chapter(ARTICLE, FIGURES)
    model = ChapterContent(**chapter)              # the contract, enforced by pydantic
    assert model.chapter_num == SYNTHETIC_CHAPTER_NUM == -1, "synthetic: never cached under a book"
    assert model.start_page == 0 and model.end_page == 0
    assert model.images == []
    assert model.title == "Cells: the basic unit of life"
    assert len(model.sections) == 3 and all(s.section_type == "body" for s in model.sections)
    assert all(s.page_num == 0 and s.subsections == [] for s in model.sections)
    assert all(b.page_num == 0 for b in model.key_boxes)


def test_sections_carry_headings_and_plain_prose():
    secs = section_dicts(ARTICLE)
    assert [s["section_title"] for s in secs] == ["What a cell is", "Structures common to all cells", "Section 3"]
    assert secs[0]["content"] == "Every living organism is built from cells. A cell is the smallest unit of life."
    assert "**" not in secs[1]["content"] and "`" not in secs[1]["content"] and "#" not in secs[1]["content"]
    assert "The cell membrane controls entry." in secs[1]["content"]
    assert "Parts" in secs[1]["content"], "a body heading keeps its words, loses its hashes"


def test_rendered_figure_captions_join_their_section_and_drafts_do_not():
    assert rendered_captions(FIGURES) == {"plant_cell": "A plant cell in cross-section"}
    secs = section_dicts(ARTICLE, FIGURES)
    assert secs[1]["content"].endswith("Figure: A plant cell in cross-section")
    assert "An animal cell" not in secs[1]["content"], "a draft figure has no asset — never promised to the model"
    assert secs[2]["content"].endswith("Figure: A plant cell in cross-section")
    assert "Figure:" not in secs[0]["content"]
    assert "Figure:" not in section_dicts(ARTICLE, None)[1]["content"]


def test_key_boxes_by_kind():
    boxes = key_boxes(ARTICLE)
    kinds = [b["type"] for b in boxes]
    assert kinds == ["definition", "definition", "misconception", "misconception", "example",
                     "key_points", "key_points", "key_points"]
    defs = [b for b in boxes if b["type"] == "definition"]
    assert [(b["title"], b["content"]) for b in defs] == [
        ("cell", "The smallest unit of a living thing."),
        ("organelle", "A structure inside a cell with a job."),      # markup stripped, empty term dropped
    ]
    mis = [b for b in boxes if b["type"] == "misconception"]
    assert mis[0]["title"] == "Plants are not made of cells." and mis[0]["content"] == "Every plant is made of cells."
    ex = next(b for b in boxes if b["type"] == "example")
    assert ex["title"].startswith("A cell has a cell wall") and "plant cells have a cell wall" in ex["content"]
    kp = {b["title"]: b["content"] for b in boxes if b["type"] == "key_points"}
    assert kp["What a cell is"] == "All organisms are made of cells.\nA cell is the smallest unit of life."
    assert kp["Structures common to all cells"] == "The nucleus holds the genetic material."
    assert kp["Cells: the basic unit of life"] == "Orphaned claim with an unknown section.", \
        "a claim with an unknown section is kept under the article title, never dropped"


def test_section_ids_by_heading_is_casefolded():
    ids = section_ids_by_heading(ARTICLE)
    assert ids == {"what a cell is": "s1", "structures common to all cells": "s2"}


@pytest.mark.parametrize("md,plain", [
    ("**bold** and *it* and `code`", "bold and it and code"),
    ("## Heading\n\n- one\n- two", "Heading\n\none\ntwo"),
    ("a   b\n\n\n\nc", "a b\n\nc"),
    (None, ""),
    ("snake_case_word stays", "snake_case_word stays"),
])
def test_plain_text(md, plain):
    assert plain_text(md) == plain


def test_an_empty_article_is_still_a_valid_chapter():
    chapter = article_to_chapter({"title": ""})
    model = ChapterContent(**chapter)
    assert model.title == "Untitled" and model.sections == [] and model.key_boxes == []
