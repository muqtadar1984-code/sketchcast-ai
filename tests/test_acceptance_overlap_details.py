"""A failed lesson's overlap pairs ride on its generation row with what
each element says — the chapter-17 probe (2026-10-08) left them in a log
that lagged the worker by 12 minutes."""

from worker.process import overlap_details


def test_overlap_pairs_resolve_to_the_elements_texts():
    report = {"overlapping_text": ["s004: TEXT_OVERLAP w1+n5", "s004: TEXT_OVERLAP fig_t3+fig_t9",
                                   "s007: TEXT_OVERLAP q0+n2"]}
    script = {"episodes": [{"segments": [
        {"id": "s004", "scene": {"elements": [
            {"id": "w1", "type": "math", "expr": "a = (1 + 7)/2"},
            {"id": "n5", "type": "text", "text": "the midpoint averages the coordinates"},
            {"id": "fig_t3", "type": "text", "text": "B"},
            {"id": "fig_t9", "type": "text", "text": "(7, 6)"}]}},
        {"id": "s007", "scene": {"elements": [{"id": "q0", "type": "text", "text": "Find x."}]}},
    ]}]}
    got = overlap_details(report, script)
    assert got == [
        {"scene": "s004", "a": "w1", "a_text": "a = (1 + 7)/2", "b": "n5",
         "b_text": "the midpoint averages the coordinates"},
        {"scene": "s004", "a": "fig_t3", "a_text": "B", "b": "fig_t9", "b_text": "(7, 6)"},
        {"scene": "s007", "a": "q0", "a_text": "Find x.", "b": "n2", "b_text": ""},   # an unknown id: no text, no crash
    ]


def test_overlap_details_survive_a_missing_script_and_cap_the_list():
    report = {"overlapping_text": [f"s001: TEXT_OVERLAP a{i}+b{i}" for i in range(60)]}
    assert overlap_details(report, None) [0] == {"scene": "s001", "a": "a0", "a_text": "", "b": "b0", "b_text": ""}
    assert len(overlap_details(report, {})) == 40
    assert overlap_details({}, {"episodes": []}) == []
