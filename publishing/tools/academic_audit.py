"""Revision 2, step 1 (founder 2026-10-26): an INDEPENDENT academic audit of every
chapter — never by the model that wrote it.

The chapters were drafted by Claude Sonnet. Two other models audit each chapter
separately, blind to each other: Gemini 3.5 Flash at its HIGHEST thinking level
and Claude Opus 5.5. Each reads the whole chapter as printed — teaching text,
figures (their captions and source identities), questions, answers and marking
schemes — with the full Cambridge 0893 objective list and the earlier chapters'
headings, and reports:

  findings    scientific errors and misleading simplifications, answer-key and
              mark-scheme errors, ambiguous or picture-dependent questions,
              mis-tiered questions, internal text printed as captions, and
              repetition of earlier chapters — each with a location, a quote,
              the problem, an exact fix and a confidence;
  mapping     the 0893 objectives (incl. Thinking and Working Scientifically and
              Science in Context) each section and question actually addresses.

The two reports are merged: a finding both validators raise at the same place
is CONFIRMED; a finding only one raises is SINGLE and is checked before any
change. Nothing in the manuscript changes here — this step only reports.

    python -m publishing.tools.academic_audit <slug> 1 10
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

log = logging.getLogger("publishing.academic_audit")

SYSTEM = ("You are a senior Cambridge Lower Secondary Science examiner and a biology subject expert reviewing a "
          "study-guide manuscript before publication. You did not write it. Be exact and sceptical: a statement is "
          "wrong if a well-informed biology teacher would mark it wrong for learners aged 11-14, or if it would "
          "plant a misconception later study must undo. Do not report matters of taste.")

TASK = """Audit Chapter {n} of "{book}" (ages {age}, {curriculum}). The chapter is given below exactly as printed:
teaching blocks with ids (sid.bN), figures with their captions and source identities, and every question (xid)
with its answer, worked solution and marking scheme.

Report:
1. findings — every problem, each one object:
   severity: "P0" (a scientific or answer-key error), "P1" (misleading simplification, ambiguous or unanswerable
   question, wrong marks, a question that depends on something its picture does not show, a challenge question that
   only tests recall, a wrong tier), "P2" (wording, repetition, caption that reads like an internal production
   note rather than a caption, minor);
   category: "science" | "assessment" | "curriculum" | "repetition" | "figure" | "editorial";
   where: the block id (e.g. "s3.b4"), the question id (e.g. "x7", or "x7.answer" / "x7.scheme"), "fig:<ref>", or
   "chapter";
   quote: the exact words at fault (short);
   problem: what is wrong and why, in one or two sentences;
   fix: the exact corrected wording to print (or, for a structural problem, the precise change);
   confidence: 0-1.
2. mapping — for EVERY section and EVERY question, the codes from the objective list below that it genuinely
   teaches or assesses (content codes AND Thinking and Working Scientifically / Science in Context codes); [] when
   none. Do not stretch: only what a teacher would agree it addresses.
3. tiers — for each question whose tier (guided / independent / challenge) is wrong for its demand:
   {{"xid", "stated", "should_be", "why"}}.
4. repetition — teaching that only repeats an earlier chapter (headings given below) without adding to it:
   {{"where", "repeats", "advice": "cut" | "shorten" | "keep as brief reinforcement"}}.

Objective list (code: text):
{objectives}

Earlier chapters (number, title, section headings, key terms):
{earlier}

CHAPTER {n} AS PRINTED:
{chapter}

Reply with JSON only: {{"findings": [...], "mapping": {{"sections": {{"s1": [codes]}}, "exercises": {{"x1": [codes]}}}},
"tiers": [...], "repetition": [...]}}"""


def _objectives(env_file: Path) -> dict[str, str]:
    """The 0893 nodes the book could touch: biology strands, TWS, SIC — and the
    non-biology content codes the book's own blueprint cites (e.g. 8Pf.07)."""
    from publishing.guard import client, load_env
    url, key = load_env(env_file)
    with client(url, key) as http:
        cur = http.get("/curricula", params={"select": "id", "code": "eq.cambridge_ls_science_0893"}).json()[0]["id"]
        rows = http.get("/curriculum_nodes", params={"select": "code,title", "curriculum_id": f"eq.{cur}"}).json()
    keep = re.compile(r"^[789](Bs|Bp|TWS[a-z]|SIC)\.|^8Pf\.07$")
    return {r["code"]: r["title"] for r in rows if keep.match(r["code"] or "")}


def chapter_text(doc: dict, full: dict) -> str:
    first = lambda d: re.split(r"(?<=[.!?])\s", re.sub(r"^(SOURCE|UNVERIFIED identity):\s*", "", (d or "").strip()),
                               maxsplit=1)[0][:300]          # a whole first sentence, never a cut-off fragment
    desc = {a["id"]: (a.get("kind"), first(a.get("description")),
                      ((a.get("identity") or {}).get("source_title") or "")) for a in full.get("library_assets") or []}
    for t in full.get("topics") or []:
        for f in t["figures"]:
            desc[f["id"]] = ("diagram", first(f.get("caption")), "")
    out = [f"Chapter {doc['chapter']['number']}: {doc['chapter']['title']}",
           "Objectives: " + "; ".join(f"{o['code']} {o['text']}" for o in doc["chapter"].get("objectives") or []),
           f"Opening: {doc['plan'].get('opening')}"]
    for s in doc["sections"]:
        out.append(f"\n## {s['sid']} {s['heading']}")
        for i, b in enumerate(s.get("blocks") or []):
            bid = f"{s['sid']}.b{i}"
            t = b.get("type")
            if t in ("figure", "figure_group"):
                refs = [b.get("ref")] if t == "figure" else [i.get("ref") for i in b.get("items") or [] if isinstance(i, dict)]
                labels = [i.get("label") for i in b.get("items") or [] if isinstance(i, dict)]
                pics = "; ".join(f"{r} = {desc.get(r)}" for r in refs)
                out.append(f"[{bid} {t}] PRINTED caption: {b.get('caption')} | labels: {labels} | "
                           f"(NOT printed — what each picture shows, from its source record: {pics})")
            elif t == "misconception":
                out.append(f"[{bid} misconception] wrong: {b.get('wrong')} | right: {b.get('right')}")
            elif t == "key_term":
                out.append(f"[{bid} key_term] {b.get('term')}: {b.get('text')}")
            elif t == "think":
                out.append(f"[{bid} think] {b.get('text')} | printed answer: {b.get('answer')}")
            elif t == "list":
                out.append(f"[{bid} list] " + " / ".join(map(str, b.get("items") or [])))
            elif t == "table":
                out.append(f"[{bid} table] {b.get('headers')} " + " | ".join(map(str, b.get("rows") or [])))
            elif t == "worked_example":
                out.append(f"[{bid} worked_example] {b.get('problem')} steps: {b.get('steps')} answer: {b.get('answer')}")
            else:
                out.append(f"[{bid} {t}] {b.get('text')}")
    out.append("\n## QUESTIONS")
    for x in doc["exercises"]:
        refs = ([x["figure_ref"]] if x.get("figure_ref") else []) + list(x.get("figure_refs") or [])
        out.append(json.dumps({k: x.get(k) for k in ("xid", "tier", "item_type", "marks", "stem", "options", "pairs",
                                                      "items", "categories", "answer", "worked_solution",
                                                      "marking_scheme", "figure_context")}, ensure_ascii=False)
                   + (" | pictures: " + "; ".join(f"{r} = {desc.get(r)}" for r in refs) if refs else ""))
    return "\n".join(out)


class OpusValidator:
    """Claude Opus 5.5 on Bedrock with ADAPTIVE thinking at high effort — the only thinking mode that model
    accepts there, and one the shared client does not send. Same reply shape as the shared clients."""

    MODEL, USD_IN, USD_OUT = "global.anthropic.claude-opus-5-5", 4.0, 20.0

    def __init__(self):
        from anthropic import AnthropicBedrock
        self.client = AnthropicBedrock(aws_region=os.getenv("BEDROCK_REGION") or os.getenv("AWS_REGION") or "us-east-1")

    def analyze(self, prompt: str, system: str = "", max_tokens: int = 32000) -> dict:
        text, usage, stop = [], None, None
        with self.client.messages.stream(
                model=self.MODEL, max_tokens=max_tokens, system=system,
                thinking={"type": "adaptive"}, extra_body={"output_config": {"effort": "high"}},
                messages=[{"role": "user", "content": prompt}]) as st:
            for ev in st:
                pass
            msg = st.get_final_message()
        for block in msg.content:
            if getattr(block, "type", "") == "text":
                text.append(block.text)
        usage, stop = msg.usage, msg.stop_reason
        raw = "".join(text)
        m0, m1 = raw.find("{"), raw.rfind("}")
        try:
            data = json.loads(raw[m0:m1 + 1]) if m0 >= 0 else None
        except json.JSONDecodeError:
            data = None
        usd = (usage.input_tokens * self.USD_IN + usage.output_tokens * self.USD_OUT) / 1e6
        return {"data": data, "truncated": stop == "max_tokens",
                "usage": {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
                          "estimated_cost_usd": usd}}


def _validators():
    from publishing.blueprint import author_client
    os.environ["GEMINI_THINKING_ARTIFACT"] = "HIGH"     # this step only: the deepest review Gemini offers
    os.environ.setdefault("PUBLISH_GEMINI_MODEL", "gemini-3.5-flash")
    return {"gemini-3.5-flash(HIGH)": author_client("gemini"), "claude-opus-5-5(adaptive,high)": OpusValidator()}


def _ask(cli, prompt: str) -> tuple[dict, dict]:
    t0 = time.time()
    for attempt in (1, 2, 3):
        try:
            r = cli.analyze(prompt, system=SYSTEM, max_tokens=32000)
            if r.get("truncated"):
                raise RuntimeError("reply truncated")
            data = r.get("data")
            if isinstance(data, dict):
                u = r.get("usage") or {}
                return data, {"usd": u.get("estimated_cost_usd") or 0, "in": u.get("input_tokens"),
                              "out": u.get("output_tokens"), "secs": round(time.time() - t0, 1)}
            raise RuntimeError(f"not JSON: {str(data)[:200]}")
        except Exception as e:  # noqa: BLE001
            log.warning("validator attempt %d failed: %s", attempt, e)
            time.sleep(15 * attempt)
    raise RuntimeError("validator failed three times")


def _same(a: dict, b: dict) -> bool:
    """Two validators found the same problem: the same place, and overlapping words."""
    wa, wb = str(a.get("where", "")).split(".answer")[0], str(b.get("where", "")).split(".answer")[0]
    if wa.split(".")[0] != wb.split(".")[0] and wa != "chapter":
        return False
    ta = set(re.findall(r"[a-z]{4,}", (str(a.get("quote")) + " " + str(a.get("problem"))).lower()))
    tb = set(re.findall(r"[a-z]{4,}", (str(b.get("quote")) + " " + str(b.get("problem"))).lower()))
    return len(ta & tb) / max(1, min(len(ta), len(tb))) >= 0.3


def _conf(f: dict) -> float:
    try:
        return float(f.get("confidence") or 0)
    except (TypeError, ValueError):       # a validator may write "high"
        return {"high": 0.9, "medium": 0.6, "low": 0.3}.get(str(f.get("confidence")).lower(), 0.0)


def merge(reports: dict[str, dict]) -> dict:
    names = list(reports)
    merged, used = [], set()
    a, b = names[0], names[1]
    for i, fa in enumerate(reports[a].get("findings") or []):
        j = next((j for j, fb in enumerate(reports[b].get("findings") or [])
                  if j not in used and _same(fa, fb)), None)
        if j is not None:
            used.add(j)
            fb = reports[b]["findings"][j]
            sev = min(fa.get("severity", "P2"), fb.get("severity", "P2"))
            merged.append({**fa, "severity": sev, "agreement": "CONFIRMED", "by": [a, b], "other": fb})
        else:
            merged.append({**fa, "agreement": "SINGLE", "by": [a]})
    for j, fb in enumerate(reports[b].get("findings") or []):
        if j not in used:
            merged.append({**fb, "agreement": "SINGLE", "by": [b]})
    merged.sort(key=lambda f: (str(f.get("severity", "P2")), f["agreement"] != "CONFIRMED", -_conf(f)))
    # mapping: a code counts when either validator assigns it; both = stronger evidence
    mapping: dict[str, dict] = {"sections": {}, "exercises": {}}
    for kind in ("sections", "exercises"):
        for name in names:
            for unit, codes in ((reports[name].get("mapping") or {}).get(kind) or {}).items():
                for c in codes or []:
                    mapping[kind].setdefault(unit, {}).setdefault(c, []).append(name)
    return {"findings": merged, "mapping": mapping,
            "tiers": {n: reports[n].get("tiers") or [] for n in names},
            "repetition": {n: reports[n].get("repetition") or [] for n in names}}


def run(slug: str, first: int, last: int, env_file: Path) -> dict:
    from publishing.blueprint import _load_model_env
    from publishing.report import OUT
    _load_model_env(env_file)
    out = OUT / "manuscripts" / slug
    audit_dir = out / "rev2" / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    objectives = _objectives(env_file)
    (audit_dir / "objectives-0893.json").write_text(json.dumps(objectives, indent=1, ensure_ascii=False), encoding="utf-8")
    obj_text = "\n".join(f"{k}: {v}" for k, v in sorted(objectives.items()))
    docs = {n: json.loads((out / f"ch{n:02d}-draft.json").read_text(encoding="utf-8")) for n in range(1, last + 1)
            if (out / f"ch{n:02d}-draft.json").exists()}
    vals = _validators()
    summary = {}

    def one(n):
        f = audit_dir / f"ch{n:02d}-audit.json"
        if f.exists():
            return n, json.loads(f.read_text(encoding="utf-8"))
        doc = docs[n]
        full = json.loads((out / f"ch{n:02d}-dossier-full.json").read_text(encoding="utf-8"))
        earlier = "\n".join(
            f"{m}. {d['chapter']['title']} — sections: " + "; ".join(s["heading"] for s in d["sections"]) +
            " — key terms: " + ", ".join(b.get("term") for s in d["sections"] for b in s["blocks"] if b.get("type") == "key_term")
            for m, d in docs.items() if m < n) or "(none: this is the first chapter)"
        prompt = TASK.format(n=n, book=doc["work"]["title"], age=doc["work"]["age_band"],
                             curriculum=doc["work"]["curriculum"], objectives=obj_text, earlier=earlier,
                             chapter=chapter_text(doc, full))
        reports, costs = {}, {}
        with ThreadPoolExecutor(max_workers=2) as pool:
            futs = {name: pool.submit(_ask, cli, prompt) for name, cli in vals.items()}
            for name, fu in futs.items():
                reports[name], costs[name] = fu.result()
        result = {"chapter": n, "validators": costs, "raw": reports, **merge(reports)}
        f.write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
        return n, result

    with ThreadPoolExecutor(max_workers=3) as pool:
        for n, res in pool.map(one, [n for n in range(first, last + 1) if n in docs]):
            fs = res["findings"]
            summary[n] = {"P0": sum(1 for f in fs if f.get("severity") == "P0"),
                          "P1": sum(1 for f in fs if f.get("severity") == "P1"),
                          "P2": sum(1 for f in fs if f.get("severity") == "P2"),
                          "confirmed": sum(1 for f in fs if f["agreement"] == "CONFIRMED"),
                          "usd": round(sum(c.get("usd") or 0 for c in res["validators"].values()), 2)}
            log.info("ch%d: %s", n, summary[n])
    (audit_dir / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return summary


def main() -> None:
    from publishing.cli import DEFAULT_ENV
    a = argparse.ArgumentParser()
    a.add_argument("slug")
    a.add_argument("first", type=int)
    a.add_argument("last", type=int)
    a.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    x = a.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    print(json.dumps(run(x.slug, x.first, x.last, x.env_file), indent=1))


if __name__ == "__main__":
    main()
