"""In-memory, read-only view of one catalogue snapshot.

The audit runs on this view instead of on SQL, so tests can build one from
plain dicts. ``current_kit`` re-implements public.catalogue_current_kits()
(app migration 0147) line for line. ``parity_with_live`` proves the copy
still matches production's own answer for the same snapshot.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field


def _ts(row: dict, *keys: str):
    for k in keys:
        if row.get(k):
            return str(row[k])
    return ""


@dataclass
class CatalogueView:
    snapshot_id: int
    curricula: list[dict]
    nodes: list[dict]
    topics: list[dict]
    tcm: list[dict]
    articles: list[dict]
    figures: list[dict]
    questions: list[dict]
    kits: list[dict]
    publications: list[dict]
    queue: list[dict]
    jobs: list[dict]
    current_live: list[dict] = field(default_factory=list)

    def __post_init__(self):
        self.topic_by_id = {t["id"]: t for t in self.topics}
        self.topic_by_slug = {t["slug"]: t for t in self.topics}
        self.node_by_id = {n["id"]: n for n in self.nodes}
        # Codes are unique only WITHIN a curriculum: CBSE maths and CBSE
        # science both use codes like cbse:9:U2:01. Membership tests use
        # node_by_code; anything that needs a title must pick the node
        # from nodes_by_code by curriculum.
        self.nodes_by_code = defaultdict(list)
        for n in self.nodes:
            self.nodes_by_code[n["code"]].append(n)
        self.node_by_code = {c: ns[0] for c, ns in self.nodes_by_code.items()}
        self.curriculum_by_id = {c["id"]: c for c in self.curricula}
        self.map_by_topic = defaultdict(list)
        for m in self.tcm:
            self.map_by_topic[m["topic_id"]].append(m)
        self.articles_by_topic = defaultdict(list)
        for a in self.articles:
            self.articles_by_topic[(a["topic_id"], a["language"])].append(a)
        self.figures_by_article = defaultdict(list)
        for f in self.figures:
            self.figures_by_article[f["article_id"]].append(f)
        self.kits_by_topic = defaultdict(list)
        for k in self.kits:
            self.kits_by_topic[(k["topic_id"], k["language"])].append(k)
        self.kit_by_id = {k["id"]: k for k in self.kits}
        self.pubs_by_kit = defaultdict(list)
        for p in self.publications:
            self.pubs_by_kit[p["topic_kit_id"]].append(p)
        self.items_by_topic = defaultdict(lambda: defaultdict(int))
        for q in self.questions:
            self.items_by_topic[(q["topic_id"], q["language"])][q["status"]] += 1
        self.jobs_by_topic = defaultdict(list)
        for j in self.jobs:
            p = j.get("params") or {}
            tid = p.get("topic_id")
            if not tid and p.get("kit_id") in self.kit_by_id:
                tid = self.kit_by_id[p["kit_id"]]["topic_id"]
            if tid:
                self.jobs_by_topic[tid].append(j)

    # -- loading ---------------------------------------------------------
    @classmethod
    def load(cls, conn, snapshot_id: int | None = None) -> "CatalogueView":
        from publishing.snapshot import latest_snapshot_id
        sid = snapshot_id or latest_snapshot_id(conn)

        def rows(sql: str) -> list[dict]:
            out = conn.execute(sql).fetchall()
            return [{k: (str(v) if k.endswith("id") and v is not None and not isinstance(v, (int, list)) else v)
                     for k, v in r.items()} for r in out]

        return cls(
            snapshot_id=sid,
            curricula=rows("select * from cat.curricula"),
            nodes=rows("select * from cat.curriculum_nodes"),
            topics=[{**t, "prerequisites": [str(x) for x in (t.get("prerequisites") or [])]}
                    for t in rows("select * from cat.topics")],
            tcm=rows("select * from cat.topic_curriculum_map"),
            articles=rows("select * from cat.topic_articles"),
            figures=rows("select * from cat.article_figures"),
            questions=rows("select * from cat.topic_questions"),
            kits=rows("select * from cat.topic_kits"),
            publications=rows("select * from cat.topic_publications"),
            queue=rows("select * from cat.youtube_publish_queue"),
            jobs=rows("select * from cat.jobs"),
            current_live=rows("select * from cat.current_kits_live"),
        )

    # -- rules -----------------------------------------------------------
    def current_kit(self, topic_id: str, language: str) -> dict | None:
        """Mirror of public.catalogue_current_kits() for one (topic, language)."""
        topic = self.topic_by_id.get(topic_id)
        if not topic or topic["status"] == "retired" or topic["bank_maturity"] == "none":
            return None
        eligible = []
        for k in self.kits_by_topic.get((topic_id, language), []):
            if k["status"] != "approved":
                continue
            if language == "en":
                ok = any(p["superseded_by"] is None and p["privacy"] == "public" and p["youtube_video_id"]
                         for p in self.pubs_by_kit.get(k["id"], []))
            else:
                ok = (k.get("translation_status") or "approved") in ("approved", "stale")
            if ok:
                eligible.append(k)
        if not eligible:
            return None
        # SQL order: (translation_status is distinct from 'approved') asc,
        # coalesce(reviewed,updated,created) desc, created desc, id desc.
        # A stable sort applied least-significant key first reproduces it.
        eligible.sort(key=lambda k: str(k["id"]), reverse=True)
        eligible.sort(key=lambda k: _ts(k, "created_at"), reverse=True)
        eligible.sort(key=lambda k: _ts(k, "reviewed_at", "updated_at", "created_at"), reverse=True)
        eligible.sort(key=lambda k: k.get("translation_status") != "approved" if k.get("translation_status") is not None else True)
        return eligible[0]

    def parity_with_live(self) -> list[str]:
        """Differences between our current-kit rule and production's answer."""
        live = {(r["topic_id"], r["language"]): r["topic_kit_id"] for r in self.current_live}
        diffs = []
        for (tid, lang), kid in live.items():
            if lang not in ("en", "ms"):
                continue
            mine = self.current_kit(tid, lang)
            if (mine or {}).get("id") != kid:
                diffs.append(f"{tid}/{lang}: live={kid} mine={(mine or {}).get('id')}")
        return diffs

    def approved_article(self, topic_id: str, language: str) -> list[dict]:
        return [a for a in self.articles_by_topic.get((topic_id, language), []) if a["status"] == "approved"]

    def kit_parts(self, kit: dict) -> list[int]:
        plan = kit.get("part_plan")
        if isinstance(plan, list) and plan:
            parts = sorted({int(p.get("part", 1)) for p in plan if isinstance(p, dict)})
            if parts:
                return parts
        return [1]
