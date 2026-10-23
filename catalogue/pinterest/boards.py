"""The ``pin_boards`` job: every board a platform admin APPROVED in the Library
becomes a real board on the SketchCast Pinterest account (guide §5).

IDEMPOTENT BY NAME. A board is looked up by name among the account's boards
before anything is created, so a run that died between Pinterest's answer and
the database write adopts the board it made instead of making a second one.
Boards are PUBLIC (they are what people follow). Grade-band sections are made
the same way — looked up, then created — when ``settings.board_sections``
lists them; the ``{grade_band: section_id}`` map lands on the row.

Board creation is not posting, so it runs in dry run too (rollout step 8:
the founder approves the board list, the agent creates the boards — in the
sandbox while the app has Trial access). A refused token or a 403 trips the
breaker like a post would.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from catalogue.pinterest import common as C
from catalogue.pinterest.api import Api, Forbidden, TokenInvalid, TrialAccessOnly
from worker import client as db

log = logging.getLogger("worker.pinterest.boards")

PRIVACY = "PUBLIC"


def _s(v: object) -> str:
    return C._s(v)


def _norm(name: str) -> str:
    return _s(name).casefold()


def sections_wanted(settings: dict) -> list[str]:
    raw = settings.get("board_sections")
    return [_s(x) for x in raw if _s(x)] if isinstance(raw, list) else []


def ensure_board(api: Api, board: dict, existing: dict[str, dict], wanted_sections: list[str]) -> dict:
    """The row fields for one approved board: its Pinterest id and sections."""
    found = existing.get(_norm(board.get("name")))
    if found:
        board_id = _s(found.get("id"))
        log.info("pinterest boards: %r already on Pinterest as %s; adopted", board.get("name"), board_id)
    else:
        made = api.create_board(_s(board.get("name")), _s(board.get("description")), PRIVACY)
        board_id = _s(made.get("id"))
        if not board_id:
            raise RuntimeError(f"Pinterest created {board.get('name')!r} without returning an id")
        existing[_norm(board.get("name"))] = made
    sections = dict(board.get("sections") or {})
    missing = [s for s in wanted_sections if s not in sections]
    if missing:
        have = {_norm(x.get("name")): _s(x.get("id")) for x in api.list_sections(board_id)}
        for band in missing:
            sid = have.get(_norm(band)) or _s(api.create_section(board_id, band).get("id"))
            if sid:
                sections[band] = sid
    return {"pinterest_board_id": board_id, "sections": sections, "status": "created"}


def create_boards(sb, job_id: Optional[str], *, api: Optional[Api] = None, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    approved = C.rows(sb.table(C.BOARDS).select("*").eq("status", "approved").execute())
    summary: dict = {"step": "done", "created": [], "errors": []}
    if not approved:
        return summary
    C.yield_to_builders(sb, job_id)
    settings = C.read_settings(sb)
    try:
        if api is None:
            from catalogue.pinterest import tokens

            api = Api(tokens.access_token(sb, now=now))
        existing = {_norm(b.get("name")): b for b in api.list_boards()}
    except (TokenInvalid, TrialAccessOnly, Forbidden) as exc:
        C.trip_breaker(sb, f"board creation: {exc}", now)
        summary.update(step="breaker", errors=[str(exc)[:300]])
        return summary
    wanted = sections_wanted(settings)
    for board in sorted(approved, key=lambda b: (_s(b.get("language")), _s(b.get("board_key")))):
        try:
            fields = ensure_board(api, board, existing, wanted)
        except db.DeferredJob:
            raise
        except (TokenInvalid, TrialAccessOnly, Forbidden) as exc:
            C.trip_breaker(sb, f"board creation: {exc}", now)
            summary.update(step="breaker")
            summary["errors"].append(f"{board.get('name')}: {exc}"[:300])
            return summary
        except Exception as exc:  # noqa: BLE001 — one board's failure is reported, the rest still go
            summary["errors"].append(f"{board.get('name')}: {type(exc).__name__}: {exc}"[:300])
            continue
        sb.table(C.BOARDS).update({**fields, "updated_at": C.iso(now)}).eq("id", board["id"]).eq(
            "status", "approved").execute()
        summary["created"].append({"board": board.get("board_key"), "language": board.get("language"),
                                   "pinterest_board_id": fields["pinterest_board_id"]})
    log.info("pinterest boards: %d created, %d errors", len(summary["created"]), len(summary["errors"]))
    return summary


def run_boards_job(sb, job: dict, *, api: Optional[Api] = None) -> Optional[dict]:
    job_id = job["id"]
    try:
        summary = create_boards(sb, job_id, api=api)
        db.set_stage(sb, job_id, summary)
        if summary.get("errors"):
            db.finish_job(sb, job_id, None, error=f"{len(summary['errors'])} board(s) not created: "
                                                  f"{'; '.join(summary['errors'])}"[:4000])
        else:
            db.finish_job(sb, job_id)
        return summary
    except db.DeferredJob as exc:
        log.info("pin_boards %s waits %ds: %s", job_id, exc.seconds, exc.note)
        raise
    except Exception as exc:  # noqa: BLE001
        log.error("pin_boards %s failed: %s", job_id, exc)
        try:
            db.finish_job(sb, job_id, None, error=f"{type(exc).__name__}: {exc}"[:4000])
        except Exception as exc2:  # noqa: BLE001
            log.error("pin_boards %s: could not record the failure: %s", job_id, exc2)
        return None


__all__ = ["run_boards_job", "create_boards", "ensure_board", "sections_wanted"]
