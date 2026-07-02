"""Option 3 spike — native object-aware renderer.

Instead of speedpainting a flat image, animate the slide's OBJECTS in teaching
order: the title writes on, the divider grows, then each bullet writes on,
left-to-right, with a pen at the frontier. Text is rendered crisply by PIL, so
fidelity is perfect; output is deterministic, instant and free.

The "plan" here is implicit (title → divider → bullets), exactly what Agent 3
already produces (slide_heading + slide_points). A real companion-JSON plan
would just reorder/extend these steps.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from spike.pseudo_speedpaint import _draw_pen, _ffmpeg

W, H = 1280, 720
BG, INK, GREEN, MUTED, RULE = (251, 246, 236), (44, 42, 38), (46, 107, 78), (111, 106, 95), (224, 217, 200)
MARGIN = 90
FONT_DIR = Path(__file__).resolve().parent.parent / "agent5_slides" / "fonts"
PX_PER_FRAME = 26  # write speed


def _font(bold: bool, size: int):
    return ImageFont.truetype(str(FONT_DIR / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")), size)


def _wrap(draw, text, font, max_w):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = f"{cur} {w}".strip()
        if draw.textlength(t, font=font) <= max_w:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def render_native(spec: dict, output_video: str | Path, fps: int = 24, hold: float = 1.2) -> Path:
    """spec = {context, heading, points:[...], footer}. Animate objects in order."""
    full = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(full)
    boxes: list[list[tuple[int, int, int, int]]] = []  # element -> list of line bboxes (draw order)
    y = 52

    if spec.get("context"):
        f = _font(False, 20)
        txt = spec["context"][:80]
        d.text((MARGIN, y), txt, fill=MUTED, font=f)
        boxes.append([(MARGIN, y, MARGIN + int(d.textlength(txt, font=f)), y + 26)])
        y += 34

    hf = _font(True, 40)
    hbox = []
    for ln in _wrap(d, spec.get("heading", ""), hf, W - 2 * MARGIN)[:2]:
        d.text((MARGIN, y), ln, fill=GREEN, font=hf)
        hbox.append((MARGIN, y, MARGIN + int(d.textlength(ln, font=hf)), y + 50))
        y += 52
    if hbox:
        boxes.append(hbox)

    d.line([(MARGIN, y + 6), (W - MARGIN, y + 6)], fill=RULE, width=3)
    boxes.append([(MARGIN, y + 1, W - MARGIN, y + 12)])
    y += 38

    bf = _font(False, 30)
    for pt in spec.get("points", []):
        lines = _wrap(d, pt, bf, W - 2 * MARGIN - 34)
        ebox = []
        for j, ln in enumerate(lines):
            if j == 0:
                d.text((MARGIN, y), "•", fill=GREEN, font=bf)
            tx = MARGIN + 34
            d.text((tx, y), ln, fill=INK, font=bf)
            x0 = MARGIN if j == 0 else tx
            ebox.append((x0, y, tx + int(d.textlength(ln, font=bf)), y + 40))
            y += 42
        boxes.append(ebox)
        y += 12

    # Animate: reveal `full` within each line's bbox, left→right, pen at frontier.
    out = Path(output_video)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp())
    mask = Image.new("L", (W, H), 0)
    md = ImageDraw.Draw(mask)
    idx = 0

    def emit(pen):
        nonlocal idx
        comp = Image.composite(full, Image.new("RGB", (W, H), BG), mask)
        if pen:
            _draw_pen(ImageDraw.Draw(comp), int(pen[0]), int(pen[1]))
        comp.save(tmp / f"f{idx:04d}.png")
        idx += 1

    for element in boxes:
        for (x0, y0, x1, y1) in element:
            x = x0
            while x < x1:
                md.rectangle([x0, y0, x, y1], fill=255)
                emit((x, (y0 + y1) // 2))
                x += PX_PER_FRAME
            md.rectangle([x0, y0, x1, y1], fill=255)

    for _ in range(int(hold * fps)):
        full.save(tmp / f"f{idx:04d}.png")
        idx += 1

    subprocess.run(
        [_ffmpeg(), "-y", "-framerate", str(fps), "-i", str(tmp / "f%04d.png"),
         "-pix_fmt", "yuv420p", "-vf", "scale=1280:720", "-c:v", "libx264", str(out)],
        check=True, capture_output=True,
    )
    shutil.rmtree(tmp, ignore_errors=True)
    return out
