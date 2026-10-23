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

ADOPT_WINDOW = timedelta(minutes=10)
POSTABLE = ("scheduled", "posting")


class Refused(RuntimeError):
    """A re-check failed: the pin is failed with this reason, nothing posted."""


def _s(v: object) -> str:
    return C._s(v)


def load_pin(sb, pin_id: str) -> Optional[dict]:
    found = C.rows(sb.table(C.PINS).select("*").eq("id", pin_id).limit(1).execute())
    return found[0] if found else None


def _update(sb, pin_id: str, fields: dict, now: datetime) -> None:
    sb.table(C.PINS).update({**fields, "updated_at": C.iso(now)}).eq("id", pin_id).execute()


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
    check = link_check or C.link_ok
    if not pin.get("link") or not check(pin["link"]):
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


def find_adoptable(board_pins: list[dict], pin: dict) -> tuple[Optional[str], str]:
    """(pinterest pin id, matcher) of an earlier run's pin, or (None, "")."""
    from catalogue.pinterest.common import utm_content

    content = "utm_content=" + utm_content(_s(pin.get("format")) or C.FORMAT_STATIC, _s(pin.get("id")))
    for p in board_pins:
        if content in _s(p.get("link")):
            return _s(p.get("id")) or None, "utm_content"
    started = C.when(pin.get("post_started_at"))
    if started is not None:
        for p in board_pins:
            made = C.when(p.get("created_at"))
            if (_s(p.get("title")) == _s(pin.get("title")) and made is not None
                    and abs(made - started) <= ADOPT_WINDOW):
                return _s(p.get("id")) or None, "title+time"
    return None, ""


def _finish_posted(sb, pin: dict, pinterest_id: str, now: datetime, job_id: Optional[str], *, how: str,
                   board: Optional[dict] = None, dry: bool = False) -> dict:
    _update(sb, pin["id"], {"status": "posted", "pinterest_pin_id": pinterest_id, "posted_at": C.iso(now),
                            "last_error": None, "post_job_id": job_id or None}, now)
    C.audit(sb, _s(pin.get("approved_by")), "pinterest_pin_post", _s(pin.get("id")),
            {"pin_row_id": pin.get("id"), "pinterest_pin_id": pinterest_id, "language": pin.get("language"),
             "board": _s((board or {}).get("board_key")) or pin.get("board_id"), "template": pin.get("template"),
             "round_id": pin.get("round_id"), "how": how, "dry_run": dry})
    if not dry:
        C.reset_failures(sb, now)
    return {"step": "done", "pin": pin.get("id"), "pinterest_pin_id": pinterest_id, "how": how, "dry_run": dry}


def _not_counted(sb, pin: dict, why: str, now: datetime) -> None:
    """Back to the approved queue without spending an attempt (a breaker cause)."""
    _update(sb, pin["id"], {"status": "approved", "round_id": None, "last_error": why[:1000]}, now)


def _counted(sb, pin: dict, why: str, now: datetime) -> str:
    attempts = int(pin.get("attempts") or 0) + 1
    if attempts >= C.MAX_ATTEMPTS:
        _update(sb, pin["id"], {"status": "failed", "attempts": attempts, "last_error": why[:1000]}, now)
        outcome = "failed"
    else:
        _update(sb, pin["id"], {"status": "approved", "attempts": attempts, "round_id": None,
                                "last_error": why[:1000]}, now)
        outcome = "retry"
    C.count_failure(sb, now, why)
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
    try:
        board = recheck(sb, pin, link_check=link_check)
    except Refused as exc:
        if pin.get("status") in POSTABLE:
            _update(sb, pin["id"], {"status": "failed", "last_error": f"re-check: {exc}"[:1000]}, now)
        log.warning("pinterest post %s refused: %s", pin_id, exc)
        return {"step": "refused", "pin": pin_id, "why": str(exc)}

    # never starve: the last thing before Pinterest is a look at the queue
    C.yield_to_builders(sb, job_id)

    if C.dry_run(settings):
        path = _s((pin.get("images") or [{}])[0].get("path"))
        payload = build_payload(pin, board, "")
        payload["media_source"]["data"] = f"<{C.BUCKET}/{path}, not read in a dry run>"
        _update(sb, pin["id"], {"status": "posting", "post_token": pin.get("post_token") or uuid.uuid4().hex,
                                "post_started_at": C.iso(now), "post_job_id": job_id or None}, now)
        log.info("pinterest post %s DRY RUN - would POST /v5/pins %s", pin_id, payload)
        return _finish_posted(sb, {**pin}, f"dryrun:{pin['id']}", now, job_id, how="dry_run", board=board, dry=True)

    try:
        from catalogue.pinterest import tokens

        if api is None:
            api = Api(tokens.access_token(sb, now=now))
        if _s(pin.get("post_token")):
            found, how = find_adoptable(api.list_board_pins(_s(board.get("pinterest_board_id")), page_size=25), pin)
            log.info("pinterest post %s: adopt check after an interrupted run — %s", pin_id,
                     f"matched by {how}" if found else "no earlier pin on the board")
            if found:
                return _finish_posted(sb, pin, found, now, job_id, how=f"adopted:{how}", board=board)
        token = uuid.uuid4().hex
        _update(sb, pin["id"], {"status": "posting", "post_token": token, "post_started_at": C.iso(now),
                                "post_job_id": job_id or None}, now)
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
        return _finish_posted(sb, pin, pinterest_id, now, job_id, how="created", board=board)
    except db.DeferredJob:
        # a rate-limit wait: the pin stays where it is; the token, if any,
        # lets the next run adopt rather than double-post
        raise
    except (TokenInvalid, TrialAccessOnly, Forbidden) as exc:
        why = str(exc)
        _not_counted(sb, pin, why, now)
        C.trip_breaker(sb, f"pin {pin_id}: {why}", now)
        return {"step": "breaker", "pin": pin_id, "why": why[:300]}
    except Exception as exc:  # noqa: BLE001 — 5xx after retries, a 4xx, storage
        why = f"{type(exc).__name__}: {exc}"
        outcome = _counted(sb, pin, why, now)
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
        if summary.get("step") in ("done", "skipped"):
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
           "is_platform_admin", "Refused", "ADOPT_WINDOW"]
