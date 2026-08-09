"""Book Health Score — pure-function tests over the index-time signals."""

from types import SimpleNamespace

from agent1_ingestion.book_health import compute_book_health, text_quality

# Real prose, not "x" * n: the text-QUALITY dimension measures word spacing and
# script coherence, so a wall of one repeated letter reads (correctly) as an
# unreadable text layer and would fail every fixture that only meant to say
# "this book has plenty of text".
_PROSE = "The cell is the basic unit of life and every living thing is made of cells. "


def _extraction(total_pages, readability, text_chars, text=None):
    # one item carrying the whole text budget is enough for the has_text check
    if text is None:
        text = (_PROSE * (text_chars // len(_PROSE) + 1))[:text_chars] if text_chars else ""
    items = [SimpleNamespace(text=text)] if text else []
    return SimpleNamespace(total_pages=total_pages, readability_score=readability, items=items)


def _items_extraction(texts, total_pages=200, readability=0.9):
    """An extraction whose ITEM BOUNDARIES matter — the quality sample measures
    spacing per item, so a fixture that hands it one big string cannot exercise
    the short-item shapes (table cells, vocabulary lists, ruby runs) at all."""
    return SimpleNamespace(total_pages=total_pages, readability_score=readability,
                           items=[SimpleNamespace(text=t) for t in texts])


def _chunks(text, size):
    """``text`` cut into items of ``size`` characters — how a PDF hands over a
    table, a bilingual vocabulary list or a column of exercise stems."""
    return [text[i:i + size] for i in range(0, len(text), size)]


def _chaps(n):
    return [{"chapter_num": i, "title": f"Unit {i}"} for i in range(n)]


def test_clean_text_book_scores_excellent():
    h = compute_book_health(_extraction(180, 0.95, 50000), _chaps(12))
    assert h["band"] == "excellent" and h["score"] >= 85
    assert h["problems"] == [] and h["recommendation"] is None
    assert h["facts"]["has_text_layer"] is True


def test_scanned_book_is_good_with_note_not_poor():
    # No text layer, but vision handles it — should NOT be scary.
    h = compute_book_health(_extraction(120, 0.0, 0), _chaps(10))
    assert h["band"] in ("good", "fair")
    assert h["facts"]["has_text_layer"] is False
    assert h["note"] and "vision" in h["note"].lower()
    # a healthy chapter count keeps it out of "poor"
    assert h["score"] >= 70


def test_sparse_text_layer_flags_problem():
    h = compute_book_health(_extraction(100, 0.25, 5000), _chaps(8))
    assert any("machine-readable" in p or "images" in p for p in h["problems"])
    assert h["dimensions"]["text_layer"] <= 68


def test_single_chapter_fallback_flags_structure():
    h = compute_book_health(_extraction(90, 0.9, 40000), _chaps(1))
    assert any("one unit" in p.lower() for p in h["problems"])
    assert h["dimensions"]["structure"] <= 55
    assert h["recommendation"] is not None


def test_very_short_doc_flagged():
    h = compute_book_health(_extraction(3, 0.9, 2000), _chaps(2))
    assert any("short" in p.lower() for p in h["problems"])


def test_worst_case_poor():
    # tiny, no text, no chapters
    h = compute_book_health(_extraction(2, 0.0, 0), _chaps(0))
    assert h["band"] == "poor" and h["score"] < 50
    assert h["recommendation"] is not None


def test_json_serializable_shape():
    import json
    h = compute_book_health(_extraction(180, 0.95, 50000), _chaps(12))
    json.dumps(h)  # must not raise
    assert set(h) == {"score", "band", "dimensions", "facts", "problems", "recommendation", "note"}
    assert isinstance(h["score"], int)


# ── text QUALITY, not text VOLUME ────────────────────────────────────────────
# The book that forced this: 1,008 pages whose "text layer" extracted as
# run-together words with CJK mojibake in the front matter. It measured 96/100
# on text_layer, scored 95 "excellent", and 17 lesson kits were generated from
# it before anyone looked.

# Verbatim from production chapter_grounding for book 5bd0d381.
STERMAN_BAD = (
    "Experienceisanexpensiveschoo l. Thegreatestconstantofmoderntimesischange."
    "AcceleratlngChangesintech-nologyandthepaceofchangeitselfmeanthatthemanager"
    "mustlearncontinuously.Businessdynamicsisthestudyofcomplexsystemsandhowthey"
    "behaveovertime,andwhythepoliciesweadoptsooftenproducetheoppositeofwhatwe"
    "intended.Systemsthinkingrequiresthatwelookbeyondeventstothestructuresthat"
    "producethem,andthatwetestourmentalmodelsagainstevidence."
)
STERMAN_MOJIBAKE = "撃i i _ 3蚤妻 賢哲季曇苧 腎 門塁 間毒雷 S Sy 離em S T 的毒軸 岳 ng and 醐 ◎ 鮎§裏mg "

# A normal, well-formed English chapter.
GOOD_ENGLISH = (
    "Cells are the basic units of every living organism. In this chapter we look at "
    "the structure of plant and animal cells, at the job each part of the cell does, "
    "and at how cells divide to let an organism grow and repair itself. We will use a "
    "microscope to look at real cells, and we will draw what we see. "
)


def test_sterman_text_layer_is_judged_low_quality():
    # Run-together words: 1.2% spaces where English prose runs 14-20%.
    q = text_quality(_extraction(1008, 0.9, 0, text=STERMAN_BAD * 40))
    assert q["checked"] is True and q["ok"] is False
    assert q["space_ratio"] < 0.06 and q["median_token"] > 12
    assert "run together" in q["reason"]


def test_well_formed_english_chapter_is_judged_fine():
    q = text_quality(_extraction(200, 0.9, 0, text=GOOD_ENGLISH * 40))
    assert q["checked"] is True and q["ok"] is True and q["reason"] is None
    assert q["space_ratio"] > 0.12 and q["median_token"] <= 12


def test_mojibake_front_matter_is_caught_by_script_coherence():
    # Spacing alone would pass this one (31% spaces) — the second signal, the
    # share of letters in one writing system, is what condemns it.
    q = text_quality(_extraction(1008, 0.9, 0, text=STERMAN_MOJIBAKE * 40))
    assert q["ok"] is False and "scripts" in q["reason"]


def test_unreadable_text_layer_is_never_excellent():
    h = compute_book_health(_extraction(1008, 0.9, 0, text=STERMAN_BAD * 40), _chaps(8))
    # This is the exact shape that scored 95/"excellent" in production.
    assert h["band"] != "excellent" and h["score"] <= 55
    assert h["facts"]["text_readable"] is False
    assert any("text layer" in p for p in h["problems"])
    assert h["recommendation"] is not None


def test_cjk_book_is_not_condemned_for_having_no_spaces():
    # Chinese does not space-separate words. A script-blind space-ratio rule
    # would fail every real Chinese textbook in the library.
    chinese = "细胞是所有生物体的基本单位。在本章中我们将研究植物细胞和动物细胞的结构以及各部分的功能。" * 40
    q = text_quality(_extraction(200, 0.9, 0, text=chinese))
    assert q["checked"] is True and q["ok"] is True


def test_spaced_non_english_scripts_pass():
    # Every language this product ships uses spaces; none may be condemned.
    samples = {
        "ms": "Sel ialah unit asas kehidupan dan semua benda hidup terdiri daripada sel. ",
        "ar": "الخلية هي الوحدة الأساسية للحياة وجميع الكائنات الحية تتكون من خلايا صغيرة جدا. ",
        "hi": "कोशिका जीवन की मूल इकाई है और सभी जीव कोशिकाओं से बने हैं यह अध्याय में समझाया गया है। ",
        "mr": "पेशी ही जीवनाची मूलभूत एकक आहे आणि सर्व सजीव पेशींनी बनलेले असतात हे येथे पाहू. ",
        "te": "కణం జీవానికి ప్రాథమిక ప్రమాణం మరియు అన్ని జీవులు కణాలతో నిర్మితమై ఉంటాయి అని చూద్దాం. ",
    }
    for code, sample in samples.items():
        q = text_quality(_extraction(200, 0.9, 0, text=sample * 40))
        assert q["ok"] is True, f"{code} wrongly judged unreadable: {q}"


def test_malay_book_with_dense_arabic_quotes_still_passes():
    # An Islamic-Education book quotes plenty of Quranic Arabic. Mixed script is
    # normal in this product's markets and must never be a condemnation on its own.
    mixed = (
        "Murid boleh membaca ayat ini dengan betul dan memahami maksudnya dalam kelas. "
        "بسم الله الرحمن الرحيم الحمد لله رب العالمين "
    ) * 40
    assert text_quality(_extraction(120, 0.9, 0, text=mixed))["ok"] is True


def test_short_text_is_not_judged_at_all():
    # A scanned book must stay in the scanned branch, not be condemned by a
    # measurement taken over a watermark.
    q = text_quality(_extraction(120, 0.0, 0, text="CamScanner"))
    assert q["checked"] is False and q["ok"] is True


# ── the English-density sweep ────────────────────────────────────────────────
# REGRESSION. The coherence rule used to threshold FAMILY SHARE, which is taken
# over letters — and a Han character carries a whole word where a Latin word
# costs 5-8 letters. So a Chinese textbook with a light sprinkling of English
# technical terms scored a lower "share" than one drenched in English, and the
# verdict flipped back and forth as the supposed contamination rose:
#   Chinese  0 blocks ok | 1 block CONDEMNED (0.56) | 2+ ok again (0.61-0.86)
#   Japanese 0 ok        | 1 block CONDEMNED (0.55) | 2+ ok again
#   Thai     1, 2 AND 3 blocks all CONDEMNED (0.73 / 0.57 / 0.53)
# A non-monotonic verdict is proof the MEASURE is wrong, not the threshold. The
# cost of the false positive is real: score 55 / band "fair", a problem string
# telling the teacher the PDF can't be read, and the whole book re-read by
# per-page vision OCR.

CJK_SWEEP = {
    "zh": "细胞是所有生物体的基本单位。在本章中我们将研究植物细胞和动物细胞的结构以及各部分的功能。" * 3,
    "ja": "細胞はすべての生物の基本的な単位です。この章では植物細胞と動物細胞の構造とそれぞれの部分の働きを学びます。" * 3,
    "th": "เซลล์เป็นหน่วยพื้นฐานของสิ่งมีชีวิตทุกชนิดในบทนี้เราจะศึกษาโครงสร้างของเซลล์พืชและเซลล์สัตว์" * 3,
}
# Two densities of English contamination: bare technical terms (which keep the
# spaceless script dominant) and full English sentences (which flip `dominant`
# to latin while the book is still overwhelmingly CJK/Thai by content — the
# second route to the same letter-vs-word error).
EN_TERMS = " mitochondrion photosynthesis chlorophyll "
EN_SENTENCE = (
    " The mitochondrion is the powerhouse of the cell and photosynthesis converts "
    "light energy into chemical energy stored in glucose. "
)


def test_spaceless_scripts_pass_at_every_english_density():
    for code, base in CJK_SWEEP.items():
        for english in (EN_TERMS, EN_SENTENCE):
            for blocks in range(0, 7):
                q = text_quality(_extraction(200, 0.9, 0, text=(base + english * blocks) * 40))
                assert q["ok"] is True, f"{code} condemned at {blocks} English blocks: {q}"


def test_the_run_together_rule_still_fires_with_cjk_below_the_gate():
    # The gate that routes a sample to the spaceless branch must not become a
    # way to smuggle a genuinely broken text layer past the spacing rule. Latin
    # prose with its spaces stripped, carrying ~9% CJK letters — under the 25%
    # gate — is still condemned for what it is.
    runon = "Thegreatestconstantofmoderntimesischangeandthemanagermustlearncontinuously "
    q = text_quality(_extraction(1008, 0.9, 0, text=runon * 200 + " " + "细胞是基本单位。" * 180))
    assert q["ok"] is False and "run together" in q["reason"]


def test_halfwidth_and_fullwidth_forms_are_still_japanese():
    # ｱｲｳ (halfwidth katakana) and ＡＢＣ (fullwidth Latin) were classified as
    # "other", which is its own family — so the sample's dominant script became
    # "other", which is not in SPACELESS_SCRIPTS, and the book was judged by the
    # SPACED rules, where its (correct) zero space ratio read as run-together
    # words. 々 had the same problem: it is category Lm, so isalpha() is True.
    text = "ｺﾝﾋﾟｭｰﾀｰの学習は各々の生徒にとって大切です。ＤＮＡと細胞の構造を学びます。" * 60
    q = text_quality(_extraction(200, 0.9, 0, text=text))
    assert q["script"] in ("kana", "han")
    assert q["ok"] is True


def test_an_unknown_script_is_unjudgeable_rather_than_bad():
    # A script this module does not know is a script whose spacing convention it
    # also does not know. Every rule would be guesswork.
    q = text_quality(_extraction(200, 0.9, 0, text="ᚠᚢᚦᚨᚱᚲᚷᚹᚺᚾᛁᛃᛇᛈᛉᛊᛏᛒᛖᛗᛚᛜᛞᛟ" * 40))
    assert q["checked"] is False and q["ok"] is True


def test_the_quality_sample_is_taken_from_across_the_book():
    # The first 40k characters of a large book are its cover, copyright/CIP
    # block, colophon and contents — systematically the least representative
    # text in it, and exactly where bilingual publisher data, ISBN blocks and
    # mixed-script imprints live. Here the front matter alone is longer than the
    # whole sample budget, so a document-order sample measured the imprint page
    # and called a Chinese textbook a Latin one.
    front = SimpleNamespace(text="Published by the University Press. "
                                 "All rights reserved. ISBN 978 0 521 00000 0. " * 600)
    body = SimpleNamespace(text="细胞是所有生物体的基本单位。"
                                "在本章中我们将研究植物细胞和动物细胞的结构以及各部分的功能。" * 4000)
    assert len(front.text) > 40000  # the whole sample budget, spent on the imprint
    q = text_quality(SimpleNamespace(total_pages=1008, readability_score=0.9,
                                     items=[front, body]))
    assert q["checked"] is True and q["script"] == "han" and q["ok"] is True


# ── the ITEM-LENGTH sweep ────────────────────────────────────────────────────
# REGRESSION. The quality sample joined extraction items with " " and then
# thresholded the space ratio of that joined string — so it manufactured the very
# thing it measured. On identical monolingual Chinese body text, varying ONLY the
# item length, the injected join spaces alone measured:
#   2 chars 0.333 BAD | 3 -> 0.250 BAD | 4 -> 0.200 BAD | 5 -> 0.166 BAD
#   6 -> 0.142 OK     | 20 -> 0.047 OK
# against _MAX_SPACE_RATIO_SPACELESS of 0.15. Japanese and Thai measured the
# same. Every one was condemned with "characters from unrelated scripts are mixed
# through the text" — for a MONOLINGUAL book — and extraction_has_text() then
# routed the whole textbook to per-page vision OCR: real money, long latency,
# wrong answer. Table cells, figure captions, bilingual vocabulary lists,
# exercise stems and ruby/furigana runs all extract at exactly these lengths.

CJK_BODY = {
    "zh": "细胞是所有生物体的基本单位在本章中我们将研究植物细胞和动物细胞的结构以及各部分的功能" * 40,
    "ja": "細胞はすべての生物の基本的な単位ですこの章では植物細胞と動物細胞の構造とそれぞれの部分の働きを学びます" * 40,
    "th": "เซลล์เป็นหน่วยพื้นฐานของสิ่งมีชีวิตทุกชนิดในบทนี้เราจะศึกษาโครงสร้างของเซลล์พืชและเซลล์สัตว์" * 40,
}


def test_monolingual_cjk_passes_at_every_item_length():
    from agent1_ingestion.vision_chapters import extraction_has_text

    for code, body in CJK_BODY.items():
        for size in (2, 3, 4, 5, 6, 20, 200):
            ex = _items_extraction(_chunks(body, size))
            q = text_quality(ex)
            assert q["ok"] is True, f"{code} condemned at {size}-char items: {q}"
            # …and the routing that hangs off it. extraction_has_text returned
            # False at item lengths 4 and 5 and True at 6, i.e. the same book
            # took the vision route or the text route depending on how the PDF
            # happened to be segmented.
            assert extraction_has_text(ex) is True, f"{code} re-OCR'd at {size}-char items"


def test_a_short_item_book_reports_its_spacing_as_unmeasured():
    # Not "0.15 spaces" and not "0.0 spaces" — NOT MEASURED. A book handed over a
    # cell at a time never showed us its word separation, and every rule that
    # thresholds the ratio is skipped rather than fed a manufactured number.
    q = text_quality(_items_extraction(_chunks(CJK_BODY["zh"], 3)))
    assert q["checked"] is True
    assert q["spacing_measured"] is False and q["space_ratio"] == 0.0
    q = text_quality(_items_extraction(_chunks(CJK_BODY["zh"], 200)))
    assert q["spacing_measured"] is True


def test_a_bilingual_glossary_in_word_items_is_not_mojibake():
    # "cat 猫 dog 狗 run 跑 …" as one item per cell. Two scripts by design, no
    # item long enough to hold a word boundary — neither fact is a defect.
    pairs = ["cat", "猫", "dog", "狗", "run", "跑", "eat", "吃", "sun", "日",
             "moon", "月", "fire", "火", "tree", "树", "bird", "鸟", "fish", "鱼"]
    q = text_quality(_items_extraction(pairs * 60))
    assert q["ok"] is True and q["spacing_measured"] is False


def test_every_shipped_locale_survives_short_items():
    # The ten shipped locales, extracted as vocabulary cells (4 chars), short
    # phrases (12) and prose lines (200). None may be condemned at any of them.
    locales = {
        "en": "The cell is the basic unit of life and every living thing is made of cells. ",
        "ms": "Sel ialah unit asas kehidupan dan semua benda hidup terdiri daripada sel. ",
        "ms-arab": "سيل اياله اونيت اساس كهيدوڤن دان سموا بندا هيدوڤ ترديري درڤد سيل. ",
        "ar": "الخلية هي الوحدة الأساسية للحياة وجميع الكائنات الحية تتكون من خلايا صغيرة جدا. ",
        "fr": "La cellule est l'unite de base de la vie et tous les etres vivants en sont faits. ",
        "es": "La celula es la unidad basica de la vida y todos los seres vivos estan hechos de celulas. ",
        "pt": "A celula e a unidade basica da vida e todos os seres vivos sao feitos de celulas. ",
        "hi": "कोशिका जीवन की मूल इकाई है और सभी जीव कोशिकाओं से बने हैं यह अध्याय में समझाया गया है। ",
        "mr": "पेशी ही जीवनाची मूलभूत एकक आहे आणि सर्व सजीव पेशींनी बनलेले असतात हे येथे पाहू. ",
        "te": "కణం జీవానికి ప్రాథమిక ప్రమాణం మరియు అన్ని జీవులు కణాలతో నిర్మితమై ఉంటాయి అని చూద్దాం. ",
    }
    for code, sample in locales.items():
        for size in (4, 12, 200):
            q = text_quality(_items_extraction(_chunks(sample * 60, size)))
            assert q["ok"] is True, f"{code} condemned at {size}-char items: {q}"


def test_the_production_book_is_still_condemned_item_by_item():
    # The triggering case must survive the fix, measured the way it really
    # arrives: a PDF hands text over line by line, not as one string.
    body = text_quality(_items_extraction(_chunks(STERMAN_BAD * 40, 60)))
    assert body["ok"] is False and "run together" in body["reason"]
    assert body["spacing_measured"] is True and body["space_ratio"] < 0.06

    front = text_quality(_items_extraction([STERMAN_MOJIBAKE.strip()] * 200))
    assert front["ok"] is False and "scripts" in front["reason"]
    # ~0.31 in production; the join spaces were never what condemned it.
    assert 0.25 < front["space_ratio"] < 0.40


def test_halfwidth_voiced_marks_are_kana_not_other():
    # ﾞ and ﾟ (U+FF9E, U+FF9F) are category Lm, so isalpha() is True and they
    # counted — as "other", their own family. Halfwidth katakana writes ガ as
    # ｶ + ﾞ, so they appear in roughly every other syllable.
    from shared.scripts import script_of

    assert script_of("ﾞ") == "kana" and script_of("ﾟ") == "kana"
    q = text_quality(_items_extraction(["ｶﾞｯｷｭｳでﾊﾟｿｺﾝを使って細胞の構造を学びます。"] * 300))
    assert q["ok"] is True and q["script"] in ("kana", "han")


def test_pages_no_chapter_covers_are_reported():
    # The structurer bounds how far the last chapter may stretch, so detection
    # stopping early leaves an unmapped tail. It must never be silent — those
    # pages are never extracted, analysed or taught.
    chapters = [{"chapter_num": i, "title": f"Unit {i}", "start_page": i * 7,
                 "end_page": i * 7 + 6} for i in range(10)]
    h = compute_book_health(_extraction(301, 0.95, 50000), chapters)
    assert any("aren't covered by any chapter" in p for p in h["problems"])
    assert h["band"] != "excellent"

    # …and a map that reaches the end says nothing.
    chapters[-1]["end_page"] = 300
    assert compute_book_health(_extraction(301, 0.95, 50000), chapters)["problems"] == []


def test_a_book_missing_most_of_its_content_is_never_good():
    # REGRESSION. The unmapped tail capped the STRUCTURE dimension at 70 but
    # nothing capped the overall score, and text_layer carries 45% of the
    # weight — so 96 of 301 pages mapped (204 dropped, 68% of the book) scored
    # 82, band "good". The structurer now rejects such a map outright; one only
    # reaches health when the rescue path came up empty too, and then the score
    # is the last thing standing between the teacher and a truncated book.
    chapters = [{"chapter_num": i, "title": f"Skill {i}", "start_page": i * 7,
                 "end_page": i * 7 + 6} for i in range(10)]
    chapters.append({"chapter_num": 10, "title": "Compare & Evaluate",
                     "start_page": 70, "end_page": 96})
    h = compute_book_health(_extraction(301, 0.95, 50000), chapters)
    assert h["facts"]["unmapped_pages"] == 204
    assert h["score"] <= 69 and h["band"] == "fair"

    # A 20% tail — the legitimate unbookmarked back-matter shape — is reported
    # but not capped: the book really does teach everything it mapped.
    good = [{"chapter_num": i, "title": f"Unit {i}", "start_page": i * 20,
             "end_page": i * 20 + 19} for i in range(10)]
    good[-1]["end_page"] = 239
    h = compute_book_health(_extraction(300, 0.95, 50000), good)
    assert h["facts"]["unmapped_pages"] == 60 and h["band"] == "good"
