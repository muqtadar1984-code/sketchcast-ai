"""The ``pin_post`` job: ONE approved, scheduled pin onto Pinterest (build plan
§3.7), with the YouTube publisher's idempotency (``publish.publish_part``).

  1. RE-CHECK everything the approval assumed — the pin is scheduled (or
     ``posting``: an earlier run of this job was interrupted), its topic is
     still public and its kit approved, the approver is STILL a platform
     admin, the link answers 200, the board is created. Any failure → the
     pin is ``failed`` with the reason; nothing is posted. Approval in the
     console is necessary, never sufficient.
  2. ADOPT. A pin with a ``post_token`` was being created when a run died:
     the create may have landed. The board's recent pins are listed and one
     whose ``link`` carries our ``utm_content`` is adopted; failing that (the
     probe could not confirm that Pinterest returns the link unchanged, Q2),
     one with the same title created within 10 minutes of
     ``post_started_at``. Which matcher hit is logged — the fallback goes
     once the first live posts show the link round-trips.
  3. INTENT: ``posting``, a fresh ``post_token``, ``post_started_at`` — written
     BEFORE the create call, so no interruption can leave a pin on Pinterest
     that the database cannot find.
  4. CREATE, and write ``pinterest_pin_id`` + ``posted_at`` the moment
     Pinterest answers.
  5. AUDIT ``pinterest_pin_post``, the approver as the actor.

The job yields to a real user's build before the create call. A 429 waits
(``DeferredJob``) and spends nothing. A refused token, a Trial-only refusal
or any 403 trips the breaker (pause + email) and does NOT count against the
pin: it goes back to ``approved`` for the next round. Any other failure
counts: back to ``approved`` until the third, then ``failed`` (``approved_at``
and ``post_token`` kept, so a Library Retry still adopts rather than
double-posts).

DRY RUN (the default) does all of it except Pinterest: no token is read and
no call is made; the payload is logged WITHOUT the image bytes, the pin gets
``pinterest_pin_id = 'dryrun:<row id>'``, ``posted_at``, and ``posted``.
"""

from __future__ import annotations

import base64
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from catalogue.pinterest import common as C
from catalogue.pinterest import sources as S
from catalogue.pinterest.api import Api, Forbidden, PinterestError, TokenInvalid, TrialAccessOnly
from worker import client as db

log = logging.getLogger("worker.pinterest.post")

ADOPT_WINDOW = timedelta(minutes=3)
POSTABLE = ("scheduled", "posting")


class Refused(RuntimeError):
    """A re-check failed: the pin is failed with this reason, nothing posted."""


class Transient(RuntimeError):
    """A re-check could not be made right now (the link did not answer): the
    pin goes back to approved, no attempt spent, nothing posted."""


class Moved(RuntimeError):
    """A Pinterest id another row already holds: not this pin's."""


def _s(v: object) -> str:
    return C._s(v)


def load_pin(sb, pin_id: str) -> Optional[dict]:
    found = C.rows(sb.table(C.PINS).select("*").eq("id", pin_id).limit(1).execute())
    return found[0] if found else None


def _transition(sb, pin_id: str, expected, fields: dict, now: datetime) -> bool:
    """Write ``fields`` only while the row is still in one of ``expected``;
    True when exactly that one row moved. An admin's reject or retire between
    this job's read and its write is never overwritten."""
    res = (sb.table(C.PINS).update({**fields, "updated_at": C.iso(now)}).eq("id", pin_id)
           .in_("status", list(expected)).execute())
    return len(C.rows(res)) == 1


def is_platform_admin(sb, user_id: str) -> bool:
    if not user_id:
        return False
    found = C.rows(sb.table("platform_admins").select("user_id,revoked_at").eq("user_id", user_id).execute())
    return any(not r.get("revoked_at") for r in found)


def recheck(sb, pin: dict, *, link_check: Optional[Callable[[str], bool]] = None) -> dict:
    """The board row when every gate holds; ``Refused`` otherwise."""
    if pin.get("status") not in POSTABLE:
        raise Refused(f"the pin is {pin.get('status')}, not scheduled")
    if not (pin.get("approved_by") and pin.get("approved_at")):
        raise Refused("the pin has no recorded approval")
    language = _s(pin.get("language")) or "en"
    if pin.get("topic_id"):
        kit = S.is_public(sb, _s(pin.get("topic_id")), language)
        if not kit:
            raise Refused(f"the topic is no longer public in {language}, or its kit is no longer approved")
    if not is_platform_admin(sb, _s(pin.get("approved_by"))):
        raise Refused("the approver is no longer a platform admin")
    if not pin.get("link"):
        raise Refused("the pin has no link")
    verdict = C.link_verdict((link_check or C.link_ok)(pin["link"]))
    if verdict == C.LINK_UNKNOWN:
        raise Transient("the link could not be checked just now")
    if verdict != C.LINK_OK:
        raise Refused("the link does not answer 200")
    boards = C.rows(sb.table(C.BOARDS).select("*").eq("id", _s(pin.get("board_id"))).limit(1).execute())
    board = boards[0] if boards else None
    if not board or board.get("status") != "created" or not _s(board.get("pinterest_board_id")):
        raise Refused("the pin's board is not created on Pinterest")
    if not (pin.get("images") or []):
        raise Refused("the pin has no image")
    return board


def build_payload(pin: dict, board: dict, image_b64: str) -> dict:
    payload = {"board_id": _s(board.get("pinterest_board_id")), "title": pin.get("title") or "",
               "description": pin.get("description") or "", "alt_text": pin.get("alt_text") or "",
               "link": pin.get("link") or "",
               "media_source": {"source_type": "image_base64", "content_type": "image/png", "data": image_b64}}
    section = (board.get("sections") or {}).get(_s(pin.get("grade_band"))) if pin.get("grade_band") else None
    if section:
        payload["board_section_id"] = _s(section)
    img = (pin.get("images") or [{}])[0]
    if img.get("dominant_color"):
        payload["dominant_color"] = img["dominant_color"]
    if pin.get("ai_modified"):
        payload["ai_disclosures"] = {"values": ["AI_MODIFIED"]}
    return payload


def loggable(payload: dict) -> dict:
    """The payload as it may be logged: the image bytes replaced by their size."""
    out = dict(payload)
    media = dict(out.get("media_source") or {})
    if "data" in media:
        media["data"] = f"<{len(media['data'])} base64 chars>"
    out["media_source"] = media
    return out


def find_adoptable(board_pins: list[dict], pin: dict, taken: Optional[set] = None) -> tuple[Optional[str], str]:
    """(pinterest pin id, matcher) of an earlier run's pin, or (None, "").
    ``taken``: Pinterest ids another row already holds — never this pin's.
    The fallback (same title, created within ADOPT_WINDOW of
    ``post_started_at``) stays tight so a sibling posted minutes apart is
    not mistaken for this one."""
    from catalogue.pinterest.common import utm_content

    taken = taken or set()
    mine = [p for p in board_pins if _s(p.get("id")) and _s(p.get("id")) not in taken]
    content = "utm_content=" + utm_content(_s(pin.get("format")) or C.FORMAT_STATIC, _s(pin.get("id")))
    for p in mine:
        if content in _s(p.get("link")):
            return _s(p.get("id")), "utm_content"
    started = C.when(pin.get("post_started_at"))
    if started is not None:
        for p in mine:
            made = C.when(p.get("created_at"))
            if (_s(p.get("title")) == _s(pin.get("title")) and made is not None
                    and abs(made - started) <= ADOPT_WINDOW):
                return _s(p.get("id")), "title+time"
    return None, ""


def ids_held_elsewhere(sb, pin_id: str, ids: list[str]) -> set[str]:
    """The Pinterest ids among ``ids`` that another pin row already holds."""
    if not ids:
        return set()
    found = C.rows(sb.table(C.PINS).select("id,pinterest_pin_id").in_("pinterest_pin_id", ids).execute())
    return {_s(r.get("pinterest_pin_id")) for r in found if _s(r.get("id")) != _s(pin_id)}


def _finish_posted(sb, pin: dict, pinterest_id: str, now: datetime, job_id: Optional[str], *, how: str,
                   board: Optional[dict] = None, dry: bool = False, expected=("posting",)) -> dict:
    """Record the pin as posted — only from ``expected``. Should an admin have
    moved the row meanwhile, the Pinterest id is still recorded (the pin IS
    on Pinterest) but the status they set stands: never resurrected. A 23505
    on the id (another row holds it) raises ``Moved``."""
    fields = {"status": "posted", "pinterest_pin_id": pinterest_id, "posted_at": C.iso(now), "last_error": None,
              "post_job_id": job_id or None}
    try:
        moved = not _transition(sb, pin["id"], expected, fields, now)
        if moved:
            sb.table(C.PINS).update({"pinterest_pin_id": pinterest_id, "posted_at": C.iso(now),
                                     "last_error": "posted to Pinterest after the row was changed in the Library",
                                     "updated_at": C.iso(now)}).eq("id", pin["id"]).execute()
    except Exception as exc:  # noqa: BLE001
        if C.is_duplicate(exc):
            raise Moved(f"Pinterest pin {pinterest_id} is already recorded on another row") from None
        raise
    C.audit(sb, _s(pin.get("approved_by")), "pinterest_pin_post", _s(pin.get("id")),
            {"pin_row_id": pin.get("id"), "pinterest_pin_id": pinterest_id, "language": pin.get("language"),
             "board": _s((board or {}).get("board_key")) or pin.get("board_id"), "template": pin.get("template"),
             "round_id": pin.get("round_id"), "how": how, "dry_run": dry, "row_moved": moved})
    if not dry:
        C.reset_failures(sb, now)
    return {"step": "done", "pin": pin.get("id"), "pinterest_pin_id": pinterest_id, "how": how, "dry_run": dry}


def _not_counted(sb, pin: dict, why: str, now: datetime, expected=POSTABLE) -> bool:
    """Back to the approved queue without spending an attempt; ``post_token``
    is kept so the next run adopts. Only from ``expected``."""
    return _transition(sb, pin["id"], expected, {"status": "approved", "round_id": None, "last_error": why[:1000]},
                       now)


def _counted(sb, pin: dict, why: str, now: datetime, expected=POSTABLE) -> str:
    """One more attempt spent: back to approved, or failed at the third —
    ``approved_at`` and ``post_token`` kept. Only from ``expected``; a row
    an admin moved is left alone ("moved")."""
    attempts = int(pin.get("attempts") or 0) + 1
    if attempts >= C.MAX_ATTEMPTS:
        ok = _transition(sb, pin["id"], expected, {"status": "failed", "attempts": attempts,
                                                   "last_error": why[:1000]}, now)
        outcome = "failed"
    else:
        ok = _transition(sb, pin["id"], expected, {"status": "approved", "attempts": attempts, "round_id": None,
                                                   "last_error": why[:1000]}, now)
        outcome = "retry"
    if not ok:
        return "moved"
    if C.count_failure(sb, now, why) >= C.BREAKER_FAILURES:
        C.trip_breaker(sb, f"{C.BREAKER_FAILURES} post failures in a row; last: {why[:200]}", now)
    return outcome


def post_pin(sb, job_id: Optional[str], params: dict, *, api: Optional[Api] = None,
             link_check: Optional[Callable[[str], bool]] = None, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    pin_id = _s(params.get("pin_row_id"))
    pin = load_pin(sb, pin_id) if pin_id else None
    if not pin:
        return {"step": "skipped", "why": f"pin {pin_id or '?'} not found"}
    if pin.get("status") == "posted" or _s(pin.get("pinterest_pin_id")):
        return {"step": "skipped", "why": "already posted", "pinterest_pin_id": pin.get("pinterest_pin_id")}
    settings = C.read_settings(sb)
    br = settings.get("breaker") if isinstance(settings.get("breaker"), dict) else {}
    if settings.get("enabled") is not True or settings.get("paused") or br.get("tripped"):
        # switched off, paused or tripped after this job was queued: the pin
        # stays scheduled and the round resumes with the agent
        return {"step": "skipped", "pin": pin_id, "why": "the Pinterest agent is off, paused or tripped"}
    expected = (_s(pin.get("status")),)
    try:
        board = recheck(sb, pin, link_check=link_check)
    except Transient as exc:
        _not_counted(sb, pin, f"re-check: {exc}", now, expected)
        log.info("pinterest post %s: %s; back to approved, no attempt spent", pin_id, exc)
        return {"step": "later", "pin": pin_id, "why": str(exc)}
    except Refused as exc:
        if pin.get("status") in POSTABLE:
            _transition(sb, pin["id"], expected, {"status": "failed", "last_error": f"re-check: {exc}"[:1000]}, now)
        log.warning("pinterest post %s refused: %s", pin_id, exc)
        return {"step": "refused", "pin": pin_id, "why": str(exc)}

    # never starve: the last thing before Pinterest is a look at the queue
    C.yield_to_builders(sb, job_id)

    if C.dry_run(settings):
        path = _s((pin.get("images") or [{}])[0].get("path"))
        payload = build_payload(pin, board, "")
        payload["media_source"]["data"] = f"<{C.BUCKET}/{path}, not read in a dry run>"
        if not _transition(sb, pin["id"], expected, {"status": "posting",
                                                     "post_token": pin.get("post_token") or uuid.uuid4().hex,
                                                     "post_started_at": C.iso(now), "post_job_id": job_id or None},
                           now):
            return {"step": "skipped", "pin": pin_id, "why": "the pin changed in the Library; nothing posted"}
        log.info("pinterest post %s DRY RUN - would POST /v5/pins %s", pin_id, payload)
        return _finish_posted(sb, {**pin}, f"dryrun:{pin['id']}", now, job_id, how="dry_run", board=board, dry=True)

    try:
        from catalogue.pinterest import tokens

        if api is None:
            api = Api(tokens.access_token(sb, now=now))
        if _s(pin.get("post_token")):
            # an earlier attempt may have reached Pinterest: LOOK before posting again
            listed = api.list_board_pins(_s(board.get("pinterest_board_id")), page_size=25)
            taken = ids_held_elsewhere(sb, pin["id"], [_s(p.get("id")) for p in listed if _s(p.get("id"))])
            found, how = find_adoptable(listed, pin, taken)
            log.info("pinterest post %s: adopt check after an earlier attempt - %s", pin_id,
                     f"matched by {how}" if found else "no earlier pin on the board")
            if found:
                try:
                    return _finish_posted(sb, pin, found, now, job_id, how=f"adopted:{how}", board=board,
                                          expected=expected)
                except Moved as exc:
                    log.warning("pinterest post %s: %s; not ours", pin_id, exc)
        token = uuid.uuid4().hex
        if not _transition(sb, pin["id"], expected, {"status": "posting", "post_token": token,
                                                     "post_started_at": C.iso(now), "post_job_id": job_id or None},
                           now):
            log.warning("pinterest post %s: the row left %s before the intent write; nothing posted", pin_id,
                        expected[0])
            return {"step": "skipped", "pin": pin_id, "why": "the pin changed in the Library; nothing posted"}
        expected = ("posting",)
        pin = {**pin, "status": "posting", "post_token": token, "post_started_at": C.iso(now)}
        path = _s((pin.get("images") or [{}])[0].get("path"))
        png = sb.storage.from_(C.BUCKET).download(path)
        if not png:
            raise PinterestError(0, None, f"the card {C.BUCKET}/{path} is empty", "storage")
        payload = build_payload(pin, board, base64.b64encode(bytes(png)).decode())
        log.info("pinterest post %s: POST /v5/pins %s", pin_id, loggable(payload))
        made = api.create_pin(payload)
        pinterest_id = _s(made.get("id"))
        if not pinterest_id:
            raise PinterestError(200, None, "Pinterest answered without a pin id", "/pins")
        return _finish_posted(sb, pin, pinterest_id, now, job_id, how="created", board=board, expected=expected)
    except db.DeferredJob:
        # a rate-limit wait: the pin stays where it is; the token, if any,
        # lets the next run adopt rather than double-post
        raise
    except (TokenInvalid, TrialAccessOnly, Forbidden) as exc:
        why = str(exc)
        _not_counted(sb, pin, why, now, expected)
        C.trip_breaker(sb, f"pin {pin_id}: {why}", now)
        return {"step": "breaker", "pin": pin_id, "why": why[:300]}
    except Exception as exc:  # noqa: BLE001 — an ambiguous create, a 4xx, storage
        # post_token stays on the row: if the create reached Pinterest, the
        # next attempt finds it on the board before it posts anything
        why = f"{type(exc).__name__}: {exc}"
        outcome = _counted(sb, pin, why, now, expected)
        log.error("pinterest post %s failed (%s): %s", pin_id, outcome, why)
        return {"step": outcome, "pin": pin_id, "why": why[:300]}


def run_post_job(sb, job: dict, *, api: Optional[Api] = None, link_check=None) -> Optional[dict]:
    """Entry point for run.py. Finishes its own row (error when the pin was
    not posted); raises only ``DeferredJob``."""
    job_id = job["id"]
    try:
        params = job.get("params") if isinstance(job.get("params"), dict) else {}
        summary = post_pin(sb, job_id, params, api=api, link_check=link_check)
        db.set_stage(sb, job_id, summary)
        if summary.get("step") in ("done", "skipped", "later"):
            db.finish_job(sb, job_id)
        else:
            db.finish_job(sb, job_id, None, error=f"pin not posted ({summary.get('step')}): "
                                                  f"{summary.get('why')}"[:4000])
        return summary
    except db.DeferredJob as exc:
        log.info("pin_post %s waits %ds: %s", job_id, exc.seconds, exc.note)
        raise
    except Exception as exc:  # noqa: BLE001
        log.error("pin_post %s failed: %s", job_id, exc)
        try:
            db.finish_job(sb, job_id, None, error=f"{type(exc).__name__}: {exc}"[:4000])
        except Exception as exc2:  # noqa: BLE001
            log.error("pin_post %s: could not record the failure: %s", job_id, exc2)
        return None


__all__ = ["run_post_job", "post_pin", "recheck", "build_payload", "loggable", "find_adoptable",
           "is_platform_admin", "ids_held_elsewhere", "Refused", "Transient", "Moved", "ADOPT_WINDOW"]
