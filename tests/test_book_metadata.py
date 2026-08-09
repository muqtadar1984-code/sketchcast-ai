"""Book title/author hygiene.

The gate that decides whether Claude's detected title replaces the
filename-derived one returned False for 19 of 19 production books — the app
started pre-cleaning the title at upload (commit 15dbe57) while the worker kept
a test that assumed the raw filename (commit e9b034e), both on 2026-07-12. So a
month of paid metadata detection was thrown away and a book stayed named
"文字BUSINESS DYNAMICS" through 17 generations, with author "John D. Sterman's".

The sharpest edge here is the script-majority edge trim. Implemented as "strip
non-Latin characters" — the obvious reading — it deletes a Telugu, Hindi,
Marathi, Arabic or Jawi title outright, breaking five shipped locales at once.
Those cases are asserted as NO-OPS below, not eyeballed.
"""

from shared.book_metadata import (clean_title_fallback, looks_like_filename,
                                  pick_book_title, sanitise_author,
                                  sanitise_title, title_needs_repair)

# The four production titles, verbatim.
CJK_PREFIX = "文字BUSINESS DYNAMICS"
ID_PREFIX = "658575611 Cambridge International As Level English General Paper Starter Pack"
ID_PREFIX_2 = "1015966800 As English General Paper Textbook"
DUP_SUFFIX = "Grade 5 Textbook Hoders (3)"
FILE_SLUG = "esl_cie_asl_genpaper_1ed_tr_ch1.3_wksht1.3a_TOR"


class TestSanitiseTitle:
    def test_a_glued_foreign_prefix_is_trimmed(self):
        assert sanitise_title(CJK_PREFIX) == "BUSINESS DYNAMICS"

    def test_a_download_site_id_prefix_is_stripped(self):
        assert sanitise_title(ID_PREFIX).startswith("Cambridge International")
        assert sanitise_title(ID_PREFIX_2) == "As English General Paper Textbook"

    def test_the_browser_duplicate_suffix_is_stripped(self):
        assert sanitise_title(DUP_SUFFIX) == "Grade 5 Textbook Hoders"

    def test_unicode_roman_numerals_are_folded_and_respaced(self):
        assert sanitise_title("PartⅠ Perspective and Process") == "Part I Perspective and Process"

    def test_case_is_left_alone(self):
        # ALL-CAPS is the model's call, not a regex's.
        assert sanitise_title("BUSINESS DYNAMICS") == "BUSINESS DYNAMICS"

    def test_a_clean_title_is_untouched(self):
        for good in ("Cambridge Primary Science Year 7",
                     "Cambridge Primary Mathematics Learner's Book 5",
                     "1000 Solved Problems in Physics"):
            assert sanitise_title(good) == good, good


class TestTenLocalesAreNoOps:
    """Every shipped locale, asserted unchanged. This is the regression that
    would break the product in five markets at once."""

    SINGLE_SCRIPT = {
        "te": "తెలుగు పాఠ్యపుస్తకం ఐదవ తరగతి",
        "hi": "गणित की पाठ्यपुस्तक कक्षा आठ",
        "mr": "विज्ञान पाठ्यपुस्तक इयत्ता सातवी",
        "ar": "كتاب العلوم للصف الخامس",
        "ms-arab": "بهاس ملايو تيڠكتن ساتو",   # Jawi — Arabic script, Malay language
        "ms": "Sains Tahun 5 Buku Teks",
        "en": "Cambridge Primary Science Year 7",
        "fr": "Sciences Cycle 3 Manuel de l'élève",
        "es": "Ciencias Naturales Quinto Grado",
        "pt": "Ciências 5º Ano Livro do Aluno",
    }

    BILINGUAL = {
        "hi+en": "गणित Mathematics Class 8",
        "ar+en": "العربية Grade 5",
        "en+hi": "Mathematics Class 8 गणित",
    }

    def test_single_script_titles_are_untouched(self):
        for code, title in self.SINGLE_SCRIPT.items():
            assert sanitise_title(title) == title, code
            assert title_needs_repair(title) is False, code

    def test_space_separated_bilingual_titles_are_untouched(self):
        # The run is not GLUED, so the edge trim must not fire.
        for code, title in self.BILINGUAL.items():
            assert sanitise_title(title) == title, code


class TestNeedsRepair:
    def test_all_four_production_junk_titles_are_flagged(self):
        for t in (CJK_PREFIX, ID_PREFIX, ID_PREFIX_2, DUP_SUFFIX, FILE_SLUG):
            assert title_needs_repair(t) is True, t

    def test_a_title_a_teacher_typed_is_not_flagged(self):
        for t in ("Biology for Form 4", "My scanned notes", "Chapter 3 revision pack"):
            assert title_needs_repair(t) is False, t

    def test_extension_less_export_slugs_look_like_filenames(self):
        assert looks_like_filename(FILE_SLUG) is True
        assert looks_like_filename("Cambridge Primary Science Year 7") is False


class TestPickTitle:
    def test_the_detected_title_finally_wins(self):
        # The whole point: this branch had a 0% hit rate in production.
        assert pick_book_title(
            CJK_PREFIX, "Business Dynamics: Systems Thinking and Modeling for a Complex World"
        ) == "Business Dynamics: Systems Thinking and Modeling for a Complex World"

    def test_a_clean_stored_title_is_kept(self):
        assert pick_book_title("Cambridge Primary Science Year 7", "Something Else") is None

    def test_a_hallucinated_title_cannot_rename_a_book(self):
        # No shared word with the stored title → fall back to the deterministic
        # tidy instead of trusting the model.
        out = pick_book_title(DUP_SUFFIX, "War and Peace")
        assert out == "Grade 5 Textbook Hoders"

    def test_the_model_may_correct_a_misspelling_it_shares_words_with(self):
        assert pick_book_title(DUP_SUFFIX, "Grade 5 Textbook Hodder") == "Grade 5 Textbook Hodder"

    def test_the_detected_title_is_sanitised_too(self):
        assert pick_book_title(CJK_PREFIX, CJK_PREFIX) == "BUSINESS DYNAMICS"

    def test_no_detection_still_tidies(self):
        assert pick_book_title(ID_PREFIX_2, None) == "As English General Paper Textbook"

    def test_a_single_word_title_a_teacher_typed_is_never_reworded(self):
        # REGRESSION, and a direct contradiction of this module's own invariant.
        # "has no space" counted as a filename signature all by itself, so a
        # one-word title fell through to clean_title_fallback, which ends in
        # .title(): measured, pick_book_title('IGCSE', None) -> 'Igcse' and
        # ('BIOLOGY', None) -> 'Biology'. process.py's
        # `if new_title == current_title` guard does not catch it, because the
        # case genuinely changed — the book is renamed on every re-index.
        for typed in ("IGCSE", "BIOLOGY", "Physics", "IX", "Matematik"):
            assert looks_like_filename(typed) is False, typed
            assert title_needs_repair(typed) is False, typed
            assert pick_book_title(typed, None) is None, typed
            # …and a detected title cannot quietly take it over either.
            assert pick_book_title(typed, "Something Else Entirely") is None, typed

    def test_a_real_single_token_filename_is_still_repaired(self):
        # The narrowing must not un-fix the production case: a file name is a
        # single token WITH file punctuation, not merely a single token.
        assert pick_book_title(FILE_SLUG, None) is not None
        assert pick_book_title("cambridge-biology.pdf", None) == "Cambridge Biology"


class TestAuthor:
    def test_the_possessive_is_stripped(self):
        assert sanitise_author("John D. Sterman's", "Business Dynamics") == "John D. Sterman"
        assert sanitise_author("John D. Sterman’s", "Business Dynamics") == "John D. Sterman"

    def test_a_leading_by_is_stripped(self):
        assert sanitise_author("by Hodder Education", "Grade 5") == "Hodder Education"

    def test_a_copied_cover_line_is_rejected(self):
        # "X's <Title>" degrades to "X <Title>" once the possessive is gone —
        # that is a cover line, not an author.
        assert sanitise_author("John D. Sterman's Business Dynamics", "Business Dynamics") is None

    def test_null_tokens_are_rejected(self):
        for v in ("null", "None", "unknown", "N/A", "", None, "-"):
            assert sanitise_author(v, "x") is None, v

    def test_a_real_publisher_survives(self):
        assert sanitise_author("Cambridge University Press", "Science Y7") == "Cambridge University Press"


class TestFallback:
    def test_download_site_slugs_are_tidied(self):
        assert clean_title_fallback(
            "pdfcoffee.com_cambridge-maths-5-learner-book-pdf-free"
        ) == "Cambridge Maths 5 Learner Book"
