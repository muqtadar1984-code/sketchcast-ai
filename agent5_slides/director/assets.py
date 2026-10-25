"""Every picture a Director deck asks for — found, licensed or drawn, under a
budget, and kept for the next deck.

THE LADDER, cheapest and most trustworthy first:

  1. REUSE. The lesson's own approved figures (labelled, reviewed), the
     pictures its video already drew, and the DECK SHELF: deck renderings a
     previous deck validated, filed under an exact key. A hit costs nothing.
  2. A LICENSED PHOTOGRAPH, for a `photo` or `micrograph` need: Openverse,
     then Wikimedia Commons, licence-gated (public domain, CC0, CC BY by default — the gate
     the library already uses for external images), with its author, licence
     and page kept for the credits slide. Looked at by a vision model before
     it is used: a search hit is a filename, and filenames lie (the library
     once filed a lobster as `wall`).
  3. A GENERATION, through the same Vertex image call, budget, back-off and
     cost ledger the video uses — but with a PRESENTATION style, not the
     whiteboard one: a deck picture is a full-colour illustration and is never
     traced to ink.
  4. VALIDATE AND SHELVE. A picture that passes (size, not blank, vision:
     shows what was asked, no baked-in text) is filed on the shelf under
     `<subject>__<need>__deck` with its provenance, licence, model, prompt and
     purpose — so the same concept rendered for a deck and for a board
     coexist under related keys and neither is ever served as the other
     (`visual_library.is_shelf_key`).

THE SHELF IS PLUGGABLE ON PURPOSE. `LocalShelf` is a folder with an index —
what the local samples use, and the only shelf a test or a sample may write.
`LibraryShelf` is the production visual library (Supabase storage + the
`visual_assets` row, through `publish_generated`); it writes only when
`DECK_SHELF_PUBLISH=1`, which nothing sets until the founder approves.

NEVER STARVE PRODUCTION. Before every generation the `yield_to_users` probe
is asked; a live user builder means this deck goes without the picture.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .spec import AssetNeed, DeckSpec, asset_refs

logger = logging.getLogger(__name__)

UA = "SketchCastDeckBot/1.0 (education; contact: support@sketchcast.app)"
DECK_SUFFIX = "__deck"


# ── budget ────────────────────────────────────────────────────────────

def _env_num(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass
class Budget:
    """What one deck may spend. Every number is an env var so a ceiling can
    move without a deploy; none of them is a quota to fill — a deck whose
    pictures all came off the shelf spends nothing and that is the goal."""

    max_new_images: int = 6
    max_image_usd: float = 0.80
    max_total_usd: float = 1.50
    max_seconds: float = 900.0
    image_retries: int = 1
    photo_lookups: int = 10
    corrections: int = 2
    vision_qa_slides: int = 30

    @classmethod
    def from_env(cls) -> "Budget":
        return cls(
            max_new_images=int(_env_num("DECK_DIRECTOR_MAX_NEW_IMAGES", 6)),
            max_image_usd=_env_num("DECK_DIRECTOR_IMAGE_USD", 0.80),
            max_total_usd=_env_num("DECK_DIRECTOR_TOTAL_USD", 1.50),
            max_seconds=_env_num("DECK_DIRECTOR_MAX_SECONDS", 900),
            image_retries=int(_env_num("DECK_DIRECTOR_IMAGE_RETRIES", 1)),
            photo_lookups=int(_env_num("DECK_DIRECTOR_PHOTO_LOOKUPS", 10)),
            corrections=int(_env_num("DECK_DIRECTOR_CORRECTIONS", 2)),
            vision_qa_slides=int(_env_num("DECK_DIRECTOR_VISION_QA_SLIDES", 30)),
        )


@dataclass
class Spend:
    started: float = field(default_factory=time.time)
    images_generated: int = 0
    image_attempts: int = 0
    image_usd: float = 0.0
    photo_lookups: int = 0
    vision_calls: int = 0

    def elapsed(self) -> float:
        return time.time() - self.started


# ── what a resolved picture is ────────────────────────────────────────

@dataclass
class Picture:
    asset_id: str                  # the spec's id (A1)
    key: str                       # shelf / figure key
    png: str
    w: int
    h: int
    origin: str                    # figure | video | shelf | licensed | generated
    need: str = "illustration"
    provenance: str = ""
    licence: str = ""
    author: str = ""
    source_url: str = ""
    model: str = ""
    prompt: str = ""
    purpose: str = ""
    subject: str = ""
    validation: dict = field(default_factory=dict)
    cost_usd: float = 0.0
    regions: dict = field(default_factory=dict)
    provider: str = ""
    # what the SOURCE says the picture is (title / description), kept apart
    # from what the deck ASKED for (subject / purpose) — 2026-10-25
    source_title: str = ""
    source_description: str = ""

    @property
    def credit(self) -> str:
        """The attribution line a picture's LICENCE requires, or "" (founder,
        2026-10-24: no credits slide unless a source must be named). Our own
        generations and public-domain / CC0 photographs need none; the
        provenance still rides on the shelf row either way."""
        if self.origin not in ("licensed", "shelf") or not self.source_url or \
                not needs_attribution(self.licence):
            return ""
        who = self.author or "unknown author"
        return f"{self.subject}: {who}, {self.licence}, via {self.provider} — {self.source_url}"


def needs_attribution(licence: str) -> bool:
    s = " ".join((licence or "").lower().replace("-", " ").split())
    if not s or s.startswith(("pd", "public domain", "cc0", "no restrictions")):
        return False
    return True                        # CC BY and anything unrecognised: name the source


def deck_key(need: AssetNeed) -> str:
    from shared.asset_keys import canonical_key
    base = canonical_key(need.subject) or re.sub(r"[^a-z0-9]+", "_", need.subject.lower()).strip("_")
    return f"{(base or need.id.lower())[:60]}__{need.need}{DECK_SUFFIX}"


def _png_info(data: bytes) -> tuple[int, int, float]:
    from PIL import Image, ImageStat
    with Image.open(io.BytesIO(data)) as im:
        im = im.convert("RGB")
        w, h = im.size
        sd = sum(ImageStat.Stat(im.resize((64, 64))).stddev) / 3
    return w, h, sd


def _to_png(data: bytes, max_edge: int = 1800) -> bytes:
    from PIL import Image
    with Image.open(io.BytesIO(data)) as im:
        im = im.convert("RGB")
        scale = min(1.0, max_edge / max(im.size))
        if scale < 1.0:
            im = im.resize((int(im.width * scale), int(im.height * scale)), Image.LANCZOS)
        out = io.BytesIO()
        im.save(out, "PNG", optimize=True)
        return out.getvalue()


# ── shelves ───────────────────────────────────────────────────────────

class LocalShelf:
    """A folder and an index.json whose rows mirror `visual_assets`. The
    samples' shelf: nothing here reaches production."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.index = self.root / "index.json"
        self.rows: list[dict] = json.loads(self.index.read_text("utf-8")) if self.index.exists() else []

    def find(self, key: str) -> Optional[dict]:
        for r in self.rows:
            if r.get("asset_key") == key and r.get("status") == "approved" and Path(r["path"]).exists():
                return r
        return None

    def save(self, pic: Picture, data: bytes, context: dict) -> dict:
        digest = hashlib.sha256(data).hexdigest()
        dup = next((r for r in self.rows if r.get("content_hash") == digest), None)
        if dup:
            if dup.get("asset_key") != pic.key and not self.find(pic.key):
                # the same bytes under a second key: file the key, share the
                # file — or the next deck asking by this key draws it again
                row = dict(dup) | {"asset_key": pic.key, "canonical_key": pic.key, "alias_of": dup["asset_key"]}
                self.rows.append(row)
                self.index.write_text(json.dumps(self.rows, indent=1), "utf-8")
                return row
            return dup
        path = self.root / f"{pic.key}" / f"{digest[:16]}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        row = _row(pic, digest, context) | {"path": str(path), "storage_path": f"local:{path}"}
        self.rows.append(row)
        self.index.write_text(json.dumps(self.rows, indent=1), "utf-8")
        return row


class LibraryShelf:
    """The production visual library. READS always; WRITES only under
    DECK_SHELF_PUBLISH=1 (off until the founder approves the rollout)."""

    def __init__(self, sb, tmp: Path):
        self.sb, self.tmp = sb, Path(tmp)

    def find(self, key: str) -> Optional[dict]:
        from shared import visual_library as vl
        try:
            res = (self.sb.table("visual_assets").select("*").eq("asset_key", key)
                   .eq("status", "approved").limit(1).execute())
            rows = getattr(res, "data", None) or []
            if not rows or not rows[0].get("storage_path"):
                return None
            row = rows[0]
            path = self.tmp / "shelf" / f"{key}.png"
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(self.sb.storage.from_(vl.BUCKET).download(row["storage_path"]))
            return row | {"path": str(path)}
        except Exception as exc:  # noqa: BLE001 — a shelf miss, not a failed deck
            logger.warning("deck shelf: lookup of %s failed (%s)", key, exc)
            return None

    def save(self, pic: Picture, data: bytes, context: dict) -> dict:
        row = _row(pic, hashlib.sha256(data).hexdigest(), context)
        if os.getenv("DECK_SHELF_PUBLISH", "0").strip() != "1":
            return row | {"published": False}
        from shared import visual_library as vl
        path = self.tmp / "shelf_out" / f"{pic.key}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        ok = vl.publish_generated(pic.key, row["description"], path,
                                  {"quality": row["quality"]}, context, asset_format="png")
        if ok:
            # publish_generated writes provenance 'generated' and no source;
            # the licence and the deck metadata ride in the same update the
            # external-image pickup has always used (no migration).
            try:
                (self.sb.table("visual_assets")
                 .update({"provenance": row["provenance"], "source": row["source"],
                          "vision": {"w": pic.w, "h": pic.h, "deck": row["deck"]}})
                 .eq("content_hash", row["content_hash"]).execute())
            except Exception as exc:  # noqa: BLE001
                logger.warning("deck shelf: metadata update for %s failed (%s)", pic.key, exc)
        return row | {"published": bool(ok)}


def _row(pic: Picture, digest: str, context: dict) -> dict:
    # A found photograph is described by what its SOURCE says it is; the
    # request is kept beside it in `deck`. Labelling a found photo by the
    # request put "Amebic colitis (H&E)" — a colon biopsy — on the shelf as
    # "Amoeba seen under a microscope" (2026-10-25 impact report).
    said = ". ".join(x for x in (pic.source_title, pic.source_description) if x).strip()
    return {
        "asset_key": pic.key,
        "canonical_key": pic.key,
        "asset_type": "visual",
        "description": (said or f"{pic.subject}. {pic.purpose}").strip()[:600],
        "subject": context.get("subject"), "curriculum": context.get("curriculum"),
        "grade": context.get("grade"), "topic": context.get("topic"),
        "concepts": context.get("concepts") or [],
        "status": "approved",
        "provenance": pic.provenance or pic.origin,
        "source": json.dumps({"url": pic.source_url, "licence": pic.licence, "author": pic.author},
                             ensure_ascii=False) if pic.source_url else (pic.model or ""),
        "quality": "deck_validated",
        "content_hash": digest,
        "asset_format": "png",
        "deck": {"visual_type": pic.need, "purpose": pic.purpose, "requested": pic.subject,
                 "source_title": pic.source_title, "source_description": pic.source_description,
                 "model": pic.model,
                 "prompt": pic.prompt, "prompt_version": PROMPT_VERSION, "labels": [],
                 "language_dependent": False, "usage_rights": pic.licence or "sketchcast-generated",
                 "w": pic.w, "h": pic.h, "aspect": f"{pic.w}:{pic.h}",
                 "validation": pic.validation},
    }


# ── step 2: a licensed photograph ─────────────────────────────────────

LICENCE_OK = tuple(x.strip() for x in os.getenv(
    "DECK_PHOTO_LICENCES", "pd,public domain,cc0,cc by,cc-by").split(",") if x.strip())


def licence_ok(short: str) -> bool:
    """Commons' LicenseShortName against the allowed list. NC and ND are
    never allowed; share-alike only if the list names it explicitly."""
    s = " ".join((short or "").lower().replace("-", " ").split())
    if not s:
        return False
    tokens = s.split()
    if "nc" in tokens or "nd" in tokens:
        return False
    allowed = [" ".join(x.lower().replace("-", " ").split()) for x in LICENCE_OK]
    if "sa" in tokens and not any("sa" in a.split() for a in allowed):
        return False
    return any(s.startswith(a) for a in allowed)


# ── identity: what the source says the picture IS ─────────────────────

# Medical / pathology content a classroom slide must not get by accident: a
# found photo whose own title or description names one of these is refused
# unless the request itself asks for it. Generic, not subject-specific
# (2026-10-25: a colon biopsy, malaria-infected and congested-lung slides
# were shelved as an amoeba, healthy red cells and a blood smear).
MEDICAL_TERMS = re.compile(
    r"\b(histopatholog\w*|patholog\w*|colitis|biops\w*|malaria\w*|parasit\w*|infected|infections?|"
    r"carcinoma\w*|cancer\w*|tumou?r\w*|lesions?|autops\w*|necros\w*|siderophag\w*|congestion|"
    r"diseased?|syndrome|lymphoma\w*|leuka?emi\w*|metasta\w*|abscess\w*|ulcer\w*|gangren\w*|wounds?|"
    r"trophozoit\w*|amebic|amoebic)\b", re.I)


def source_name(title: str) -> str:
    """A readable name from a source title ("File:Red_blood_cells_infected_with_malaria.jpg")."""
    t = re.sub(r"^file:", "", str(title or "").strip(), flags=re.I)
    t = re.sub(r"\.(jpe?g|png|gif|tiff?|webp|svg)$", "", t, flags=re.I)
    return " ".join(t.replace("_", " ").split())


def need_text(need) -> str:
    return " ".join([need.subject or "", need.purpose or "", *(need.must_show or []), *(need.search_terms or [])])


def identity_conflict(title: str, description: str, need) -> str:
    """Why a found picture's OWN identity disqualifies it for this need, or "".
    Medical or pathology content named by the source and not by the request."""
    said = f"{source_name(title)} {description or ''}"
    asked = need_text(need).lower()
    hits = sorted({m.group(0).lower() for m in MEDICAL_TERMS.finditer(said)} - {""})
    hits = [h for h in hits if h not in asked]
    return f"the source names {', '.join(hits)}" if hits else ""


def _strip_html(s: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", s or "").split())


def wikimedia_candidates(query: str, limit: int = 8, http=None) -> list[dict]:
    import requests
    http = http or requests
    params = {"action": "query", "format": "json", "generator": "search", "gsrnamespace": 6,
              "gsrsearch": f"{query} filetype:bitmap", "gsrlimit": limit, "prop": "imageinfo",
              "iiprop": "url|extmetadata|size|mime", "iiurlwidth": 1600}
    r = http.get("https://commons.wikimedia.org/w/api.php", params=params,
                 headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    pages = ((r.json() or {}).get("query") or {}).get("pages") or {}
    out = []
    for p in sorted(pages.values(), key=lambda x: x.get("index", 99)):
        ii = (p.get("imageinfo") or [{}])[0]
        meta = ii.get("extmetadata") or {}
        short = (meta.get("LicenseShortName") or {}).get("value", "")
        out.append({"title": p.get("title", ""), "url": ii.get("thumburl") or ii.get("url"),
                    "page": ii.get("descriptionurl", ""), "w": ii.get("width", 0), "h": ii.get("height", 0),
                    "mime": ii.get("mime", ""), "licence": short,
                    "author": _strip_html((meta.get("Artist") or {}).get("value", ""))[:120],
                    "description": _strip_html((meta.get("ImageDescription") or {}).get("value", ""))[:300]})
    return out


_OV_LICENCE = {"by": "CC BY", "cc0": "CC0", "pdm": "Public domain"}


def openverse_candidates(query: str, limit: int = 10, http=None) -> list[dict]:
    """Openverse (openly licensed images from Flickr, Wikimedia and others),
    filtered AT THE SEARCH to CC BY / CC0 / public domain. Its relevance on
    everyday objects is far better than Commons full-text search, which
    answered "bicycle wheel" with a spoke wrench and a motorbike (run 2)."""
    import requests
    http = http or requests
    r = http.get("https://api.openverse.org/v1/images/",
                 params={"q": query, "license": "by,cc0,pdm", "page_size": limit, "mature": "false"},
                 headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    out = []
    for it in (r.json() or {}).get("results") or []:
        lic = _OV_LICENCE.get(str(it.get("license") or "").lower())
        if not lic:
            continue
        ver = str(it.get("license_version") or "")
        ft = str(it.get("filetype") or "jpg").lower()
        # Fetched through Openverse's own image proxy: Flickr's static host
        # answers a direct download with 403 (run 3, every candidate)
        proxy = f"https://api.openverse.org/v1/images/{it['id']}/thumb/?full_size=true" if it.get("id") else None
        out.append({"title": it.get("title") or "", "url": proxy or it.get("url"),
                    "page": it.get("foreign_landing_url") or "",
                    "w": int(it.get("width") or 0), "h": int(it.get("height") or 0),
                    "mime": f"image/{'jpeg' if ft in ('jpg', 'jpeg') else ft}",
                    "licence": f"{lic} {ver}".strip() if lic == "CC BY" else lic,
                    "author": str(it.get("creator") or "")[:120],
                    "description": str(it.get("title") or "")[:300],
                    "provider": str(it.get("source") or it.get("provider") or "openverse")})
    return out


def default_photos(query: str, http=None) -> list[dict]:
    """Openverse first, Wikimedia Commons after: the candidates of both, in
    that order, each still licence-gated and looked at before use."""
    out: list[dict] = []
    for fn in (openverse_candidates, wikimedia_candidates):
        try:
            out += fn(query, http=http)
        except Exception as exc:  # noqa: BLE001 — one source down is not no photos
            logger.warning("deck assets: %s failed for %r (%s)", fn.__name__, query, exc)
    return out


# ── step 3: a generation ──────────────────────────────────────────────

PROMPT_VERSION = "deck-art-v1"

_STYLE = {
    "photo": ("A high-quality, realistic photograph of {subject}. {show}Natural light, sharp focus, "
              "uncluttered composition with the subject clearly visible, suitable for a school classroom. "
              "No text, no captions, no watermark, no logos."),
    "illustration": ("A polished, modern, full-colour educational illustration of {subject}, in the style of "
                     "a premium science or maths textbook infographic: clean shapes, soft shading, a calm "
                     "harmonious palette, accurate proportions, plain white or very light background. "
                     "{show}Absolutely no text, no labels, no letters, no numbers, no arrows with words."),
    "micrograph": ("A realistic light-microscope image of {subject}, as a student would see it through a "
                   "school microscope with a stain: circular field of view, natural colours, accurate "
                   "structure. {show}No text, no scale bar labels, no watermark."),
    "diagram": ("A clean, accurate, full-colour scientific diagram of {subject} in a modern textbook style, "
                "flat colours with subtle shading, white background. {show}No text, no labels, no letters, "
                "no numbers — labels will be added separately."),
}


def generation_prompt(need: AssetNeed) -> str:
    show = ""
    if need.must_show:
        show = "It must clearly show: " + ", ".join(need.must_show) + ". "
    if need.avoid:
        show += "Avoid: " + ", ".join(need.avoid) + ". "
    return _STYLE.get(need.need, _STYLE["illustration"]).format(subject=need.subject, show=show)


_ASPECTS = ("1:1", "3:4", "4:3", "16:9", "9:16", "3:2", "2:3")


def default_generate(prompt: str, aspect: str) -> tuple[Optional[bytes], str, float]:
    """One image through the production call (budget, back-off, ledger).
    Returns (bytes, model id, usd)."""
    from shared import image_models as images
    from spike.scene_engine import raster_assets as ra
    with ra.image_role(images.FIGURE):
        model = ra.current_image_model()
        data = ra._vertex_call(prompt, model, aspect if aspect in _ASPECTS else "4:3")  # noqa: SLF001
    return data, model.id, (model.cost_usd or 0.0) if data else 0.0


# ── vision validation ─────────────────────────────────────────────────

VISION_PROMPT = (
    "You are checking a picture before it goes on a classroom slide for learners aged {age}.\n"
    "The slide needs: {subject}. Purpose: {purpose}. It must show: {must}.\n"
    "`is_the_thing_itself` is true only if the picture shows the actual thing named — not a museum "
    "artefact, artwork, toy, statue, cartoon, or a different object that merely resembles it.\n"
    "{source}"
    "Reply JSON only: {{\"shows_subject\": true|false, \"is_the_thing_itself\": true|false, "
    "\"source_agrees\": true|false, "
    "\"relevance\": 0-10, \"quality\": 0-10, "
    "\"has_text\": true|false, \"scientifically_wrong\": true|false, \"classroom_safe\": true|false, "
    "\"problems\": [\"...\"]}}"
)


SOURCE_LINE = (
    "The picture's SOURCE calls it: \"{name}\"{desc}. `source_agrees` is true only if the thing the source "
    "names IS the thing the slide needs — a diseased, infected or parasite-laden specimen is not a healthy "
    "one, and a tissue section from a patient is not a free-living organism.\n")


def vision_verdict(client, png: Path, need: AssetNeed, age: str, source: Optional[dict] = None) -> dict:
    if client is None or not hasattr(client, "analyze_image"):
        return {"skipped": "no vision client"}
    line = ""
    if source and (source.get("title") or source.get("description")):
        desc = str(source.get("description") or "").strip()
        line = SOURCE_LINE.format(name=source_name(source.get("title", "")),
                                  desc=f" ({desc[:200]})" if desc and desc != source.get("title") else "")
    try:
        r = client.analyze_image(str(png), VISION_PROMPT.format(
            age=age or "11-14", subject=need.subject, purpose=need.purpose or "-",
            must=", ".join(need.must_show) or "-", source=line), max_tokens=800)
        d = r.get("data") if isinstance(r, dict) else None
        return d if isinstance(d, dict) else {"error": "unparsed"}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:200]}


def verdict_ok(v: dict, origin: str) -> bool:
    if "skipped" in v or "error" in v:
        return origin != "licensed"      # an unchecked search hit is a filename, not a picture
    if not v.get("shows_subject") or not v.get("classroom_safe", True) or v.get("scientifically_wrong"):
        return False
    if origin == "generated" and v.get("has_text"):
        return False                     # baked-in text: garbled labels on a projected slide
    if origin == "licensed":
        # a search hit is held to more: "open scissors showing an angle"
        # passed at 6 as a medieval artefact on a museum scale bar (matrix
        # cover, Types of Angles) — the thing itself, and clearly relevant
        if v.get("is_the_thing_itself") is False or v.get("source_agrees") is False:
            return False
        return int(v.get("relevance") or 0) >= 7 and int(v.get("quality") or 0) >= 6
    return int(v.get("relevance") or 0) >= 6 and int(v.get("quality") or 0) >= 5


# ── the ladder ────────────────────────────────────────────────────────

@dataclass
class AssetReport:
    pictures: dict[str, Picture] = field(default_factory=dict)
    missing: dict[str, str] = field(default_factory=dict)
    log: list[str] = field(default_factory=list)

    def counts(self) -> dict:
        c: dict[str, int] = {}
        for p in self.pictures.values():
            c[p.origin] = c.get(p.origin, 0) + 1
        c["missing"] = len(self.missing)
        return c

    def to_dict(self) -> dict:
        return {"counts": self.counts(), "pictures": {k: asdict(v) for k, v in self.pictures.items()},
                "missing": self.missing, "log": self.log}


def resolve(deck: DeckSpec, *, tmp: Path, shelf, ctx, budget: Budget, spend: Spend,
            vision_client=None, generate: Callable = default_generate,
            photos: Callable = default_photos, yield_to_users: Callable[[], bool] = lambda: False,
            context: Optional[dict] = None, http=None) -> AssetReport:
    """Resolve every asset the slides actually use. A picture that cannot be
    had is reported missing; the renderer then drops the element and QA sees
    a slide without it — never an empty frame."""
    import requests
    http = http or requests
    rep = AssetReport()
    used = asset_refs(deck)
    tmp = Path(tmp)
    (tmp / "pics").mkdir(parents=True, exist_ok=True)
    context = dict(context or {})

    def note(msg):
        rep.log.append(msg)
        logger.info("deck assets: %s", msg)

    for aid in used:
        need = deck.assets.get(aid)
        if need is None:
            continue
        # 1a — the lesson's own figure, or a picture its video drew
        reuse = need.reuse_key
        if reuse and reuse in ctx.figures and ctx.figures[reuse].get("png"):
            f = ctx.figures[reuse]
            rep.pictures[aid] = Picture(aid, reuse, f["png"], int(f["w"] or 0), int(f["h"] or 0), "figure",
                                        need.need, subject=need.subject, purpose=need.purpose,
                                        regions=f.get("regions") or {})
            note(f"{aid}: lesson figure {reuse}")
            continue
        hit = next((r for r in ctx.shelf if reuse and r.get("key") == reuse and r.get("png")), None)
        if hit and need.need in ("diagram", "illustration"):
            rep.pictures[aid] = Picture(aid, reuse, hit["png"], int(hit.get("w") or 0), int(hit.get("h") or 0),
                                        "video", need.need, subject=need.subject, purpose=need.purpose)
            note(f"{aid}: video picture {reuse}")
            continue
        # 1b — the deck shelf, exact key
        key = deck_key(need)
        row = shelf.find(key) if shelf is not None else None
        if row:
            # a shelved photo meets the same identity rule as a fresh one: its
            # recorded source title (or the file name in its source url)
            # the library keeps deck metadata under vision.deck (LibraryShelf.save); a local row under deck
            vis = row.get("vision") if isinstance(row.get("vision"), dict) else {}
            deck_meta = vis.get("deck") if isinstance(vis.get("deck"), dict) else \
                (row.get("deck") if isinstance(row.get("deck"), dict) else {})
            try:
                src0 = json.loads(row.get("source") or "{}") if str(row.get("source") or "").startswith("{") else {}
            except ValueError:
                src0 = {}
            said = deck_meta.get("source_title") or str(src0.get("url") or "").rsplit("/", 1)[-1]
            why = identity_conflict(said, deck_meta.get("source_description", ""), need)
            if why:
                note(f"{aid}: shelf {key} not reused ({why})")
                row = None
        if row:
            w, h, _ = _png_info(Path(row["path"]).read_bytes())
            # A reused photograph keeps its licence: the shelf row carries the
            # url / licence / author it was taken under (`_row`), and a CC BY
            # picture owes its credit in every deck that shows it — reuse
            # dropped it once (run 5, the Flickr wheel and the onion cells).
            try:
                src = json.loads(row.get("source") or "{}") if str(row.get("source") or "").startswith("{") else {}
            except ValueError:
                src = {}
            prov = str(row.get("provenance") or "")
            rep.pictures[aid] = Picture(aid, key, row["path"], w, h, "shelf", need.need,
                                        provenance=prov, subject=need.subject, purpose=need.purpose,
                                        licence=src.get("licence", ""), author=src.get("author", ""),
                                        source_url=src.get("url", ""),
                                        provider="" if prov.startswith("generated") else prov.title())
            note(f"{aid}: shelf hit {key}")
            continue
        got = None
        # 2 — a licensed photograph
        if need.need in ("photo", "micrograph") and spend.elapsed() < budget.max_seconds:
            for q in (need.search_terms or [need.subject])[:3]:
                if got or spend.photo_lookups >= budget.photo_lookups:
                    break
                spend.photo_lookups += 1
                try:
                    cands = photos(q, http=http)
                except Exception as exc:  # noqa: BLE001
                    note(f"{aid}: photo search {q!r} failed ({exc})")
                    continue
                for c in cands:
                    if not licence_ok(c["licence"]):
                        continue
                    why = identity_conflict(c.get("title", ""), c.get("description", ""), need)
                    if why:
                        note(f"{aid}: refused {c.get('title')} ({why})")
                        continue
                    if not c.get("url") or not str(c.get("mime", "")).startswith("image/") or \
                            min(c.get("w") or 0, c.get("h") or 0) < 600:
                        continue
                    try:
                        raw = http.get(c["url"], headers={"User-Agent": UA}, timeout=60)
                        raw.raise_for_status()
                        data = _to_png(raw.content)
                    except Exception as exc:  # noqa: BLE001
                        note(f"{aid}: download failed for {c['title']} ({exc})")
                        continue
                    w, h, sd = _png_info(data)
                    if sd < 12:
                        continue
                    png = tmp / "pics" / f"{aid}_wm.png"
                    png.write_bytes(data)
                    spend.vision_calls += 1
                    v = vision_verdict(vision_client, png, need, ctx.age_band,
                                       source={"title": c.get("title", ""), "description": c.get("description", "")})
                    if not verdict_ok(v, "licensed"):
                        note(f"{aid}: rejected {c['title']} ({v.get('problems') or v})")
                        continue
                    provider = "Wikimedia Commons" if "wikimedia" in c["page"] else (c.get("provider") or "Openverse").title()
                    got = Picture(aid, key, str(png), w, h, "licensed", need.need, provenance=provider.lower(),
                                  provider=provider,
                                  licence=c["licence"], author=c["author"], source_url=c["page"],
                                  subject=need.subject, purpose=need.purpose, validation=v,
                                  source_title=source_name(c.get("title", "")),
                                  source_description=str(c.get("description") or "")[:300])
                    note(f"{aid}: {provider} {c['title']} ({c['licence']})")
                    break
        # 3 — a generation
        tries = 0
        while got is None and tries <= budget.image_retries:
            tries += 1
            if spend.images_generated >= budget.max_new_images:
                rep.missing[aid] = "image budget: max new images reached"
                break
            if spend.image_usd >= budget.max_image_usd:
                rep.missing[aid] = "image budget: USD ceiling reached"
                break
            if spend.elapsed() > budget.max_seconds:
                rep.missing[aid] = "time budget reached"
                break
            if yield_to_users():
                rep.missing[aid] = "a user's generation is live; not generating (never-starve rule)"
                break
            prompt = generation_prompt(need)
            spend.image_attempts += 1
            try:
                data, model_id, usd = generate(prompt, need.aspect)
            except Exception as exc:  # noqa: BLE001
                note(f"{aid}: generation error ({exc})")
                data, model_id, usd = None, "", 0.0
            spend.image_usd += usd
            if not data:
                rep.missing.setdefault(aid, "generation returned no image")
                continue
            spend.images_generated += 1
            data = _to_png(data)
            w, h, sd = _png_info(data)
            png = tmp / "pics" / f"{aid}_gen{tries}.png"
            png.write_bytes(data)
            if sd < 8 or min(w, h) < 400:
                note(f"{aid}: generated picture refused (blank or {w}x{h})")
                continue
            spend.vision_calls += 1
            v = vision_verdict(vision_client, png, need, ctx.age_band)
            if not verdict_ok(v, "generated"):
                note(f"{aid}: generated picture refused ({v.get('problems') or v})")
                continue
            got = Picture(aid, key, str(png), w, h, "generated", need.need, provenance=f"generated:{model_id}",
                          model=model_id, prompt=prompt, subject=need.subject, purpose=need.purpose,
                          validation=v, cost_usd=usd)
            rep.missing.pop(aid, None)
            note(f"{aid}: generated {key} (${usd:.3f})")
        if got is None:
            rep.missing.setdefault(aid, "no picture found or drawn")
            continue
        # 4 — shelve it for the next deck
        if shelf is not None and got.origin in ("licensed", "generated"):
            try:
                row = shelf.save(got, Path(got.png).read_bytes(), context)
                note(f"{aid}: shelved as {row.get('asset_key')} ({'published' if row.get('published') else 'local'})")
            except Exception as exc:  # noqa: BLE001
                note(f"{aid}: shelving failed ({exc})")
        rep.pictures[aid] = got
    return rep
