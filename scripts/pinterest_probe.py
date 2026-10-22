"""Answer the Pinterest Pin Engine's open questions before anything is built on them.

    python scripts/pinterest_probe.py fonts --out probe-out/          # anywhere; on Railway via `railway ssh`
    python scripts/pinterest_probe.py api   --out probe-out/          # LOCALLY, by the founder, once
    python scripts/pinterest_probe.py analytics --pin-id ID --out …   # days 1 and 2, against a kept pin

Build plan W0 (``docs/feature-guides/pinterest-pin-engine-build-plan.md`` §1.4).
It adds no job, no table and no worker code path; nothing imports it.

``fonts`` (guide Q1) needs no network. It reports whether Pillow has RAQM and
draws one 1000×1500 test card per script (Latin, Arabic, Devanagari, Telugu)
through the slide builder's own ``_font`` — the helper every pin template will
use — so the PNGs show exactly what a pin would show. Run it on the deployed
image (``railway ssh`` into the worker, then this command): a local Pillow
proves nothing about Railway's.

``api`` (Q2, Q3, Q5, Q7 and token rotation) runs the Pinterest consent flow on
a loopback port, keeps the tokens IN MEMORY ONLY (never printed, never written,
never logged), and then, against the app's own account:
  1. finds which base URL accepts the token (api vs api-sandbox — Q7);
  2. creates ONE board named ``sc-probe-<time>`` and ONE pin on it, whose link
     carries our utm tags (Trial-tier objects are visible only to us);
  3. reads the pin back twice (``GET /pins/{id}`` and ``GET /boards/{id}/pins``)
     and compares the link character for character (Q2: the adopt check needs
     it; Q3: whether our utm tags survive);
  4. optionally (``--video FILE``) uploads a short mp4 and creates a video pin
     WITHOUT a cover, recording whether Pinterest refuses it (Q5);
  5. refreshes the token once and records whether a NEW refresh token came
     back (continuous refresh, build plan §3.3) — without printing either;
  6. deletes the pin and the board it made, unless ``--keep`` (then the
     ``analytics`` subcommand can measure the data delay on days 1–2, Q4).
It touches nothing it did not create.

Everything lands in ``<out>/report.json`` (no secrets) for
``docs/feature-guides/pinterest-probe-results.md``.

Credentials: ``PINTEREST_CLIENT_ID`` / ``PINTEREST_CLIENT_SECRET`` in the shell.
The app's redirect URI must include ``http://localhost:<port>/callback``.
"""
from __future__ import annotations

import argparse
import base64
import http.server
import json
import os
import secrets
import sys
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

# Run as `python scripts/pinterest_probe.py`, the repo root is not on sys.path;
# the fonts check imports the slide builder from it.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

API_BASES = ("https://api.pinterest.com/v5", "https://api-sandbox.pinterest.com/v5")
AUTHORIZE_URL = "https://www.pinterest.com/oauth/"
TOKEN_PATH = "/oauth/token"
# What the engine will need, plus the *_secret pair so the probe board can be SECRET.
SCOPES = ("boards:read", "boards:write", "boards:read_secret", "boards:write_secret",
          "pins:read", "pins:write", "pins:read_secret", "pins:write_secret", "user_accounts:read")
CLIENT_ID_ENV = "PINTEREST_CLIENT_ID"
CLIENT_SECRET_ENV = "PINTEREST_CLIENT_SECRET"
PROBE_LINK = "https://sketchcast.app/topics/cells"
SCRIPTS = {
    "latin": "Plant and animal cells: a labelled diagram",
    "arabic": "الخلايا النباتية والحيوانية: رسم توضيحي",
    "devanagari": "पादप और जंतु कोशिकाएँ: नामांकित आरेख",
    "telugu": "మొక్క మరియు జంతు కణాలు: పేర్లతో చిత్రం",
}
CARD = (1000, 1500)


# ── pure helpers (tested) ────────────────────────────────────────────────────

def utm_link(base: str, *, content: str, language: str = "en") -> str:
    """The engine's link scheme (guide §7): our tags only, utm_content unique per pin."""
    query = urllib.parse.urlencode({
        "utm_source": "pinterest", "utm_medium": "organic", "utm_campaign": "pin_engine",
        "utm_content": content, "utm_term": language,
    })
    return f"{base}{'&' if '?' in base else '?'}{query}"


def authorize_url(client_id: str, redirect_uri: str, state: str, scopes=SCOPES) -> str:
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": ",".join(scopes), "state": state,
    })


def compare_links(sent: str, got: str | None) -> dict:
    """Q2/Q3. ``exact`` is what the adopt check wants; failing that, which of
    our utm keys survived and what Pinterest added."""
    if got is None:
        return {"exact": False, "returned": False}
    ours = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(sent).query))
    theirs = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(got).query))
    return {
        "exact": sent == got,
        "returned": True,
        "same_path": urllib.parse.urlsplit(sent)[:3] == urllib.parse.urlsplit(got)[:3],
        "utm_kept": sorted(k for k, v in ours.items() if theirs.get(k) == v),
        "utm_changed": sorted(k for k, v in ours.items() if k in theirs and theirs[k] != v),
        "utm_dropped": sorted(k for k in ours if k not in theirs),
        "added": sorted(k for k in theirs if k not in ours),
        "utm_content_found": ours.get("utm_content") is not None and ours.get("utm_content") in got,
    }


def rate_headers(headers) -> dict:
    return {k: headers.get(k) for k in ("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset")
            if headers.get(k) is not None}


def redact(value):
    """Belt and braces for the report: no token-looking field ever leaves."""
    if isinstance(value, dict):
        return {k: ("<redacted>" if "token" in k.lower() or k.lower() in {"code", "client_secret"} else redact(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


# ── fonts (Q1) ───────────────────────────────────────────────────────────────

def probe_fonts(out: Path) -> dict:
    from PIL import Image, ImageDraw

    from shared import pillow_raqm

    # status() runs the same FriBiDi preload the worker runs at startup.
    report = {**pillow_raqm.status(), "shaping_required": pillow_raqm.required(), "cards": {}}
    from agent5_slides.slide_builder import _font, _wrap, display_text

    margin = 60
    for name, text in SCRIPTS.items():
        entry: dict = {}
        try:
            font = _font(True, 64, text)
            img = Image.new("RGB", CARD, "white")
            draw = ImageDraw.Draw(img)
            lines = _wrap(draw, text, font, CARD[0] - 2 * margin)
            y, widest = 200, 0
            for line in lines:
                shown = display_text(line)
                box = draw.textbbox((margin, y), shown, font=font)
                draw.text((margin, y), shown, font=font, fill="black")
                widest = max(widest, box[2])
                y = box[3] + 16
            path = out / f"card_{name}.png"
            img.save(path)
            # Devanagari and Telugu are only correct with RAQM; Arabic is
            # reshaped in Python and Latin needs nothing. Look at the PNG anyway.
            needs_raqm = name in ("devanagari", "telugu")
            entry = {"ok": True, "file": path.name, "font": Path(getattr(font, "path", "")).name,
                     "lines": len(lines), "fits": widest <= CARD[0] - margin,
                     "shaped": (not needs_raqm) or bool(report.get("raqm"))}
        except Exception as exc:  # noqa: BLE001 — the point is to record it
            entry = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        report["cards"][name] = entry
    return report


# ── OAuth (in memory only) ───────────────────────────────────────────────────

class _Catch(http.server.BaseHTTPRequestHandler):
    result: dict = {}

    def do_GET(self):  # noqa: N802
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))
        _Catch.result = q
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"SketchCast probe: authorization received. You can close this tab.")

    def log_message(self, *args):  # silence: the query string carries the code
        pass


def consent(client_id: str, port: int) -> tuple[str, str]:
    """Returns (code, redirect_uri). The code is short-lived and single-use."""
    redirect = f"http://localhost:{port}/callback"
    state = secrets.token_urlsafe(16)
    server = http.server.HTTPServer(("127.0.0.1", port), _Catch)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    url = authorize_url(client_id, redirect, state)
    print("Opening Pinterest's consent screen; sign in as the SketchCast business account.")
    print(f"If no browser opens, visit:\n  {url}\n")
    webbrowser.open(url)
    thread.join(timeout=300)
    server.server_close()
    got = _Catch.result
    if got.get("state") != state:
        raise SystemExit("consent failed: state mismatch or no callback within 5 minutes")
    if "code" not in got:
        raise SystemExit(f"consent refused: {got.get('error', 'no code returned')}")
    return got["code"], redirect


class Api:
    def __init__(self, client_id: str, client_secret: str):
        import requests

        self._rq = requests
        self._basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        self.base = API_BASES[0]
        self._access = ""
        self._refresh = ""
        self.calls: list[dict] = []

    def _token(self, data: dict) -> dict:
        r = self._rq.post(self.base + TOKEN_PATH, data=data, timeout=30,
                          headers={"Authorization": f"Basic {self._basic}"})
        body = r.json() if r.content else {}
        if r.status_code != 200:
            raise SystemExit(f"token call failed: HTTP {r.status_code} {redact(body)}")
        self._access = body.get("access_token", "")
        new_refresh = body.get("refresh_token", "")
        rotated = bool(new_refresh) and bool(self._refresh) and new_refresh != self._refresh
        self._refresh = new_refresh or self._refresh
        return {"scope": body.get("scope"), "expires_in": body.get("expires_in"),
                "refresh_token_expires_in": body.get("refresh_token_expires_in"),
                "got_refresh_token": bool(new_refresh), "rotated": rotated}

    def exchange(self, code: str, redirect: str) -> dict:
        return self._token({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect,
                            "continuous_refresh": "true"})

    def refresh(self) -> dict:
        return self._token({"grant_type": "refresh_token", "refresh_token": self._refresh,
                            "continuous_refresh": "true"})

    def call(self, method: str, path: str, **kw):
        r = self._rq.request(method, self.base + path, timeout=60,
                             headers={"Authorization": f"Bearer {self._access}"}, **kw)
        try:
            body = r.json() if r.content else None
        except ValueError:
            body = {"raw": r.text[:300]}
        self.calls.append({"method": method, "path": path, "status": r.status_code,
                           "ratelimit": rate_headers(r.headers)})
        return r.status_code, body


# ── api (Q2, Q3, Q5, Q7, rotation) ───────────────────────────────────────────

def probe_api(args, out: Path) -> dict:
    cid = os.getenv(CLIENT_ID_ENV, "").strip()
    secret = os.getenv(CLIENT_SECRET_ENV, "").strip()
    if not cid or not secret:
        raise SystemExit(f"set {CLIENT_ID_ENV} and {CLIENT_SECRET_ENV} in this shell")
    api = Api(cid, secret)
    code, redirect = consent(cid, args.port)
    report: dict = {"token": api.exchange(code, redirect)}

    # Q7: which base accepts the token for reads.
    report["base"] = None
    for base in API_BASES:
        api.base = base
        status, body = api.call("GET", "/user_account")
        if status == 200:
            report["base"] = base
            report["account"] = {k: (body or {}).get(k) for k in ("username", "account_type")}
            break
    if not report["base"]:
        report["error"] = "no base URL accepted the token for GET /user_account"
        return report

    stamp = time.strftime("%Y%m%d-%H%M%S")
    status, board = api.call("POST", "/boards", json={
        "name": f"sc-probe-{stamp}", "description": "SketchCast API probe; deleted after the run.",
        "privacy": "SECRET"})
    report["board_create"] = {"status": status, "id": (board or {}).get("id")}
    board_id = (board or {}).get("id")
    if not board_id:
        report["board_body"] = redact(board)
        return report

    card = out / "card_latin.png"
    if not card.exists():
        report["fonts_for_api"] = probe_fonts(out)
    link = utm_link(PROBE_LINK, content=f"static-probe{stamp[-6:]}")
    status, pin = api.call("POST", "/pins", json={
        "board_id": board_id, "title": "SketchCast probe pin", "description": "API probe; deleted after the run.",
        "alt_text": "A white test card with one line of text.", "link": link,
        "media_source": {"source_type": "image_base64", "content_type": "image/png",
                         "data": base64.b64encode(card.read_bytes()).decode()}})
    pin_id = (pin or {}).get("id")
    report["pin_create"] = {"status": status, "id": pin_id, "sent_link": link}
    if pin_id:
        _, one = api.call("GET", f"/pins/{pin_id}")
        report["link_via_get_pin"] = compare_links(link, (one or {}).get("link"))
        _, page = api.call("GET", f"/boards/{board_id}/pins", params={"page_size": 25})
        listed = next((p for p in (page or {}).get("items", []) if p.get("id") == pin_id), None)
        report["link_via_board_list"] = compare_links(link, (listed or {}).get("link"))
        report["board_list_has_pin"] = listed is not None

    if args.video:
        report["video"] = probe_video(api, board_id, Path(args.video), stamp)

    report["refresh"] = api.refresh()

    if args.keep:
        report["kept"] = {"board_id": board_id, "pin_id": pin_id}
    else:
        if pin_id:
            report["pin_delete"] = api.call("DELETE", f"/pins/{pin_id}")[0]
        report["board_delete"] = api.call("DELETE", f"/boards/{board_id}")[0]
    report["calls"] = api.calls
    return report


def probe_video(api: Api, board_id: str, path: Path, stamp: str) -> dict:
    status, media = api.call("POST", "/media", json={"media_type": "video"})
    if status not in (200, 201) or not media:
        return {"register": status, "body": redact(media)}
    with path.open("rb") as fh:
        up = api._rq.post(media["upload_url"], data=media.get("upload_parameters", {}),
                          files={"file": (path.name, fh, "video/mp4")}, timeout=300)
    result = {"register": status, "upload": up.status_code, "media_id": media.get("media_id")}
    deadline = time.time() + 300
    while time.time() < deadline:
        _, st = api.call("GET", f"/media/{media['media_id']}")
        result["media_status"] = (st or {}).get("status")
        if result["media_status"] in ("succeeded", "failed"):
            break
        time.sleep(10)
    if result.get("media_status") == "succeeded":
        s, pin = api.call("POST", "/pins", json={
            "board_id": board_id, "title": "SketchCast probe video", "link": utm_link(PROBE_LINK, content=f"video-probe{stamp[-6:]}"),
            "media_source": {"source_type": "video_id", "media_id": media["media_id"]}})
        result["pin_without_cover"] = {"status": s, "id": (pin or {}).get("id"),
                                       "error": None if s in (200, 201) else redact(pin)}
        if (pin or {}).get("id"):
            api.call("DELETE", f"/pins/{pin['id']}")
    return result


def probe_analytics(args) -> dict:
    cid = os.getenv(CLIENT_ID_ENV, "").strip()
    secret = os.getenv(CLIENT_SECRET_ENV, "").strip()
    api = Api(cid, secret)
    code, redirect = consent(cid, args.port)
    api.exchange(code, redirect)
    api.base = args.base
    today = time.strftime("%Y-%m-%d")
    start = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 3 * 86400))
    status, body = api.call("GET", f"/pins/{args.pin_id}/analytics", params={
        "start_date": start, "end_date": today, "metric_types": "IMPRESSION,SAVE,PIN_CLICK,OUTBOUND_CLICK"})
    return {"asked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "status": status, "body": redact(body)}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Pinterest Pin Engine probe (build plan W0).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fonts")
    f.add_argument("--out", default="probe-out")
    a = sub.add_parser("api")
    a.add_argument("--out", default="probe-out")
    a.add_argument("--port", type=int, default=8765)
    a.add_argument("--video", default=None, help="a short mp4 (4 s or more) for the video-pin check")
    a.add_argument("--keep", action="store_true", help="keep the probe board and pin for the analytics check")
    n = sub.add_parser("analytics")
    n.add_argument("--out", default="probe-out")
    n.add_argument("--port", type=int, default=8765)
    n.add_argument("--pin-id", required=True)
    n.add_argument("--base", default=API_BASES[0])
    args = ap.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if args.cmd == "fonts":
        report = {"fonts": probe_fonts(out)}
    elif args.cmd == "api":
        report = {"api": probe_api(args, out)}
    else:
        report = {"analytics": probe_analytics(args)}
    report = redact(report)
    path = out / f"report-{args.cmd}.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nwritten: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
