"""A page LABEL must never fail a lesson.

Measured 2026-09-05: a teacher's first kit on a book numbering its pages A1,
A2, A3 lost three of its four failing artifacts here. `first_introduced_page`
was declared `int`, the extractor returned what the book actually says, and
pydantic refused the whole ConceptResult:

    12 validation errors for ConceptResult
    concepts.0.first_introduced_page
      Input should be a valid integer, unable to parse string as an integer
      [type=int_parsing, input_value='A2', input_type=str]

lesson_plan, worksheet and case_study all died on it. exam_paper, deck and
activity — which carry no concept extraction — came through fine, which is
what pins the cause to this field rather than to the book.

Nothing in the codebase reads the value.
"""
import pytest

from agent2_analysis.models import Concept, ConceptResult


def _concept(page):
    return {"concept_id": "c1", "name": "Tawheed",
            "definition": "The oneness of God", "first_introduced_page": page}


class TestAPageLabelIsNotAnIndex:
    def test_the_exact_input_that_failed_her_kit(self):
        # 'A2' is what the book says. Before this, the whole result was refused.
        r = ConceptResult(**{"concepts": [_concept("A2")]})
        assert r.concepts[0].first_introduced_page == "A2"

    @pytest.mark.parametrize("label", ["A1", "A2", "iv", "7a", "xii", "B-3"])
    def test_a_book_may_label_its_pages_however_it_likes(self, label):
        assert ConceptResult(**{"concepts": [_concept(label)]}) is not None

    def test_a_real_number_is_still_a_number(self):
        # Existing rows and fixtures must be untouched: a numeric page stays an
        # int, whether the model sent it as one or as a digit string.
        assert Concept(**_concept(12)).first_introduced_page == 12
        assert Concept(**_concept("7")).first_introduced_page == 7

    @pytest.mark.parametrize("empty", [None, "", "   "])
    def test_nothing_at_all_reads_as_zero(self, empty):
        assert Concept(**_concept(empty)).first_introduced_page == 0

    def test_a_flag_is_not_a_page(self):
        # bool is an int in Python; True must not become page 1.
        assert Concept(**_concept(True)).first_introduced_page == 0

    def test_the_whole_result_survives_a_mixed_book(self):
        r = ConceptResult(**{"concepts": [
            _concept("A1"), _concept(3), _concept("ii"), _concept(None)]})
        assert [c.first_introduced_page for c in r.concepts] == ["A1", 3, "ii", 0]
