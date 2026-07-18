"""Language detection, prompt directives, shaping, and RTL slide rendering."""

from shared.languages import detect_language, get_language, is_rtl, prompt_directive
from shared.text_shaping import contains_arabic, display_text
from shared.tts.registry import default_voice_id_for


EN = "The cell is the basic unit of life and all living things are made of cells that divide."
MS = (
    "Sel ialah unit asas kehidupan dan semua benda hidup terdiri daripada sel. "
    "Bahagian ini menerangkan struktur sel dengan contoh untuk murid dalam kelas. "
    "Guru boleh menggunakan nota ini untuk soalan dan latihan yang sesuai dengan topik ini. "
    "Ini adalah bahagian penting dan murid tidak boleh lupa untuk mengulang kaji dengan teliti."
)
AR = "الخلية هي الوحدة الأساسية للحياة وجميع الكائنات الحية تتكون من خلايا"
FR = (
    "La cellule est l'unité de base de la vie et tous les êtres vivants sont constitués de cellules. "
    "Dans cette partie nous allons étudier la structure des cellules avec des exemples pour les élèves. "
    "Le professeur peut utiliser ces notes pour des questions et des exercices qui sont adaptés à cette leçon."
)
ES = (
    "La célula es la unidad básica de la vida y todos los seres vivos están formados por células. "
    "En esta parte vamos a estudiar la estructura de las células con ejemplos para los alumnos. "
    "El profesor puede usar estas notas para preguntas y ejercicios que están adaptados a esta lección del tema."
)
PT = (
    "A célula é a unidade básica da vida e todos os seres vivos são formados por células. "
    "Nesta parte não vamos apenas estudar uma estrutura, mas também exemplos para os alunos. "
    "O professor pode usar estas notas para perguntas e exercícios que são adequados a esta lição do tema."
)


class TestDetection:
    def test_detects_each_language(self):
        assert detect_language(EN * 4) == "en"
        assert detect_language(MS * 3) == "ms"
        assert detect_language(AR) == "ar"
        assert detect_language(FR * 3) == "fr"
        assert detect_language(ES * 3) == "es"
        assert detect_language(PT * 3) == "pt"

    def test_ambiguous_and_empty_return_none(self):
        assert detect_language("") is None
        assert detect_language("12345 67890 !!!") is None
        assert detect_language("one two three") is None  # too short

    def test_arabic_wins_by_script_even_with_latin_mixed_in(self):
        mixed = AR + " chapter one test " + AR + " " + AR
        assert detect_language(mixed) == "ar"


class TestRegistry:
    def test_language_metadata(self):
        assert get_language("ar").direction == "rtl"
        assert is_rtl("ar") and not is_rtl("ms")
        assert get_language("unknown").code == "en"  # safe fallback

    def test_prompt_directive(self):
        assert prompt_directive("en") == ""
        d = prompt_directive("ms")
        assert "Malay" in d and "Do not translate" in d

    def test_default_voices_per_language(self):
        assert default_voice_id_for("ms") == "edge-yasmin"
        assert default_voice_id_for("ar") == "edge-zariyah"
        assert default_voice_id_for("fr") == "edge-denise"
        assert default_voice_id_for("es") == "edge-elvira"
        assert default_voice_id_for("pt") == "edge-francisca"
        assert default_voice_id_for("en") == "edge-aria"
        assert default_voice_id_for(None) == "edge-aria"


class TestShaping:
    def test_arabic_is_reshaped_and_reordered(self):
        import pytest

        pytest.importorskip("arabic_reshaper")
        s = display_text(AR, rtl_base=True)
        assert s != AR  # joined presentation forms differ from raw codepoints
        assert contains_arabic(s)

    def test_non_arabic_passes_through_untouched(self):
        assert display_text(EN) == EN
        assert display_text(MS) == MS
        assert display_text("") == ""


class TestRtlSlide:
    def test_arabic_slide_renders_mirrored_without_error(self):
        from agent5_slides.slide_builder import compose_slide

        canvas, anim, static = compose_slide(
            heading="الخلية ووظائفها",
            points=["الخلية هي الوحدة الأساسية للحياة", "تنقسم الخلايا لتكوين خلايا جديدة"],
            number=1,
            direction="rtl",
        )
        assert canvas.size == (1280, 720)
        assert anim  # badge + title + bullets all produced reveal boxes
        # Mirrored layout: the badge (first element) sits on the RIGHT half.
        badge_box = anim[0][0]
        assert badge_box[0] > 1280 / 2
        # Every box stays inside the canvas.
        for el in anim:
            for (x0, y0, x1, y1) in el:
                assert 0 <= x0 <= x1 <= 1280 and 0 <= y0 <= y1 <= 720

    def test_ltr_slide_unchanged(self):
        from agent5_slides.slide_builder import compose_slide

        canvas, anim, _static = compose_slide(
            heading="Cells and their functions",
            points=["The cell is the basic unit of life"],
            number=1,
        )
        assert canvas.size == (1280, 720)
        assert anim[0][0][0] < 1280 / 2  # badge on the LEFT
