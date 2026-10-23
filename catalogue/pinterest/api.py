"""A thin Pinterest API v5 client over ``requests`` — only the calls the Pin
Engine makes, and the failure readings the engine acts on.

WHY THESE READINGS (probe results, 2026-10-23):

  * 429 → ``DeferredJob(max(30, x-ratelimit-reset))``: a wait, not a failure,
    and no attempt is spent (guide §11).
  * 401 → ``TokenInvalid``: the agent pauses and emails; nothing else moves.
  * 403 code 29 → ``TrialAccessOnly``: "Apps with Trial access may not create
    Pins in production". A clear message, because the fix is the founder's
    Standard-access request (F9), not a retry.
  * any other 403 → ``Forbidden`` (an account-level block reads like this):
    the breaker trips; the engine never retries its way past a refusal.
  * 5xx / a dropped connection on a READ (GET) → two more tries with jitter.
    A WRITE (POST) is sent exactly ONCE: a timeout, a reset connection or a
    5xx on ``POST /pins`` may still have created the pin, so it raises
    ``AmbiguousWrite`` and the caller adopts (lists the board) before it ever
    posts again. A blind retry is how a pin gets posted twice.

Every refusal keeps Pinterest's own ``{code, message}``. Nothing here ever
logs, raises or returns a token outside ``exchange_code`` / ``refresh``,
whose answers the caller encrypts at once (``tokens``).

``PINTEREST_API_BASE`` picks the host (default production; the sandbox host
``https://api-sandbox.pinterest.com/v5`` takes the portal's sandbox token).
The OAuth token endpoint always lives on the production host
(``PINTEREST_OAUTH_BASE``): the probe refreshed there.
"""

from __future__ import annotations

import base64
import logging
import os
import random
import time
from typing import Any, Optional

from worker import client as db

log = logging.getLogger("worker.pinterest.api")

BASE_ENV = "PINTEREST_API_BASE"
OAUTH_BASE_ENV = "PINTEREST_OAUTH_BASE"
DEFAULT_BASE = "https://api.pinterest.com/v5"
SANDBOX_HOST = "api-sandbox.pinterest.com"
CLIENT_ID_ENV = "PINTEREST_CLIENT_ID"
CLIENT_SECRET_ENV = "PINTEREST_CLIENT_SECRET"
TIMEOUT = 60
RETRIES_5XX = 2
IDEMPOTENT_METHODS = ("GET", "HEAD", "DELETE")
MIN_DEFER = 30
TRIAL_CODE = 29


class PinterestError(RuntimeError):
    """A non-2xx answer: ``status`` and Pinterest's own ``code`` / ``message``."""

    def __init__(self, status: int, code: Any = None, message: str = "", path: str = ""):
        self.status, self.code, self.message, self.path = int(status or 0), code, str(message or ""), path
        super().__init__(f"Pinterest {path or 'call'} answered HTTP {self.status}"
                         f"{f' (code {code})' if code is not None else ''}: {self.message[:300]}")


class TokenInvalid(PinterestError):
    """401, or no usable token: the agent pauses and the founder re-consents."""


class TrialAccessOnly(PinterestError):
    """403 code 29: a Trial app cannot create pins on the production host."""

    def __init__(self, status: int, code: Any = None, message: str = "", path: str = ""):
        super().__init__(status, code, message, path)
        self.args = ("Pinterest refused: this app has Trial access, which cannot create Pins in production "
                     "(code 29). Use the sandbox host (PINTEREST_API_BASE) until Standard access is granted. "
                     f"Pinterest said: {self.message[:200]}",)


class Forbidden(PinterestError):
    """Any other 403 — an account-level refusal. Trips the breaker."""


class AmbiguousWrite(PinterestError):
    """A write whose outcome is unknown (no answer, or a 5xx): it may have
    landed. Never retried here; the caller looks before it writes again."""


class TokenUnavailable(PinterestError):
    """The token endpoint did not answer usefully (5xx, a dropped connection,
    an unexpected status). Transient: wait and refresh later — the token is
    not known to be bad, so this never trips the breaker."""


def api_base() -> str:
    return (os.getenv(BASE_ENV) or DEFAULT_BASE).strip().rstrip("/")


def oauth_base() -> str:
    return (os.getenv(OAUTH_BASE_ENV) or DEFAULT_BASE).strip().rstrip("/")


def is_sandbox(base: Optional[str] = None) -> bool:
    return SANDBOX_HOST in (base or api_base())


def rate_headers(headers) -> dict:
    """The ``x-ratelimit-*`` headers, lower-cased."""
    out = {}
    for k, v in dict(headers or {}).items():
        if str(k).lower().startswith("x-ratelimit"):
            out[str(k).lower()] = v
    return out


def reset_seconds(headers, now: Optional[float] = None) -> int:
    """Seconds until the limit resets. Pinterest sends a count of seconds; an
    epoch timestamp is read as one too. Unknown → MIN_DEFER."""
    raw = rate_headers(headers).get("x-ratelimit-reset")
    try:
        v = float(str(raw).split(",")[0].strip())
    except (TypeError, ValueError):
        return MIN_DEFER
    if v > 1e9:
        v -= now if now is not None else time.time()
    return max(MIN_DEFER, int(v + 0.999))


def _body(resp) -> Any:
    try:
        return resp.json() if getattr(resp, "content", b"") else None
    except ValueError:
        return {"message": str(getattr(resp, "text", ""))[:300]}


class Api:
    """``access_token`` is the bearer for every call except the token calls.
    ``session`` is anything with ``request(method, url, **kw)`` — tests pass a
    fake; production uses ``requests``."""

    def __init__(self, access_token: str = "", *, base: Optional[str] = None, session=None,
                 sleep=time.sleep, client_id: Optional[str] = None, client_secret: Optional[str] = None):
        if session is None:
            import requests

            session = requests.Session()
        self._token = access_token or ""
        self.base = (base or api_base()).rstrip("/")
        self._session = session
        self._sleep = sleep
        self._client_id = client_id if client_id is not None else os.getenv(CLIENT_ID_ENV, "")
        self._client_secret = client_secret if client_secret is not None else os.getenv(CLIENT_SECRET_ENV, "")
        self.last_ratelimit: dict = {}

    def __repr__(self) -> str:  # never the token
        return f"<pinterest.Api {self.base}>"

    # ── the one transport ──
    def _send(self, method: str, url: str, path: str, **kw):
        """One request; a READ is retried twice on a dropped connection or a
        5xx, a WRITE never is (``AmbiguousWrite``: it may have landed)."""
        retry = method.upper() in IDEMPOTENT_METHODS
        tries = 0
        while True:
            tries += 1
            try:
                resp = self._session.request(method, url, timeout=TIMEOUT, **kw)
            except Exception as exc:  # noqa: BLE001
                if retry and tries <= RETRIES_5XX:
                    self._sleep(random.uniform(1.0, 3.0) * tries)
                    continue
                cls = PinterestError if retry else AmbiguousWrite
                raise cls(0, None, f"{type(exc).__name__}: {exc}", path) from None
            status = int(getattr(resp, "status_code", 0) or 0)
            self.last_ratelimit = rate_headers(getattr(resp, "headers", {}))
            if 500 <= status < 600:
                if retry and tries <= RETRIES_5XX:
                    self._sleep(random.uniform(1.0, 3.0) * tries)
                    continue
                if not retry:
                    body = _body(resp)
                    raise AmbiguousWrite(status, body.get("code") if isinstance(body, dict) else None,
                                         (body.get("message") if isinstance(body, dict) else "") or "server error",
                                         path)
            return resp, status

    def _raise_for(self, resp, status: int, path: str) -> None:
        body = _body(resp)
        code = body.get("code") if isinstance(body, dict) else None
        message = body.get("message") if isinstance(body, dict) else ""
        if status == 429:
            wait = reset_seconds(getattr(resp, "headers", {}))
            raise db.DeferredJob(max(MIN_DEFER, wait), f"Pinterest rate limit; waiting {wait}s",
                                 {"pinterest_rate_limited": True})
        if status == 401:
            raise TokenInvalid(status, code, message or "the token was refused", path)
        if status == 403:
            try:
                trial = int(code) == TRIAL_CODE
            except (TypeError, ValueError):
                trial = False
            raise (TrialAccessOnly if trial else Forbidden)(status, code, message, path)
        raise PinterestError(status, code, message, path)

    def call(self, method: str, path: str, *, params: Optional[dict] = None, json: Optional[dict] = None) -> Any:
        if not self._token:
            raise TokenInvalid(401, None, "no access token", path)
        resp, status = self._send(method, self.base + path, path, params=params, json=json,
                                  headers={"Authorization": f"Bearer {self._token}"})
        if not 200 <= status < 300:
            self._raise_for(resp, status, path)
        return _body(resp)

    # ── reads and writes the engine makes ──
    def user_account(self) -> dict:
        return self.call("GET", "/user_account") or {}

    def list_boards(self, page_size: int = 100, max_pages: int = 10) -> list[dict]:
        out: list[dict] = []
        bookmark = None
        for _ in range(max_pages):
            params = {"page_size": page_size, **({"bookmark": bookmark} if bookmark else {})}
            body = self.call("GET", "/boards", params=params) or {}
            out.extend(i for i in (body.get("items") or []) if isinstance(i, dict))
            bookmark = body.get("bookmark")
            if not bookmark:
                break
        return out

    def create_board(self, name: str, description: str = "", privacy: str = "PUBLIC") -> dict:
        return self.call("POST", "/boards", json={"name": name, "description": description, "privacy": privacy}) or {}

    def list_sections(self, board_id: str) -> list[dict]:
        body = self.call("GET", f"/boards/{board_id}/sections", params={"page_size": 100}) or {}
        return [i for i in (body.get("items") or []) if isinstance(i, dict)]

    def create_section(self, board_id: str, name: str) -> dict:
        return self.call("POST", f"/boards/{board_id}/sections", json={"name": name}) or {}

    def list_board_pins(self, board_id: str, page_size: int = 25) -> list[dict]:
        body = self.call("GET", f"/boards/{board_id}/pins", params={"page_size": page_size}) or {}
        return [i for i in (body.get("items") or []) if isinstance(i, dict)]

    def create_pin(self, payload: dict) -> dict:
        return self.call("POST", "/pins", json=payload) or {}

    def pin_analytics(self, pin_id: str, start: str, end: str, metrics) -> dict:
        return self.call("GET", f"/pins/{pin_id}/analytics",
                         params={"start_date": start, "end_date": end, "metric_types": ",".join(metrics)}) or {}

    # ── the token endpoint (Basic auth with the app's id and secret) ──
    def _token_call(self, data: dict) -> dict:
        """One POST to the token endpoint. 400 / 401 → ``TokenInvalid`` (the
        grant is bad: re-consent); 429 → ``DeferredJob``; anything else —
        5xx, a dropped connection — → ``TokenUnavailable`` (try later)."""
        if not (self._client_id and self._client_secret):
            raise TokenInvalid(401, None, f"{CLIENT_ID_ENV} / {CLIENT_SECRET_ENV} are not set", "/oauth/token")
        basic = base64.b64encode(f"{self._client_id}:{self._client_secret}".encode()).decode()
        try:
            resp, status = self._send("POST", oauth_base() + "/oauth/token", "/oauth/token", data=data,
                                      headers={"Authorization": f"Basic {basic}"})
        except AmbiguousWrite as exc:
            raise TokenUnavailable(exc.status, exc.code, exc.message, "/oauth/token") from None
        body = _body(resp)
        code = body.get("code") if isinstance(body, dict) else None
        message = (body.get("message") if isinstance(body, dict) else "") or ""
        if status in (400, 401):
            raise TokenInvalid(status, code, message or "token call refused", "/oauth/token")
        if status == 429:
            self._raise_for(resp, status, "/oauth/token")
        if not 200 <= status < 300:
            raise TokenUnavailable(status, code, message or "the token endpoint answered unexpectedly",
                                   "/oauth/token")
        if not isinstance(body, dict) or not body.get("access_token"):
            raise TokenUnavailable(status, None, "the token call answered without an access token", "/oauth/token")
        return body

    def refresh(self, refresh_token: str) -> dict:
        return self._token_call({"grant_type": "refresh_token", "refresh_token": refresh_token,
                                 "continuous_refresh": "true"})

    def exchange_code(self, code: str, redirect_uri: str) -> dict:
        return self._token_call({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                                 "continuous_refresh": "true"})


__all__ = ["Api", "PinterestError", "TokenInvalid", "TrialAccessOnly", "Forbidden", "AmbiguousWrite",
           "TokenUnavailable", "api_base", "oauth_base",
           "is_sandbox", "rate_headers", "reset_seconds"]
