"""catalogue/youtube_meta.py — one title and description structure for every
video, with the words written once by the model and stored on the kit."""

from __future__ import annotations

from datetime import datetime, timezone

from catalogue import youtube_meta as Y
from tests.catalogue_fakes import FakeSB

BOARDS = [("CBSE Science (Class 6-10)", "9"), ("Cambridge Lower Secondary Science 0893", "7")]
SUMMARY = ("Cell membrane, cytoplasm, nucleus, mitochondria, cell wall, chloroplasts and vacuole — "
           "what plant and animal cells share and where they differ.")


class TestAudience:
    def test_board_labels_use_each_boards_own_idiom(self):
        assert Y.board_label("CBSE Science (Class 6-10)", "9") == "CBSE Class 9"
        assert Y.board_label("Cambridge Lower Secondary Science 0893", "7") == "Cambridge Stage 7"
        assert Y.board_label("Ontario Science", "Grade 8") == "Ontario Grade 8"
        assert Y.board_label("IB Middle Years", None) == "IB"
        assert Y.board_label("", "9") == ""

    def test_the_audience_tag_joins_boards_and_ends_with_the_subject(self):
        assert Y.audience_tag(BOARDS, "Science") == "CBSE Class 9 & Cambridge Stage 7 Science"
        assert Y.audience_tag([], "Biology") == "Biology"
        assert Y.audience_tag(BOARDS[:1], None) == "CBSE Class 9"
        assert Y.audience_tag([], "") == ""


class TestTermsAndTags:
    def test_terms_come_from_the_summarys_enumeration_not_its_clause(self):
        assert Y.terms_from_summary(SUMMARY) == ["cell membrane", "cytoplasm", "nucleus", "mitochondria",
                                                 "cell wall", "chloroplasts", "vacuole"]
        assert Y.terms_from_summary("Every living thing is made of cells.") == []

    def test_default_hashtags_are_terms_boards_subject_and_the_channel(self):
        tags = Y.default_hashtags(["plant cell", "animal cell"], BOARDS, "Science")
        assert tags == ["PlantCell", "AnimalCell", "CBSE", "Class9Science", "Cambridge", "Stage7Science",
                        "Science", "SketchCast"]

    def test_hashtags_are_camel_case_alphanumeric_and_distinct(self):
        assert Y.clean_hashtags(["#plant cell", "Plant-Cell", "cbse", "x" * 50, "", 7]) == ["PlantCell", "Cbse"]


class TestTitle:
    def test_topic_terms_audience_in_that_order(self):
        t = Y.compose_title("Photosynthesis", key_terms=["chlorophyll", "glucose", "stomata"],
                            audience="CBSE Class 10 Science")
        assert t == "Photosynthesis Explained | Chlorophyll, Glucose, Stomata | CBSE Class 10 Science"

    def test_terms_are_dropped_before_the_audience_when_the_title_is_long(self):
        aud = Y.audience_tag(BOARDS, "Science")
        t = Y.compose_title("Plant and Animal Cells Compared", key_terms=["prokaryotes", "eukaryotes", "organelles"],
                            audience=aud)
        assert len(t) <= Y.TITLE_MAX
        assert t.endswith(" | " + aud)
        assert "Organelles" not in t and "Prokaryotes, Eukaryotes" in t

    def test_a_topic_ending_on_a_participle_does_not_stutter(self):
        assert Y.headline("Plant and Animal Cells Compared") == "Plant and Animal Cells Compared"
        assert Y.headline("Photosynthesis") == "Photosynthesis Explained"
        assert Y.headline("The Seed") == "The Seed Explained"        # 'seed' is not a participle

    def test_the_part_label_is_never_cut(self):
        long = "The Structure and Function of Eukaryotic and Prokaryotic Cells in Living Organisms Everywhere"
        t = Y.compose_title(long, key_terms=["a", "b"], audience="CBSE Class 9 Science", part=2, total=3)
        assert len(t) <= Y.TITLE_MAX and t.endswith(" — Part 2 of 3")

    def test_a_stored_title_wins_and_still_takes_the_part_label(self):
        t = Y.compose_title("Cells", meta={"title": "Cells for Beginners | Nucleus | CBSE Class 9 Science"},
                            key_terms=["x"], audience="ignored", part=2, total=2)
        assert t == "Cells for Beginners | Nucleus | CBSE Class 9 Science — Part 2 of 2"

    def test_no_terms_and_no_audience_is_the_bare_topic(self):
        assert Y.compose_title("Cells") == "Cells"


def _hhmmss(t):
    t = int(t or 0)
    return f"{t // 60}:{t % 60:02d}"


class TestDescription:
    def _desc(self, meta=None, total=1, part=1, chapters=None):
        chapters = chapters if chapters is not None else [{"t": 0, "label": "Intro"}, {"t": 32, "label": "Two cells"},
                                                          {"t": 90, "label": "Wrap up"}]
        terms = Y.effective_terms(meta, SUMMARY)
        return Y.compose_description(
            topic_title="Plant and Animal Cells Compared", summary=SUMMARY, meta=meta,
            header_lines=["CBSE Science (Class 6-10) · Class 9 · Cell - Basic Unit of life"],
            chapter_lines_=Y.chapter_lines(chapters, _hhmmss), key_terms=terms,
            hashtags=Y.effective_hashtags(meta, terms, BOARDS, "Science"), part=part, total=total,
            next_title="Next one" if part < total else None, link="https://sketchcast.app/?utm_source=youtube")

    def test_the_blocks_in_order(self):
        blocks = self._desc().split("\n\n")
        assert blocks[0] == SUMMARY                       # no stored intro: the summary opens
        assert blocks[1] == "Aligned to\nCBSE Science (Class 6-10) · Class 9 · Cell - Basic Unit of life"
        assert blocks[2] == "Chapters\n0:00 Intro\n0:32 Two cells\n1:30 Wrap up"
        assert blocks[3].startswith("Key terms: cell membrane, cytoplasm, nucleus")
        from shared.outro import DESCRIPTION_CTA
        assert blocks[4] == DESCRIPTION_CTA
        assert blocks[5] == Y.SKETCHCAST_LINE + "\nhttps://sketchcast.app/?utm_source=youtube"
        assert blocks[6].startswith("#CellMembrane #Cytoplasm") and blocks[6].endswith("#Science #SketchCast")

    def test_the_stored_intro_terms_and_tags_replace_the_defaults(self):
        meta = {"intro": "What do a plant cell and an animal cell share?", "key_terms": ["nucleus"], "hashtags": ["Cells"]}
        blocks = self._desc(meta).split("\n\n")
        assert blocks[0] == "What do a plant cell and an animal cell share?"
        assert "Key terms: nucleus." in blocks
        assert blocks[-1] == "#Cells"

    def test_a_multi_part_kit_points_at_the_next_part(self):
        assert "Part 1 of 2. Next: Next one" in self._desc(total=2, part=1)
        assert "Part 2 of 2." in self._desc(total=2, part=2) and "Next:" not in self._desc(total=2, part=2)

    def test_a_short_chapter_list_is_omitted_whole(self):
        assert "Chapters" not in self._desc(chapters=[{"t": 0, "label": "Only one"}])

    def test_never_over_youtubes_limit(self):
        meta = {"intro": "x" * 5000}
        assert len(self._desc(meta)) <= Y.DESCRIPTION_MAX


class TestCleanMeta:
    def test_lenient_and_bounded(self):
        m = Y.clean_meta({"title": "  T ", "intro": " a\n b ", "key_terms": ["Nucleus", "", "a sentence that is far too long to be a term"],
                          "hashtags": ["#one two", "one-two"], "extra": 1})
        assert m == {"title": "T", "intro": "a b", "key_terms": ["nucleus"], "hashtags": ["OneTwo"]}
        assert Y.clean_meta({"title": "", "key_terms": []}) is None
        assert Y.clean_meta("junk") is None


class FakeClient:
    def __init__(self, data):
        self.data, self.prompts = data, []

    def analyze(self, prompt, system=None, max_tokens=None, response_schema=None):
        self.prompts.append(prompt)
        return {"data": self.data, "usage": {}, "truncated": False}


TOPIC = {"id": "t1", "title": "Plant and Animal Cells Compared", "subject": "Science", "summary": SUMMARY}


class TestGenerate:
    def test_the_models_words_are_cleaned_and_tagged_generated(self):
        client = FakeClient({"title": "Plant vs Animal Cells Explained | Prokaryotes, Eukaryotes | CBSE Class 9 & Cambridge Stage 7 Science",
                             "intro": "What do they share? A teacher and a student find out.",
                             "key_terms": ["prokaryotes", "eukaryotes"], "hashtags": ["PlantCell", "#SketchCast"]})
        meta = Y.generate_meta(client, TOPIC, ["CBSE Science (Class 6-10) · Class 9 · Cell"], BOARDS, ["Intro"],
                               "A cell is the basic unit.", now=datetime(2026, 9, 21, tzinfo=timezone.utc))
        assert meta["title"].startswith("Plant vs Animal Cells Explained")
        assert meta["hashtags"] == ["PlantCell", "SketchCast"]
        assert meta["source"] == "generated" and meta["generated_at"].startswith("2026-09-21")
        prompt = client.prompts[0]
        assert "<narration>\nA cell is the basic unit.\n</narration>" in prompt
        assert "CBSE Class 9 & Cambridge Stage 7 Science" in prompt

    def test_a_title_that_lost_the_audience_block_gets_it_back(self):
        client = FakeClient({"title": "Cells!!!", "intro": "i", "key_terms": ["nucleus", "cell wall"], "hashtags": ["A"]})
        meta = Y.generate_meta(client, TOPIC, [], BOARDS, [], "narration")
        assert meta["title"] == "Plant and Animal Cells Compared | Nucleus, Cell wall | CBSE Class 9 & Cambridge Stage 7 Science"

    def test_nothing_usable_is_none(self):
        assert Y.generate_meta(FakeClient({"title": "", "intro": "", "key_terms": [], "hashtags": []}), TOPIC, [], [], [], "n") is None
        assert Y.generate_meta(FakeClient("garbage"), TOPIC, [], [], [], "n") is None


class TestWriteForKit:
    def _sb(self):
        sb = FakeSB()
        sb.tables["topics"] = [dict(TOPIC)]
        sb.tables["topic_kits"] = [{"id": "k1", "topic_id": "t1", "language": "en", "status": "generating"}]
        sb.tables["curricula"] = [{"id": "c1", "code": "cbse_science_086", "name": "CBSE Science (Class 6-10)"}]
        sb.tables["curriculum_nodes"] = [{"id": "n1", "curriculum_id": "c1", "code": "cbse:9:U2:01", "kind": "topic",
                                          "grade": "9", "title": "Cell - Basic Unit of life"}]
        sb.tables["topic_curriculum_map"] = [{"topic_id": "t1", "node_id": "n1", "coverage": "full"}]
        return sb

    def test_writes_the_words_onto_the_kit(self):
        sb = self._sb()
        client = FakeClient({"title": "Cells Explained | Nucleus | CBSE Class 9 Science", "intro": "i",
                             "key_terms": ["nucleus"], "hashtags": ["Cells"]})
        kit = sb.tables["topic_kits"][0]
        parts = [{"part": 1, "chapters": [{"t": 0, "label": "Intro"}], "narration": "A cell is the basic unit."}]
        meta = Y.write_for_kit(sb, kit, parts, client_factory=lambda: client)
        assert meta["title"] == "Cells Explained | Nucleus | CBSE Class 9 Science"
        assert sb.tables["topic_kits"][0]["youtube_meta"]["source"] == "generated"
        assert "Intro" in client.prompts[0] and "CBSE Class 9 Science" in client.prompts[0]

    def test_a_kit_with_no_narration_or_no_topic_writes_nothing_and_never_raises(self):
        sb = self._sb()
        kit = sb.tables["topic_kits"][0]
        assert Y.write_for_kit(sb, kit, [{"part": 1, "chapters": []}], client_factory=lambda: FakeClient({})) is None
        assert "youtube_meta" not in sb.tables["topic_kits"][0]
        sb.tables["topics"] = []
        assert Y.write_for_kit(sb, kit, [{"part": 1, "narration": "x"}], client_factory=lambda: FakeClient({})) is None

    def test_a_model_fault_is_swallowed(self):
        sb = self._sb()

        class Boom:
            def analyze(self, *a, **k):
                raise RuntimeError("down")

        assert Y.write_for_kit(sb, sb.tables["topic_kits"][0], [{"part": 1, "narration": "x"}],
                               client_factory=lambda: Boom()) is None
