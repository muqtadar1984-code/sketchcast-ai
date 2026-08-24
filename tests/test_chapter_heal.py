"""Self-healing chapter relocation for mislabeled scanned books.

Regression for the user-reported failure: a scanned Cambridge Computing book was
indexed with "Unit 3: Computer storage" pointing at the networking/IP pages,
because the vision detector copied the printed contents-page number (34) and used
it as a physical page index. These tests pin the fix in four layers:

* detection reports openers by IMAGE POSITION, never a printed number (and rejects
  an out-of-range number outright);
* the verifier actually checks descriptive "Unit N: <topic>" titles, and its
  strict mode confirms the PRIMARY topic;
* the index-time audit can SEE a scanned book (vision snippets);
* index-time + generation-time self-heal relocate a mislabeled chapter to the
  pages that match its title — without ever corrupting the stored list.
"""

from __future__ import annotations

from pathlib import Path

import agent1_ingestion.vision_chapters as vc
from agent1_ingestion import chapter_check
from agent1_ingestion.chapter_check import audit_chapter_list, verify_chapter_content


class Ext:
    """Minimal ExtractionResult stand-in."""

    def __init__(self, total_pages=88, items=None):
        self.total_pages = total_pages
        self.items = items or []  # empty items => scanned (no text layer)


def _fake_render(pdf, pages, width, out_dir):
    """Render nothing — just hand back one Path per page, named p{page}.jpg so the
    fake client can recover which physical pages a batch covered."""
    return [Path(f"p{p:04d}.jpg") for p in pages]


def _page_of(path) -> int:
    return int(Path(path).stem[1:])


# ── Layer A: detection reports physical positions, not printed numbers ──────────

class _DetectClient:
    """analyze_images_batch answers with openers by IMAGE NUMBER. `openers` maps a
    physical page -> title; a page in the batch becomes image_number = its offset."""

    def __init__(self, openers, leak_number=None):
        self.openers = openers
        self.leak_number = leak_number  # simulate the model copying a printed page no.

    def analyze_images_batch(self, paths, prompt, max_tokens=0, **k):
        pages = [_page_of(p) for p in paths]
        items = []
        for i, pg in enumerate(pages):
            if pg in self.openers:
                num = self.leak_number if self.leak_number is not None else i + 1
                items.append({"image_number": num, "title": self.openers[pg]})
        return {"data": {"openers": items}}


def test_detect_maps_image_position_to_physical_page(monkeypatch):
    monkeypatch.setattr(vc, "_render_pages", _fake_render)
    # Ground truth from Mona's book: Unit 3 "Computer storage" physically opens at
    # 0-idx 18, NOT the printed page 34.
    openers = {5: "Be a designer", 11: "Be a data storyteller", 18: "Computer storage",
               23: "Be a storyteller", 30: "Network devices and websites"}
    defs = vc.detect_chapters_vision("x.pdf", 88, _DetectClient(openers))
    by_title = {d["title"]: d for d in defs}
    assert by_title["Computer storage"]["start_page"] == 18
    assert by_title["Computer storage"]["end_page"] == 22  # up to Unit 4 @23
    # strictly ascending, no overlaps
    starts = [d["start_page"] for d in defs]
    assert starts == sorted(starts)


def test_detect_rejects_printed_number_leak(monkeypatch):
    monkeypatch.setattr(vc, "_render_pages", _fake_render)
    # The exact bug: the model returns "34" (a printed page number) for the
    # Computer storage opener. 34 is outside the batch's 1..N image range, so it is
    # dropped instead of becoming physical page 33 (the networking pages).
    openers = {18: "Computer storage", 5: "Be a designer"}
    defs = vc.detect_chapters_vision("x.pdf", 88, _DetectClient(openers, leak_number=34))
    # The leaked "Computer storage" is rejected; it must NOT appear at 33.
    assert all(d["start_page"] != 33 for d in defs)
    assert all(d["title"] != "Computer storage" for d in defs)


def test_parse_openers_range_guard():
    # in range -> kept (mapped to the actual rendered page); out of range
    # (printed number) -> dropped.
    data = {"openers": [{"image_number": 3, "title": "A"}, {"image_number": 99, "title": "B"}]}
    pairs = vc._parse_openers(data, list(range(20, 40)))
    # A reply without a "kind" field degrades to "unit" (pre-apparatus shape).
    assert pairs == [(22, "A", "unit")]


# ── Layer B: verifier catches descriptive titles; strict confirms primary topic ─

class _VerifyClient:
    """Judges match on the PAGE TEXT only (not the label/topic, which naturally
    contains the word 'storage'): storage words present + no networking words."""

    def analyze(self, prompt, max_tokens=0, **k):
        text = prompt.split("(opening):", 1)[1].lower() if "(opening):" in prompt else prompt.lower()
        networking = "ip address" in text or "network" in text or "packet" in text
        has_storage = any(w in text for w in ("bit", "byte", "hard drive", "ascii", "ssd"))
        match = has_storage and not networking
        return {"data": {"match": match,
                         "actual_topic": "computer storage" if match else "network / IP addresses"}}


def test_verify_skips_bare_generic_title():
    ok, actual = verify_chapter_content("Unit 3", "anything about IP addresses", _VerifyClient())
    assert ok is True and actual == ""  # nothing descriptive to verify


def test_verify_catches_descriptive_mislabel():
    # The reported bug: "Unit 3: Computer storage" over networking text must FAIL.
    ok, actual = verify_chapter_content(
        "Unit 3: Computer storage", "How do IP addresses route packets across a network?",
        _VerifyClient(),
    )
    assert ok is False and "network" in actual.lower()


def test_verify_passes_matching_content():
    ok, _ = verify_chapter_content(
        "Unit 3: Computer storage", "Storage measures data in bits and bytes; hard drives...",
        _VerifyClient(),
    )
    assert ok is True


def test_verify_strict_requires_primary_topic():
    # strict prompt asks for PRIMARY topic; a networking page that merely name-drops
    # "network-attached storage" is still networking -> reject.
    class Strict:
        def analyze(self, prompt, max_tokens=0, **k):
            assert "PRIMARILY teaching" in prompt  # strict path taken
            return {"data": {"match": False, "actual_topic": "networking"}}

    ok, _ = verify_chapter_content("Unit 3: Computer storage", "network-attached storage is a device on a network", Strict(), strict=True)
    assert ok is False


# ── Layer B: audit gets vision eyes on a scanned book ───────────────────────────

class _AuditClient:
    def analyze(self, prompt, max_tokens=0, **k):
        # Flag the chapter whose opening_text is about networking under a storage title.
        issues = []
        if "Computer storage" in prompt and "network" in prompt.lower():
            issues.append({"num": 2})
        return {"data": {"issues": issues}}


def test_audit_flags_scanned_mismatch_via_snippets():
    chapters = [
        {"chapter_num": 2, "title": "Computer storage", "start_page": 33, "end_page": 42},
        {"chapter_num": 3, "title": "Be a storyteller", "start_page": 43, "end_page": 50},
    ]
    # No text layer: without snippets the audit is blind (opening_text empty).
    blind = audit_chapter_list(Ext(items=[]), chapters, _AuditClient())
    assert blind["mismatched"] == []
    # With vision snippets it can finally judge — and flags the mislabel.
    snippets = {2: "IP addresses and network devices", 3: "creating stories"}
    seeing = audit_chapter_list(Ext(items=[]), chapters, _AuditClient(), snippets=snippets)
    assert 2 in seeing["mismatched"]


# ── Layer B/C: match a title to a detected unit ─────────────────────────────────

class _MatchClient:
    def __init__(self, index):
        self.index = index

    def analyze(self, prompt, max_tokens=0, **k):
        return {"data": {"index": self.index}}


def test_match_title_to_units_picks_and_filters():
    units = [
        {"title": "Network devices and websites", "start_page": 30, "end_page": 40},
        {"title": "Computer storage", "start_page": 18, "end_page": 22},
    ]
    got = vc.match_title_to_units("Unit 3: Computer storage", units, _MatchClient(1))
    assert got["start_page"] == 18
    # avoid_starts removes a candidate BEFORE indexing, so index 0 now points at the
    # remaining unit — proves duplicates already used are excluded.
    got2 = vc.match_title_to_units("x", units, _MatchClient(0), avoid_starts={30})
    assert got2["start_page"] == 18
    # -1 => no match
    assert vc.match_title_to_units("x", units, _MatchClient(-1)) is None


# ── Layer B: index-time heal relocates, preserves nums, validates-or-reverts ────

def _mona_stored():
    return [
        {"chapter_num": 0, "title": "Be a designer", "start_page": 5, "end_page": 10},
        {"chapter_num": 1, "title": "Be a data storyteller", "start_page": 11, "end_page": 17},
        {"chapter_num": 2, "title": "Computer storage", "start_page": 33, "end_page": 42},  # WRONG
        {"chapter_num": 3, "title": "Be a storyteller", "start_page": 43, "end_page": 50},
    ]


def _correct_units():
    return [
        {"chapter_num": 0, "title": "Be a designer", "start_page": 5, "end_page": 10},
        {"chapter_num": 1, "title": "Be a data storyteller", "start_page": 11, "end_page": 17},
        {"chapter_num": 2, "title": "Computer storage", "start_page": 18, "end_page": 22},
        {"chapter_num": 3, "title": "Be a storyteller", "start_page": 23, "end_page": 29},
        {"chapter_num": 4, "title": "Network devices and websites", "start_page": 30, "end_page": 87},
    ]


def _patch_heal(monkeypatch, units, mismatched=(2,)):
    monkeypatch.setattr(vc, "extraction_has_text", lambda e: False)
    monkeypatch.setattr(vc, "chapter_opening_snippets_vision", lambda p, ch, c: {2: "IP addresses"})
    monkeypatch.setattr(vc, "audit_chapter_list",
                        lambda ext, ch, c, snippets=None: {"mismatched": list(mismatched), "titles": {}})
    monkeypatch.setattr(vc, "detect_chapters_vision", lambda p, tp, c: units)

    def _match(title, us, client, avoid_starts=None, near_page=None):
        for u in us:
            if u["title"] == title and u["start_page"] not in (avoid_starts or set()):
                return u
        return None

    monkeypatch.setattr(vc, "match_title_to_units", _match)
    # Orchestration tests exercise relocate/validate/clamp logic; the destination
    # strict-confirm is covered separately (test_confirm_relocation_*).
    monkeypatch.setattr(vc, "_confirm_relocation", lambda *a, **k: True)


def test_index_heal_relocates_and_preserves_num(monkeypatch):
    _patch_heal(monkeypatch, _correct_units())
    healed, relocated = vc.heal_chapter_boundaries("x.pdf", Ext(), _mona_stored(), object())
    u3 = next(c for c in healed if c["chapter_num"] == 2)
    assert relocated == [2]
    assert (u3["start_page"], u3["end_page"]) == (18, 22)
    assert u3["title"] == "Computer storage"  # title + num preserved, only pages moved
    assert vc._validate_chapter_list(healed, 88)


def test_index_heal_reverts_when_detection_empty(monkeypatch):
    _patch_heal(monkeypatch, [])  # detection finds nothing
    healed, relocated = vc.heal_chapter_boundaries("x.pdf", Ext(), _mona_stored(), object())
    assert relocated == []
    assert next(c for c in healed if c["chapter_num"] == 2)["start_page"] == 33  # untouched


def test_index_heal_no_flags_is_noop(monkeypatch):
    _patch_heal(monkeypatch, _correct_units(), mismatched=())
    stored = _mona_stored()
    healed, relocated = vc.heal_chapter_boundaries("x.pdf", Ext(), stored, object())
    assert relocated == []
    assert healed == stored  # nothing flagged, nothing moved


# ── validate + clamp guards ─────────────────────────────────────────────────────

def test_validate_rejects_corruption():
    assert not vc._validate_chapter_list(
        [{"chapter_num": 0, "title": "a", "start_page": 5, "end_page": 4}], 10)  # end<start
    assert not vc._validate_chapter_list([
        {"chapter_num": 0, "title": "a", "start_page": 0, "end_page": 3},
        {"chapter_num": 1, "title": "b", "start_page": 0, "end_page": 9},  # dup start
    ], 10)
    assert vc._validate_chapter_list([
        {"chapter_num": 0, "title": "a", "start_page": 0, "end_page": 4},
        {"chapter_num": 1, "title": "b", "start_page": 5, "end_page": 9},
    ], 10)


def test_clamp_uses_only_trusted_anchors():
    ch = [
        {"chapter_num": 0, "title": "a", "start_page": 0, "end_page": 40},  # overruns next
        {"chapter_num": 1, "title": "b", "start_page": 5, "end_page": 9},
    ]
    vc._clamp_overlaps(ch, 10, {0, 5})  # both trusted
    assert ch[0]["end_page"] == 4 and ch[0]["chapter_num"] == 0  # clamped, not reordered


def test_clamp_suspect_does_not_truncate_relocation():
    # finding 4: a SUSPECT neighbor with a stale start (20) must NOT truncate a
    # confirmed relocation (18-22) down to a sliver.
    ch = [
        {"chapter_num": 2, "title": "storage", "start_page": 18, "end_page": 22},  # relocated (trusted)
        {"chapter_num": 0, "title": "?", "start_page": 20, "end_page": 25},          # suspect (stale)
    ]
    vc._clamp_overlaps(ch, 30, {18})  # only the relocation is a trusted anchor
    reloc = next(c for c in ch if c["chapter_num"] == 2)
    assert reloc["end_page"] == 22  # NOT shrunk to 19 by the suspect@20


def test_confirm_relocation_gates_on_destination_content(monkeypatch):
    # finding 3: index-time relocation must strict-confirm the DESTINATION pages.
    def snip(pdf, chs, client):
        sp = chs[0].get("start_page")
        return {chs[0].get("chapter_num", 0): "storage bits bytes hard drive" if sp == 18
                else "IP addresses and networks"}

    monkeypatch.setattr(vc, "chapter_opening_snippets_vision", snip)
    ext = Ext()
    # destination that really is storage -> confirmed
    assert vc._confirm_relocation("x.pdf", ext, "Unit 3: Computer storage", 18, 22, _VerifyClient(), True) is True
    # destination that is networking -> rejected (keeps chapter where it is)
    assert vc._confirm_relocation("x.pdf", ext, "Unit 3: Computer storage", 30, 40, _VerifyClient(), True) is False
    # unreadable destination -> refuse
    monkeypatch.setattr(vc, "chapter_opening_snippets_vision", lambda p, c, cl: {})
    assert vc._confirm_relocation("x.pdf", ext, "Unit 3: Computer storage", 18, 22, _VerifyClient(), True) is False


# ── Layer C: generation-time relocation confirms before committing ──────────────

def test_relocate_for_generation_confirms(monkeypatch):
    monkeypatch.setattr(vc, "extraction_has_text", lambda e: False)
    monkeypatch.setattr(vc, "detect_chapters_vision", lambda p, tp, c: _correct_units())
    monkeypatch.setattr(vc, "match_title_to_units",
                        lambda title, us, client, avoid_starts=None, near_page=None:
                        next((u for u in us if u["title"] == "Computer storage"
                              and u["start_page"] not in (avoid_starts or set())), None))
    monkeypatch.setattr(vc, "chapter_text_vision",
                        lambda pdf, s, e, c: "Storage is measured in bits and bytes; hard drives, SSDs, ASCII.")
    requested = {"chapter_num": 2, "title": "Computer storage", "start_page": 33, "end_page": 42}
    out = vc.relocate_chapter_for_generation("x.pdf", Ext(), requested, _VerifyClient())
    assert out["status"] == "ok"
    assert (out["start_page"], out["end_page"]) == (18, 22)
    assert "storage" in out["source_text"].lower()


def test_relocate_for_generation_gives_up_when_absent(monkeypatch):
    # Every candidate transcribes to networking text -> strict verify rejects both ->
    # None (caller persists 'not_found' and fails loud).
    monkeypatch.setattr(vc, "extraction_has_text", lambda e: False)
    monkeypatch.setattr(vc, "detect_chapters_vision", lambda p, tp, c: _correct_units())
    seen = {"n": 0}

    def _match(title, us, client, avoid_starts=None, near_page=None):
        # hand back a different networking unit each attempt, then None
        opts = [u for u in us if u["start_page"] in (30, 40) and u["start_page"] not in (avoid_starts or set())]
        return opts[0] if opts else None

    monkeypatch.setattr(vc, "match_title_to_units", _match)
    monkeypatch.setattr(vc, "chapter_text_vision", lambda pdf, s, e, c: "IP addresses and network packets")
    requested = {"chapter_num": 2, "title": "Computer storage", "start_page": 33, "end_page": 42}
    out = vc.relocate_chapter_for_generation("x.pdf", Ext(), requested, _VerifyClient())
    # We read real content and strict-rejected it -> PROVEN absent (safe to remember).
    assert out["status"] == "absent"


def test_relocate_incomplete_on_detection_outage(monkeypatch):
    # finding 1: a transient vision outage (detection returns []) must NOT be
    # reported as 'absent' — that would let the caller persist a permanent
    # not_found and brick a chapter that is actually present.
    monkeypatch.setattr(vc, "extraction_has_text", lambda e: False)
    monkeypatch.setattr(vc, "detect_chapters_vision", lambda p, tp, c: [])
    requested = {"chapter_num": 2, "title": "Computer storage", "start_page": 33, "end_page": 42}
    out = vc.relocate_chapter_for_generation("x.pdf", Ext(), requested, _VerifyClient())
    assert out["status"] == "incomplete"


def test_index_heal_refuses_a_hole_opening_relocation(monkeypatch):
    # Sara Junaidi's book (e0459f87, 2026-08-23): the heal relocated a chapter
    # owning pages 22-61 onto a 4-page vision unit; _clamp_overlaps shrank it
    # to 22-25 while the next chapter still started at 62, and pages 26-61 —
    # 36 pages — fell out of the book. _validate_chapter_list cannot see a
    # hole (non-contiguity is structurally valid), so the heal must re-measure
    # what it LEAVES BEHIND and refuse a relocation that uncovers a material
    # share of the book — a suspect label is recoverable at generation time; a
    # hole is not.
    stored = [
        {"chapter_num": 0, "title": "Introduction", "start_page": 0, "end_page": 21},
        {"chapter_num": 1, "title": "Paper chromatography", "start_page": 22, "end_page": 61},
        {"chapter_num": 2, "title": "Forces", "start_page": 62, "end_page": 87},
    ]
    tiny_unit = [{"chapter_num": 0, "title": "Paper chromatography", "start_page": 22, "end_page": 25}]
    _patch_heal(monkeypatch, tiny_unit, mismatched=(1,))
    healed, relocated = vc.heal_chapter_boundaries("x.pdf", Ext(), stored, object())
    assert relocated == []
    flagged = next(c for c in healed if c["chapter_num"] == 1)
    # Pages kept (no hole), suspicion kept (health gates on the marker).
    assert (flagged["start_page"], flagged["end_page"]) == (22, 61)
    assert flagged["relocation"] == "suspect"


def test_index_heal_still_accepts_a_small_legitimate_loss(monkeypatch):
    # The other side of the bound, pinned so the refusal cannot creep: the
    # Mona relocation (chapter moved OFF pages it never owned) loses 5 of 88
    # pages and must keep working exactly as test_index_heal_relocates pins.
    _patch_heal(monkeypatch, _correct_units())
    healed, relocated = vc.heal_chapter_boundaries("x.pdf", Ext(), _mona_stored(), object())
    assert relocated == [2]


def test_relocate_incomplete_on_empty_ocr(monkeypatch):
    # OCR/read failure (empty transcription) is not evidence of absence either.
    monkeypatch.setattr(vc, "extraction_has_text", lambda e: False)
    monkeypatch.setattr(vc, "detect_chapters_vision", lambda p, tp, c: _correct_units())
    monkeypatch.setattr(vc, "match_title_to_units",
                        lambda title, us, client, avoid_starts=None, near_page=None:
                        next((u for u in us if u["start_page"] not in (avoid_starts or set())), None))
    monkeypatch.setattr(vc, "chapter_text_vision", lambda pdf, s, e, c: "")  # OCR failed
    requested = {"chapter_num": 2, "title": "Computer storage", "start_page": 33, "end_page": 42}
    out = vc.relocate_chapter_for_generation("x.pdf", Ext(), requested, _VerifyClient())
    assert out["status"] == "incomplete"
