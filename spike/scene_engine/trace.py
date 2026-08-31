"""Drawing-order trace for raster line-art.

The §26 requirement: an AI-generated illustration must APPEAR DRAWN, never
swept in by a rectangular mask. Strategy: sample the ink pixels on a coarse
grid, split them into connected components, order the components the way a
person draws (big outline first, then interior details), and within each
component walk pixel-to-nearest-pixel from the top of the stroke. The reveal
mask then grows as stamps along that walk, and the pen rides the frontier —
which reads as sketching, because it IS a plausible pen path.

numpy-only; runs once per asset at bind time and is cached with the asset.
"""

from __future__ import annotations

import numpy as np

Point = tuple[float, float]


def ink_grid(alpha: np.ndarray, target_w: int = 340) -> tuple[np.ndarray, float]:
    """Downsample an alpha channel (H,W uint8) to a boolean ink grid ~target_w
    wide. Returns (grid, scale) where full_res = grid_coord * scale."""
    h, w = alpha.shape
    scale = max(1, round(w / target_w))
    gh, gw = h // scale, w // scale
    trimmed = alpha[: gh * scale, : gw * scale]
    blocks = trimmed.reshape(gh, scale, gw, scale)
    return blocks.max(axis=(1, 3)) > 100, float(scale)


def _components(grid: np.ndarray) -> list[np.ndarray]:
    """8-connected components as arrays of (y, x), iterative BFS."""
    seen = np.zeros_like(grid, dtype=bool)
    comps: list[np.ndarray] = []
    h, w = grid.shape
    ys, xs = np.nonzero(grid)
    for y0, x0 in zip(ys.tolist(), xs.tolist()):
        if seen[y0, x0]:
            continue
        stack = [(y0, x0)]
        seen[y0, x0] = True
        acc = []
        while stack:
            y, x = stack.pop()
            acc.append((y, x))
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < h and 0 <= nx < w and grid[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
        comps.append(np.array(acc))
    return comps


def _walk(comp: np.ndarray, max_pts: int) -> list[tuple[int, int]]:
    """Greedy nearest-neighbour walk through a component, starting at its
    topmost-left pixel (where a hand naturally starts a stroke)."""
    pts = comp
    if len(pts) > max_pts:  # thin dense components down before walking
        idx = np.linspace(0, len(pts) - 1, max_pts).astype(int)
        pts = pts[np.argsort(pts[:, 0] * 10000 + pts[:, 1])][idx]
    start = int(np.argmin(pts[:, 0] * 10000 + pts[:, 1]))
    remaining = pts.astype(np.float64)
    order = [start]
    used = np.zeros(len(remaining), dtype=bool)
    used[start] = True
    cur = remaining[start]
    for _ in range(len(remaining) - 1):
        d = np.where(used, np.inf,
                     (remaining[:, 0] - cur[0]) ** 2 + (remaining[:, 1] - cur[1]) ** 2)
        nxt = int(np.argmin(d))
        used[nxt] = True
        order.append(nxt)
        cur = remaining[nxt]
    return [(int(pts[i, 0]), int(pts[i, 1])) for i in order]


def drawing_order(alpha: np.ndarray, max_points: int = 3200) -> list[Point]:
    """Ordered (x, y) points in FULL-RES asset coordinates approximating how a
    person would draw the ink: components largest-first, each walked."""
    grid, scale = ink_grid(alpha)
    comps = _components(grid)
    if not comps:
        return []
    comps.sort(key=len, reverse=True)  # outline (largest) first, details after
    budget_total = sum(len(c) for c in comps)
    out: list[Point] = []
    for comp in comps:
        share = max(24, int(max_points * len(comp) / budget_total))
        for (gy, gx) in _walk(comp, share):
            out.append((gx * scale + scale / 2, gy * scale + scale / 2))
        if len(out) >= max_points:
            break
    return out
