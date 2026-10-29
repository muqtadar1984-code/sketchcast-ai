"""The built deck, checked as a deck someone will project.

A TECHNICALLY VALID .pptx IS NOT A GOOD ONE. The storyboard's validator asks
one question — did anything run off the slide — and the before-decks pass it
with slides that are 80% white space, a zoom crop blurred to mush and a
worked example printed as a grey paragraph. This module asks the questions a
reviewer asks, in two passes:

DETERMINISTIC, from the renderer's own record of every box it drew:
  bounds · text-on-text collisions · text over pictures · type below the
  floor · contrast (WCAG ratio) · word density · a picture stretched beyond
  its pixels · a near-empty or lopsided slide · repeated titles · an answer
  visible on a question slide · columns the renderer could not fit.

VISUAL, on the slides rendered to images (PowerPoint locally, LibreOffice on
a server that has it): a vision model scores each slide against a rubric —
attractiveness, readability, image relevance, balance, wrong labels, maths
errors, exposed answers — and names the problems. Its HIGH-severity findings
go back to the Director with the deterministic ones; the correction budget
bounds how many rounds that can take.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .render import Box, SlideReport, W_IN, H_IN

logger = logging.getLogger(__name__)

SMALL_OK = {"kicker", "badge", "chip_label", "caption", "credit", "step_number", "question_number",
            "answer_label", "callout_label", "diagram_label", "figure_label"}
MIN_PT = 14.0


def _lum(c):
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = c
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a, b) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _inter(a, b) -> float:
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def _area(r):
    return max(0.0, r[2] - r[0]) * max(0.0, r[3] - r[1])


def check_slide(rep: SlideReport, title: str = "") -> list[dict]:
    """Deterministic issues: [{severity, category, detail}]."""
    out = []

    def add(sev, cat, detail):
        out.append({"severity": sev, "category": cat, "detail": detail})

    texts = [b for b in rep.boxes if b.kind == "text"]
    pics = [b for b in rep.boxes if b.kind == "picture"]
    for f in rep.faults:
        add("high", "layout", f)
    for b in texts + pics:
        x0, y0, x1, y1 = b.rect
        if x0 < 0.2 or y0 < 0.15 or x1 > W_IN - 0.2 or y1 > H_IN - 0.12:
            add("high", "bounds", f"{b.role or b.kind} '{b.text[:30]}' leaves the safe area "
                                  f"({x0:.2f},{y0:.2f})-({x1:.2f},{y1:.2f})")
    for i, a in enumerate(texts):
        for b in texts[i + 1:]:
            ov = _inter(a.rect, b.rect)
            if ov > 0.02 and ov > 0.08 * min(_area(a.rect), _area(b.rect)):
                add("high", "overlap", f"'{a.text[:25]}' overlaps '{b.text[:25]}'")
        for p in pics:
            ov = _inter(a.rect, p.rect)
            if a.role not in ("diagram_label", "figure_label") and ov > 0.08 * _area(a.rect):
                add("high", "overlap", f"text '{a.text[:25]}' sits on a picture")
        if a.pt and a.pt < MIN_PT and a.role not in SMALL_OK:
            add("medium", "type", f"{a.role} at {a.pt}pt (floor {MIN_PT}pt): '{a.text[:30]}'")
        # WCAG: "large" text (>= 18pt, or bold >= 14pt) needs 3:1, the rest 4.5:1
        need = 3.0 if (a.pt >= 18 or (a.bold and a.pt >= 14)) else 4.5
        ratio = contrast(a.fg, a.bg)
        if a.text.strip() and ratio < need and a.role not in ("credit",):
            add("high" if ratio < 3 else "medium", "contrast", f"{a.role} contrast {ratio:.1f}:1")
    for p in pics:
        pw, ph = p.px
        if pw and p.w > 0:
            ppi = pw / p.w if p.role not in ("photo", "card_picture", "cover_picture") else \
                max(pw / p.w, ph / p.h) if p.h > 0 else 0
            if 0 < ppi < 70:
                add("medium", "resolution", f"{p.role} shown at {ppi:.0f} px/in (source {pw}x{ph})")
    words = sum(len(re.findall(r"\w+", b.text)) for b in texts if b.role not in ("credit", "table"))
    rep.words = words
    if words > 75 and not any(b.role in ("step_line", "table") for b in texts):
        add("medium", "density", f"{words} visible words")
    content = [b for b in texts + pics if b.role not in ("kicker", "title", "subtitle")]
    if not content and title:
        add("high", "empty", "slide has a title and nothing else")
    elif content and rep.index > 1:
        x0 = min(b.x for b in content); y0 = min(b.y for b in content)
        x1 = max(b.x + b.w for b in content); y1 = max(b.y + b.h for b in content)
        used = (x1 - x0) * (y1 - y0) / ((W_IN - 1.24) * 5.2)
        if used < 0.22:
            add("medium", "whitespace", f"content fills {used:.0%} of the body")
    return out


def check_deck(deck, reports: list[SlideReport]) -> dict[str, list[dict]]:
    issues: dict[str, list[dict]] = {}
    titles: dict[str, str] = {}
    by_id = {s.id: s for s in deck.slides}
    for rep in reports:
        if rep.id == "credits":
            continue
        sl = by_id.get(rep.id)
        got = check_slide(rep, sl.title if sl else "")
        if sl:
            t = sl.title.strip().lower()
            if t and t in titles and "continued" not in t:
                got.append({"severity": "medium", "category": "repetition",
                            "detail": f"same title as slide {titles[t]}"})
            titles.setdefault(t, rep.id)
            vis = " ".join(b.text for b in rep.boxes if b.kind == "text").lower()
            for e in sl.elements():
                if e["type"] == "questions":
                    for it in e.get("items") or []:
                        a = (it.get("answer") or "").strip().lower()
                        q = (it.get("q") or "").lower()
                        if len(a) >= 2 and a in vis.replace(q, ""):
                            got.append({"severity": "high", "category": "answer_exposed",
                                        "detail": f"answer {it.get('answer')!r} is visible"})
        if got:
            issues[rep.id] = got
    return issues


# ── render to images ──────────────────────────────────────────────────

_PS = r"""
param([string]$Pptx, [string]$OutDir, [string]$Pdf)
$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force $OutDir | Out-Null
$app = New-Object -ComObject PowerPoint.Application
try {
  $p = $app.Presentations.Open($Pptx, $true, $false, $false)
  $i = 1
  foreach ($s in $p.Slides) { $s.Export((Join-Path $OutDir ("slide_{0:D2}.png" -f $i)), "PNG", 1600, 900); $i++ }
  if ($Pdf -ne "") { $p.SaveAs($Pdf, 32) }
  $p.Close()
} finally { $app.Quit() }
"""


def render_images(pptx: Path, out_dir: Path, pdf: Optional[Path] = None,
                  timeout: int = 600) -> tuple[list[Path], str]:
    """Slides as PNGs (and the deck as a PDF). PowerPoint via COM on Windows;
    LibreOffice + pdftoppm elsewhere. Returns ([], reason) when neither is
    available — QA then runs its deterministic half only and says so.
    ``timeout`` bounds each renderer subprocess (the deck-pages export
    passes a short one so a stuck render never holds a deck job)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("slide_*.png"):
        old.unlink()
    if sys.platform == "win32" and shutil.which("powershell"):
        ps1 = out_dir / "_render.ps1"
        ps1.write_text(_PS, encoding="utf-8")
        try:
            subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1),
                            "-Pptx", str(Path(pptx).resolve()), "-OutDir", str(out_dir.resolve()),
                            "-Pdf", str(Path(pdf).resolve()) if pdf else ""],
                           check=True, capture_output=True, timeout=timeout)
            return sorted(out_dir.glob("slide_*.png")), "powerpoint"
        except Exception as exc:  # noqa: BLE001
            logger.warning("qa: PowerPoint render failed (%s)", exc)
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if soffice and shutil.which("pdftoppm"):
        try:
            # its own throwaway profile: two decks rendering at once on one
            # worker would otherwise lock each other out of the shared one
            profile = (out_dir / "_lo_profile").resolve().as_uri()
            subprocess.run([soffice, f"-env:UserInstallation={profile}", "--headless", "--convert-to", "pdf",
                            "--outdir", str(out_dir), str(pptx)],
                           check=True, capture_output=True, timeout=timeout)
            made = out_dir / (Path(pptx).stem + ".pdf")
            subprocess.run(["pdftoppm", "-png", "-r", "120", str(made), str(out_dir / "slide")],
                           check=True, capture_output=True, timeout=timeout)
            for p in out_dir.glob("slide-*.png"):
                p.rename(out_dir / f"slide_{int(p.stem.split('-')[-1]):02d}.png")
            if pdf:
                shutil.copy(made, pdf)
            return sorted(out_dir.glob("slide_*.png")), "libreoffice"
        except Exception as exc:  # noqa: BLE001
            logger.warning("qa: LibreOffice render failed (%s)", exc)
    return [], "no renderer available (PowerPoint or LibreOffice+pdftoppm)"


# ── vision ────────────────────────────────────────────────────────────

VISION_RUBRIC = """\
You are a demanding reviewer of classroom slide decks (learners aged {age}). This is slide {n} of {total}.
Intended purpose: {purpose}
Judge ONLY what you can see. Score 1-10 for: attractiveness, readability (from the back of a classroom),
balance (layout, whitespace), image_relevance (10 if no image is needed and none is shown).
List concrete problems, each with severity high|medium|low and category one of:
overlap, clipping, empty_space, density, contrast, typography, image_irrelevant, image_low_quality,
image_missing, wrong_label, maths_error, science_error, answer_exposed, inconsistent, other.
"high" = a teacher would not project this slide as is. Do not invent problems; say [] if there are none.
Reply JSON only: {{"scores": {{"attractiveness": n, "readability": n, "balance": n, "image_relevance": n}},
"issues": [{{"severity": "...", "category": "...", "detail": "..."}}]}}"""


def vision_review(client, images: list[Path], deck, age: str, limit: int = 30) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if client is None or not hasattr(client, "analyze_image"):
        return out
    slides = list(deck.slides)
    for i, img in enumerate(images[:limit]):
        if i >= len(slides):
            break                     # the credits slide is a list of sources, not teaching
        sid = slides[i].id
        purpose = slides[i].purpose
        try:
            r = client.analyze_image(str(img), VISION_RUBRIC.format(age=age or "11-14", n=i + 1, total=len(images),
                                                                     purpose=purpose or "-"), max_tokens=1500)
            d = r.get("data") if isinstance(r, dict) else None
            out[sid] = d if isinstance(d, dict) else {"error": "unparsed"}
        except Exception as exc:  # noqa: BLE001
            out[sid] = {"error": str(exc)[:200]}
    return out


def merge(det: dict[str, list[dict]], vis: dict[str, dict]) -> dict[str, list[dict]]:
    allp: dict[str, list[dict]] = {k: list(v) for k, v in det.items()}
    for sid, d in vis.items():
        for it in (d.get("issues") or []) if isinstance(d, dict) else []:
            if isinstance(it, dict) and it.get("detail"):
                allp.setdefault(sid, []).append({"severity": str(it.get("severity") or "low"),
                                                 "category": str(it.get("category") or "other"),
                                                 "detail": str(it["detail"]), "source": "vision"})
    return allp


def blocking(issues: dict[str, list[dict]]) -> dict[str, list[str]]:
    """The problems worth a redesign: every HIGH one."""
    out: dict[str, list[str]] = {}
    for sid, its in issues.items():
        hi = [f"[{i['category']}] {i['detail']}" for i in its if i.get("severity") == "high"]
        if hi:
            out[sid] = hi
    return out


def summary(issues: dict[str, list[dict]], vis: dict[str, dict]) -> dict:
    sev: dict[str, int] = {}
    for its in issues.values():
        for i in its:
            sev[i["severity"]] = sev.get(i["severity"], 0) + 1
    scores = [d.get("scores") for d in vis.values() if isinstance(d, dict) and isinstance(d.get("scores"), dict)]
    avg = {}
    for k in ("attractiveness", "readability", "balance", "image_relevance"):
        vals = [float(s[k]) for s in scores if isinstance(s.get(k), (int, float))]
        if vals:
            avg[k] = round(sum(vals) / len(vals), 2)
    return {"by_severity": sev, "vision_avg": avg, "slides_with_issues": len(issues)}


def write_report(path: Path, data: dict) -> None:
    Path(path).write_text(json.dumps(data, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
