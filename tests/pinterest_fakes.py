"""Test doubles for the Pinterest Pin Engine (catalogue/pinterest/).

``PinSB`` is the catalogue FakeSB with what migration 0154 adds: the partial
unique indexes (one daily round per day, one open round, one live pin_post,
one live pin_draft per topic × language, the pin source key, the content
hash) raising 23505 as Postgres would, and ``catalogue_current_kits()``.

``FakeApi`` stands in for ``catalogue.pinterest.api.Api`` and ``FakeSession``
for ``requests`` under the real ``Api``. ``FakeCopyClient`` is the text
client. NOTHING here opens a socket.
"""

from __future__ import annotations

import io
import json

from tests.catalogue_fakes import DuplicateKey, FakeSB, _Query, _Res

LIVE = ("queued", "processing")

PARTIAL = {
    "pinterest_pins": [
        lambda r: ("src", r.get("source_kind"), r.get("source_ref"), r.get("language"), r.get("format"),
                   r.get("template")),
        lambda r: ("hash", r.get("content_hash")) if r.get("content_hash") else None,
        lambda r: ("pid", r.get("pinterest_pin_id")) if r.get("pinterest_pin_id") else None,
    ],
    "pinterest_publish_rounds": [
        lambda r: ("daily", r.get("day")) if r.get("trigger") == "daily" else None,
        lambda r: ("open",) if r.get("status") in ("requested", "running") else None,
    ],
    "pinterest_boards": [lambda r: (r.get("language"), r.get("board_key"))],
    "jobs": [
        lambda r: ("pin_post",) if r.get("type") == "pin_post" and r.get("status") in LIVE else None,
        lambda r: ("pin_draft", (r.get("params") or {}).get("topic_id"), (r.get("params") or {}).get("language"))
        if r.get("type") == "pin_draft" and r.get("status") in LIVE else None,
    ],
}


class _PinQuery(_Query):
    def _violations(self, table, candidates, ignore_ids=()):
        for keyf in PARTIAL.get(self.table, []):
            seen = {keyf(r) for r in table if r.get("id") not in ignore_ids} - {None}
            for row in candidates:
                k = keyf(row)
                if k is None:
                    continue
                if k in seen:
                    raise DuplicateKey(self.table, k)
                seen.add(k)

    def execute(self):
        table = self.store.tables.setdefault(self.table, [])
        if self.op == "insert":
            self._violations(table, self._rows_in(self.payload))
        elif self.op == "update":
            hit = [r for r in table if self._match(r)]
            after = [{**r, **self.payload} for r in hit]
            self._violations(table, after, ignore_ids={r.get("id") for r in hit})
        return super().execute()


class PinSB(FakeSB):
    def __init__(self):
        super().__init__()
        self.tables.update({t: [] for t in ("pinterest_boards", "pinterest_pins", "pinterest_publish_rounds",
                                            "pinterest_pin_stats", "pinterest_oauth", "platform_settings",
                                            "platform_audit_log", "platform_admins", "topic_term_translations")})
        self.current_kits: list[dict] = []

    def table(self, name):
        return _PinQuery(self, name)

    def rpc(self, name, params=None):
        store = self

        class _R:
            def execute(self_):
                store.calls.append(("rpc", name))
                if name == "catalogue_current_kits":
                    return _Res([dict(r) for r in store.current_kits])
                raise AssertionError(f"unexpected rpc {name}")

        return _R()

    # helpers
    def pins(self):
        return self.tables["pinterest_pins"]

    def settings(self):
        row = next((r for r in self.tables["platform_settings"] if r["key"] == "pinterest_agent"), None)
        return (row or {}).get("value") or {}

    def set_settings(self, **kw):
        rows = self.tables["platform_settings"]
        row = next((r for r in rows if r["key"] == "pinterest_agent"), None)
        if row is None:
            rows.append({"key": "pinterest_agent", "value": dict(kw)})
        else:
            row["value"] = {**row["value"], **kw}


def png_bytes(size=(800, 700), colour=(30, 90, 160)) -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    d.ellipse([60, 60, size[0] - 60, size[1] - 60], outline="black", width=8)
    d.rectangle([size[0] // 3, size[1] // 3, size[0] // 2, size[1] // 2], fill=colour)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


TOPIC = "11111111-aaaa-bbbb-cccc-000000000001"
KIT = "kit-1"
ARTICLE = "art-1"
ADMIN = "admin-1"


def world(*, language="en", figures=True, parts=True, preview=True, boards="created", subject="Biology",
          public=True) -> PinSB:
    """One public biology topic with an approved kit and article, two figures
    (one with parts) and a worksheet preview; English boards ``boards``."""
    sb = PinSB()
    sb.tables["topics"].append({"id": TOPIC, "slug": "plant-cells", "title": "Plant cells", "subject": subject,
                                "status": "published"})
    sb.tables["topic_articles"].append({
        "id": ARTICLE, "topic_id": TOPIC, "language": language, "status": "approved",
        "title": "Plant and animal cells",
        "objectives": [{"id": "o1", "text": "Describe the parts of a plant cell"},
                       {"id": "o2", "text": "Compare plant and animal cells"},
                       {"id": "o3", "text": "Explain why plant cells have a cell wall"}],
        "sections": [{"id": "s1", "heading": "The cell", "body_md": "A cell has 3 main parts and 2 kinds."}],
        "glossary": [{"term": "nucleus", "definition": "the control centre"}], "misconceptions": []})
    sb.tables["topic_kits"].append({"id": KIT, "topic_id": TOPIC, "article_id": ARTICLE, "language": language,
                                    "status": "approved", "translation_status": None if language == "en" else "approved",
                                    "previews": [{"kind": "worksheet-1", "path": "plant-cells/kit-1/worksheet-1.png"}]
                                    if preview else []})
    if public:
        sb.current_kits.append({"topic_id": TOPIC, "language": language, "topic_kit_id": KIT,
                                "article_id": ARTICLE, "approved_at": "2026-10-01T00:00:00+00:00"})
    if figures:
        sb.tables["article_figures"] += [
            {"id": "fig-1", "article_id": ARTICLE, "figure_key": "plant_cell", "caption": "A plant cell",
             "labels": [{"label": "cell wall"}, {"label": "nucleus"}, {"label": "chloroplast"}],
             "status": "rendered", "sort": 1, "visual_asset_id": "va-1", "spec": {}},
            {"id": "fig-2", "article_id": ARTICLE, "figure_key": "animal_cell", "caption": "An animal cell",
             "labels": [{"label": "membrane"}], "status": "rendered", "sort": 2, "visual_asset_id": "va-2",
             "spec": {}},
        ]
        sb.tables["visual_assets"] += [
            {"id": "va-1", "asset_key": "plant_cell", "storage_path": "lib/plant.png", "asset_format": "png",
             "group_ids": ["cell_wall", "nucleus", "chloroplast"] if parts else [], "status": "approved",
             "provenance": "generated"},
            {"id": "va-2", "asset_key": "animal_cell", "storage_path": "lib/animal.png", "asset_format": "png",
             "group_ids": [], "status": "approved", "provenance": "generated"},
        ]
        sb.files[("visual-assets", "lib/plant.png")] = png_bytes()
        sb.files[("visual-assets", "lib/animal.png")] = png_bytes(colour=(160, 60, 30))
    if preview:
        sb.files[("catalogue-previews", "plant-cells/kit-1/worksheet-1.png")] = png_bytes((640, 360))
    for key in ("biology", "free_worksheets"):
        sb.tables["pinterest_boards"].append({"id": f"b-{key}", "language": "en" if language == "en" else language,
                                              "board_key": key, "name": key.title(), "description": "",
                                              "status": boards, "sections": {},
                                              "pinterest_board_id": f"pb-{key}" if boards == "created" else None})
    sb.tables["platform_admins"].append({"user_id": ADMIN, "revoked_at": None})
    sb.set_settings(enabled=True, dry_run=True, paused=False, languages=["en"],
                    breaker={"tripped": False, "failures": 0}, summary_email="founder@example.com")
    return sb


class FakeCopyClient:
    """The text client: answers ``analyze`` from ``replies`` (a callable of
    the prompt, or a list consumed in order) and records every prompt."""

    def __init__(self, replies=None, model="claude-haiku-4-5", backend="bedrock"):
        self.replies = replies
        self.prompts: list[str] = []
        self.model, self.backend = model, backend
        self.session_usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}

    def analyze(self, prompt, system="", max_tokens=0, **kw):
        self.prompts.append(prompt)
        self.session_usage["calls"] += 1
        self.session_usage["input_tokens"] += 100
        self.session_usage["output_tokens"] += 50
        self.session_usage["cost_usd"] += 0.001
        if callable(self.replies):
            return self.replies(prompt)
        return self.replies.pop(0)


def good_copy(prompt: str) -> dict:
    """A clean answer for every pin key the prompt names."""
    keys = [(line.split()[2].rstrip(":"), line.split()[4].rstrip(";")) for line in prompt.splitlines()
            if line.startswith("- key p")]
    pins = []
    for k, template in keys:
        pins.append({"key": k, "title": f"Plant cell parts explained ({template})",
                     "description": "A clear guide to the 3 main parts of a plant cell for science teachers. "
                                    "Compare plant and animal cells with a labelled diagram. "
                                    "See the full lesson on SketchCast.",
                     "alt_text": "A labelled diagram of a plant cell showing the cell wall, nucleus and chloroplast.",
                     "keywords": ["plant cell diagram", "cell biology"]})
    return {"pins": pins}


class FakeApi:
    """``catalogue.pinterest.api.Api`` in memory."""

    def __init__(self, *, board_pins=None, raise_on=None, boards=None, analytics=None):
        self.board_pins = list(board_pins or [])
        self.raise_on = dict(raise_on or {})
        self.boards = list(boards or [])
        self.analytics = analytics or {}
        self.created: list[dict] = []
        self.calls: list[tuple] = []
        self.sections: dict[str, list] = {}
        self.last_ratelimit: dict = {}

    def _maybe(self, name):
        self.calls.append((name,))
        if name in self.raise_on:
            raise self.raise_on[name]

    def list_board_pins(self, board_id, page_size=25):
        self._maybe("list_board_pins")
        return list(self.board_pins)

    def create_pin(self, payload):
        self._maybe("create_pin")
        self.created.append(payload)
        return {"id": f"pin-{len(self.created)}"}

    def list_boards(self, page_size=100, max_pages=10):
        self._maybe("list_boards")
        return list(self.boards)

    def create_board(self, name, description="", privacy="PUBLIC"):
        self._maybe("create_board")
        b = {"id": f"pb-new-{len(self.boards) + 1}", "name": name, "privacy": privacy}
        self.boards.append(b)
        return b

    def list_sections(self, board_id):
        self._maybe("list_sections")
        return list(self.sections.get(board_id, []))

    def create_section(self, board_id, name):
        self._maybe("create_section")
        s = {"id": f"sec-{name}", "name": name}
        self.sections.setdefault(board_id, []).append(s)
        return s

    def pin_analytics(self, pin_id, start, end, metrics):
        self._maybe("pin_analytics")
        self.calls.append(("analytics", pin_id, start, end, tuple(metrics)))
        return self.analytics.get(pin_id, {"all": {"daily_metrics": []}})


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.content = b"" if body is None else json.dumps(body).encode()
        self.text = self.content.decode()

    def json(self):
        return self._body


class FakeSession:
    """``requests.Session`` for the real ``Api``: answers from ``responses``
    (a list, consumed in order) and records each request."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def request(self, method, url, **kw):
        self.requests.append({"method": method, "url": url, **kw})
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt
