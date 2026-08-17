"""Part-scoped coverage + Arabic-script folding — the two halves of incident
8b79d4e0 (2026-08-16).

A Persian book («علوم») detected as "ar" generated a part-3-of-4 presentation
and the coverage gate hard-failed the job: "covers only 3 of 17 topics". Two
defects compounded:

  A — the part job's analysis yields a single-episode plan with empty
      key_concepts_introduced, which is byte-identical to a legacy
      whole-chapter plan, so chapter_topics pooled the denominator to ALL of
      the analysis's concepts. Correct for the legacy row; for a PART job it
      judges a part-3 script against topics parts 1, 2 and 4 own — a bar no
      part script can clear. Worse, the must_cover retry then explicitly
      ordered the model to teach the other parts' topics.

  B — _tokens kept Arabic harakat glued into tokens (the keep-combining-marks
      rule that Devanagari and Telugu genuinely need) and never folded the
      Persian/Urdu letter variants (ک U+06A9 vs ك U+0643, ی U+06CC vs ي
      U+064A), so a Persian topic list vs an Arabic-language script matched
      ~nothing: the incident's 0.059 first draft.

The fixes under test: measure(..., part_scoped=True) marks a pooled-denominator
report "pooled" and takes it out of should_fail / should_retry (defect A), and
_tokens folds the Arabic block — ONLY the Arabic block — before tokenising
(defect B). The Devanagari/Telugu tokenisation is pinned byte-for-byte below
because keeping it intact is load-bearing: see the _tokens docstring.
"""

from __future__ import annotations

import pytest

from shared.coverage import (_tokens, measure, script_text, should_fail,
                             should_retry)


def _analysis(names, sections=(), episodes=1):
    """A MasterAnalysis-shaped dict whose episode carries every concept."""
    concepts = [
        {"concept_id": f"c{i + 1:03d}", "name": n, "definition": f"{n} explained."}
        for i, n in enumerate(names)
    ]
    return {
        "chapter_title": "Chapter",
        "concepts": {"concepts": concepts, "dependencies": [], "prerequisites": []},
        "episodes": {
            "chapter_title": "Chapter",
            "total_episodes": episodes,
            "episodes": [{
                "episode_num": 1,
                "title": "Chapter",
                "sections_covered": list(sections),
                "key_concepts_introduced": [c["concept_id"] for c in concepts],
            }],
        },
    }


def _script(*paragraphs):
    """A ChapterScripts-shaped episode: one narration segment per paragraph."""
    return {"segments": [{"type": "explore", "text": p} for p in paragraphs]}


# The incident's shape: 17 concepts extracted, all pooled into one episode's
# denominator. Distinct technical terms plus the shared-head-word siblings.
TOPICS_17 = [
    "Photosynthesis", "Chlorophyll", "Stomata", "Cell wall", "Cell membrane",
    "Chloroplast", "Glucose", "Respiration", "Xylem", "Phloem",
    "Transpiration", "Osmosis", "Diffusion", "Enzyme", "Vacuole",
    "Mitochondria", "Cytoplasm",
]

# A part-3 script: on-topic for ITS part, silent on the other parts' fourteen
# topics — which is what a correct part script looks like.
PART_SCRIPT = _script(
    "Photosynthesis needs chlorophyll, and the chlorophyll sits inside each "
    "chloroplast."
)


def _pooled_episode(analysis):
    """The incident's episode: a single-episode plan that declares no ids —
    what segmenter.build_single_episode leaves behind on every part job."""
    return dict(analysis["episodes"]["episodes"][0], key_concepts_introduced=[])


class TestPartScopedPooling:
    def test_a_pooled_part_report_is_measured_but_cannot_fail(self):
        # The incident row, re-run through the fix: 3 of 17 addressed, the
        # 0.176 recorded for analytics, and no failure — the other 14 topics
        # belong to parts 1, 2 and 4.
        analysis = _analysis(TOPICS_17)
        report = measure(analysis, _pooled_episode(analysis),
                         script_text(PART_SCRIPT), part_scoped=True)
        assert report["pooled"] is True
        assert report["topics"] == 17
        assert report["addressed"] == 3
        assert report["covered"] == pytest.approx(0.176, abs=0.001)
        assert report["verdict"] not in ("short", "floor")
        assert should_fail(report, "warn") is False
        assert should_fail(report, "strict") is False

    def test_the_same_pooled_denominator_still_fails_a_whole_chapter_job(self):
        # part_scoped=False is every existing caller. Pooling there is correct
        # — a legacy single-episode plan really does cover the whole chapter —
        # so a script this thin still hits the floor exactly as before.
        analysis = _analysis(TOPICS_17)
        report = measure(analysis, _pooled_episode(analysis),
                         script_text(PART_SCRIPT))
        assert "pooled" not in report
        assert report["verdict"] == "floor"
        assert should_fail(report, "warn") is True
        assert should_fail(report, "strict") is True

    def test_a_properly_scoped_part_is_still_gated(self):
        # part_scoped must not blind the gate: when the episode DOES carry its
        # own concept ids, the denominator is this part's topics and a script
        # that misses them fails like any other. Eight ids — above
        # _MIN_GATED_TOPICS — of which the script teaches one.
        analysis = _analysis(TOPICS_17)
        episode = dict(analysis["episodes"]["episodes"][0],
                       key_concepts_introduced=[f"c{i:03d}" for i in range(1, 9)])
        report = measure(analysis, episode,
                         script_text(_script("Photosynthesis is where we start.")),
                         part_scoped=True)
        assert "pooled" not in report
        assert report["topics"] == 8
        assert report["verdict"] == "floor"
        assert should_fail(report, "warn") is True
        assert should_retry(report, "warn") is True

    def test_a_multi_episode_plan_is_never_pooled(self):
        # The empty-ids fallback only fires on single-episode plans; a chunked
        # plan with a blank episode scopes to sections only, part_scoped or not.
        analysis = _analysis(TOPICS_17, sections=["Leaf structure"])
        analysis["episodes"]["episodes"].append({
            "episode_num": 2, "title": "Part 2",
            "sections_covered": [], "key_concepts_introduced": [],
        })
        report = measure(analysis, _pooled_episode(analysis),
                         script_text(PART_SCRIPT), part_scoped=True)
        assert "pooled" not in report


class TestRetryDecision:
    # The process-level decision, kept in coverage.py so it is testable
    # without importing worker.process: retry exactly the reports that would
    # otherwise fail outright — and NEVER a pooled one, whose missed list is
    # other parts' topics and whose must_cover retry orders the model to
    # teach material this part does not contain.

    def _pooled_report(self):
        analysis = _analysis(TOPICS_17)
        return measure(analysis, _pooled_episode(analysis),
                       script_text(PART_SCRIPT), part_scoped=True)

    def _failing_report(self):
        analysis = _analysis(TOPICS_17)
        return measure(analysis, analysis["episodes"]["episodes"][0],
                       script_text(PART_SCRIPT))

    def test_a_pooled_report_is_never_retried(self):
        report = self._pooled_report()
        assert should_retry(report, "warn") is False
        assert should_retry(report, "strict") is False

    def test_a_failing_unpooled_report_is_retried(self):
        report = self._failing_report()
        assert should_fail(report, "warn") is True
        assert should_retry(report, "warn") is True

    def test_a_passing_report_is_not_retried(self):
        analysis = _analysis(["Photosynthesis", "Chlorophyll", "Chloroplast"])
        report = measure(analysis, analysis["episodes"]["episodes"][0],
                         script_text(PART_SCRIPT))
        assert report["verdict"] == "ok"
        assert should_retry(report, "strict") is False


class TestArabicFolding:
    def test_persian_kaf_matches_arabic_kaf(self):
        # ک (U+06A9) vs ك (U+0643) — same letter, different codepoint. The
        # analyzer named the topic one way, the script wrote it the other.
        analysis = _analysis(["کاغذ"])
        script = _script("الورق يسمى كاغذ في بعض البلدان.")
        report = measure(analysis, analysis["episodes"]["episodes"][0],
                         script_text(script))
        assert report["covered"] == 1.0

    def test_persian_yeh_matches_arabic_yeh(self):
        # ی (U+06CC) vs ي (U+064A).
        analysis = _analysis(["دانشگاهی"])
        script = _script("این متن دانشگاهي است.")
        report = measure(analysis, analysis["episodes"]["episodes"][0],
                         script_text(script))
        assert report["covered"] == 1.0

    def test_harakat_and_teh_marbuta_fold_away(self):
        # A vocalised topic name "مَدْرَسَة" against the plain "مدرسه" every
        # Persian script writes: harakat stripped, ة → ه.
        analysis = _analysis(["مَدْرَسَة"])
        script = _script("هذه مدرسه كبيرة وجميلة.")
        report = measure(analysis, analysis["episodes"]["episodes"][0],
                         script_text(script))
        assert report["covered"] == 1.0

    def test_urdu_heh_goal_folds_to_arabic_heh(self):
        # ہ (U+06C1) → ه (U+0647).
        assert _tokens("علاقہ") == _tokens("علاقه")

    def test_tatweel_is_stripped(self):
        # U+0640 is category Lm — isalnum() glues it into tokens — but it is
        # pure typography: كـتـاب and كتاب are the same word.
        assert _tokens("كـتـاب") == _tokens("كتاب")


class TestOtherScriptsUntouched:
    # The keep-combining-marks rule is load-bearing for Devanagari and Telugu
    # (see the _tokens docstring); the fold must not move a single byte of
    # their tokenisation. Pinned exactly, not just "still measures 1.0".

    def test_devanagari_tokenisation_is_byte_identical(self):
        assert _tokens("प्रकाश संश्लेषण") == ["प्रकाश", "संश्लेषण"]

    def test_telugu_tokenisation_is_byte_identical(self):
        assert _tokens("కిరణజన్య సంయోగక్రియ") == ["కిరణజన్య", "సంయోగక్రియ"]

    def test_latin_tokenisation_is_unchanged(self):
        assert _tokens("Photosynthesis and respiration") == \
            ["photosynthesi", "and", "respiration"]

    def test_malay_tokenisation_is_unchanged(self):
        assert _tokens("Fotosintesis berlaku dalam daun") == \
            ["fotosintesi", "berlaku", "dalam", "daun"]
