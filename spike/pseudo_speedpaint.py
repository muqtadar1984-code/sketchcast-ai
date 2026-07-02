"""Deterministic 'pseudo speedpaint' — no AI, no account, no GPU.

ONE continuous drawing pass that follows the pen along a zig-zag path across the
canvas: the pen inks the line-art outline at its tip, and full colour fills in
just behind it. The artwork is built along the stroke (not wiped in flat layers),
then the finished slide holds.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _line_art(img):
    """Crisp dark line-art (outline) of the slide on white."""
    from PIL import ImageFilter, ImageOps

    gray = ImageOps.grayscale(img)
    edges = gray.filter(ImageFilter.FIND_EDGES).filter(ImageFilter.MaxFilter(3))
    inv = ImageOps.invert(edges)
    return inv.point(lambda p: 35 if p < 110 else 255).convert("RGB")


def _pen_path(w: int, h: int, rows: int, step: int = 10):
    """Boustrophedon (zig-zag) path: left→right, drop a row, right→left, …"""
    pts: list[tuple[int, int]] = []
    band = h / rows
    for r in range(rows):
        y = int(band * (r + 0.5))
        xs = range(0, w + 1, step) if r % 2 == 0 else range(w, -1, -step)
        for x in xs:
            pts.append((x, y))
    return pts


def _draw_pen(draw, x: int, y: int) -> None:
    tip = (x, y)
    body = (x + 30, y - 84)
    draw.line([(x + 3, y + 4), (body[0] + 3, body[1] + 4)], fill=(0, 0, 0), width=10)  # shadow
    draw.line([tip, body], fill=(35, 35, 40), width=12)       # barrel
    draw.line([tip, body], fill=(120, 120, 130), width=4)     # highlight
    draw.polygon([(x - 7, y - 1), (x + 7, y + 1), (x, y + 14)], fill=(15, 15, 18))  # nib


def pseudo_speedpaint(
    image_path: str | Path,
    output_video: str | Path,
    draw_secs: float = 5.0,
    hold: float = 0.9,
    fps: int = 24,
    rows: int = 7,
) -> Path:
    from PIL import Image, ImageDraw

    final = Image.open(str(image_path)).convert("RGB")
    W, H = final.size
    white = Image.new("RGB", (W, H), (255, 255, 255))
    try:
        sketch = _line_art(final)
    except Exception:
        sketch = final

    path = _pen_path(W, H, rows)
    M = len(path)
    r = int(H / rows / 2) + 34          # brush radius so adjacent rows overlap
    lead = max(1, int(0.14 * M))        # colour trails the ink tip by this much

    smask = Image.new("L", (W, H), 0)   # where the outline has been inked
    cmask = Image.new("L", (W, H), 0)   # where colour has filled in
    sdraw, cdraw = ImageDraw.Draw(smask), ImageDraw.Draw(cmask)

    def stamp(draw, i0: int, i1: int) -> None:
        for k in range(max(0, i0), min(M, i1)):
            x, y = path[k]
            draw.ellipse([x - r, y - r, x + r, y + r], fill=255)

    out = Path(output_video)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp())
    idx = 0

    N = max(1, int(draw_secs * fps))
    sprev = cprev = 0
    for i in range(N):
        tip = int((i + 1) / N * M)
        cidx = max(0, tip - lead)
        stamp(sdraw, sprev, tip); sprev = tip
        stamp(cdraw, cprev, cidx); cprev = cidx
        layer = Image.composite(sketch, white, smask)   # ink the outline on white
        comp = Image.composite(final, layer, cmask)      # colour fills behind the tip
        _draw_pen(ImageDraw.Draw(comp), *path[min(tip, M - 1)])
        comp.save(tmp / f"f{idx:04d}.png"); idx += 1

    for _ in range(int(hold * fps)):     # hold the finished slide
        final.save(tmp / f"f{idx:04d}.png"); idx += 1

    cmd = [
        _ffmpeg(), "-y", "-framerate", str(fps), "-i", str(tmp / "f%04d.png"),
        "-pix_fmt", "yuv420p", "-vf", "scale=1280:720", "-c:v", "libx264", str(out),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    shutil.rmtree(tmp, ignore_errors=True)
    return out
