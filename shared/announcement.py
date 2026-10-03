"""A product announcement to the members, in SketchCast's own clothes.

The founder's ask (2026-10-03): tell teachers (never students) what is new —
lessons drawn in colour, and the YouTube channel — in an email that looks
like SketchCast rather than a mail template, with a sample to one address
before anyone else sees it.

The email is HTML with a plain-text twin, built here with the portal's own
tokens (sketchcast-app globals.css: canvas, paper, ink, teal, marker, the
hairline) and sent through Resend from the worker, which holds the key and
can reach the API. One job (``announcement_email``) does both the sample and
the send:

    params.campaign   which announcement (CAMPAIGNS)
    params.to         a list of addresses — the sample; nothing else is read
    params.audience   "members": every account that is not a student, not
                      opted out, not suspended, not a demo, and has an email
    params.dry_run    count the audience and send nothing
    params.force      send a campaign that was already sent

A campaign sent to the audience is recorded in platform_settings under
``announcement_<campaign>`` and refused a second time without ``force``.
The audience send also needs LIFECYCLE_TOKEN_SECRET (the portal's own
unsubscribe HMAC, lifecycle/token.ts) so every email carries the one-click
unsubscribe link the lifecycle emails carry; without it the sample still
goes out (its footer says the link is missing) and the audience send is
refused. Resend's batch endpoint takes up to 100 messages a call.
"""

from __future__ import annotations

import hashlib
import hmac
import html as html_mod
import logging
import os
from datetime import datetime, timezone
from typing import Callable, Optional

from worker import client as db

log = logging.getLogger(__name__)

JOB_TYPE = "announcement_email"
FROM = "SketchCast <noreply@sketchcast.app>"
APP_URL = "https://app.sketchcast.app"
CHANNEL_URL = "https://www.youtube.com/@Sketchcast-AI-app"
BATCH = 100
STUDENT_DOMAIN = "@students.sketchcast.app"

# the portal's palette (sketchcast-app src/app/globals.css)
CANVAS, PAPER, MIST = "#fcfcfa", "#ffffff", "#f5f6f3"
INK, TEAL, TEAL_INK, TEAL_MIST = "#14181f", "#1fb8a6", "#0c8175", "#e2f4f1"
MARKER, GRAPHITE, FAINT, LINE = "#ffb020", "#5b6470", "#98a0a9", "#e6e8e4"
DISPLAY = "'Space Grotesk', 'Helvetica Neue', Arial, sans-serif"
SANS = "Inter, 'Helvetica Neue', Arial, sans-serif"
MONO = "'JetBrains Mono', Menlo, Consolas, monospace"


def _video(vid: str, title: str) -> dict:
    return {"id": vid, "title": title, "url": f"https://www.youtube.com/watch?v={vid}",
            "thumb": f"https://img.youtube.com/vi/{vid}/hqdefault.jpg"}


CAMPAIGNS: dict[str, dict] = {
    "colour_and_youtube_2026_10": {
        "subject": "New on SketchCast: lessons in colour, and our YouTube channel",
        "preheader": "The board now draws in colour, and the topic library is on YouTube.",
        "chip": "Product update · October 2026",
        "headline": ("The board just got ", "colour", "."),
        "videos": [_video("FkEZ-oiQSnA", "Photosynthesis"),
                   _video("fCs9F7Fqz5w", "Forces and Motion"),
                   _video("-3FnkvQ2JtQ", "Elements, Compounds and Mixtures"),
                   _video("bVgF1Msv4vw", "Weather and Climate")],
        "playlists": [("Biology", "PLcUMxCw3sDwo"), ("Chemistry", "PLAXVsA7M1SAI"),
                      ("Physics", "PLEKo_rsBNz6U"), ("Algebra", "PLEf4GW33Vuz8")],
    },
}


# ── the words ───────────────────────────────────────────────────────────────

def _e(s: str) -> str:
    return html_mod.escape(str(s), quote=True)


def _playlist_url(pl_id: str) -> str:
    return f"https://www.youtube.com/playlist?list={pl_id}"


def render_html(campaign: str, first_name: Optional[str], unsubscribe_url: Optional[str]) -> str:
    """The email, in the portal's clothes: canvas behind a paper card, ink
    for words, teal for the strokes, one marker highlight, the hairline
    between sections. Tables and inline styles, as email needs."""
    c = CAMPAIGNS[campaign]
    hi = f"Hi {_e(first_name)}," if first_name else "Hi there,"
    pre, word, post = c["headline"]
    thumbs = "".join(
        f'<td width="50%" valign="top" style="padding:0 {"8px 16px 0" if i % 2 == 0 else "0 16px 8px"};">'
        f'<a href="{_e(v["url"])}" style="text-decoration:none;color:{INK};">'
        f'<img src="{_e(v["thumb"])}" width="264" alt="{_e(v["title"])}" '
        f'style="display:block;width:100%;max-width:264px;height:auto;border-radius:10px;border:1px solid {LINE};">'
        f'<div style="font-family:{SANS};font-size:13px;line-height:18px;color:{GRAPHITE};padding-top:8px;">'
        f'<span style="color:{TEAL_INK};">&#9654;</span>&nbsp;{_e(v["title"])}</div></a></td>'
        + ("</tr><tr>" if i % 2 == 1 and i < len(c["videos"]) - 1 else "")
        for i, v in enumerate(c["videos"]))
    chips = "".join(
        f'<a href="{_e(_playlist_url(pid))}" style="display:inline-block;font-family:{MONO};font-size:12px;'
        f'letter-spacing:.04em;color:{TEAL_INK};background:{MIST};border:1px solid {LINE};border-radius:999px;'
        f'padding:6px 12px;margin:0 8px 8px 0;text-decoration:none;">{_e(name)}</a>'
        for name, pid in c["playlists"])
    unsub = (f'<a href="{_e(unsubscribe_url)}" style="color:{GRAPHITE};text-decoration:underline;">Unsubscribe</a>'
             if unsubscribe_url else
             f'<span style="color:{MARKER};">Unsubscribe link goes here once the worker has the portal\'s token secret</span>')

    def h2(t: str) -> str:
        return (f'<h2 style="font-family:{DISPLAY};font-size:19px;line-height:26px;font-weight:600;color:{INK};'
                f'margin:0 0 10px;">{t}</h2>')

    def p(t: str) -> str:
        return f'<p style="font-family:{SANS};font-size:15.5px;line-height:25px;color:{INK};margin:0 0 14px;">{t}</p>'

    def tick(t: str) -> str:
        return (f'<tr><td valign="top" width="22" style="padding:0 0 9px;font-family:{SANS};font-size:15px;'
                f'line-height:24px;color:{TEAL};font-weight:700;">&#10003;</td>'
                f'<td valign="top" style="padding:0 0 9px;font-family:{SANS};font-size:15px;line-height:24px;color:{INK};">{t}</td></tr>')

    def rule() -> str:
        return f'<tr><td style="padding:22px 0 24px;"><div style="height:1px;background:{LINE};line-height:1px;font-size:1px;">&nbsp;</div></td></tr>'

    def button(href: str, label: str, dark: bool = True) -> str:
        bg, fg, border = (INK, PAPER, INK) if dark else (PAPER, INK, LINE)
        return (f'<a href="{_e(href)}" style="display:inline-block;font-family:{SANS};font-size:15px;font-weight:500;'
                f'color:{fg};background:{bg};border:1px solid {border};border-radius:9px;padding:12px 22px;'
                f'text-decoration:none;margin:0 10px 10px 0;">{label}</a>')

    body_rows = [
        f'<tr><td>{p(hi)}'
        + p("Two things we have been working on since the summer are live, and both are things you can see.")
        + '</td></tr>',
        f'<tr><td>{h2("Lessons drawn in colour")}'
        + p("Every lesson video is drawn on the board the way a teacher draws it. Until now that was ink only. "
            "Now the board carries colour, used sparingly and always for a reason:")
        + '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">'
        + tick("Relationship arrows take a second accent, leader arrows the first, so what points at what is clear at a glance.")
        + tick("Equations are coloured side by side, so the term that moves is the term you can follow.")
        + tick("Pictures are drawn as outlines first, then the colour washes in under each part once its outline is complete, the way you would colour a diagram after drawing it.")
        + tick("Labels sit on the parts they name, and a zoom keeps a picture's labels in the frame with it.")
        + '</table>'
        + p("Here are four lessons from our topic library, drawn this way:")
        + f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"><tr>{thumbs}</tr></table>'
        + '</td></tr>',
        rule(),
        f'<tr><td>{h2("SketchCast is on YouTube")}'
        + p("Our topic library is now a public channel: free lessons on the core science and maths topics, "
            "in four playlists, with more added every week. Share a link with a class, or set one as homework.")
        + f'<div style="padding:4px 0 14px;">{chips}</div>'
        + button(CHANNEL_URL, "Watch on YouTube")
        + button(f"{CHANNEL_URL}?sub_confirmation=1", "Subscribe", dark=False)
        + '</td></tr>',
        rule(),
        f'<tr><td>{h2("And your own lessons")}'
        + p("Nothing to switch on. Every lesson video you generate from your own book from today draws this way, "
            "along with its slide deck, worksheet, test paper and the rest of the kit.")
        + button(f"{APP_URL}/dashboard", "Open SketchCast")
        + '</td></tr>',
        f'<tr><td style="padding-top:8px;">'
        + p("If you face any issue with it, or anything else, just reply to this email and we will take it up.")
        + f'<p style="font-family:{SANS};font-size:15.5px;line-height:25px;color:{INK};margin:0;">Muqtadar<br>'
        f'<span style="color:{GRAPHITE};">SketchCast</span></p></td></tr>',
    ]
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><title>{_e(c["subject"])}</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=Inter:wght@400;500&family=JetBrains+Mono:wght@500&display=swap" rel="stylesheet">
</head>
<body style="margin:0;padding:0;background:{CANVAS};">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent;">{_e(c["preheader"])}</div>
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background:{CANVAS};">
<tr><td align="center" style="padding:36px 16px;">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="600" style="max-width:600px;width:100%;">
<tr><td style="padding:0 4px 18px;">
  <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
    <td width="30" height="30" align="center" valign="middle" style="width:30px;height:30px;background:{INK};border-radius:8px;font-family:{DISPLAY};font-size:17px;font-weight:700;color:{TEAL};line-height:30px;">S</td>
    <td style="padding-left:10px;font-family:{DISPLAY};font-size:17px;font-weight:600;color:{INK};letter-spacing:-.01em;">SketchCast</td>
  </tr></table>
</td></tr>
<tr><td style="background:{PAPER};border:1px solid {LINE};border-radius:14px;padding:34px 34px 30px;">
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
  <tr><td style="padding-bottom:16px;"><span style="display:inline-block;font-family:{MONO};font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:{TEAL_INK};background:{MIST};border-radius:999px;padding:4px 10px;">{_e(c["chip"])}</span></td></tr>
  <tr><td style="padding-bottom:18px;"><h1 style="font-family:{DISPLAY};font-size:28px;line-height:36px;font-weight:600;color:{INK};margin:0;letter-spacing:-.01em;">{_e(pre)}<span style="box-shadow:inset 0 -0.42em 0 {MARKER};">{_e(word)}</span>{_e(post)}</h1></td></tr>
  {"".join(body_rows)}
  </table>
</td></tr>
<tr><td style="padding:22px 8px 0;font-family:{SANS};font-size:12.5px;line-height:19px;color:{GRAPHITE};">
  You're receiving this because you registered at <a href="https://sketchcast.app" style="color:{GRAPHITE};">sketchcast.app</a>.
  {unsub} &nbsp;&middot;&nbsp; <a href="{_e(CHANNEL_URL)}" style="color:{GRAPHITE};">YouTube</a>
</td></tr>
</table>
</td></tr></table>
</body></html>"""


def render_text(campaign: str, first_name: Optional[str], unsubscribe_url: Optional[str]) -> str:
    c = CAMPAIGNS[campaign]
    pre, word, post = c["headline"]
    lines = [f"Hi {first_name}," if first_name else "Hi there,", "",
             f"{pre}{word}{post}", "",
             "Two things we have been working on since the summer are live, and both are things you can see.", "",
             "LESSONS DRAWN IN COLOUR", "",
             "Every lesson video is drawn on the board the way a teacher draws it. Until now that was ink only. "
             "Now the board carries colour, used sparingly and always for a reason:",
             "- Relationship arrows take a second accent, leader arrows the first, so what points at what is clear at a glance.",
             "- Equations are coloured side by side, so the term that moves is the term you can follow.",
             "- Pictures are drawn as outlines first, then the colour washes in under each part once its outline is complete.",
             "- Labels sit on the parts they name, and a zoom keeps a picture's labels in the frame with it.", "",
             "Four lessons from our topic library, drawn this way:"]
    lines += [f"- {v['title']}: {v['url']}" for v in c["videos"]]
    lines += ["", "SKETCHCAST IS ON YOUTUBE", "",
              "Our topic library is now a public channel: free lessons on the core science and maths topics, in four "
              "playlists, with more added every week. Share a link with a class, or set one as homework.", ""]
    lines += [f"- {name}: {_playlist_url(pid)}" for name, pid in c["playlists"]]
    lines += ["", f"Watch and subscribe: {CHANNEL_URL}", "",
              "AND YOUR OWN LESSONS", "",
              "Nothing to switch on. Every lesson video you generate from your own book from today draws this way, "
              "along with its slide deck, worksheet, test paper and the rest of the kit.",
              f"Open SketchCast: {APP_URL}/dashboard", "",
              "If you face any issue with it, or anything else, just reply to this email and we will take it up.", "",
              "Muqtadar", "SketchCast", "", "---",
              "You're receiving this because you registered at sketchcast.app."]
    lines.append(f"Unsubscribe: {unsubscribe_url}" if unsubscribe_url else "Unsubscribe: reply with the word unsubscribe.")
    return "\n".join(lines)


# ── the people ──────────────────────────────────────────────────────────────

def unsubscribe_url(user_id: str, secret: Optional[str] = None) -> Optional[str]:
    """The portal's one-click unsubscribe link (lifecycle/token.ts: the user
    id, a dot, the first 32 hex of an HMAC-SHA256 of it). None without the
    secret — a wrong link is worse than none."""
    secret = secret if secret is not None else os.getenv("LIFECYCLE_TOKEN_SECRET", "")
    if not secret or not user_id:
        return None
    mac = hmac.new(secret.encode("utf-8"), str(user_id).encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    return f"{APP_URL}/api/lifecycle/unsubscribe?t={user_id}.{mac}"


def first_name(full_name: Optional[str]) -> Optional[str]:
    name = " ".join(str(full_name or "").split())
    return name.split(" ")[0] if name else None


def _auth_emails(sb) -> dict[str, str]:
    """id -> email for every auth user, page by page."""
    out: dict[str, str] = {}
    page = 1
    while True:
        res = sb.auth.admin.list_users(page=page, per_page=200)
        users = res if isinstance(res, list) else getattr(res, "users", None) or []
        if not users:
            break
        for u in users:
            uid, email = getattr(u, "id", None), getattr(u, "email", None)
            if uid and email:
                out[str(uid)] = str(email)
        if len(users) < 200:
            break
        page += 1
    return out


def audience(sb) -> list[dict]:
    """Every member to write to: an account with an email that is not a
    student (by role or by the students' domain), not opted out of our
    emails, not suspended and not a demo. ``[{id, email, first_name}]``."""
    emails = _auth_emails(sb)
    res = sb.table("profiles").select("id,role,full_name,email_optout_at,suspended_at,is_demo").execute()
    rows = getattr(res, "data", None) or []
    out: list[dict] = []
    for p in rows:
        uid = str(p.get("id") or "")
        email = emails.get(uid, "")
        if not email or email.lower().endswith(STUDENT_DOMAIN):
            continue
        if str(p.get("role") or "") == "student" or p.get("email_optout_at") or p.get("suspended_at") or p.get("is_demo"):
            continue
        out.append({"id": uid, "email": email, "first_name": first_name(p.get("full_name"))})
    out.sort(key=lambda r: r["email"].lower())
    return out


# ── the send ────────────────────────────────────────────────────────────────

def message(campaign: str, to: str, first: Optional[str], unsub: Optional[str]) -> dict:
    c = CAMPAIGNS[campaign]
    msg = {"from": FROM, "to": [to], "reply_to": os.getenv("SUPPORT_STAFF_EMAIL", "muqtadar.quraishi@sketchcast.app"),
           "subject": c["subject"], "html": render_html(campaign, first, unsub),
           "text": render_text(campaign, first, unsub)}
    if unsub:
        msg["headers"] = {"List-Unsubscribe": f"<{unsub}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}
    return msg


def resend_batch(messages: list[dict]) -> int:
    """POST the batch to Resend; the number accepted. Raises on a refusal."""
    import requests

    key = os.getenv("RESEND_API_KEY")
    if not key:
        raise RuntimeError("RESEND_API_KEY not set")
    r = requests.post("https://api.resend.com/emails/batch",
                      headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                      json=messages, timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"Resend {r.status_code}: {r.text[:300]}")
    data = r.json().get("data") if isinstance(r.json(), dict) else None
    return len(data) if isinstance(data, list) else len(messages)


Transport = Callable[[list[dict]], int]


def send(sb, params: dict, transport: Optional[Transport] = None) -> dict:
    """The job's work. Returns the summary the job records."""
    transport = transport or resend_batch
    campaign = str(params.get("campaign") or "")
    if campaign not in CAMPAIGNS:
        raise RuntimeError(f"unknown campaign {campaign!r}; one of {sorted(CAMPAIGNS)}")
    to = params.get("to")
    secret = os.getenv("LIFECYCLE_TOKEN_SECRET", "")
    if isinstance(to, list) and to:
        # the sample: these addresses, no audience, no record
        recipients = [{"id": "", "email": str(a).strip(), "first_name": None} for a in to if str(a).strip()]
        mode = "sample"
    elif str(params.get("audience") or "") == "members":
        recipients = audience(sb)
        mode = "audience"
    else:
        raise RuntimeError("say who: params.to (a sample) or params.audience = 'members'")
    summary = {"campaign": campaign, "mode": mode, "recipients": len(recipients), "sent": 0, "batches": 0,
               "unsubscribe_links": bool(secret)}
    if params.get("dry_run"):
        summary["dry_run"] = True
        return summary
    if mode == "audience":
        if not secret:
            raise RuntimeError("audience send refused: LIFECYCLE_TOKEN_SECRET is not set on the worker, so the "
                               "emails would carry no unsubscribe link")
        key = f"announcement_{campaign}"
        prior = sb.table("platform_settings").select("value").eq("key", key).limit(1).execute()
        rows = getattr(prior, "data", None) or []
        if rows and not params.get("force"):
            raise RuntimeError(f"campaign {campaign} was already sent ({(rows[0].get('value') or {}).get('sent_at')}); "
                               "params.force = true to send again")
    for i in range(0, len(recipients), BATCH):
        chunk = recipients[i:i + BATCH]
        msgs = [message(campaign, r["email"], r["first_name"], unsubscribe_url(r["id"], secret) if r["id"] else None)
                for r in chunk]
        summary["sent"] += transport(msgs)
        summary["batches"] += 1
    if mode == "audience":
        sb.table("platform_settings").upsert(
            {"key": f"announcement_{campaign}",
             "value": {"sent_at": datetime.now(timezone.utc).isoformat(), "sent": summary["sent"],
                       "recipients": summary["recipients"]}},
            on_conflict="key").execute()
    return summary


def run_announcement_job(sb, job: dict, transport: Optional[Transport] = None) -> Optional[dict]:
    """Entry point for run.py. Self-contained: finishes its own row with the
    summary in ``stage``, or error with the sentence."""
    job_id = job["id"]
    params = job.get("params") if isinstance(job.get("params"), dict) else {}
    try:
        summary = send(sb, params, transport)
        db.set_stage(sb, job_id, summary)
        db.finish_job(sb, job_id)
        log.info("announcement job %s: %s", job_id, summary)
        return summary
    except Exception as exc:  # noqa: BLE001
        log.error("announcement job %s failed: %s", job_id, exc)
        try:
            db.finish_job(sb, job_id, None, error=f"{type(exc).__name__}: {exc}"[:4000])
        except Exception as exc2:  # noqa: BLE001
            log.error("announcement job %s: could not record the failure: %s", job_id, exc2)
        return None


__all__ = ["JOB_TYPE", "CAMPAIGNS", "render_html", "render_text", "unsubscribe_url", "first_name", "audience",
           "message", "send", "run_announcement_job"]
