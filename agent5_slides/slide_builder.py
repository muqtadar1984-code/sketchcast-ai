"""Agent 5: Slide Builder — content slides (PNG for video, PPTX for download).

Each segment becomes a clean 1280x720 lesson slide in the Scholar palette
showing the TEXTBOOK CHAPTER CONTENT (a heading + key bullet points) — not the
narration. The spoken Socratic narration is what plays as audio and what goes
into the PPTX speaker notes.

Rendering uses bundled DejaVu fonts (shipped in ./fonts) so it works on any OS.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from shared.text_shaping import display_text
from .theme import CANVAS as _BG, GRAPHITE as _MUTED, INK as _INK, LINE as _RULE, TEAL as _GREEN, WHITE as _WHITE

logger = logging.getLogger(__name__)

_WIDTH = 1280
_HEIGHT = 720
# Palette names kept for back-compat: _GREEN is now the Live Ink teal accent and
# _BG the near-white canvas. Values live in theme.py (shared with the deck).

_FONT_DIR = Path(__file__).resolve().parent / "fonts"
_MARGIN_X = 90

# Script-aware font selection. DejaVu covers Latin + Arabic presentation
# forms; Devanagari (Marathi) and Telugu need the bundled Noto families AND
# HarfBuzz shaping (Pillow's RAQM layout engine — enabled at runtime when
# libraqm is present; see nixpacks.toml). Without RAQM those scripts render
# with misplaced vowel signs — we still draw (best-effort) rather than fail.
import re as _re

_DEVA_RE = _re.compile(r"[ऀ-ॿ]")
_TELU_RE = _re.compile(r"[ఀ-౿]")
try:
    from PIL import features as _pil_features

    _RAQM = bool(_pil_features.check("raqm"))
except Exception:  # noqa: BLE001
    _RAQM = False


def _font(bold: bool, size: int, sample: str = "") -> ImageFont.FreeTypeFont:
    if sample and _DEVA_RE.search(sample):
        name = "NotoSansDevanagari-Bold.ttf" if bold else "NotoSansDevanagari-Regular.ttf"
    elif sample and _TELU_RE.search(sample):
        name = "NotoSansTelugu-Bold.ttf" if bold else "NotoSansTelugu-Regular.ttf"
    else:
        name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    if _RAQM and name.startswith("Noto"):
        try:
            return ImageFont.truetype(str(_FONT_DIR / name), size, layout_engine=ImageFont.Layout.RAQM)
        except Exception:  # noqa: BLE001
            pass
    return ImageFont.truetype(str(_FONT_DIR / name), size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    """Greedy word-wrap to a pixel width.

    Wraps on LOGICAL words but measures the SHAPED string — Arabic joins
    letters (narrower than isolated forms), so measuring raw codepoints would
    wrap too early. Returned lines are logical; callers shape at draw time.
    """
    words = text.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(display_text(trial), font=font) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


# An element is an ordered list of per-line bounding boxes (x0, y0, x1, y1) to
# reveal left→right; a slide's animation is an ordered list of such elements.
_Box = tuple[int, int, int, int]


def compose_slide(
    heading: str,
    points: list[str],
    footer_text: str = "",
    context_title: str = "",
    fallback_text: str = "",
    accent: tuple[int, int, int] | None = None,
    logo_path: str | None = None,
    visual: dict | None = None,
    number: int | None = None,
    concept: str | None = None,
    direction: str = "ltr",
) -> tuple[Image.Image, list[list[_Box]], list[_Box]]:
    """Render the canonical 1280x720 Live Ink lesson slide + its object layout.

    Returns ``(image, anim_elements, static_boxes)``:

    * ``image`` is the fully-drawn slide (what the Agent 6 native renderer
      animates toward — pixel-identical).
    * ``anim_elements`` is the content in *teaching order* (badge → title →
      bullets / diagram); each element is a list of per-line boxes so the
      renderer can write it on left→right.
    * ``static_boxes`` are regions shown from the first frame — the concept
      illustration (introduced at slide entry, NOT interleaved with the bullet
      reveal), the logo, and any footer.

    ``accent`` overrides the teal accent; ``logo_path`` stamps a logo top-right;
    ``number`` draws the teal number badge (the repeated motif); ``concept`` is
    a keyword/icon name for the right-column illustration.
    """
    acc = accent or _GREEN  # teal
    rtl = direction == "rtl"
    canvas = Image.new("RGB", (_WIDTH, _HEIGHT), _BG)
    draw = ImageDraw.Draw(canvas)
    M = _MARGIN_X
    anim: list[list[_Box]] = []
    static: list[_Box] = []

    # Shape once at draw time (Arabic joins + bidi); no-op for other scripts.
    disp = lambda s: display_text(s, rtl_base=rtl)  # noqa: E731

    # Optional school logo — top-right for LTR, top-LEFT mirrored for RTL.
    if logo_path:
        try:
            logo = Image.open(logo_path).convert("RGBA")
            lh = 52
            lw = max(1, int(logo.width * lh / max(1, logo.height)))
            logo = logo.resize((lw, lh))
            lx, ly = (M, 34) if rtl else (_WIDTH - M - lw, 34)
            canvas.paste(logo, (lx, ly), logo)
            static.append((lx, ly, lx + lw, ly + lh))
        except Exception:  # noqa: BLE001
            pass

    has_visual = bool(visual)
    show_illus = bool(concept) and not has_visual
    # Illustration column: right for LTR, LEFT for RTL (whole layout mirrors).
    content_right = 800 if (show_illus and not rtl) else _WIDTH - M
    content_left = (M + 480) if (show_illus and rtl) else M

    # Number badge (teal circle) — the repeated motif and first reveal element.
    title_x = content_left
    title_right = content_right
    if number is not None:
        br = 27
        bcx, bcy = (_WIDTH - M - br, 92) if rtl else (M + br, 92)
        draw.ellipse([bcx - br, bcy - br, bcx + br, bcy + br], fill=acc)
        nf = _font(bold=True, size=22)
        ns = str(number)
        nw = draw.textlength(ns, font=nf)
        draw.text((bcx - nw / 2, bcy - 15), ns, fill=_WHITE, font=nf)
        anim.append([(bcx - br, bcy - br, bcx + br, bcy + br)])
        if rtl:
            title_right = bcx - br - 22
        else:
            title_x = bcx + br + 22

    # Title (ink), beside the badge — right-anchored for RTL.
    head = (heading or context_title or "SketchCast AI").strip()[:90]
    hf = _font(bold=True, size=38, sample=head)
    ty = 66
    hbox: list[_Box] = []
    for ln in _wrap(draw, head, hf, title_right - title_x)[:2]:
        s = disp(ln)
        lw_px = int(draw.textlength(s, font=hf))
        lx = (title_right - lw_px) if rtl else title_x
        draw.text((lx, ty), s, fill=_INK, font=hf)
        hbox.append((lx, ty, lx + lw_px, ty + 46))
        ty += 50
    if hbox:
        anim.append(hbox)

    # Concept illustration (static — shown at slide entry, opposite the text).
    if show_illus:
        from .theme import draw_concept

        disc = 300
        icx = (M + disc // 2) if rtl else (_WIDTH - M - disc // 2)
        icy = 422
        static.append(draw_concept(canvas, icx, icy, disc, concept, accent=acc))

    y = max(ty + 30, 212)

    # A composable diagram claims the content area when present; it falls back to
    # plain bullets if the spec is unusable, so slides are never bare.
    diagram_elems: list[list[_Box]] = []
    if has_visual:
        from .diagram_builder import caption_element, render_diagram, set_direction

        set_direction(rtl)  # bidi base for mixed runs inside diagram labels
        region = (content_left, y, content_right, _HEIGHT - 70)
        diagram_elems = render_diagram(draw, region, visual, accent=acc)
        if diagram_elems:
            anim.extend(diagram_elems)
            cap = caption_element(draw, region, visual.get("caption", "") if isinstance(visual, dict) else "")
            if cap:
                anim.append(cap)

    if not has_visual or not diagram_elems:
        pts = [p for p in (points or []) if p.strip()]
        text_w = content_right - content_left
        if pts:
            # Bullet points (chapter content) — teal dots + ink text. RTL puts
            # the dot on the RIGHT edge with text right-anchored beside it.
            size = 30
            _pts_sample = " ".join(pts)
            while size >= 20:
                bf = _font(bold=False, size=size, sample=_pts_sample)
                line_h = int(size * 1.5)
                wrapped: list[list[str]] = [_wrap(draw, p, bf, text_w - 40) for p in pts]
                total_h = sum(len(w) * line_h + 16 for w in wrapped)
                if total_h <= (_HEIGHT - y - 60) or size == 20:
                    break
                size -= 2
            dot_r = 7
            for wlines in wrapped:
                ebox: list[_Box] = []
                if rtl:
                    dot_x0 = content_right - 2 * dot_r
                    draw.ellipse([(dot_x0, y + size // 2 - dot_r + 2), (content_right, y + size // 2 + dot_r + 2)], fill=acc)
                else:
                    draw.ellipse([(content_left, y + size // 2 - dot_r + 2), (content_left + 2 * dot_r, y + size // 2 + dot_r + 2)], fill=acc)
                for j, ln in enumerate(wlines):
                    s = disp(ln)
                    lw_px = int(draw.textlength(s, font=bf))
                    if rtl:
                        tx = content_right - 30 - lw_px
                        x1 = content_right if j == 0 else content_right - 30
                        ebox.append((tx, y, x1, y + size))
                    else:
                        tx = content_left + 30
                        x0 = content_left if j == 0 else content_left + 30
                        ebox.append((x0, y, tx + lw_px, y + size))
                    draw.text((tx, y), s, fill=_INK, font=bf)
                    y += line_h
                if ebox:
                    anim.append(ebox)
                y += 16
        elif not has_visual:
            # Fallback: no bullets — show the narration text so the slide isn't bare
            bf = _font(bold=False, size=28, sample=fallback_text or "")
            fb: list[_Box] = []
            for ln in _wrap(draw, " ".join((fallback_text or "").split()), bf, text_w)[:8]:
                s = disp(ln)
                lw_px = int(draw.textlength(s, font=bf))
                lx = (content_right - lw_px) if rtl else content_left
                draw.text((lx, y), s, fill=_MUTED, font=bf)
                fb.append((lx, y, lx + lw_px, y + 28))
                y += int(28 * 1.45)
            if fb:
                anim.append(fb)

    # Footer is a dev label only — the video composer passes "" in production
    # (gated behind DEBUG_VIDEO), so it never appears in shipped frames.
    if footer_text:
        ff = _font(bold=False, size=16)
        fw = int(draw.textlength(footer_text, font=ff))
        fx = _WIDTH - M - fw
        draw.text((fx, _HEIGHT - 46), footer_text, fill=_MUTED, font=ff)
        static.append((fx, _HEIGHT - 46, _WIDTH - M, _HEIGHT - 46 + 20))

    return canvas, anim, static


def export_slide_png(
    heading: str,
    points: list[str],
    output_png_path: str | Path,
    footer_text: str = "",
    context_title: str = "",
    fallback_text: str = "",
    accent: tuple[int, int, int] | None = None,
    logo_path: str | None = None,
    visual: dict | None = None,
    direction: str = "ltr",
) -> Path:
    """Render a 1280x720 lesson slide PNG: heading + chapter bullets or a diagram.

    Thin wrapper over :func:`compose_slide` (the single source of slide layout,
    shared with the Agent 6 native video renderer) that just saves the image.
    """
    output_png_path = Path(output_png_path)
    output_png_path.parent.mkdir(parents=True, exist_ok=True)

    canvas, _anim, _static = compose_slide(
        heading=heading,
        points=points,
        footer_text=footer_text,
        context_title=context_title,
        fallback_text=fallback_text,
        accent=accent,
        logo_path=logo_path,
        visual=visual,
        direction=direction,
    )
    canvas.save(str(output_png_path), "PNG")
    logger.info("Slide PNG: %s (%d points)", output_png_path.name, len([p for p in (points or []) if p.strip()]))
    return output_png_path


# "Live Ink" deck palette — the same single source as the video (theme.py), so
# deck and video can't drift: one dominant ink, white content, one teal accent.
from .theme import (  # noqa: E402
    FAINT as _LI_DIM,
    GRAPHITE as _LI_GRAPHITE,
    INK as _LI_INK,
    MIST as _LI_MIST,
    TEAL as _LI_TEAL,
    TEAL_DK as _LI_TEAL_DK,
    WHITE as _LI_WHITE,
)


def build_episode_deck(
    slides: list[dict],
    output_pptx_path: str | Path,
    episode_title: str = "",
    template: str | None = None,
    accent: tuple[int, int, int] | None = None,
    direction: str = "ltr",
) -> Path:
    """Build one editable PPTX deck: a designed lesson deck, narration in notes.

    ``slides`` is a list of {"heading", "points", "narration"} dicts. Without a
    ``template`` it builds the designed "Live Ink" deck (dark title/closing,
    light content, icon-in-circle motif, alternating layouts). When a school
    ``template`` (.pptx) is given the deck inherits that theme instead; ``accent``
    overrides the brand colour in both.
    """
    output_pptx_path = Path(output_pptx_path)
    output_pptx_path.parent.mkdir(parents=True, exist_ok=True)
    if template:
        path = _build_branded_deck(slides, output_pptx_path, episode_title, template, accent)
    else:
        path = _build_designed_deck(slides, output_pptx_path, episode_title, accent)
    # RTL (Arabic) lessons: PowerPoint does the SHAPING itself — the deck only
    # needs paragraph-level right alignment + the rtl flag. Applied as a post-
    # pass over the saved file so both deck builders stay untouched.
    if direction == "rtl":
        try:
            _mirror_deck_rtl(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("RTL deck pass failed for %s: %s", path, exc)
    return path


def _mirror_deck_rtl(pptx_path: Path) -> None:
    from pptx import Presentation
    from pptx.enum.text import PP_ALIGN

    prs = Presentation(str(pptx_path))
    for slide in prs.slides:
        for shape in slide.shapes:
            if not getattr(shape, "has_text_frame", False):
                continue
            for p in shape.text_frame.paragraphs:
                # Centred paragraphs (number badges, title slides) stay centred.
                if p.alignment != PP_ALIGN.CENTER:
                    p.alignment = PP_ALIGN.RIGHT
                # python-pptx has no first-class rtl API — set the OOXML attr.
                p._p.get_or_add_pPr().set("rtl", "1")  # noqa: SLF001
    prs.save(str(pptx_path))


def _build_branded_deck(slides, output_pptx_path, episode_title, template, accent):
    """School-template path — inherit the uploaded theme (unchanged behaviour)."""
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.util import Emu, Pt

    prs = Presentation(template)
    layout = prs.slide_layouts[6] if len(prs.slide_layouts) > 6 else prs.slide_layouts[-1]
    head_rgb = RGBColor(*(accent or _GREEN))
    for s in slides:
        slide = prs.slides.add_slide(layout)
        box = slide.shapes.add_textbox(Emu(700000), Emu(900000), Emu(10800000), Emu(5000000))
        tf = box.text_frame
        tf.word_wrap = True
        head = tf.paragraphs[0]
        head.text = (s.get("heading") or episode_title or "SketchCast AI")[:90]
        head.font.size = Pt(30)
        head.font.bold = True
        head.font.color.rgb = head_rgb
        for pt in s.get("points") or []:
            para = tf.add_paragraph()
            para.text = f"•  {pt}"
            para.font.size = Pt(20)
        slide.notes_slide.notes_text_frame.text = s.get("narration") or ""
    prs.save(str(output_pptx_path))
    logger.info("Episode deck (branded) saved: %s (%d content slides)", output_pptx_path.name, len(slides))
    return output_pptx_path


def _build_designed_deck(slides, output_pptx_path, episode_title, accent):
    """Default path — a designed "Live Ink" deck built deterministically."""
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
    from pptx.util import Emu, Pt

    IN = 914400
    INK, WHITE = RGBColor(*_LI_INK), RGBColor(*_LI_WHITE)
    ACC, ACC_DK = RGBColor(*(accent or _LI_TEAL)), RGBColor(*_LI_TEAL_DK)
    GRA, MIST, DIM = RGBColor(*_LI_GRAPHITE), RGBColor(*_LI_MIST), RGBColor(*_LI_DIM)
    BODY = "Calibri"

    prs = Presentation()
    prs.slide_width = Emu(12192000)
    prs.slide_height = Emu(6858000)
    blank = prs.slide_layouts[6] if len(prs.slide_layouts) > 6 else prs.slide_layouts[-1]

    def new_slide(bg):
        s = prs.slides.add_slide(blank)
        f = s.background.fill
        f.solid()
        f.fore_color.rgb = bg
        return s

    def textbox(s, l, t, w, h, anchor=MSO_ANCHOR.TOP):
        tf = s.shapes.add_textbox(int(l), int(t), int(w), int(h)).text_frame
        tf.word_wrap = True
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
        tf.vertical_anchor = anchor
        return tf

    def para(tf, text, size, color, bold=False, first=False, space=6):
        p = tf.paragraphs[0] if first else tf.add_paragraph()
        p.alignment = PP_ALIGN.LEFT
        p.space_before = Pt(0)
        p.space_after = Pt(space)
        r = p.add_run()
        r.text = text
        r.font.size = Pt(size)
        r.font.bold = bold
        r.font.name = BODY
        r.font.color.rgb = color
        return p

    def circle(s, cx, cy, d, fill, label="", label_size=15):
        o = s.shapes.add_shape(MSO_SHAPE.OVAL, int(cx - d / 2), int(cy - d / 2), int(d), int(d))
        o.fill.solid()
        o.fill.fore_color.rgb = fill
        o.line.fill.background()
        o.shadow.inherit = False
        if label:
            tf = o.text_frame
            tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
            tf.vertical_anchor = MSO_ANCHOR.MIDDLE
            p = tf.paragraphs[0]
            p.alignment = PP_ALIGN.CENTER
            r = p.add_run()
            r.text = label
            r.font.size = Pt(label_size)
            r.font.bold = True
            r.font.name = BODY
            r.font.color.rgb = WHITE
        return o

    def panel(s, l, t, w, h, fill):
        sh = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, int(l), int(t), int(w), int(h))
        sh.fill.solid()
        sh.fill.fore_color.rgb = fill
        sh.line.fill.background()
        sh.shadow.inherit = False
        return sh

    def heading_block(s, n, heading):
        circle(s, 0.95 * IN, 1.02 * IN, 0.52 * IN, ACC, str(n))
        tf = textbox(s, 1.45 * IN, 0.7 * IN, 11.1 * IN, 0.95 * IN, anchor=MSO_ANCHOR.MIDDLE)
        para(tf, (heading or episode_title or "Lesson")[:90], 27, INK, bold=True, first=True, space=0)

    def bullet_rows(s, points, left, top, width, size=16, step=0.66):
        y = top
        for pt in points:
            circle(s, left + 0.09 * IN, y + 0.13 * IN, 0.17 * IN, ACC)
            tf = textbox(s, left + 0.36 * IN, y - 0.03 * IN, width - 0.36 * IN, step * IN)
            para(tf, str(pt), size, INK, first=True, space=0)
            y += step * IN

    # Concept illustrations: rendered once per concept to a temp PNG, then
    # embedded as a discrete picture shape (the SAME drawing the video composites,
    # via theme.draw_concept) so deck and video show identical imagery.
    import shutil
    import tempfile

    from .theme import concepts_for_slides, render_concept_png

    img_dir = Path(tempfile.mkdtemp(prefix="deck_imgs_"))
    _img_cache: dict[str, str] = {}

    def concept_png(name):
        if name not in _img_cache:
            p = img_dir / f"{name}.png"
            try:
                render_concept_png(name, p, size=560, accent=(accent or _LI_TEAL))
                _img_cache[name] = str(p)
            except Exception:  # noqa: BLE001
                _img_cache[name] = ""
        return _img_cache[name]

    def add_illus(s, name, left, top, size):
        path = concept_png(name)
        if path:
            s.shapes.add_picture(path, int(left), int(top), int(size), int(size))

    # Title (dark)
    s = new_slide(INK)
    circle(s, 1.0 * IN, 0.95 * IN, 0.62 * IN, ACC, "S", label_size=20)
    tf = textbox(s, 1.0 * IN, 2.45 * IN, 11.3 * IN, 2.3 * IN)
    para(tf, episode_title or "SketchCast Lesson", 40, WHITE, bold=True, first=True, space=8)
    para(tf, "A narrated lesson", 18, ACC, space=0)
    para(textbox(s, 1.0 * IN, 6.7 * IN, 11.3 * IN, 0.5 * IN), "SketchCast AI", 12, DIM, first=True, space=0)

    # Content (light) — every slide carries a concept illustration; the text and
    # the illustration swap columns slide-to-slide so no two are laid out alike.
    DISC = 2.7 * IN
    ILL_T = 2.95 * IN
    # One art-direction pass over the whole chapter → a coherent, non-repeating set.
    slide_concepts = concepts_for_slides([(sd.get("heading") or "").strip() or episode_title for sd in slides])
    for i, sd in enumerate(slides, 1):
        heading = (sd.get("heading") or "").strip()
        points = [str(p).strip() for p in (sd.get("points") or []) if str(p).strip()][:5]
        concept = slide_concepts[i - 1]
        s = new_slide(WHITE)
        heading_block(s, i, heading)
        if not points:
            add_illus(s, concept, 9.15 * IN, ILL_T, DISC)
            para(textbox(s, 0.95 * IN, 2.6 * IN, 7.6 * IN, 2.2 * IN, anchor=MSO_ANCHOR.MIDDLE),
                 "Pause here — discuss this together before moving on.", 18, GRA, first=True, space=0)
        elif i % 2 == 1:
            bullet_rows(s, points, 0.95 * IN, 2.4 * IN, 7.6 * IN, step=0.7)
            add_illus(s, concept, 9.15 * IN, ILL_T, DISC)
        else:
            add_illus(s, concept, 0.95 * IN, ILL_T, DISC)
            bullet_rows(s, points, 4.3 * IN, 2.4 * IN, 7.9 * IN, step=0.7)
        s.notes_slide.notes_text_frame.text = sd.get("narration") or ""

    # Closing (dark)
    s = new_slide(INK)
    circle(s, 1.0 * IN, 0.95 * IN, 0.62 * IN, ACC)
    tf = textbox(s, 1.0 * IN, 2.5 * IN, 11.3 * IN, 2.4 * IN)
    para(tf, "Ready to teach.", 34, WHITE, bold=True, first=True, space=8)
    para(tf, episode_title or "", 18, ACC, space=12)
    para(tf, "Every lesson slide's speaker notes carry the narration.", 14, DIM, space=0)
    para(textbox(s, 1.0 * IN, 6.7 * IN, 11.3 * IN, 0.5 * IN), "SketchCast AI", 12, DIM, first=True, space=0)

    prs.save(str(output_pptx_path))
    shutil.rmtree(img_dir, ignore_errors=True)  # images are embedded in the .pptx by now
    logger.info("Episode deck (designed) saved: %s (%d content slides)", output_pptx_path.name, len(slides))
    return output_pptx_path
