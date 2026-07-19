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


TE = "కణం జీవానికి ప్రాథమిక ప్రమాణం మరియు అన్ని జీవులు కణాలతో నిర్మితమై ఉంటాయి"
MR = "पेशी ही जीवनाची मूलभूत एकक आहे आणि सर्व सजीव पेशींनी बनलेले असतात"
HI = "कोशिका जीवन की मूल इकाई है और सभी जीव कोशिकाओं से बने हैं यह अध्याय में समझाया गया है"


class TestDetection:
    def test_detects_each_language(self):
        assert detect_language(EN * 4) == "en"
        assert detect_language(MS * 3) == "ms"
        assert detect_language(AR) == "ar"
        assert detect_language(FR * 3) == "fr"
        assert detect_language(ES * 3) == "es"
        assert detect_language(PT * 3) == "pt"
        assert detect_language(TE * 2) == "te"
        assert detect_language(MR * 2) == "mr"
        assert detect_language(HI * 2) == "hi"

    def test_hindi_and_marathi_disambiguate_despite_shared_script(self):
        # Same Devanagari script — the function-word vote must separate them.
        assert detect_language(MR * 3) == "mr"
        assert detect_language(HI * 3) == "hi"
        # Signal-free Devanagari (bare nouns) defaults to Hindi, the majority
        # Devanagari language — never None once the script dominates.
        assert detect_language("कोशिका विज्ञान पुस्तक अध्याय " * 20) == "hi"

    def test_quran_quotes_do_not_flip_a_malay_book_to_arabic(self):
        # Dense Arabic quotations inside a Malay book: Arabic needs script
        # DOMINANCE (more Arabic letters than Latin), not just presence.
        mixed = MS * 4 + " " + AR
        assert detect_language(mixed) == "ms"

    def test_unsupported_languages_return_none_not_a_near_miss(self):
        italian = (
            "La cellula è l'unità fondamentale della vita e tutti gli esseri viventi "
            "sono costituiti da cellule che si dividono durante la crescita del corpo umano. "
            "Questo capitolo presenta la struttura delle cellule con esempi per gli studenti."
        )
        assert detect_language(italian * 3) in (None, "es") or True  # must not crash; ideally None
        # The hardened margins should reject it outright:
        assert detect_language(italian * 3) is None

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

    def test_jawi_is_malay_in_arabic_script_rtl(self):
        j = get_language("ms-arab")
        assert j.code == "ms-arab" and j.direction == "rtl"
        assert is_rtl("ms-arab")
        # get_language lower-cases, so a stored "ms-Arab" still resolves.
        assert get_language("ms-Arab").code == "ms-arab"

    def test_jawi_directive_demands_jawi_not_rumi(self):
        d = prompt_directive("ms-arab")
        assert "JAWI" in d and "Malay" in d
        assert "Rumi" in d  # must tell the model NOT to use Rumi/Latin
        assert "چ" in d  # names the Jawi-specific letters

    def test_default_voices_per_language(self):
        assert default_voice_id_for("ms") == "edge-yasmin"
        assert default_voice_id_for("ar") == "edge-zariyah"
        assert default_voice_id_for("fr") == "edge-denise"
        assert default_voice_id_for("es") == "edge-elvira"
        assert default_voice_id_for("pt") == "edge-francisca"
        assert default_voice_id_for("te") == "edge-shruti"
        assert default_voice_id_for("mr") == "edge-aarohi"
        assert default_voice_id_for("hi") == "edge-swara"
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
