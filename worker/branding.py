"""School branding: load a teacher's uploaded .docx/.pptx templates and derive
a brand accent colour + logo from the .pptx. Everything is best-effort — any
field may come back None and the generators fall back to the default style."""

from __future__ import annotations

import logging
import re
import zipfile
from pathlib import Path
from typing import Optional

from supabase import Client

logger = logging.getLogger("worker")


def _download(sb: Client, path: str, dest: Path) -> Optional[Path]:
    try:
        dest.write_bytes(sb.storage.from_("uploads").download(path))
        return dest
    except Exception as exc:  # noqa: BLE001
        logger.warning("branding download failed (%s): %s", path, exc)
        return None


def _accent_from_pptx(pptx_path: str) -> Optional[tuple[int, int, int]]:
    """ppt/theme/theme1.xml → a:accent1 srgbClr hex → (r, g, b)."""
    try:
        with zipfile.ZipFile(pptx_path) as z:
            themes = sorted(n for n in z.namelist() if re.match(r"ppt/theme/theme\d+\.xml", n))
            if not themes:
                return None
            xml = z.read(themes[0]).decode("utf-8", "replace")
        m = re.search(r"<a:accent1>.*?<a:srgbClr val=\"([0-9A-Fa-f]{6})\"", xml, re.S)
        if not m:  # some themes use a system colour for accent1; grab the first explicit srgb
            m = re.search(r"<a:srgbClr val=\"([0-9A-Fa-f]{6})\"", xml)
        if m:
            h = m.group(1)
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except Exception as exc:  # noqa: BLE001
        logger.warning("accent extract failed: %s", exc)
    return None


def _logo_from_pptx(pptx_path: str, out_dir: Path) -> Optional[str]:
    """Best-effort: the first image embedded in the .pptx (master/layout logo)."""
    try:
        with zipfile.ZipFile(pptx_path) as z:
            media = sorted(
                n for n in z.namelist() if re.match(r"ppt/media/image\d+\.(png|jpe?g)", n, re.I)
            )
            if not media:
                return None
            logo = out_dir / f"logo{Path(media[0]).suffix.lower()}"
            logo.write_bytes(z.read(media[0]))
            return str(logo)
    except Exception as exc:  # noqa: BLE001
        logger.warning("logo extract failed: %s", exc)
    return None


def load_branding(sb: Client, owner_id: str, out_dir: str | Path) -> dict:
    """Return {docx_template, pptx_template, accent_rgb, logo_path} — any None."""
    out: dict = {"docx_template": None, "pptx_template": None, "accent_rgb": None, "logo_path": None}
    try:
        res = sb.table("branding").select("docx_path, pptx_path").eq("owner_id", owner_id).limit(1).execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("branding lookup failed: %s", exc)
        return out
    row = (res.data or [None])[0]
    if not row:
        return out

    out_dir = Path(out_dir)
    if row.get("docx_path"):
        d = _download(sb, row["docx_path"], out_dir / "template.docx")
        out["docx_template"] = str(d) if d else None
    if row.get("pptx_path"):
        p = _download(sb, row["pptx_path"], out_dir / "template.pptx")
        if p:
            out["pptx_template"] = str(p)
            out["accent_rgb"] = _accent_from_pptx(str(p))
            out["logo_path"] = _logo_from_pptx(str(p), out_dir)
    logger.info(
        "branding for %s: docx=%s pptx=%s accent=%s logo=%s",
        owner_id, bool(out["docx_template"]), bool(out["pptx_template"]),
        out["accent_rgb"], bool(out["logo_path"]),
    )
    return out
