"""What an external picture really IS (founder 2026-10-25, decisions 1 and 3).

The deck sourcer labelled photos by what it had been ASKED for: an "Amoeba"
that was a colitis biopsy, "normal red blood cells" that were malaria-infected.
Here identity comes from the SOURCE: the Wikimedia Commons API (title,
description, author, licence), or the provider page's own title for Flickr and
Rawpixel. The record is cached in ``pub.asset_identity`` and shown to the author
and to every gate, so no picture is ever described from its request alone.

Three findings are kept apart: an identity that contradicts our label, missing
attribution, and a licence problem. A licence we hold evidence for stays valid
even when the source now shows another (CC licences are irrevocable); both are
recorded.
"""
from __future__ import annotations

import html as _html
import json
import re
from urllib.parse import unquote, urlparse

import httpx

# Generic content flag, not a subject rule: disease and pathology pictures are
# not general illustrations of healthy structure.
MEDICAL = re.compile(r"patholog|histopath|colitis|biopsy|carcinoma|tumou?r|lesion|necros|infect|parasit|malaria|"
                     r"autopsy|wound|disease|siderophag|congestion|lesion|cancer|syndrome|lymphoma", re.I)
UA = {"User-Agent": "SketchCast-publishing/0.1 (book source verification; contact muqtadar.quraishi@sketchcast.app)"}


def _strip(s: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def _commons(url: str) -> dict | None:
    m = re.search(r"/wiki/(File:[^?#]+)", url)
    if not m:
        return None
    title = unquote(m.group(1))
    r = httpx.get("https://commons.wikimedia.org/w/api.php", headers=UA, timeout=30, params={
        "action": "query", "titles": title, "prop": "imageinfo", "iiprop": "extmetadata|url", "format": "json"})
    pages = (r.json().get("query") or {}).get("pages") or {}
    page = next(iter(pages.values()), {})
    meta = ((page.get("imageinfo") or [{}])[0].get("extmetadata") or {})
    val = lambda k: _strip((meta.get(k) or {}).get("value", ""))  # noqa: E731
    return {"title": val("ObjectName") or title[5:], "description": val("ImageDescription")[:600],
            "author": val("Artist")[:120], "licence_seen": val("LicenseShortName"),
            "evidence": {"source": "commons-api", "file": title, "credit": val("Credit")[:300],
                         "date": val("DateTimeOriginal") or val("DateTime")}}


def _flickr(url: str) -> dict | None:
    """Flickr's oEmbed endpoint gives the photo's own title, author and licence
    (its HTML page does not render them for a script)."""
    r = httpx.get("https://www.flickr.com/services/oembed/", headers=UA, timeout=30,
                  params={"url": url, "format": "json"})
    if r.status_code >= 400:
        return None
    j = r.json()
    return {"title": _strip(j.get("title", "")), "description": "", "author": _strip(j.get("author_name", "")),
            "licence_seen": j.get("license") or "",
            "evidence": {"source": "flickr-oembed", "license_url": j.get("license_url"), "license_id": j.get("license_id")}}


def _page(url: str) -> dict | None:
    r = httpx.get(url, headers=UA, timeout=30, follow_redirects=True)
    if r.status_code >= 400:
        return None
    t = r.text
    og = lambda p: (re.search(rf'<meta[^>]+property="og:{p}"[^>]+content="([^"]*)"', t) or [None, ""])[1]  # noqa: E731
    lic = re.search(r"creativecommons\.org/(?:licenses/([a-z\-]+)/(\d\.\d)|publicdomain/zero/1\.0)", t)
    seen = ""
    if lic:
        seen = "CC0" if not lic.group(1) else f"CC {lic.group(1).upper()} {lic.group(2)}"
    return {"title": _strip(og("title")), "description": _strip(og("description"))[:600], "author": "",
            "licence_seen": seen, "evidence": {"source": "provider-page", "host": urlparse(url).hostname}}


def fetch(source_url: str) -> dict:
    """Identity from the source; never raises (an unreachable source = unverified)."""
    try:
        got = (_commons(source_url) if "commons.wikimedia.org" in source_url else
               _flickr(source_url) if "flickr.com" in source_url else _page(source_url))
    except Exception as exc:  # noqa: BLE001
        got = None
        err = str(exc)[:200]
    else:
        err = ""
    if not got or not (got.get("title") or got.get("description")):
        return {"title": "", "description": "", "author": "", "licence_seen": "", "suitability": "unverified",
                "evidence": {"error": err or "no title or description at the source"}}
    text = f"{got['title']} {got['description']}"
    got["suitability"] = "medical" if MEDICAL.search(text) else "ok"
    return got


def identity(conn, asset_ref: str, source_url: str | None, licence_recorded: str, refresh: bool = False) -> dict:
    """The cached identity record for one external asset."""
    row = conn.execute("select * from pub.asset_identity where asset_ref=%s", (asset_ref,)).fetchone()
    if row and not refresh:
        return dict(row)
    got = fetch(source_url or "") if source_url else {"title": "", "description": "", "author": "",
                                                       "licence_seen": "", "suitability": "unverified",
                                                       "evidence": {"error": "no source url"}}
    conn.execute(
        "insert into pub.asset_identity (asset_ref, source_url, title, description, author, licence_recorded, "
        "licence_seen, licence_evidence, suitability) values (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "on conflict (asset_ref) do update set title=excluded.title, description=excluded.description, "
        "author=excluded.author, licence_recorded=excluded.licence_recorded, licence_seen=excluded.licence_seen, "
        "licence_evidence=excluded.licence_evidence, suitability=excluded.suitability, checked_at=now()",
        (asset_ref, source_url, got["title"], got["description"], got["author"], licence_recorded,
         got["licence_seen"], json.dumps(got["evidence"]), got["suitability"]))
    conn.commit()
    return dict(conn.execute("select * from pub.asset_identity where asset_ref=%s", (asset_ref,)).fetchone())
