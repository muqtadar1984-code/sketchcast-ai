"""Stage `render_proof` — free, no model calls. Renders one chapter on the books runner (Linux, Chromium) once per
candidate heading font, so the founder can pick the open serif that replaces Sitka (D3, 2026-10-28: Sitka is
Windows-only). The text, pictures, layout search and page art are the engine's own (publishing.manuscript.render);
only the theme's heading font changes between renders.

    python -m publishing.runner.render_proof <slug> <run_rel> <chapter>

Writes <book>/<run_rel>/<font>/chNN.pdf (+ print edition, Gate D review page, diagnostics) and
<book>/<run_rel>/compare/<font>-p1.png, -p2.png (the first two pages at screen size), and proof.json.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# (folder, label, heading font stack); the body font is the theme's own stack ('Segoe UI', 'Noto Sans', …), which on
# Linux resolves to Noto Sans
FONTS = [
    ("as-designed", "As designed (Sitka is not available on Linux: shows the fallback serif)", None),
    ("source-serif-4", "Source Serif 4", "'Source Serif 4', Georgia, serif"),
    ("literata", "Literata", "'Literata', Georgia, serif"),
]


def main() -> None:
    import fitz
    from publishing.manuscript.render import render
    slug, rel, chapter = sys.argv[1], sys.argv[2], int(sys.argv[3])
    book = Path(os.environ["PUBLISH_OUT"]) / "manuscripts" / slug
    out = book / rel
    env_file = out / "empty.env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("", encoding="utf-8")            # Supabase comes from the environment (load_env falls back to it)
    (out / "compare").mkdir(exist_ok=True)
    results = []
    for folder, label, font in FONTS:
        if font:
            os.environ["PUBLISH_HEADING_FONT"] = font
        else:
            os.environ.pop("PUBLISH_HEADING_FONT", None)
        print(f"rendering chapter {chapter} · {label}", flush=True)
        doc = json.loads((book / f"ch{chapter:02d}-draft.json").read_text(encoding="utf-8"))
        full = json.loads((book / f"ch{chapter:02d}-dossier-full.json").read_text(encoding="utf-8"))
        target = out / folder
        target.mkdir(exist_ok=True)
        r = render(doc, full, env_file, target)
        pdf = fitz.open(r["pdf"])
        for i in range(min(2, pdf.page_count)):
            pdf[i].get_pixmap(dpi=110).save(str(out / "compare" / f"{folder}-p{i + 1}.png"))
        fonts = sorted({f[3] for p in pdf for f in p.get_fonts()})
        pdf.close()
        # keep what the comparison needs (the digital PDF and its diagnostics): the HTML pages embed every picture
        # (~17 MB each) and the print edition repeats the PDF with bleed — neither is uploaded for a proof
        for f in target.iterdir():
            if f.suffix == ".html" or f.name.endswith("-print.pdf"):
                f.unlink()
        results.append({"font": label, "folder": folder, "pages": r["pages"], "hard_defects": r["hard_defects"],
                        "embedded_fonts": fonts})
        print(f"  {label}: {r['pages']} pages, {r['hard_defects']} hard layout defects; fonts {', '.join(fonts)}",
              flush=True)
    env_file.unlink()
    (out / "proof.json").write_text(json.dumps({"chapter": chapter, "renders": results}, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
