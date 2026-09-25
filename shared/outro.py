"""The end screen every lesson video closes on (founder direction 2026-09-25).

Two kinds, chosen by where the video goes:

* ``cta`` — the full call to action on a catalogue lesson, which is
  published to YouTube: like, share, comment, subscribe, and the invitation
  to make lessons at sketchcast.app. The hand draws the four icons as the
  teacher names them, then writes the site.
* ``signoff`` — a short "Made with SketchCast AI" on a school's or
  teacher's own lesson: a plea to subscribe has no place in a classroom.
  ``params.outro = "none"`` turns it off (white-label schools).

The card is a compiled whiteboard scene in the lesson's language, spoken by
the lesson's own voice, and rendered by the same composer as every other
segment — so it joins the lesson without a re-encode and shares its avatars.
It is appended to the VIDEO only, never to the deck or the documents.
"""

from __future__ import annotations

import copy
from typing import Optional

SITE = "sketchcast.app"
SITE_URL = "https://sketchcast.app"
KIND_CTA = "cta"
KIND_SIGNOFF = "signoff"
KIND_NONE = "none"
KINDS = (KIND_CTA, KIND_SIGNOFF, KIND_NONE)
SEGMENT_ID = "s990"

# ── words (en, ms, ms-arab, ar, fr, es, pt, hi, mr, te; unknown -> en) ────
STRINGS: dict[str, dict[str, str]] = {
    "cta_1": {
        "en": "If this lesson helped, please like, share and leave a comment, and subscribe for more. "
              "We publish new lessons regularly.",
        "ms": "Jika pelajaran ini membantu, sila suka, kongsi dan tinggalkan komen, dan langgan untuk lebih "
              "banyak lagi. Kami menerbitkan pelajaran baharu secara berkala.",
        "ms-arab": "جک ڤلاجرن اين ممبنتو، سيلا سوک، کوڠسي دان تيڠݢلکن کومن، دان لڠݢن اونتوق لبيه باڽق لاݢي. "
                   "کامي منربيتکن ڤلاجرن بهارو سچارا برکالا.",
        "ar": "إذا أفادك هذا الدرس، فاضغط إعجاب وشاركه واترك تعليقًا، واشترك لمزيد من الدروس. "
              "ننشر دروسًا جديدة بانتظام.",
        "fr": "Si cette leçon vous a aidé, aimez-la, partagez-la, laissez un commentaire et abonnez-vous. "
              "Nous publions régulièrement de nouvelles leçons.",
        "es": "Si esta lección te ayudó, dale me gusta, compártela, deja un comentario y suscríbete. "
              "Publicamos nuevas lecciones con regularidad.",
        "pt": "Se esta aula ajudou, curta, compartilhe, deixe um comentário e inscreva-se. "
              "Publicamos novas aulas regularmente.",
        "hi": "अगर यह पाठ मददगार रहा, तो लाइक करें, शेयर करें, कमेंट करें और आगे के पाठों के लिए सब्सक्राइब करें। "
              "हम नियमित रूप से नए पाठ प्रकाशित करते हैं।",
        "mr": "हा पाठ उपयोगी वाटला असेल, तर लाइक करा, शेअर करा, कमेंट करा आणि पुढील पाठांसाठी सबस्क्राइब करा. "
              "आम्ही नियमितपणे नवीन पाठ प्रकाशित करतो.",
        "te": "ఈ పాఠం ఉపయోగపడితే, లైక్ చేయండి, షేర్ చేయండి, కామెంట్ చేయండి, మరిన్ని పాఠాల కోసం సబ్‌స్క్రైబ్ చేయండి. "
              "మేము కొత్త పాఠాలను క్రమం తప్పకుండా ప్రచురిస్తాము.",
    },
    "cta_2": {
        "en": f"Want lessons like this for your own class? Visit {SITE}.",
        "ms": f"Mahukan pelajaran seperti ini untuk kelas anda sendiri? Lawati {SITE}.",
        "ms-arab": f"ماهوکن ڤلاجرن سڤرتي اين اونتوق کلس اندا سنديري؟ لاواتي {SITE}.",
        "ar": f"هل تريد دروسًا كهذه لصفك؟ تفضّل بزيارة {SITE}.",
        "fr": f"Vous voulez des leçons comme celle-ci pour votre propre classe ? Rendez-vous sur {SITE}.",
        "es": f"¿Quieres lecciones como esta para tu propia clase? Visita {SITE}.",
        "pt": f"Quer aulas como esta para a sua própria turma? Visite {SITE}.",
        "hi": f"अपनी कक्षा के लिए ऐसे पाठ चाहिए? {SITE} पर जाइए।",
        "mr": f"तुमच्या वर्गासाठी असे पाठ हवे आहेत? {SITE} ला भेट द्या.",
        "te": f"మీ తరగతికి ఇలాంటి పాఠాలు కావాలా? {SITE} ను సందర్శించండి.",
    },
    "signoff": {
        "en": "Made with SketchCast AI.", "ms": "Dihasilkan dengan SketchCast AI.",
        "ms-arab": "دهاصيلکن دڠن SketchCast AI.", "ar": "صُنع بواسطة SketchCast AI.",
        "fr": "Réalisé avec SketchCast AI.", "es": "Hecho con SketchCast AI.", "pt": "Feito com SketchCast AI.",
        "hi": "SketchCast AI से बनाया गया।", "mr": "SketchCast AI ने बनवले.", "te": "SketchCast AI తో రూపొందించబడింది.",
    },
    "thanks": {
        "en": "Thank you for watching", "ms": "Terima kasih kerana menonton", "ms-arab": "تريما کاسيه کران منونتون",
        "ar": "شكرًا للمشاهدة", "fr": "Merci d'avoir regardé", "es": "Gracias por ver", "pt": "Obrigado por assistir",
        "hi": "देखने के लिए धन्यवाद", "mr": "पाहिल्याबद्दल धन्यवाद", "te": "చూసినందుకు ధన్యవాదాలు",
    },
    "like": {"en": "Like", "ms": "Suka", "ms-arab": "سوک", "ar": "إعجاب", "fr": "J'aime", "es": "Me gusta",
             "pt": "Curtir", "hi": "लाइक", "mr": "लाइक", "te": "లైక్"},
    "share": {"en": "Share", "ms": "Kongsi", "ms-arab": "کوڠسي", "ar": "مشاركة", "fr": "Partager", "es": "Compartir",
              "pt": "Compartilhar", "hi": "शेयर", "mr": "शेअर", "te": "షేర్"},
    "comment": {"en": "Comment", "ms": "Komen", "ms-arab": "کومن", "ar": "تعليق", "fr": "Commenter", "es": "Comentar",
                "pt": "Comentar", "hi": "कमेंट", "mr": "कमेंट", "te": "కామెంట్"},
    "subscribe": {"en": "Subscribe", "ms": "Langgan", "ms-arab": "لڠݢن", "ar": "اشتراك", "fr": "S'abonner",
                  "es": "Suscribirse", "pt": "Inscrever-se", "hi": "सब्सक्राइब", "mr": "सबस्क्राइब", "te": "సబ్‌స్క్రైబ్"},
    "made_with": {
        "en": "Made with SketchCast AI", "ms": "Dihasilkan dengan SketchCast AI",
        "ms-arab": "دهاصيلکن دڠن SketchCast AI", "ar": "صُنع بواسطة SketchCast AI", "fr": "Réalisé avec SketchCast AI",
        "es": "Hecho con SketchCast AI", "pt": "Feito com SketchCast AI", "hi": "SketchCast AI से बनाया गया",
        "mr": "SketchCast AI ने बनवले", "te": "SketchCast AI తో రూపొందించబడింది",
    },
}

# the YouTube description's call to action (English: the channel's language)
DESCRIPTION_CTA = ("If this lesson helped, please like, share and comment, and subscribe — "
                   "we publish new lessons regularly.")


def _lang(code: str | None) -> str:
    c = (code or "en").strip().lower()
    if c in ("ms-arab-my", "jawi"):
        return "ms-arab"
    if "-" in c and c != "ms-arab":
        c = c.split("-", 1)[0]
    return c if c in STRINGS["like"] else "en"


def text(key: str, lang: str | None) -> str:
    table = STRINGS[key]
    return table.get(_lang(lang)) or table["en"]


def outro_kind(params: object) -> str:
    """Which end screen a generation gets: an explicit ``params.outro``
    wins; a catalogue kit (published to YouTube) gets the call to action;
    everyone else the sign-off."""
    p = params if isinstance(params, dict) else {}
    explicit = str(p.get("outro") or "").strip().lower()
    if explicit in KINDS:
        return explicit
    flag = p.get("catalogue")
    if flag is True or str(flag or "").strip().lower() == "true":
        return KIND_CTA
    return KIND_SIGNOFF


# ── the board ───────────────────────────────────────────────────────────

def _icon_paths(kind: str, cx: float, cy: float, s: float = 30.0) -> list[list[list[float]]]:
    """Hand-drawable polylines for the four icons, centred on (cx, cy),
    half-size ``s``."""
    if kind == "like":       # a thumb: fist + raised thumb
        return [[[cx - s * 0.9, cy - s * 0.1], [cx - s * 0.9, cy + s], [cx + s * 0.7, cy + s], [cx + s, cy + s * 0.2],
                 [cx + s * 0.9, cy - s * 0.2], [cx + s * 0.2, cy - s * 0.2], [cx + s * 0.4, cy - s], [cx + s * 0.1, cy - s],
                 [cx - s * 0.4, cy - s * 0.1], [cx - s * 0.9, cy - s * 0.1]],
                [[cx - s * 0.4, cy - s * 0.1], [cx - s * 0.4, cy + s]]]
    if kind == "share":      # a box with an arrow leaving its top
        return [[[cx - s * 0.8, cy - s * 0.2], [cx - s * 0.8, cy + s], [cx + s * 0.8, cy + s], [cx + s * 0.8, cy - s * 0.2]],
                [[cx, cy + s * 0.4], [cx, cy - s]],
                [[cx - s * 0.5, cy - s * 0.5], [cx, cy - s], [cx + s * 0.5, cy - s * 0.5]]]
    if kind == "comment":    # a speech bubble
        return [[[cx - s, cy - s * 0.8], [cx + s, cy - s * 0.8], [cx + s, cy + s * 0.4], [cx - s * 0.2, cy + s * 0.4],
                 [cx - s * 0.7, cy + s], [cx - s * 0.6, cy + s * 0.4], [cx - s, cy + s * 0.4], [cx - s, cy - s * 0.8]],
                [[cx - s * 0.6, cy - s * 0.3], [cx + s * 0.6, cy - s * 0.3]],
                [[cx - s * 0.6, cy + s * 0.05], [cx + s * 0.2, cy + s * 0.05]]]
    # a bell
    return [[[cx - s, cy + s * 0.5], [cx - s * 0.7, cy + s * 0.1], [cx - s * 0.7, cy - s * 0.3], [cx - s * 0.3, cy - s * 0.8],
             [cx + s * 0.3, cy - s * 0.8], [cx + s * 0.7, cy - s * 0.3], [cx + s * 0.7, cy + s * 0.1], [cx + s, cy + s * 0.5],
             [cx - s, cy + s * 0.5]],
            [[cx - s * 0.25, cy + s * 0.5], [cx - s * 0.15, cy + s * 0.9], [cx + s * 0.15, cy + s * 0.9], [cx + s * 0.25, cy + s * 0.5]]]


def _cta_scene(lang: str, narration: str) -> dict:
    els: list[dict] = [{"id": "o_h", "type": "text", "text": text("thanks", lang), "role": "title", "size": 40,
                        "at": [60, 44], "anchor": "lt", "fixed": True}]
    acts: list[dict] = [{"verb": "write", "target": "o_h"}]
    # four icons across the upper left, clear of the caption band (x >= 747
    # from y 274) and the avatars (y >= 424)
    xs = (130.0, 300.0, 470.0, 640.0)
    for k, (key, cx) in enumerate(zip(("like", "share", "comment", "subscribe"), xs)):
        paths = _icon_paths(key, cx, 170.0)
        ids = []
        for j, pts in enumerate(paths):
            iid = f"o_{key}_{j}"
            els.append({"id": iid, "type": "shape", "shape": "path", "width": 3.4, "color": "accent", "points": pts})
            ids.append(iid)
        els.append({"id": f"o_g_{key}", "type": "group", "children": ids})
        els.append({"id": f"o_l_{key}", "type": "text", "text": text(key, lang), "size": 24, "at": [cx, 222],
                    "anchor": "mt", "fixed": True})
        acts.append({"verb": "draw", "target": f"o_g_{key}", "duration": 0.8, "at": {"frac": 0.08 + 0.13 * k}})
        acts.append({"verb": "write", "target": f"o_l_{key}"})
    els.append({"id": "o_site", "type": "text", "text": SITE, "role": "title", "color": "accent", "size": 52,
                "at": [60, 330], "anchor": "lt", "fixed": True})
    acts.append({"verb": "write", "target": "o_site", "at": {"frac": 0.72}})
    acts.append({"verb": "underline", "target": "o_site"})
    return {"id": "outro_cta", "compiled": True, "scene_type": "generic", "narration": narration,
            "elements": els, "actions": acts, "min_hold": 1.0}


def _signoff_scene(lang: str, narration: str) -> dict:
    els: list[dict] = [
        {"id": "o_made", "type": "text", "text": text("made_with", lang), "role": "title", "size": 40,
         "at": [60, 150], "anchor": "lt", "fixed": True},
        {"id": "o_site", "type": "text", "text": SITE, "role": "title", "color": "accent", "size": 46,
         "at": [60, 230], "anchor": "lt", "fixed": True},
    ]
    acts = [{"verb": "write", "target": "o_made"}, {"verb": "write", "target": "o_site", "at": {"frac": 0.45}},
            {"verb": "underline", "target": "o_site"}]
    return {"id": "outro_signoff", "compiled": True, "scene_type": "generic", "narration": narration,
            "elements": els, "actions": acts, "min_hold": 0.8}


def outro_segment(kind: str, language: str | None, *, segment_id: str = SEGMENT_ID) -> Optional[dict]:
    """The end screen as a script segment (the shape compose_episode_videos
    reads), or None for ``none``. Teacher-only dialogue: per-line audio,
    measured captions, the avatars on the board."""
    lang = _lang(language)
    if kind == KIND_NONE:
        return None
    if kind == KIND_CTA:
        lines = [text("cta_1", lang), text("cta_2", lang)]
        narration = " ".join(lines)
        scene = _cta_scene(lang, narration)
        hold = 1.5
    else:
        lines = [text("signoff", lang)]
        narration = lines[0]
        scene = _signoff_scene(lang, narration)
        hold = 1.5
    return {"segment_id": segment_id, "type": "preview", "text": narration, "elevenlabs_text": narration,
            "dialogue": [{"who": "teacher", "line": ln} for ln in lines],
            "slide_heading": text("thanks", lang) if kind == KIND_CTA else text("made_with", lang),
            "slide_points": [], "pause_for_question": False, "no_sketches": True, "hold_secs": hold,
            "scene": scene, "estimated_duration_seconds": 14 if kind == KIND_CTA else 4}


def with_outro(part_scripts: dict, slides: dict, kind: str, language: str | None) -> tuple[dict, dict]:
    """Copies of the script data and slide manifest with the end screen as
    the last segment — for the VIDEO only; the deck and documents keep the
    originals."""
    seg = outro_segment(kind, language)
    if seg is None:
        return part_scripts, slides
    scripts = copy.deepcopy(part_scripts)
    episodes = scripts.get("episodes") or []
    if not episodes:
        return part_scripts, slides
    segs = list(episodes[0].get("segments") or [])
    if any(str(s.get("segment_id")) == seg["segment_id"] for s in segs):
        return part_scripts, slides
    segs.append(seg)
    episodes[0]["segments"] = segs
    manifest = copy.deepcopy(slides)
    manifest["segments"] = list(manifest.get("segments") or []) + [{"segment_id": seg["segment_id"], "type": "preview"}]
    return scripts, manifest


__all__ = ["SITE", "SITE_URL", "KIND_CTA", "KIND_SIGNOFF", "KIND_NONE", "KINDS", "SEGMENT_ID", "STRINGS",
           "DESCRIPTION_CTA", "text", "outro_kind", "outro_segment", "with_outro"]
