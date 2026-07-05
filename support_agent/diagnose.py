"""Diagnosis: one structured Sonnet-5 call over the bundle, plus a re-run of
the chapter validation gate as a concrete signal for wrong-content cases."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

DIAGNOSIS_MODEL = "claude-sonnet-5"

CATEGORIES = [
    "scanned_low_confidence",   # transcription too weak to teach from
    "detection_split",          # headers found but chapter split failed/degenerate
    "corrupt_pdf",              # unreadable / password-protected / too short
    "chapter_out_of_range",     # requested chapter doesn't exist in this book
    "transient_error",          # model/API/infra hiccup — retryable
    "wrong_chapter_detection",  # stored boundaries point at the wrong pages
    "wrong_chapter_slicing",    # boundaries fine, wrong slice reached the pipeline
    "generation_drift",         # source correct, model drifted off-topic
    "unknown",
]

ACTIONS = ["retry_transient", "user_fix", "reindex_regenerate", "escalate", "none"]


def _gate_signals(bundle: dict, client) -> dict:
    """Re-run the validation gate as a diagnostic: does the SOURCE slice match
    the requested title, and does the ARTIFACT match it? The pair separates
    detection/slicing errors (source wrong) from generation drift (source
    right, artifact wrong)."""
    from agent1_ingestion.chapter_check import verify_chapter_content

    chapters = bundle.get("chapters") or []
    want = None
    try:
        want = int(str((bundle.get("generation") or {}).get("chapter_ref")).strip())
    except (ValueError, TypeError):
        pass
    title = next((c.get("title") for c in chapters if c.get("num") == want), None) or ""

    out: dict = {"requested_title": title}
    src = bundle.get("source_text") or ""
    art = bundle.get("artifact_text") or ""
    if title and src:
        ok, actual = verify_chapter_content(title, src, client)
        out["source_matches_title"] = ok
        if not ok:
            out["source_reads_as"] = actual
    if title and art:
        ok, actual = verify_chapter_content(title, art, client)
        out["artifact_matches_title"] = ok
        if not ok:
            out["artifact_reads_as"] = actual
    return out


def diagnose(client, bundle: dict) -> dict:
    """Returns {category, confidence, user_message, staff_note,
    recommended_action} — validated against the known vocabularies."""
    signals = _gate_signals(bundle, client)

    prompt = (
        "You are SketchCast's support-diagnosis agent. A teacher/parent hit a problem "
        "with an AI-generated lesson. Given the diagnostic bundle, classify the root "
        "cause and recommend ONE action.\n\n"
        f"CATEGORIES: {', '.join(CATEGORIES)}\n"
        f"ACTIONS: {', '.join(ACTIONS)}\n"
        "Action guidance: transient_error → retry_transient; corrupt_pdf / "
        "scanned_low_confidence / chapter_out_of_range → user_fix (tell them exactly "
        "what to do, e.g. the valid chapter range or 'upload a clearer scan'); "
        "wrong_chapter_detection / wrong_chapter_slicing → reindex_regenerate; "
        "generation_drift or anything you are not confident about → escalate.\n\n"
        "user_message: 2-3 plain sentences for a teacher/parent — honest, no jargon, "
        "never promise something the action doesn't do.\n"
        "staff_note: 1-2 technical sentences for SketchCast staff.\n\n"
        "Respond ONLY as JSON: {\"category\": ..., \"confidence\": 0.0-1.0, "
        "\"user_message\": ..., \"staff_note\": ..., \"recommended_action\": ...}\n\n"
        f"VALIDATION-GATE SIGNALS (already re-run for you): {signals}\n\n"
        f"BUNDLE: {_compact(bundle)}"
    )
    data = client.analyze(prompt, max_tokens=700).get("data") or {}

    category = data.get("category") if data.get("category") in CATEGORIES else "unknown"
    action = data.get("recommended_action") if data.get("recommended_action") in ACTIONS else "escalate"
    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0.0

    # Ground-truth override: the validation gate RE-RAN the check and concretely
    # found the requested chapter's stored slice does NOT match its title, while
    # the artifact does match (or there is no artifact) — that is a
    # detection/slicing error the reindex path fixes deterministically. Don't
    # leave the auto-fix to model timidity: route to reindex_regenerate (the
    # downstream guards still gate the mutation — post-reindex verify, assigned
    # → pending, loop cap, rollback). Never override toward regenerate when the
    # ARTIFACT is what mismatches with a good source (that's drift → escalate).
    source_wrong = signals.get("source_matches_title") is False
    artifact_ok = signals.get("artifact_matches_title") is not False
    if source_wrong and artifact_ok:
        action = "reindex_regenerate"
        if category not in ("wrong_chapter_detection", "wrong_chapter_slicing"):
            category = "wrong_chapter_slicing"
        confidence = max(confidence, 0.85)  # the gate confirmed it

    return {
        "category": category,
        "confidence": confidence,
        "user_message": str(data.get("user_message") or "We are looking into this.")[:600],
        "staff_note": str(data.get("staff_note") or "")[:600],
        "recommended_action": action,
        "gate_signals": signals,
    }


def _compact(bundle: dict) -> str:
    """Bundle → prompt text, with the long fields clearly labeled."""
    parts = []
    for key in ("issue", "generation", "book", "source_meta", "recent_jobs", "assigned_to_students"):
        if bundle.get(key) is not None:
            parts.append(f"{key}: {bundle[key]}")
    chapters = bundle.get("chapters") or []
    parts.append("detected_chapters: " + "; ".join(f"#{c.get('num')} {c.get('title')}" for c in chapters[:20]))
    if bundle.get("source_text"):
        parts.append(f"source_text (requested chapter, first chars): {bundle['source_text'][:2500]!r}")
    if bundle.get("artifact_text"):
        parts.append(f"artifact_text ({bundle.get('artifact_kind')}, first chars): {bundle['artifact_text'][:2500]!r}")
    return "\n".join(parts)
