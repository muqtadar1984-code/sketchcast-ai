"""SK R1 retrieval bench: Postgres FTS vs pgvector vs hybrid (RRF) over Biology Chapter 2.

Runs against a Supabase BRANCH database through PostgREST RPC only (no direct Postgres
connection). The sk_r1_* objects must exist first (spike/sk_r1/schema.sql, applied with
the Supabase MCP or psql). Nothing here touches sk_book_* or topic_* tables.

    python -I spike/sk_r1/bench.py --all            # embed corpus, run, summarise
    python -I spike/sk_r1/bench.py --embed          # embed docs that lack a vector
    python -I spike/sk_r1/bench.py --run --repeats 3
    python -I spike/sk_r1/bench.py --summary        # re-print tables from results.json

Environment (or --env-file pointing at a KEY=VALUE file):
    SK_R1_SUPABASE_URL   https://<branch-ref>.supabase.co
    SK_R1_SUPABASE_KEY   the branch's publishable (anon) key
    GOOGLE_AI_API_KEY    Gemini key for gemini-embedding-001 (fallback embedder, see report)
Embeddings: amazon.titan-embed-text-v2:0 through Bedrock when the AWS identity may invoke it
(--embedder titan); otherwise gemini-embedding-001 at 1024 dims (--embedder gemini).
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
QUESTIONS = HERE / "questions.json"
CORPUS = HERE / "corpus_dump.json"
QCACHE = HERE / "query_embeddings.json"
RESULTS = HERE / "results.json"
SUMMARY = HERE / "summary.md"

FAKE_PRODUCT = "00000000-0000-4000-8000-00000000fa4e"
FAKE_CHAPTER = 9
METHODS = ["fts_and", "fts_or", "vec", "hybrid_and", "hybrid_or"]
K = 5
DIMS = 1024

# Public list prices used for the cost tables (USD). Verify against the pricing pages before any approval.
PRICE = {
    "titan_v2_per_1k_tokens": 0.00002,          # Amazon Titan Text Embeddings V2, on-demand
    "gemini_embedding_per_1m_tokens": 0.15,     # gemini-embedding-001, paid tier
}


# ----------------------------------------------------------------------------- config
def load_env_file(path: str | None) -> None:
    if not path:
        return
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def cfg(name: str, default: str | None = None) -> str:
    v = os.environ.get(name, default)
    if not v:
        sys.exit(f"missing {name}")
    return v


class Rest:
    def __init__(self) -> None:
        self.url = cfg("SK_R1_SUPABASE_URL").rstrip("/")
        key = cfg("SK_R1_SUPABASE_KEY")
        self.h = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        self.s = requests.Session()

    def rpc(self, fn: str, args: dict) -> tuple[int, object, float]:
        t0 = time.perf_counter()
        r = self.s.post(f"{self.url}/rest/v1/rpc/{fn}", headers=self.h, data=json.dumps(args), timeout=60)
        ms = (time.perf_counter() - t0) * 1000
        try:
            body = r.json()
        except ValueError:
            body = r.text
        return r.status_code, body, ms


# ----------------------------------------------------------------------------- embedders
class GeminiEmbedder:
    name = "gemini-embedding-001"
    price_note = "gemini-embedding-001 at $0.15 per 1M input tokens (paid tier)"

    def __init__(self) -> None:
        self.key = cfg("GOOGLE_AI_API_KEY")
        self.base = "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001"
        self.s = requests.Session()
        self.calls = 0
        self.chars = 0
        self.tokens = 0          # from countTokens when it works, else chars/4 estimate
        self.tokens_estimated = False
        self.latencies: list[float] = []

    def _count_tokens(self, text: str) -> int:
        try:
            r = self.s.post(f"{self.base}:countTokens", headers={"x-goog-api-key": self.key},
                            json={"contents": [{"parts": [{"text": text}]}]}, timeout=30)
            if r.ok and "totalTokens" in r.json():
                return int(r.json()["totalTokens"])
        except requests.RequestException:
            pass
        self.tokens_estimated = True
        return max(1, len(text) // 4)

    def embed(self, text: str, kind: str) -> list[float]:
        task = "RETRIEVAL_QUERY" if kind == "query" else "RETRIEVAL_DOCUMENT"
        body = {"content": {"parts": [{"text": text}]}, "taskType": task, "outputDimensionality": DIMS}
        t0 = time.perf_counter()
        r = self.s.post(f"{self.base}:embedContent", headers={"x-goog-api-key": self.key}, json=body, timeout=60)
        self.latencies.append((time.perf_counter() - t0) * 1000)
        r.raise_for_status()
        self.calls += 1
        self.chars += len(text)
        self.tokens += self._count_tokens(text)
        return r.json()["embedding"]["values"]


class TitanEmbedder:
    name = "amazon.titan-embed-text-v2:0"
    price_note = "Titan Text Embeddings V2 at $0.00002 per 1K input tokens"

    def __init__(self) -> None:
        import boto3  # noqa: WPS433 (optional dependency)
        self.c = boto3.client("bedrock-runtime", region_name=os.environ.get("BEDROCK_REGION", "us-east-1"))
        self.calls = 0
        self.chars = 0
        self.tokens = 0
        self.tokens_estimated = False
        self.latencies: list[float] = []

    def embed(self, text: str, kind: str) -> list[float]:
        t0 = time.perf_counter()
        r = self.c.invoke_model(modelId=self.name, body=json.dumps({"inputText": text, "dimensions": DIMS, "normalize": True}))
        self.latencies.append((time.perf_counter() - t0) * 1000)
        b = json.loads(r["body"].read())
        self.calls += 1
        self.chars += len(text)
        self.tokens += int(b.get("inputTextTokenCount", len(text) // 4))
        return b["embedding"]


def make_embedder(which: str):
    if which == "titan":
        return TitanEmbedder()
    return GeminiEmbedder()


# ----------------------------------------------------------------------------- steps
def step_embed(rest: Rest, emb) -> dict:
    status, docs, _ = rest.rpc("sk_r1_dump", {})
    if status != 200:
        sys.exit(f"sk_r1_dump failed: {status} {docs}")
    real = [d for d in docs if d["src_kind"] in ("block", "article_section")]
    CORPUS.write_text(json.dumps(real, indent=1, ensure_ascii=False), encoding="utf-8")
    todo = [d for d in real if not d["has_embedding"]]
    print(f"corpus: {len(real)} real docs ({sum(1 for d in real if d['src_kind']=='article_section')} article sections), "
          f"{len(todo)} to embed with {emb.name}")
    t0 = time.perf_counter()
    batch, written = [], 0
    for i, d in enumerate(todo, 1):
        batch.append({"id": d["id"], "embedding": json.dumps(emb.embed(d["content"], "document"))})
        if len(batch) == 20 or i == len(todo):
            st, n, _ = rest.rpc("sk_r1_set_embeddings", {"p": batch})
            if st != 200:
                sys.exit(f"sk_r1_set_embeddings failed: {st} {n}")
            written += int(n)
            batch = []
    wall = time.perf_counter() - t0
    st, docs, _ = rest.rpc("sk_r1_dump", {})
    by_kind: dict[str, list[int]] = {}
    for d in docs:
        by_kind.setdefault(d["src_kind"], [0, 0])
        by_kind[d["src_kind"]][0] += 1
        by_kind[d["src_kind"]][1] += 1 if d["has_embedding"] else 0
    info = {
        "embedder": emb.name, "docs_embedded_this_run": written, "calls": emb.calls, "chars": emb.chars,
        "tokens": emb.tokens, "tokens_estimated": emb.tokens_estimated, "wall_s": round(wall, 1),
        "embed_latency_ms_p50": round(statistics.median(emb.latencies), 1) if emb.latencies else None,
        "coverage_by_kind": {k: f"{v[1]}/{v[0]}" for k, v in by_kind.items()},
        "cost_usd_measured": round(emb.tokens / 1e6 * PRICE["gemini_embedding_per_1m_tokens"], 6) if emb.name.startswith("gemini")
                             else round(emb.tokens / 1000 * PRICE["titan_v2_per_1k_tokens"], 6),
        "cost_usd_titan_v2_equivalent": round(emb.tokens / 1000 * PRICE["titan_v2_per_1k_tokens"], 6),
    }
    print(json.dumps(info, indent=1))
    return info


def embed_queries(emb, questions: list[dict]) -> tuple[dict[str, list[float]], dict]:
    cache = json.loads(QCACHE.read_text(encoding="utf-8")) if QCACHE.exists() else {}
    if cache.get("_embedder") not in (None, emb.name):
        cache = {}
    cache["_embedder"] = emb.name
    before_calls, before_tokens = emb.calls, emb.tokens
    for q in questions:
        if q["text"] not in cache:
            cache[q["text"]] = emb.embed(q["text"], "query")
    QCACHE.write_text(json.dumps(cache), encoding="utf-8")
    lat = emb.latencies[len(emb.latencies) - (emb.calls - before_calls):] if emb.calls > before_calls else []
    info = {"embedder": emb.name, "queries_embedded_this_run": emb.calls - before_calls,
            "tokens": emb.tokens - before_tokens, "tokens_estimated": emb.tokens_estimated,
            "latency_ms_p50": round(statistics.median(lat), 1) if lat else None,
            "latency_ms_p95": round(pct(lat, 95), 1) if lat else None}
    return {k: v for k, v in cache.items() if not k.startswith("_")}, info


def call_method(rest: Rest, method: str, q: dict, qv: list[float], product: str, chapter: int) -> dict:
    vec = json.dumps(qv)
    if method == "fts_and":
        st, body, ms = rest.rpc("sk_r1_search_fts", {"q": q["text"], "p_product": product, "p_chapter": chapter, "k": K, "mode": "and"})
    elif method == "fts_or":
        st, body, ms = rest.rpc("sk_r1_search_fts", {"q": q["text"], "p_product": product, "p_chapter": chapter, "k": K, "mode": "or"})
    elif method == "vec":
        st, body, ms = rest.rpc("sk_r1_search_vec", {"qv": vec, "p_product": product, "p_chapter": chapter, "k": K})
    elif method == "hybrid_and":
        st, body, ms = rest.rpc("sk_r1_search_hybrid", {"q": q["text"], "qv": vec, "p_product": product, "p_chapter": chapter, "k": K, "pool": 20, "rrf_k": 60, "mode": "and"})
    else:
        st, body, ms = rest.rpc("sk_r1_search_hybrid", {"q": q["text"], "qv": vec, "p_product": product, "p_chapter": chapter, "k": K, "pool": 20, "rrf_k": 60, "mode": "or"})
    out = {"status": st, "e2e_ms": round(ms, 1)}
    if st == 200 and isinstance(body, dict):
        out["db_ms"] = round(float(body.get("db_ms", 0)), 3)
        out["tsquery"] = body.get("tsquery")
        out["rows"] = body.get("rows", [])
    else:
        out["error"] = body if isinstance(body, str) else json.dumps(body)[:500]
        out["rows"] = []
    return out


def step_run(rest: Rest, emb, repeats: int) -> dict:
    qs = json.loads(QUESTIONS.read_text(encoding="utf-8"))
    questions = qs["questions"]
    product, chapter = qs["product_id"], qs["chapter"]
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    ref_by_src = {d["src_id"]: f"{d['unit_key']}#{d['src_index']:02d}" for d in corpus if d["src_kind"] == "block"}
    qvecs, qinfo = embed_queries(emb, questions)

    # Warm-up so the first measured call does not carry PostgREST connection setup.
    for m in METHODS:
        call_method(rest, m, questions[0], qvecs[questions[0]["text"]], product, chapter)

    runs = []
    for rep in range(repeats):
        for q in questions:
            for m in METHODS:
                r = call_method(rest, m, q, qvecs[q["text"]], product, chapter)
                r.update({"rep": rep, "qid": q["id"], "method": m})
                for row in r["rows"]:
                    row["ref"] = ref_by_src.get(row["src_id"], f"?{row['src_kind']}")
                runs.append(r)
        print(f"repeat {rep + 1}/{repeats} done")

    # Filter proof: ask for the fake product / fake chapter explicitly and confirm only those rows come back.
    probe_q = questions[0]
    filter_probe = {}
    for label, pid, ch in (("fake_product", FAKE_PRODUCT, chapter), ("fake_chapter", product, FAKE_CHAPTER)):
        filter_probe[label] = {}
        for m in METHODS:
            r = call_method(rest, m, probe_q, qvecs[probe_q["text"]], pid, ch)
            kinds = sorted({row["src_kind"] for row in r["rows"]})
            filter_probe[label][m] = {"rows": len(r["rows"]), "src_kinds": kinds,
                                      "all_requested_scope": all(row["product_id"] == pid and row["chapter"] == ch for row in r["rows"])}

    results = {"ran_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "repeats": repeats, "k": K,
               "supabase_url": rest.url, "query_embedding": qinfo, "runs": runs, "filter_probe": filter_probe}
    RESULTS.write_text(json.dumps(results, indent=1), encoding="utf-8")
    return results


# ----------------------------------------------------------------------------- scoring
def pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    i = (len(xs) - 1) * p / 100
    lo, hi = int(i), min(int(i) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (i - lo)


def top_score(r: dict, method: str) -> float:
    if not r["rows"]:
        return 0.0
    if method.startswith("hybrid"):
        return max(float(row.get("vec_score") or 0) for row in r["rows"])
    return float(r["rows"][0]["score"])


def score_run(r: dict, q: dict) -> dict:
    refs = [row["ref"] for row in r["rows"][:K]]
    units = [row["unit_key"] for row in r["rows"][:K]]
    gb, gu = set(q["gold_blocks"]), set(q["gold_units"])
    b_rank = next((i + 1 for i, x in enumerate(refs) if x in gb), 0)
    u_rank = next((i + 1 for i, x in enumerate(units) if x in gu), 0)
    leaks = sum(1 for row in r["rows"] if row["src_kind"] != "block" or row["chapter"] != 2)
    return {"block_hit": int(b_rank > 0), "block_rr": 1 / b_rank if b_rank else 0.0,
            "unit_hit": int(u_rank > 0), "unit_rr": 1 / u_rank if u_rank else 0.0,
            "leaks": leaks, "n_rows": len(r["rows"]), "ok": int(r["status"] == 200)}


def thresholds_for(method: str, in_scores: list[float], off_scores: list[float]) -> dict:
    """Fixed, declared-in-advance threshold plus the best separating one found on this data."""
    fixed = {"fts_and": 1e-9, "fts_or": 0.2, "vec": 0.60, "hybrid_and": 0.60, "hybrid_or": 0.60}[method]
    # A threshold of 0 accepts every empty result, so only positive scores are candidates.
    cands = sorted({s for s in in_scores + off_scores if s > 0} | {fixed})
    best = None
    for t in cands:
        acc = (sum(s >= t for s in in_scores) + sum(s < t for s in off_scores)) / (len(in_scores) + len(off_scores))
        if best is None or acc > best[1]:
            best = (t, acc)
    def rates(t: float) -> dict:
        return {"threshold": round(t, 4),
                "off_chapter_false_positive": f"{sum(s >= t for s in off_scores)}/{len(off_scores)}",
                "in_chapter_rejected": f"{sum(s < t for s in in_scores)}/{len(in_scores)}"}
    return {"fixed": rates(fixed), "best_on_this_data": rates(best[0]) | {"accuracy": round(best[1], 3)},
            "in_chapter_top1_min": round(min(in_scores), 4) if in_scores else None,
            "off_chapter_top1_max": round(max(off_scores), 4) if off_scores else None}


def step_summary(results: dict) -> str:
    qs = json.loads(QUESTIONS.read_text(encoding="utf-8"))
    qmap = {q["id"]: q for q in qs["questions"]}
    first = [r for r in results["runs"] if r["rep"] == 0]
    lines = []
    lines.append(f"Run {results['ran_at']} against {results['supabase_url']} (k={K}, {results['repeats']} repeats; metrics from repeat 1, latency from all repeats)")
    lines.append("")
    lines.append("### Quality (recall@5 and MRR, block level and unit level)")
    lines.append("")
    lines.append("| group | n | method | recall@5 block | MRR block | recall@5 unit | MRR unit | empty results |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for group in ("in_chapter", "alt_example", "injection"):
        for m in METHODS:
            rs = [r for r in first if r["method"] == m and qmap[r["qid"]]["group"] == group and qmap[r["qid"]]["gold_blocks"]]
            sc = [score_run(r, qmap[r["qid"]]) for r in rs]
            if not sc:
                continue
            n = len(sc)
            lines.append(f"| {group} | {n} | {m} | {sum(s['block_hit'] for s in sc)/n:.2f} | {sum(s['block_rr'] for s in sc)/n:.2f} | "
                         f"{sum(s['unit_hit'] for s in sc)/n:.2f} | {sum(s['unit_rr'] for s in sc)/n:.2f} | {sum(1 for s in sc if s['n_rows']==0)} |")
    lines.append("")
    lines.append("### Off-chapter questions (8): top-1 score vs threshold")
    lines.append("")
    lines.append("| method | threshold (fixed) | off-chapter false positives | in-chapter wrongly rejected | best threshold on this data | FP / rejected at best | in-chapter top-1 min | off-chapter top-1 max |")
    lines.append("|---|---|---|---|---|---|---|---|")
    thr = {}
    for m in METHODS:
        ins = [top_score(r, m) for r in first if r["method"] == m and qmap[r["qid"]]["group"] in ("in_chapter", "alt_example")]
        offs = [top_score(r, m) for r in first if r["method"] == m and qmap[r["qid"]]["group"] == "off_chapter"]
        t = thresholds_for(m, ins, offs)
        thr[m] = t
        lines.append(f"| {m} | {t['fixed']['threshold']} | {t['fixed']['off_chapter_false_positive']} | {t['fixed']['in_chapter_rejected']} | "
                     f"{t['best_on_this_data']['threshold']} | {t['best_on_this_data']['off_chapter_false_positive']} / {t['best_on_this_data']['in_chapter_rejected']} | "
                     f"{t['in_chapter_top1_min']} | {t['off_chapter_top1_max']} |")
    lines.append("")
    lines.append("### Injection attempts (6): errors and leaks")
    lines.append("")
    lines.append("| method | HTTP 200 | rows outside filter | questions with 0 rows |")
    lines.append("|---|---|---|---|")
    for m in METHODS:
        rs = [r for r in first if r["method"] == m and qmap[r["qid"]]["group"] == "injection"]
        lines.append(f"| {m} | {sum(r['status']==200 for r in rs)}/{len(rs)} | {sum(score_run(r, qmap[r['qid']])['leaks'] for r in rs)} | {sum(1 for r in rs if not r['rows'])} |")
    lines.append("")
    lines.append("### Filter enforcement (all 50 questions x all repeats)")
    lines.append("")
    lines.append("| method | calls | rows returned | rows from fake product or fake chapter |")
    lines.append("|---|---|---|---|")
    for m in METHODS:
        rs = [r for r in results["runs"] if r["method"] == m]
        lines.append(f"| {m} | {len(rs)} | {sum(len(r['rows']) for r in rs)} | {sum(score_run(r, qmap[r['qid']])['leaks'] for r in rs)} |")
    lines.append("")
    lines.append("Probe with the filter pointed AT the fake copies (proves the copies are indexed and the filter selects, not hides):")
    lines.append("")
    for label, per in results["filter_probe"].items():
        lines.append(f"- {label}: " + "; ".join(f"{m}: {v['rows']} rows, kinds={v['src_kinds']}, in scope={v['all_requested_scope']}" for m, v in per.items()))
    lines.append("")
    lines.append("### Latency (ms)")
    lines.append("")
    lines.append("| method | DB-side p50 (clock_timestamp inside the function) | DB-side p95 | end-to-end PostgREST p50 (this machine) | end-to-end p95 |")
    lines.append("|---|---|---|---|---|")
    for m in METHODS:
        rs = [r for r in results["runs"] if r["method"] == m and r["status"] == 200]
        db = [r["db_ms"] for r in rs]
        e2e = [r["e2e_ms"] for r in rs]
        lines.append(f"| {m} | {statistics.median(db):.2f} | {pct(db, 95):.2f} | {statistics.median(e2e):.0f} | {pct(e2e, 95):.0f} |")
    qe = results["query_embedding"]
    lines.append("")
    lines.append(f"Query embedding ({qe['embedder']}, from this machine): p50 {qe['latency_ms_p50']} ms, p95 {qe['latency_ms_p95']} ms "
                 f"over {qe['queries_embedded_this_run']} calls; vec and hybrid pay this once per turn on top of the RPC.")
    lines.append("")
    lines.append("### Per-question detail (repeat 1): first gold rank per method (0 = none in top 5)")
    lines.append("")
    lines.append("| id | group | " + " | ".join(METHODS) + " |")
    lines.append("|---|---|" + "---|" * len(METHODS))
    for q in qs["questions"]:
        cells = []
        for m in METHODS:
            r = next(x for x in first if x["method"] == m and x["qid"] == q["id"])
            if q["gold_blocks"]:
                s = score_run(r, q)
                rank = int(round(1 / s["block_rr"])) if s["block_rr"] else 0
                cells.append(f"{rank} ({top_score(r, m):.2f})")
            else:
                cells.append(f"- ({top_score(r, m):.2f})")
        lines.append(f"| {q['id']} | {q['group']} | " + " | ".join(cells) + " |")
    text = "\n".join(lines) + "\n"
    SUMMARY.write_text(text, encoding="utf-8")
    (HERE / "thresholds.json").write_text(json.dumps(thr, indent=1), encoding="utf-8")
    return text


# ----------------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--embed", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--embedder", choices=["gemini", "titan"], default="gemini")
    ap.add_argument("--env-file", default=None)
    a = ap.parse_args()
    load_env_file(a.env_file)
    if a.all:
        a.embed = a.run = a.summary = True
    if not (a.embed or a.run or a.summary):
        ap.print_help()
        return
    rest = Rest() if (a.embed or a.run) else None
    emb = make_embedder(a.embedder) if (a.embed or a.run) else None
    if a.embed:
        info = step_embed(rest, emb)
        (HERE / "embed_info.json").write_text(json.dumps(info, indent=1), encoding="utf-8")
    if a.run:
        step_run(rest, emb, a.repeats)
    if a.summary:
        print(step_summary(json.loads(RESULTS.read_text(encoding="utf-8"))))


if __name__ == "__main__":
    main()
