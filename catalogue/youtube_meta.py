"""The YouTube title, description and tags of a catalogue video — ONE
structure for every video, written once and reviewed in the library.

Founder, 2026-09-21, after hand-writing the metadata for three videos: "ensure
the title and description structure we have just generated is applied to
every video we generate, that way I don't have to revise the title and
description every time I post". The structure that came out of those three:

    TITLE        <Topic> Explained | <two or three key terms> | <audience>
                 e.g. Plant vs Animal Cells Explained | Prokaryotes, Eukaryotes,
                 Organelles | CBSE Class 9 & Cambridge Stage 7 Science
    DESCRIPTION  a short hook paragraph (what the lesson covers, that a teacher
                 and a student work through it);
                 "Aligned to" + the curriculum lines every document carries;
                 "Chapters" + the measured timestamps;
                 "Key terms:" the vocabulary the lesson uses;
                 the part pointer for a multi-part kit;
                 the SketchCast line and the UTM-tagged link;
                 a hashtag line.

TWO HALVES, ON PURPOSE.
  * The ASSEMBLY is pure and deterministic (compose_title / compose_description
    below), mirrored line for line by the portal's preview
    (sketchcast-app src/utils/catalogue/publish.ts), so what the reviewer
    reads in the library is what goes up — the same stance as the curriculum
    header (catalogue.kit.header_lines).
  * The WORDS that need writing — the hook paragraph, the pick of key terms,
    the hashtags, and the title's middle — are proposed by ONE model call
    when the video finishes (generate_meta, from the narration itself, so it
    never claims something the lesson does not say), stored on
    ``topic_kits.youtube_meta`` (app migration 0121), and EDITABLE in the
    library's publish block before anyone clicks Post. The publish reads the
    stored words; a kit with none (an old kit, a model outage) falls back to
    the deterministic defaults, so a video is never held up by a paragraph.

``youtube_meta`` shape, both sides: ``{title, intro, key_terms[], hashtags[],
source: 'generated'|'edited', generated_at?, edited_at?, edited_by?}``. Every
field optional; the composers treat a missing one as "use the default".
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Iterable, Optional

from catalogue.harvest import clean_heading

log = logging.getLogger("worker.youtube_meta")

TITLE_MAX = 100          # YouTube's own limit; videos.insert rejects more
TITLE_MODEL_MAX = 90     # what the model is asked for, leaving room for a part label
DESCRIPTION_MAX = 5000
INTRO_MAX = 700
MAX_KEY_TERMS = 12
MAX_HASHTAGS = 12
TITLE_TERMS = 3          # key terms in the title's middle block
MIN_CHAPTERS = 3         # YouTube ignores a chapter list shorter than this or not at 0:00
NARRATION_EXCERPT_CHARS = 6000

# The link line under the SketchCast paragraph: "Full kit: <the lesson's own
# public page>" (catalogue plan §5.6). ONE prefix, so the enrich pass can find
# and replace the line on every video already posted.
KIT_LINK_PREFIX = "Full kit: "


def kit_link_line(link: object, language: object = "en") -> str:
    return f"{kit_link_prefix(language)}{str(link or '').strip()}"


SKETCHCAST_LINE = ("This lesson was generated with SketchCast. Upload a textbook chapter and get a "
                   "whiteboard video, slide deck, lesson plan, activities, worksheet, test paper and "
                   "case study in one click.")

# ── the FIXED words of the structure, per lesson language ────────────────
# Founder, 2026-10-17, after the first Arabic post went up with an Arabic
# title over an English description: "ensure the same approach applies for
# title and description for all translated videos, not just Arabic". The
# STRUCTURE is one for every video; the words that are not the model's — the
# block labels, the ask, the SketchCast line, the link prefix, the part
# pointer — read in the video's language. English stays the fallback for a
# language without a row (and the single source: shared.outro.DESCRIPTION_CTA
# and SKETCHCAST_LINE above are the English row). The audience tag (the
# boards: "CBSE Class 9 & Cambridge Stage 7 Science") is deliberately NOT
# translated: it is a proper name a teacher types in Latin letters.
_EN_WORDS = {
    "explained": "{topic} Explained",
    "aligned": "Aligned to",
    "chapters": "Chapters",
    "key_terms": "Key terms: ",
    "part": "Part {part} of {total}.",
    "next": "Next: ",
    "full_kit": KIT_LINK_PREFIX,
    "sketchcast": SKETCHCAST_LINE,
    "fallback_intro": "{topic} — a SketchCast lesson.",
    "more_lessons": "More {discipline} lessons: ",
}
WORDS: dict[str, dict[str, str]] = {
    "en": _EN_WORDS,
    "ar": {
        "explained": "شرح {topic}",
        "aligned": "متوافق مع",
        "chapters": "الفصول",
        "key_terms": "المصطلحات الأساسية: ",
        "part": "الجزء {part} من {total}.",
        "next": "التالي: ",
        "full_kit": "الحقيبة الكاملة: ",
        "sketchcast": ("أُنتج هذا الدرس باستخدام سكتش كاست. ارفع فصلًا من كتاب مدرسي واحصل على فيديو سبورة بيضاء "
                       "وعرض تقديمي وخطة درس وأنشطة وورقة عمل وورقة اختبار ودراسة حالة بنقرة واحدة."),
        "cta": "إذا أفادك هذا الدرس، فاضغط إعجابًا وشاركه وعلّق عليه واشترك في القناة — ننشر دروسًا جديدة بانتظام.",
        "fallback_intro": "{topic} — درس من سكتش كاست.",
        "more_lessons": "المزيد من دروس {discipline}: ",
    },
    "ms": {
        "explained": "{topic} Dijelaskan",
        "aligned": "Selaras dengan",
        "chapters": "Bab",
        "key_terms": "Istilah utama: ",
        "part": "Bahagian {part} daripada {total}.",
        "next": "Seterusnya: ",
        "full_kit": "Kit penuh: ",
        "sketchcast": ("Pelajaran ini dijana dengan SketchCast. Muat naik satu bab buku teks dan dapatkan video papan "
                       "putih, slaid, rancangan pengajaran, aktiviti, lembaran kerja, kertas ujian dan kajian kes "
                       "dengan satu klik."),
        "cta": "Jika pelajaran ini membantu, sila like, kongsi dan komen, dan langgan — kami menerbitkan pelajaran baharu secara berkala.",
        "fallback_intro": "{topic} — pelajaran SketchCast.",
        "more_lessons": "Lagi pelajaran {discipline}: ",
    },
    "hi": {
        "explained": "{topic} की व्याख्या",
        "aligned": "इनके अनुरूप",
        "chapters": "अध्याय",
        "key_terms": "मुख्य शब्द: ",
        "part": "भाग {part} / {total}।",
        "next": "अगला: ",
        "full_kit": "पूरी किट: ",
        "sketchcast": ("यह पाठ SketchCast से बनाया गया है। पाठ्यपुस्तक का एक अध्याय अपलोड करें और एक क्लिक में व्हाइटबोर्ड वीडियो, "
                       "स्लाइड डेक, पाठ योजना, गतिविधियाँ, वर्कशीट, परीक्षा पत्र और केस स्टडी पाएँ।"),
        "cta": "अगर यह पाठ उपयोगी लगा, तो कृपया लाइक, शेयर और कमेंट करें, और सब्सक्राइब करें — हम नियमित रूप से नए पाठ प्रकाशित करते हैं।",
        "fallback_intro": "{topic} — एक SketchCast पाठ।",
        "more_lessons": "{discipline} के और पाठ: ",
    },
    "te": {
        "explained": "{topic} వివరణ",
        "aligned": "దీనికి అనుగుణంగా",
        "chapters": "అధ్యాయాలు",
        "key_terms": "ముఖ్య పదాలు: ",
        "part": "భాగం {part} / {total}.",
        "next": "తదుపరి: ",
        "full_kit": "పూర్తి కిట్: ",
        "sketchcast": ("ఈ పాఠం SketchCast తో రూపొందించబడింది. పాఠ్యపుస్తకంలోని ఒక అధ్యాయాన్ని అప్‌లోడ్ చేసి, ఒకే క్లిక్‌లో వైట్‌బోర్డ్ "
                       "వీడియో, స్లయిడ్‌లు, పాఠ ప్రణాళిక, కార్యకలాపాలు, వర్క్‌షీట్, పరీక్షా పత్రం మరియు కేస్ స్టడీ పొందండి."),
        "cta": "ఈ పాఠం ఉపయోగపడితే, దయచేసి లైక్ చేయండి, షేర్ చేయండి, కామెంట్ చేయండి మరియు సబ్‌స్క్రైబ్ చేయండి — మేము క్రమం తప్పకుండా కొత్త పాఠాలను ప్రచురిస్తాము.",
        "fallback_intro": "{topic} — ఒక SketchCast పాఠం.",
        "more_lessons": "మరిన్ని {discipline} పాఠాలు: ",
    },
    "mr": {
        "explained": "{topic} स्पष्टीकरण",
        "aligned": "यांच्याशी सुसंगत",
        "chapters": "प्रकरणे",
        "key_terms": "मुख्य संज्ञा: ",
        "part": "भाग {part} / {total}.",
        "next": "पुढील: ",
        "full_kit": "संपूर्ण किट: ",
        "sketchcast": ("हा धडा SketchCast ने तयार केला आहे. पाठ्यपुस्तकातील एक प्रकरण अपलोड करा आणि एका क्लिकमध्ये व्हाइटबोर्ड "
                       "व्हिडिओ, स्लाइड्स, पाठ योजना, उपक्रम, वर्कशीट, परीक्षा पेपर आणि केस स्टडी मिळवा."),
        "cta": "हा धडा उपयुक्त वाटला असल्यास, कृपया लाइक, शेअर आणि कमेंट करा, आणि सबस्क्राइब करा — आम्ही नियमितपणे नवीन धडे प्रकाशित करतो.",
        "fallback_intro": "{topic} — एक SketchCast धडा.",
        "more_lessons": "{discipline} चे आणखी धडे: ",
    },
    "ur": {
        "explained": "{topic} کی وضاحت",
        "aligned": "ان کے مطابق",
        "chapters": "ابواب",
        "key_terms": "کلیدی اصطلاحات: ",
        "part": "حصہ {part} از {total}۔",
        "next": "اگلا: ",
        "full_kit": "مکمل کٹ: ",
        "sketchcast": ("یہ سبق SketchCast سے تیار کیا گیا ہے۔ نصابی کتاب کا ایک باب اپ لوڈ کریں اور ایک کلک میں وائٹ بورڈ ویڈیو، "
                       "سلائیڈز، سبق کا منصوبہ، سرگرمیاں، ورک شیٹ، امتحانی پرچہ اور کیس اسٹڈی حاصل کریں۔"),
        "cta": "اگر یہ سبق مفید رہا تو براہِ کرم لائک، شیئر اور کمنٹ کریں، اور سبسکرائب کریں — ہم باقاعدگی سے نئے اسباق شائع کرتے ہیں۔",
        "fallback_intro": "{topic} — ایک SketchCast سبق۔",
        "more_lessons": "{discipline} کے مزید اسباق: ",
    },
    "fr": {
        "explained": "{topic} expliqué",
        "aligned": "Aligné sur",
        "chapters": "Chapitres",
        "key_terms": "Termes clés : ",
        "part": "Partie {part} sur {total}.",
        "next": "Suite : ",
        "full_kit": "Kit complet : ",
        "sketchcast": ("Cette leçon a été générée avec SketchCast. Téléversez un chapitre de manuel et obtenez en un clic "
                       "une vidéo tableau blanc, un diaporama, un plan de leçon, des activités, une fiche d'exercices, "
                       "un sujet d'examen et une étude de cas."),
        "cta": "Si cette leçon vous a aidé, aimez, partagez, commentez et abonnez-vous — nous publions régulièrement de nouvelles leçons.",
        "fallback_intro": "{topic} — une leçon SketchCast.",
        "more_lessons": "Plus de leçons de {discipline} : ",
    },
    "es": {
        "explained": "{topic} explicado",
        "aligned": "Alineado con",
        "chapters": "Capítulos",
        "key_terms": "Términos clave: ",
        "part": "Parte {part} de {total}.",
        "next": "Siguiente: ",
        "full_kit": "Kit completo: ",
        "sketchcast": ("Esta lección se generó con SketchCast. Sube un capítulo de un libro de texto y obtén con un clic "
                       "un vídeo de pizarra, una presentación, un plan de clase, actividades, una hoja de ejercicios, "
                       "un examen y un estudio de caso."),
        "cta": "Si esta lección te ayudó, dale me gusta, compártela, comenta y suscríbete — publicamos lecciones nuevas con regularidad.",
        "fallback_intro": "{topic} — una lección de SketchCast.",
        "more_lessons": "Más lecciones de {discipline}: ",
    },
    "pt": {
        "explained": "{topic} explicado",
        "aligned": "Alinhado com",
        "chapters": "Capítulos",
        "key_terms": "Termos-chave: ",
        "part": "Parte {part} de {total}.",
        "next": "A seguir: ",
        "full_kit": "Kit completo: ",
        "sketchcast": ("Esta aula foi gerada com o SketchCast. Envie um capítulo de livro didático e receba, com um clique, "
                       "vídeo de quadro branco, slides, plano de aula, atividades, folha de exercícios, prova e estudo de caso."),
        "cta": "Se esta aula ajudou, curta, compartilhe, comente e inscreva-se — publicamos novas aulas regularmente.",
        "fallback_intro": "{topic} — uma aula SketchCast.",
        "more_lessons": "Mais aulas de {discipline}: ",
    },
}


def _lang_key(language: object) -> str:
    return str(language or "en").strip().lower() or "en"


def words_for(language: object) -> dict[str, str]:
    """The fixed words in ``language`` — every key present, English filling
    any a language's row leaves out."""
    lang = _lang_key(language)
    out = dict(_EN_WORDS)
    out.update(WORDS.get(lang) or {})
    if "cta" not in out:
        from shared.outro import DESCRIPTION_CTA
        out["cta"] = DESCRIPTION_CTA
    return out


def cta_line(language: object = "en") -> str:
    return words_for(language)["cta"]


def sketchcast_line(language: object = "en") -> str:
    return words_for(language)["sketchcast"]


def kit_link_prefix(language: object = "en") -> str:
    return words_for(language)["full_kit"]


def is_sketchcast_line(paragraph: object) -> bool:
    """Whether ``paragraph`` opens with the SketchCast line of ANY language —
    the anchor the enrich and backfill passes insert above."""
    p = str(paragraph or "").lstrip()
    return any(p.startswith(w["sketchcast"]) for w in WORDS.values())


def is_kit_link_line(line: object) -> bool:
    s = str(line or "").strip()
    return any(s.startswith(w["full_kit"]) for w in WORDS.values())


def _s(value: object) -> str:
    return " ".join(str(value or "").split())


# ── the audience tag ─────────────────────────────────────────────────────

_CLASS_BOARDS = re.compile(r"\b(cbse|icse|ncert|class)\b", re.IGNORECASE)
_STAGE_BOARDS = re.compile(r"\b(cambridge|stage)\b", re.IGNORECASE)


def board_label(curriculum_name: object, grade: object) -> str:
    """``"CBSE Class 9"`` / ``"Cambridge Stage 7"`` / ``"Ontario Grade 8"`` from
    a curriculum's name and a node's grade — the board is the name's first
    word, the level word is the board's own idiom, the number the grade's.
    A curriculum without a numeric grade is its board alone."""
    name = clean_heading(curriculum_name)
    if not name:
        return ""
    board = name.split()[0]
    m = re.search(r"\d+", _s(grade))
    if not m:
        return board
    level = "Class" if _CLASS_BOARDS.search(name) else "Stage" if _STAGE_BOARDS.search(name) else "Grade"
    return f"{board} {level} {m.group(0)}"


def boards_of(mappings: Iterable) -> list[tuple[str, str]]:
    """``[(curriculum name, grade)]`` from catalogue.article.Mapping objects
    (or dicts shaped the same), one per curriculum, in mapping order."""
    seen: dict[str, tuple[str, str]] = {}
    for m in mappings or []:
        cur = getattr(m, "curriculum", None) if not isinstance(m, dict) else m.get("curriculum")
        node = getattr(m, "node", None) if not isinstance(m, dict) else m.get("node")
        name = clean_heading((cur or {}).get("name"))
        if not name or name in seen:
            continue
        seen[name] = (name, _s((node or {}).get("grade")))
    return list(seen.values())


def audience_tag(boards: Iterable[tuple[str, str]], subject: object) -> str:
    """``"CBSE Class 9 & Cambridge Stage 7 Science"`` — the title's last block,
    which is what a teacher types into search. Boards joined with " & ", the
    subject once at the end; empty when there is no board and no subject."""
    labels: list[str] = []
    for name, grade in boards or []:
        lbl = board_label(name, grade)
        if lbl and lbl not in labels:
            labels.append(lbl)
    subj = clean_heading(subject)
    head = " & ".join(labels)
    if head and subj:
        return f"{head} {subj}"
    return head or subj


# ── key terms and hashtags ───────────────────────────────────────────────

def terms_from_summary(summary: object) -> list[str]:
    """The vocabulary a summary ENUMERATES, when no model has picked terms:
    the comma list before the first dash or colon, items of at most three
    words. "Cell membrane, cytoplasm, nucleus … and vacuole — what plant and
    animal cells share" yields the seven organelles and not the clause."""
    head = re.split(r"\s+[—–-]\s+|:\s|\.\s", _s(summary), maxsplit=1)[0]
    out: list[str] = []
    for piece in re.split(r",\s*|\s+and\s+|;\s*|\s+or\s+", head):
        t = piece.strip(" .;:").lower()
        if not t or len(t.split()) > 3 or t in out:
            continue
        out.append(t)
    return out[:MAX_KEY_TERMS]


def clean_terms(values: object, limit: int = MAX_KEY_TERMS) -> list[str]:
    """Short, distinct, lower-cased; never a sentence."""
    out: list[str] = []
    for v in (values if isinstance(values, (list, tuple)) else []):
        t = _s(v).strip(" .;:#").lower()
        if not t or len(t.split()) > 4 or len(t) > 40 or t in out:
            continue
        out.append(t)
    return out[:limit]


def _camel(text: str) -> str:
    """The words of ``text`` run together, each capitalised: letters and
    digits of ANY script (a hashtag in Arabic or Devanagari is a hashtag on
    YouTube), never spaces, punctuation or underscores. Combining marks — a
    Devanagari vowel sign, an Arabic diacritic — belong to their letter and
    stay (a regex word class would drop them: कोशिका → कशक)."""
    import unicodedata
    words: list[str] = []
    cur: list[str] = []
    for ch in _s(text):
        if ch.isalnum() or unicodedata.category(ch).startswith("M"):
            cur.append(ch)
        elif cur:
            words.append("".join(cur))
            cur = []
    if cur:
        words.append("".join(cur))
    return "".join(w[:1].upper() + w[1:] for w in words)


def clean_hashtags(values: object, limit: int = MAX_HASHTAGS) -> list[str]:
    """CamelCase, letters and digits of any script, no '#', distinct
    (case-insensitively); a tag needs at least one letter."""
    out: list[str] = []
    seen: set[str] = set()
    for v in (values if isinstance(values, (list, tuple)) else []):
        tag = _camel(_s(v).lstrip("#"))
        if not tag or len(tag) > 40 or tag.lower() in seen or not re.search(r"[^\W\d_]", tag):
            continue
        seen.add(tag.lower())
        out.append(tag)
    return out[:limit]


def default_hashtags(key_terms: Iterable[str], boards: Iterable[tuple[str, str]], subject: object) -> list[str]:
    """Terms, then the boards, then the subject and the channel: #PlantCell
    #AnimalCell … #CBSE #Class9Science #CambridgeScience #Science #SketchCast."""
    tags: list[str] = [t for t in list(key_terms or [])[:6]]
    subj = _camel(clean_heading(subject) or "Science")
    for name, grade in boards or []:
        lbl = board_label(name, grade)
        if not lbl:
            continue
        words = lbl.split()
        tags.append(words[0])                                  # CBSE, Cambridge
        if len(words) == 3:
            tags.append(f"{words[1]}{words[2]}{subj}")         # Class9Science, Stage7Science
        else:
            tags.append(f"{words[0]}{subj}")                   # CambridgeScience
    tags += [subj, "SketchCast"]
    return clean_hashtags(tags)


# ── the composers (pure; mirrored by the portal's preview) ───────────────

# YouTube's videos.insert rejects a title or description holding "<" or ">"
# ("invalidDescription" / "invalidTitle": every UTF-8 character except those
# two). A maths video names its chapters by its examples — "x > 7 on the
# number line", "3x - 4 <= 8" — and Linear Inequalities (2026-10-13, kit
# 43d0743e) could not be posted at all. The relation is kept, in the
# characters YouTube allows: ≤ ≥ and the full-width ＜ ＞.
_YOUTUBE_SIGNS = (("<=", "≤"), (">=", "≥"), ("=<", "≤"), ("=>", "≥"), ("<", "＜"), (">", "＞"))


def youtube_safe(text: object) -> str:
    """``text`` with the characters YouTube refuses in a title or a
    description replaced by their allowed look-alikes."""
    out = _s(text) if not isinstance(text, str) else text
    for raw, safe in _YOUTUBE_SIGNS:
        out = out.replace(raw, safe)
    return out


def compose_title(topic_title: object, *, meta: Optional[dict] = None, key_terms: Iterable[str] = (),
                  audience: str = "", part: int = 1, total: int = 1, language: object = "en") -> str:
    """``<Topic> Explained | <terms> | <audience>`` — the stored title when the
    reviewer (or the model) wrote one, else composed from the terms and the
    audience; a bare topic when there is neither. Over YouTube's 100
    characters the middle block loses terms first, then the audience goes,
    and the part label (`` — Part k of N``) is the one thing never cut.
    ``language``: the headline and the part label read in the video's
    language (words_for); the audience tag is never translated."""
    topic = clean_heading(topic_title) or "Topic"
    stored = _s((meta or {}).get("title"))
    words = words_for(language)
    suffix = (" — " + words["part"].format(part=int(part), total=int(total)).rstrip(".।۔")) if int(total) > 1 else ""
    room = TITLE_MAX - len(suffix)
    if stored:
        base = stored
    else:
        terms = [t for t in list(key_terms or []) if _s(t)][:TITLE_TERMS]
        aud = _s(audience)
        if not terms and not aud:
            base = topic
        else:
            # Terms go first (three, two, one, none), the audience last: the
            # audience block is what a teacher searches for.
            base = ""
            for with_aud in (True, False):
                if with_aud and not aud:
                    continue
                for n in range(len(terms), -1, -1):
                    mid = ", ".join(_title_case(t) for t in terms[:n])
                    blocks = [headline(topic, language)] + ([mid] if mid else []) + ([aud] if with_aud else [])
                    cand = " | ".join(blocks)
                    if len(cand) <= room:
                        base = cand
                        break
                if base:
                    break
            if not base:
                base = headline(topic, language)
    if len(base) > room:
        base = base[:room].rstrip()
    return youtube_safe((base + suffix).strip())


def headline(topic_title: object, language: object = "en") -> str:
    """``"<Topic> Explained"`` — unless the topic already ends on a participle
    ("Plant and Animal Cells Compared", "Photosynthesis Explained"), where a
    second one would read as a stutter. Another language uses its own
    template (words_for: "شرح {topic}", "{topic} expliqué" …)."""
    topic = clean_heading(topic_title) or "Topic"
    if _lang_key(language) != "en":
        return words_for(language)["explained"].format(topic=topic)
    last = topic.split()[-1].lower() if topic.split() else ""
    if last.endswith("ed") and len(last) > 4:
        return topic
    return f"{topic} Explained"


def _title_case(term: str) -> str:
    """A key term as it reads in a title: first letter up, the rest as given
    ("DNA" stays "DNA", "cell wall" becomes "Cell wall")."""
    t = _s(term)
    return t[:1].upper() + t[1:] if t else t


def chapter_lines(chapters: Iterable[dict], hhmmss) -> list[str]:
    """``["0:00 Introduction", …]`` or NOTHING (YouTube's all-or-nothing rule:
    the first at 0:00 and at least three). ``hhmmss`` is the caller's formatter
    so this module owns no time arithmetic."""
    lines: list[str] = []
    for c in chapters or []:
        label = _s((c or {}).get("label"))
        if label:
            lines.append(f"{hhmmss((c or {}).get('t'))} {label}")
    if len(lines) < MIN_CHAPTERS or not lines[0].startswith("0:00 "):
        return []
    return lines


def compose_description(*, topic_title: object, summary: object, meta: Optional[dict], header_lines: Iterable[str],
                        chapter_lines_: Iterable[str], key_terms: Iterable[str], hashtags: Iterable[str],
                        part: int, total: int, next_title: Optional[str], link: str,
                        language: object = "en") -> str:
    """The description, block by block, each omitted when empty — never a
    fabricated one. Cut from the END on a line boundary when over 5000. The
    fixed words (block labels, the ask, the SketchCast line, the link prefix)
    read in ``language``; the model's words come in through ``meta``."""
    topic = clean_heading(topic_title) or "This topic"
    words = words_for(language)
    blocks: list[list[str]] = []

    intro = _s((meta or {}).get("intro")) or clean_heading(summary) or words["fallback_intro"].format(topic=topic)
    blocks.append([intro])

    codes = [_s(line) for line in (header_lines or []) if _s(line)]
    if codes:
        blocks.append([words["aligned"], *codes])

    lines = [ln for ln in (chapter_lines_ or []) if ln]
    if lines:
        blocks.append([words["chapters"], *lines])

    terms = [t for t in (key_terms or []) if _s(t)]
    if terms:
        blocks.append([words["key_terms"] + ", ".join(terms) + "."])

    if int(total) > 1:
        line = words["part"].format(part=int(part), total=int(total))
        blocks.append([f"{line} {words['next']}{_s(next_title)}" if next_title else line])

    # the end screen's ask, in the description too (founder direction
    # 2026-09-25): where most clicks come from
    blocks.append([words["cta"]])
    blocks.append([words["sketchcast"], kit_link_line(link, language)])

    tags = [t for t in (hashtags or []) if t]
    if tags:
        blocks.append([" ".join("#" + t for t in tags)])

    text = youtube_safe("\n\n".join("\n".join(b) for b in blocks))
    if len(text) <= DESCRIPTION_MAX:
        return text
    cut = text[:DESCRIPTION_MAX]
    return cut[:cut.rfind("\n")].rstrip() if "\n" in cut else cut.rstrip()


# ── the stored words: validation, defaults, and the one model call ───────

def clean_meta(raw: object) -> Optional[dict]:
    """A ``youtube_meta`` payload reduced to what the composers read, or None
    when it holds nothing usable. Lenient on purpose: an edit that leaves the
    title blank means 'use the default', not 'refuse the video'."""
    if not isinstance(raw, dict):
        return None
    out: dict = {}
    title = _s(raw.get("title"))
    if title:
        out["title"] = title[:TITLE_MAX]
    intro = " ".join(str(raw.get("intro") or "").split())
    if intro:
        out["intro"] = intro[:INTRO_MAX]
    terms = clean_terms(raw.get("key_terms"))
    if terms:
        out["key_terms"] = terms
    tags = clean_hashtags(raw.get("hashtags"))
    if tags:
        out["hashtags"] = tags
    lang = _s(raw.get("language")).lower()
    if out and lang and len(lang) <= 8:
        # the language the words were WRITTEN in (generate_meta stamps it);
        # a translated kit's publish refuses words in another language
        out["language"] = lang
    return out or None


def meta_in_language(meta: Optional[dict], language: object) -> bool:
    """Whether stored words were written in ``language``. Words without a
    stamp predate the stamp and were written in English (the prompt's only
    language until 2026-10-17)."""
    if not meta:
        return False
    return _lang_key((meta or {}).get("language") or "en") == _lang_key(language)


def effective_terms(meta: Optional[dict], summary: object) -> list[str]:
    return list((meta or {}).get("key_terms") or []) or terms_from_summary(summary)


def effective_hashtags(meta: Optional[dict], key_terms: Iterable[str], boards, subject: object) -> list[str]:
    return list((meta or {}).get("hashtags") or []) or default_hashtags(key_terms, boards, subject)


def narration_excerpt(script: dict, limit: int = NARRATION_EXCERPT_CHARS) -> str:
    """The lesson's own words, for the model: every segment's text in order,
    cut at a sentence boundary near ``limit``."""
    text = " ".join(_s(seg.get("text")) for seg in ((script or {}).get("segments") or []) if isinstance(seg, dict))
    if len(text) <= limit:
        return text
    cut = text[:limit]
    dot = cut.rfind(". ")
    return (cut[:dot + 1] if dot > limit // 2 else cut).strip()


SYSTEM_PROMPT = (
    "You write YouTube metadata for SketchCast, an education channel whose videos are whiteboard lessons "
    "in which a teacher explains a school topic and a student asks questions. You are given the lesson's own "
    "narration and its curriculum alignment. Everything you write must be true to that narration: never claim "
    "content the lesson does not cover. Reply with JSON only."
)

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "intro": {"type": "string"},
        "key_terms": {"type": "array", "items": {"type": "string"}},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "intro", "key_terms", "hashtags"],
}


def language_name(language: object) -> str:
    """``"Arabic"`` for ``"ar"`` — the shared registry's English name."""
    from shared.languages import get_language
    return get_language(_lang_key(language)).name


def build_prompt(topic: dict, header_lines: Iterable[str], audience: str, chapter_labels: Iterable[str],
                 narration: str, *, dialogue: bool, language: object = "en",
                 glossary: Iterable[tuple[str, str]] = ()) -> str:
    """The one text the model sees. Pure. ``language`` is the narration's
    (the kit's): the title, intro, key terms and hashtags are written in it,
    in the same structure as the English ones; the audience tag stays as
    given. ``glossary``: the translator's (English, translated) term pairs —
    the lesson's own vocabulary, which the key terms must follow."""
    title = clean_heading(topic.get("title")) or "?"
    align = "\n".join(f"  {_s(l)}" for l in header_lines if _s(l)) or "  (none)"
    chapters = "\n".join(f"  {_s(c)}" for c in chapter_labels if _s(c)) or "  (none)"
    lang = _lang_key(language)
    name = language_name(lang)
    head = words_for(lang)["explained"].format(topic="<Topic>")
    terms = [(_s(a), _s(b)) for a, b in (glossary or ()) if _s(a) and _s(b)]
    gloss = ("\n".join(f"  {a} = {b}" for a, b in terms[:40]) + "\n") if terms else ""
    if lang == "en":
        lang_lines = ""
        title_shape = ("\"<Topic, rephrased for search if that reads better> Explained | <two or three key terms, "
                       "comma-separated> | <the audience tag>\". Example: \"Plant vs Animal Cells Explained | "
                       "Prokaryotes, Eukaryotes, Organelles | CBSE Class 9 & Cambridge Stage 7 Science\".")
        tag_rule = ("CamelCase, letters and digits only, no '#'. Include the topic, the key structures, the subject, "
                    "the boards named in the audience tag with their class or stage (e.g. Class9Science, "
                    "CambridgeScience) and SketchCast.")
    else:
        lang_lines = (f"Language: {name} — the narration is in {name}; write the title, the intro, the key terms "
                      f"and the hashtags in {name}, the way a {name}-speaking teacher would search for them. The "
                      f"audience tag stays EXACTLY as given, in Latin letters.\n"
                      + (f"Preferred terms (English = {name}, the lesson's own vocabulary; use these words):\n{gloss}"
                         if gloss else ""))
        title_shape = (f"\"{head} | <two or three key terms in {name}, comma-separated> | <the audience tag>\" "
                       f"— the first block is the topic in {name} in that exact phrasing.")
        tag_rule = (f"no spaces, no '#', letters and digits only — in {name} where a teacher would type it that way, "
                    f"plus the Latin tags for the boards named in the audience tag with their class or stage "
                    f"(e.g. Class9Science, CambridgeScience) and SketchCast.")
    return (
        f"Topic: {title}\n"
        f"Subject: {clean_heading(topic.get('subject')) or 'Science'}\n"
        f"Summary on file: {clean_heading(topic.get('summary')) or '(none)'}\n"
        f"Audience tag (use it verbatim as the title's last block): {audience or '(none)'}\n"
        f"{lang_lines}"
        f"Curriculum alignment:\n{align}\n"
        f"Chapter headings, in order:\n{chapters}\n"
        f"Format: {'a teacher and a student in conversation' if dialogue else 'a teacher explaining'}\n\n"
        f"NARRATION (the only source):\n<narration>\n{narration}\n</narration>\n\n"
        "Write:\n"
        f"1. \"title\": at most {TITLE_MODEL_MAX} characters, EXACTLY this shape — {title_shape}\n"
        "2. \"intro\": two to four sentences for the description's opening paragraph. Open with the question the "
        "lesson answers; say what it works through, in the order the chapters go; if the format is a conversation, "
        "say that a teacher and a student work through it; end with what the viewer will be able to explain. "
        "No hashtags, no links, no calls to subscribe.\n"
        "3. \"key_terms\": six to twelve terms the narration actually uses, most important first, each one to "
        "three words, lower case except proper names and abbreviations.\n"
        f"4. \"hashtags\": eight to twelve, {tag_rule}\n"
    )


def generate_meta(client, topic: dict, header_lines: Iterable[str], boards, chapter_labels: Iterable[str],
                  narration: str, *, dialogue: bool = True, now: Optional[datetime] = None,
                  language: object = "en", glossary: Iterable[tuple[str, str]] = ()) -> Optional[dict]:
    """One model call → a cleaned ``youtube_meta`` dict tagged
    ``source: 'generated'`` and stamped with the ``language`` it was written
    in, or None when the reply held nothing usable. The title the model
    returns is re-checked against the audience tag: a title that dropped it
    gets it back through the deterministic composer."""
    audience = audience_tag(boards, topic.get("subject"))
    lang = _lang_key(language)
    prompt = build_prompt(topic, header_lines, audience, chapter_labels, narration, dialogue=dialogue,
                          language=lang, glossary=glossary)
    # a reply in Arabic, Devanagari or Telugu is about twice the tokens
    from shared.languages import output_token_factor
    budget = int(1200 * output_token_factor(lang))
    reply = client.analyze(prompt, system=SYSTEM_PROMPT, max_tokens=budget, response_schema=RESPONSE_SCHEMA)
    data = reply.get("data") if isinstance(reply, dict) and "data" in reply else reply
    meta = clean_meta(data)
    if not meta:
        return None
    title = meta.get("title", "")
    if audience and audience.lower() not in title.lower():
        # The model rewrote the audience block; compose it back from its own
        # terms so the last block stays the searchable one.
        meta["title"] = compose_title(topic.get("title"), key_terms=meta.get("key_terms") or [], audience=audience,
                                      language=lang)
    meta["source"] = "generated"
    meta["language"] = lang
    meta["generated_at"] = (now or datetime.now(timezone.utc)).isoformat()
    return meta


def write_for_kit(sb, kit: dict, parts: list[dict], *, client_factory=None, now: Optional[datetime] = None) -> Optional[dict]:
    """Generate and store ``topic_kits.youtube_meta`` for a kit whose video
    just finished. Best-effort by contract: returns None (and logs) on any
    fault — a video is never held up by its own description. ``parts`` is
    the presentation loop's ``[{part, chapters, narration?}]``."""
    kit_id = _s(kit.get("id"))
    try:
        if not kit_id:
            return None
        from catalogue.article import load_mappings
        from catalogue.kit import header_lines as _header_lines
        from catalogue.publish import load_topic

        topic = load_topic(sb, _s(kit.get("topic_id")))
        if not topic:
            return None
        language = _lang_key(kit.get("language"))
        glossary: list[tuple[str, str]] = []
        if language != "en":
            # Kit Translator: the words are written in the kit's language,
            # headed by the TRANSLATED article's title; the English summary
            # stays off the prompt (the narration is the only source) and the
            # translator's glossary names the vocabulary the key terms follow.
            rows = list(getattr(sb.table("topic_articles").select("title").eq("id", _s(kit.get("article_id")))
                                .limit(1).execute(), "data", None) or [])
            translated_title = _s((rows[0] if rows else {}).get("title"))
            topic = {**topic, "title": translated_title or topic.get("title"), "summary": None}
            tr = kit.get("translation") if isinstance(kit.get("translation"), dict) else {}
            gl = tr.get("glossary") if isinstance(tr.get("glossary"), dict) else {}
            glossary = [(str(p[0]), str(p[1])) for p in (gl.get("pairs") or [])
                        if isinstance(p, (list, tuple)) and len(p) == 2]
        mappings = load_mappings(sb, _s(kit.get("topic_id")))
        headers = _header_lines(mappings)
        boards = boards_of(mappings)
        labels = [c.get("label") for p in sorted(parts, key=lambda p: int(p.get("part") or 0))
                  for c in (p.get("chapters") or []) if isinstance(c, dict)]
        narration = " ".join(_s(p.get("narration")) for p in parts if _s(p.get("narration")))
        if not narration:
            return None
        dialogue = _s((kit.get("voice_pair") or {}).get("student") if isinstance(kit.get("voice_pair"), dict) else "") != "" \
            or _s(kit.get("narration_style") or "dialogue") == "dialogue"
        if client_factory is None:
            from shared.llm import client_for
            client_factory = lambda: client_for(_s(kit.get("language")) or "en")  # noqa: E731
        meta = generate_meta(client_factory(), topic, headers, boards, labels, narration[:NARRATION_EXCERPT_CHARS],
                             dialogue=dialogue, now=now, language=language, glossary=glossary)
        if not meta:
            log.warning("youtube meta: the model returned nothing usable for kit %s", kit_id)
            return None
        sb.table("topic_kits").update({"youtube_meta": meta}).eq("id", kit_id).execute()
        kit["youtube_meta"] = meta
        log.info("youtube meta: written for kit %s (%r)", kit_id, meta.get("title"))
        return meta
    except Exception as exc:  # noqa: BLE001 — never the video's problem
        log.warning("youtube meta: not written for kit %s (%s: %s)", kit_id, type(exc).__name__, exc)
        return None


__all__ = ["DESCRIPTION_MAX", "INTRO_MAX", "MAX_HASHTAGS", "MAX_KEY_TERMS", "MIN_CHAPTERS", "SKETCHCAST_LINE",
           "TITLE_MAX", "WORDS", "audience_tag", "board_label", "boards_of", "build_prompt", "chapter_lines",
           "headline", "clean_hashtags", "clean_meta", "clean_terms", "compose_description", "compose_title",
           "cta_line", "default_hashtags", "effective_hashtags", "effective_terms", "generate_meta",
           "is_kit_link_line", "is_sketchcast_line", "kit_link_prefix", "language_name", "meta_in_language",
           "narration_excerpt", "sketchcast_line", "terms_from_summary", "words_for", "write_for_kit"]
