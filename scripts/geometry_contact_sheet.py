"""Render every verified corpus figure onto one contact sheet — the
acceptance artefact for geometry.figure.v1. A parser being correct does
not make a drawing good; this is what gets looked at.

    .venv/Scripts/python.exe scripts/geometry_contact_sheet.py [out.png]

Reasoning figures are shown twice: metric (worked example) and schematic
(the figure a student answers from). Evidence figures once, at true scale.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from maths.geometry import GeometryRefusal, parse_question, realise, verify_question  # noqa: E402
from maths.geometry.render_static import render_figure  # noqa: E402

FONT = ROOT / "agent5_slides" / "fonts" / "DejaVuSans.ttf"
CELL = (520, 420)
COLS = 4
DPI = 110


def main(out: Path) -> None:
    corpus = json.loads((ROOT / "tests" / "fixtures" / "geometry_corpus_v1.json").read_text(encoding="utf-8"))
    tiles: list[tuple[str, Image.Image | None, str]] = []
    for item in corpus["items"]:
        raw = item["question"]
        rep = verify_question(raw)
        if not rep.ok:
            tiles.append((f"{raw['id']} — refused: {rep.refusal['code']}", None, rep.refusal["message"][:90]))
            continue
        q = parse_question(raw)
        policies = ["instructional_metric", "assessment_schematic"] if q.figure_role == "reasoning" else ["instructional_metric"]
        for ref in q.figures:
            for pol in policies:
                try:
                    m = realise(ref.figure, pol, metric=rep.models[ref.id])
                    r = render_figure(m, ref.figure, role=q.figure_role, policy=pol, dpi=DPI)
                    img = Image.open(io.BytesIO(r.png)).convert("RGB")
                    tag = "metric" if pol == "instructional_metric" else "schematic"
                    extra = f"{r.width_mm:.0f}×{r.height_mm:.0f} mm" + (" TRUE SCALE" if r.true_scale else "")
                    tiles.append((f"{raw['id']} {ref.label or ref.id} · {tag}", img, extra))
                except GeometryRefusal as exc:
                    tiles.append((f"{raw['id']} {ref.label or ref.id} · {pol}", None, f"render refused: {exc.code}: {exc.message[:70]}"))
    rows = (len(tiles) + COLS - 1) // COLS
    sheet = Image.new("RGB", (COLS * CELL[0], rows * CELL[1]), "white")
    dr = ImageDraw.Draw(sheet)
    title_font = ImageFont.truetype(str(FONT), 16)
    note_font = ImageFont.truetype(str(FONT), 12)
    for i, (title, img, note) in enumerate(tiles):
        x0, y0 = (i % COLS) * CELL[0], (i // COLS) * CELL[1]
        dr.rectangle([x0, y0, x0 + CELL[0] - 1, y0 + CELL[1] - 1], outline="#dddddd")
        dr.text((x0 + 8, y0 + 6), title, fill="#111111", font=title_font)
        dr.text((x0 + 8, y0 + 26), note, fill="#666666" if img is not None else "#b00020", font=note_font)
        if img is not None:
            box = (CELL[0] - 16, CELL[1] - 50)
            s = min(box[0] / img.width, box[1] / img.height, 1.0)
            if s < 1.0:
                img = img.resize((max(1, int(img.width * s)), max(1, int(img.height * s))), Image.LANCZOS)
            sheet.paste(img, (x0 + 8 + (box[0] - img.width) // 2, y0 + 44 + (box[1] - img.height) // 2))
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    print(f"{len(tiles)} tiles -> {out}")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "spike" / "out" / "geometry_contact_sheet.png")
