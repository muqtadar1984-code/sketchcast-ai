"""The Pinterest OAuth token: ciphertext in the database, plaintext only in
memory, for as long as one call needs it.

WHY THE DATABASE AND NOT AN ENV VAR (build plan §3.3). Pinterest's access
token lives 30 days and its refresh token is a CONTINUOUS one: every refresh
returns a NEW refresh token (the probe saw it rotate on every run). A Railway
variable cannot hold a secret the worker must rewrite every few weeks. So the
token JSON is encrypted with Fernet under ``PINTEREST_TOKEN_KEY`` — a key that
lives only in Railway and on the founder's machine for the one-time consent —
and the single row of ``pinterest_oauth`` (0154) holds the ciphertext. A
database dump alone reveals nothing.

RULES:
  * the access token is refreshed when fewer than ``REFRESH_WITHIN_DAYS`` (5)
    remain, and the NEW refresh token is stored every time;
  * a token never reaches a log line, a job's params / stage / error, or an
    audit row — errors say what happened, never what was sent;
  * ``check_expiry``: under 14 days left on the refresh token, one email a
    day; under 3, the agent pauses itself (``token_expiring``) and says so.

``sandbox_access_token`` (optional, inside the same ciphertext) is the
developer portal's sandbox token: the sandbox host refuses the production
token (probe run 5), so a development run against ``api-sandbox`` uses it.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from catalogue.pinterest import common as C
from catalogue.pinterest.api import Api, PinterestError, TokenInvalid, TokenUnavailable, is_sandbox

log = logging.getLogger("worker.pinterest.tokens")

KEY_ENV = "PINTEREST_TOKEN_KEY"
REFRESH_WITHIN_DAYS = 5
ALERT_DAYS = 14
PAUSE_DAYS = 3
DEFAULT_ACCESS_SECONDS = 30 * 86400
DEFAULT_REFRESH_SECONDS = 60 * 86400
SAVE_TRIES = 3
SAVE_BACKOFF = 1.0
TRANSIENT_WAIT_SECONDS = 300

# One refresh at a time in this process: Pinterest ROTATES the refresh token,
# so two threads refreshing with the same old one would leave one of them
# holding a token Pinterest has already replaced.
_refresh_lock = threading.Lock()
# A refreshed token the database has not accepted yet. Pinterest has already
# retired the old refresh token, so losing this one would lose the account:
# it is kept here and saved again on the next call.
_unsaved: dict = {"token": None}


class TokenKeyMissing(TokenInvalid):
    def __init__(self):
        super().__init__(401, None, f"{KEY_ENV} is not set on this worker; the stored token cannot be read",
                         "tokens")


@dataclass
class Token:
    access_token: str
    refresh_token: str
    access_expires_at: datetime
    refresh_expires_at: datetime
    scopes: list = field(default_factory=list)
    account_username: Optional[str] = None
    sandbox_access_token: Optional[str] = None

    def __repr__(self) -> str:  # a Token printed by accident shows no secret
        return (f"Token(account={self.account_username!r}, access_expires_at={self.access_expires_at.isoformat()}, "
                f"refresh_expires_at={self.refresh_expires_at.isoformat()})")


def _fernet():
    from cryptography.fernet import Fernet

    key = os.getenv(KEY_ENV, "").strip()
    if not key:
        raise TokenKeyMissing()
    return Fernet(key.encode())


def generate_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


def encrypt(payload: dict) -> str:
    return _fernet().encrypt(json.dumps(payload, sort_keys=True).encode()).decode()


def decrypt(ciphertext: str) -> dict:
    from cryptography.fernet import InvalidToken

    try:
        return json.loads(_fernet().decrypt(str(ciphertext).encode()).decode())
    except InvalidToken:
        raise TokenInvalid(401, None, f"the stored token does not decrypt with this {KEY_ENV}", "tokens") from None


def from_grant(body: dict, now: datetime, previous: Optional[Token] = None) -> Token:
    """A token from Pinterest's answer. A refresh answer that somehow carries
    no refresh token keeps the previous one — but Pinterest's continuous
    refresh always sends a new one, and THAT is what gets stored."""
    scopes = body.get("scope")
    scopes = [s for s in (scopes.replace(",", " ").split() if isinstance(scopes, str) else (scopes or []))]
    access_s = int(body.get("expires_in") or DEFAULT_ACCESS_SECONDS)
    refresh_new = str(body.get("refresh_token") or "")
    if refresh_new:
        refresh_exp = now + timedelta(seconds=int(body.get("refresh_token_expires_in") or DEFAULT_REFRESH_SECONDS))
    else:
        refresh_exp = previous.refresh_expires_at if previous else now + timedelta(seconds=DEFAULT_REFRESH_SECONDS)
    return Token(access_token=str(body.get("access_token") or ""),
                 refresh_token=refresh_new or (previous.refresh_token if previous else ""),
                 access_expires_at=now + timedelta(seconds=access_s), refresh_expires_at=refresh_exp,
                 scopes=scopes or (previous.scopes if previous else []),
                 account_username=previous.account_username if previous else None,
                 sandbox_access_token=previous.sandbox_access_token if previous else None)


def load(sb) -> Optional[Token]:
    found = C.rows(sb.table(C.OAUTH).select("*").eq("id", 1).limit(1).execute())
    if not found:
        return None
    row = found[0]
    secret = decrypt(row.get("ciphertext") or "")
    return Token(access_token=str(secret.get("access_token") or ""),
                 refresh_token=str(secret.get("refresh_token") or ""),
                 access_expires_at=C.when(row.get("access_expires_at")) or datetime.now(timezone.utc),
                 refresh_expires_at=C.when(row.get("refresh_expires_at")) or datetime.now(timezone.utc),
                 scopes=list(row.get("scopes") or []), account_username=row.get("account_username"),
                 sandbox_access_token=secret.get("sandbox_access_token") or None)


def save(sb, token: Token, now: Optional[datetime] = None) -> None:
    now = now or datetime.now(timezone.utc)
    secret = {"access_token": token.access_token, "refresh_token": token.refresh_token}
    if token.sandbox_access_token:
        secret["sandbox_access_token"] = token.sandbox_access_token
    sb.table(C.OAUTH).upsert({"id": 1, "account_username": token.account_username, "scopes": list(token.scopes),
                              "ciphertext": encrypt(secret), "access_expires_at": C.iso(token.access_expires_at),
                              "refresh_expires_at": C.iso(token.refresh_expires_at), "refreshed_at": C.iso(now),
                              "last_error": None}, on_conflict="id").execute()


def _note_error(sb, text: str) -> None:
    try:
        sb.table(C.OAUTH).update({"last_error": text[:500]}).eq("id", 1).execute()
    except Exception as exc:  # noqa: BLE001
        log.warning("pinterest tokens: last_error not written: %s", exc)


def save_with_retry(sb, token: Token, now: datetime, *, sleep=time.sleep) -> bool:
    """``save``, up to SAVE_TRIES times with backoff. False when every try
    failed — the caller keeps the token in memory (``_unsaved``)."""
    for i in range(SAVE_TRIES):
        try:
            save(sb, token, now)
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("pinterest tokens: the refreshed token was not stored (try %d of %d): %s",
                      i + 1, SAVE_TRIES, type(exc).__name__)
            if i + 1 < SAVE_TRIES:
                sleep(SAVE_BACKOFF * (2 ** i))
    return False


def _current(sb, now: datetime, sleep) -> Optional[Token]:
    """The newest token: one still waiting to be saved wins over the row."""
    pending = _unsaved.get("token")
    if pending is not None:
        if save_with_retry(sb, pending, now, sleep=sleep):
            _unsaved["token"] = None
        return pending
    return load(sb)


def access_token(sb, *, now: Optional[datetime] = None, api_factory: Optional[Callable[..., Api]] = None,
                 base: Optional[str] = None, sleep=time.sleep) -> str:
    """A usable bearer, refreshing (and re-storing BOTH tokens) when the
    access token has fewer than five days left. One refresh at a time per
    process, re-reading the row once the lock is held. Raises
    ``TokenInvalid`` when the grant is bad (re-consent), ``DeferredJob``
    when the token endpoint is only unavailable; no message names a secret."""
    from worker import client as db

    now = now or datetime.now(timezone.utc)
    token = _current(sb, now, sleep)
    if token is None:
        raise TokenInvalid(401, None, "no Pinterest token is stored; run scripts/pinterest_oauth.py", "tokens")
    if is_sandbox(base) and token.sandbox_access_token:
        return token.sandbox_access_token

    def fresh_enough(t: Token) -> bool:
        return bool(t.access_token) and t.access_expires_at - now > timedelta(days=REFRESH_WITHIN_DAYS)

    if fresh_enough(token):
        return token.access_token
    with _refresh_lock:
        token = _current(sb, now, sleep) or token     # another thread may have refreshed meanwhile
        if fresh_enough(token):
            return token.access_token
        if not token.refresh_token or token.refresh_expires_at <= now:
            _note_error(sb, "the refresh token has expired; run scripts/pinterest_oauth.py")
            raise TokenInvalid(401, None, "the refresh token has expired; the founder must re-consent", "tokens")
        factory = api_factory or Api
        try:
            body = factory().refresh(token.refresh_token)
        except TokenInvalid as exc:
            _note_error(sb, f"refresh refused: HTTP {exc.status} {exc.code}: {exc.message[:200]}")
            raise TokenInvalid(exc.status or 401, exc.code, f"the token refresh was refused: {exc.message[:200]}",
                               "/oauth/token") from None
        except (TokenUnavailable, PinterestError) as exc:
            _note_error(sb, f"refresh unavailable: HTTP {exc.status}: {exc.message[:200]}")
            raise db.DeferredJob(TRANSIENT_WAIT_SECONDS, "the Pinterest token endpoint is unavailable; "
                                                         "refreshing later") from None
        fresh = from_grant(body, now, previous=token)
        if save_with_retry(sb, fresh, now, sleep=sleep):
            _unsaved["token"] = None
        else:
            _unsaved["token"] = fresh
            log.error("pinterest tokens: the rotated token is held in memory until the database takes it")
    log.info("pinterest tokens: refreshed; access valid until %s, refresh until %s",
             fresh.access_expires_at.date(), fresh.refresh_expires_at.date())
    return fresh.access_token


def check_expiry(sb, settings: dict, now: datetime) -> Optional[str]:
    """The refresh token's countdown. Returns what was done ("email", "pause")
    or None. One email per UTC day (``settings.token_alerted_on``)."""
    try:
        found = C.rows(sb.table(C.OAUTH).select("refresh_expires_at").eq("id", 1).limit(1).execute())
    except Exception as exc:  # noqa: BLE001
        log.warning("pinterest tokens: expiry unreadable: %s", exc)
        return None
    if not found:
        return None
    expires = C.when(found[0].get("refresh_expires_at"))
    if expires is None:
        return None
    left = expires - now
    if left >= timedelta(days=ALERT_DAYS):
        return None
    days = max(0, left.days)
    today = now.date().isoformat()
    if left < timedelta(days=PAUSE_DAYS):
        if not settings.get("paused"):
            C.merge_settings(sb, {"paused": True, "pause_reason": "token_expiring", "token_alerted_on": today}, now)
            C.send_email(settings, "Pinterest agent PAUSED: the token is about to expire",
                         f"The Pinterest refresh token expires in {days} day(s) ({expires.date()}). The agent "
                         "paused itself. Run scripts/pinterest_oauth.py to re-consent, then resume on "
                         f"{C.library_url('/library/pinterest/scheduled')}.")
            return "pause"
        return None
    if settings.get("token_alerted_on") == today:
        return None
    C.merge_settings(sb, {"token_alerted_on": today}, now)
    C.send_email(settings, f"Pinterest token expires in {days} days",
                 f"The Pinterest refresh token expires on {expires.date()}. The worker refreshes it when it posts; "
                 "if it keeps failing, run scripts/pinterest_oauth.py. The agent pauses itself at 3 days.")
    return "email"


__all__ = ["Token", "TokenKeyMissing", "generate_key", "encrypt", "decrypt", "from_grant", "load", "save",
           "access_token", "check_expiry", "KEY_ENV"]
