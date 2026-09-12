"""The deck's own chrome, in every lesson language.

The first Arabic, Hindi and Telugu decks rendered with every heading the
storyboard writes itself — "By the end of this lesson", "Common
misunderstandings", "Term / Meaning", "Ready to teach." — in English. The
lesson was in the teacher's language; the furniture around it was not.

Same contract as ``docgen/strings.py``, which already localises the document
builders and is REUSED here for the keys it has (learning objectives, key
vocabulary, term, definition, answer key): key → {lang → text}, English
pinned, Jawi in Arabic script with MALAY vocabulary and the house letters
(ڤ ݢ ڠ چ ڽ), digits Western, unknown language → English.

The ms-arab column was transliterated from the Malay by hand, following the
conventions of the existing table (Kuiz → کويز, Bahagian → بهاݢين). It has
not been checked by a Jawi reader; the ``te``/``mr`` columns likewise want
a native eye. Every string is a real text object in the file, so a teacher
can correct one on the slide in a second.
"""

from __future__ import annotations

from docgen.strings import _t as _doc_t

_STRINGS: dict[str, dict[str, str]] = {
    "by_end": {
        "en": "By the end of this lesson", "ms": "Menjelang akhir pelajaran ini",
        "ms-arab": "منجلڠ اخير ڤلاجرن اين", "ar": "بنهاية هذا الدرس",
        "fr": "À la fin de cette leçon", "es": "Al final de esta lección",
        "pt": "No final desta lição", "hi": "इस पाठ के अंत तक",
        "mr": "या पाठाच्या शेवटी", "te": "ఈ పాఠం ముగిసేసరికి",
    },
    "check": {
        "en": "Check", "ms": "Semak", "ms-arab": "سمق", "ar": "تحقق", "fr": "Vérification",
        "es": "Comprobación", "pt": "Verificação", "hi": "जाँच", "mr": "तपासणी", "te": "తనిఖీ",
    },
    "remember": {
        "en": "Remember", "ms": "Ingat", "ms-arab": "ايڠت", "ar": "تذكّر", "fr": "À retenir",
        "es": "Para recordar", "pt": "Para lembrar", "hi": "याद रखें", "mr": "लक्षात ठेवा",
        "te": "గుర్తుంచుకోండి",
    },
    "watch_out": {
        "en": "Watch out for", "ms": "Awas", "ms-arab": "اواس", "ar": "انتبه إلى", "fr": "Attention à",
        "es": "Cuidado con", "pt": "Atenção a", "hi": "सावधान", "mr": "सावध रहा", "te": "జాగ్రత్త",
    },
    "misunderstandings": {
        "en": "Common misunderstandings", "ms": "Salah faham lazim", "ms-arab": "ساله فهم لازيم",
        "ar": "مفاهيم خاطئة شائعة", "fr": "Idées fausses courantes", "es": "Malentendidos comunes",
        "pt": "Equívocos comuns", "hi": "आम ग़लतफ़हमियाँ", "mr": "सामान्य गैरसमज", "te": "సాధారణ అపోహలు",
    },
    "learners_think": {
        "en": "Learners often think", "ms": "Murid sering fikir", "ms-arab": "موريد سريڠ فيکير",
        "ar": "يظن المتعلمون غالباً", "fr": "Les élèves pensent souvent", "es": "Los alumnos suelen pensar",
        "pt": "Os alunos costumam pensar", "hi": "विद्यार्थी अक्सर सोचते हैं",
        "mr": "विद्यार्थ्यांना अनेकदा वाटते", "te": "విద్యార్థులు తరచుగా అనుకుంటారు",
    },
    "in_fact": {
        "en": "In fact", "ms": "Sebenarnya", "ms-arab": "سبنرڽ", "ar": "في الواقع", "fr": "En réalité",
        "es": "En realidad", "pt": "Na verdade", "hi": "वास्तव में", "mr": "प्रत्यक्षात", "te": "వాస్తవానికి",
    },
    "worked_example": {
        "en": "Worked example", "ms": "Contoh kerja", "ms-arab": "چونتوه کرجا", "ar": "مثال محلول",
        "fr": "Exemple résolu", "es": "Ejemplo resuelto", "pt": "Exemplo resolvido",
        "hi": "हल किया हुआ उदाहरण", "mr": "सोडवलेले उदाहरण", "te": "సాధించిన ఉదాహరణ",
    },
    "worked_example_n": {
        "en": "Worked example {n}", "ms": "Contoh kerja {n}", "ms-arab": "چونتوه کرجا {n}",
        "ar": "مثال محلول {n}", "fr": "Exemple résolu {n}", "es": "Ejemplo resuelto {n}",
        "pt": "Exemplo resolvido {n}", "hi": "हल किया हुआ उदाहरण {n}", "mr": "सोडवलेले उदाहरण {n}",
        "te": "సాధించిన ఉదాహరణ {n}",
    },
    "words_to_know": {
        "en": "Words to know", "ms": "Perkataan untuk diketahui", "ms-arab": "ڤرکاتاءن اونتوق دکتاهوي",
        "ar": "كلمات يجب معرفتها", "fr": "Mots à connaître", "es": "Palabras que debes conocer",
        "pt": "Palavras para conhecer", "hi": "जानने योग्य शब्द", "mr": "जाणून घ्यायचे शब्द",
        "te": "తెలుసుకోవాల్సిన పదాలు",
    },
    "zoom_in": {
        "en": "Zoom in", "ms": "Zum masuk", "ms-arab": "زوم ماسوق", "ar": "تكبير", "fr": "Zoom",
        "es": "Acercar", "pt": "Ampliar", "hi": "ज़ूम करें", "mr": "झूम करा", "te": "జూమ్ చేయండి",
    },
    "compare": {
        "en": "Compare", "ms": "Banding", "ms-arab": "بنديڠ", "ar": "قارن", "fr": "Comparer",
        "es": "Comparar", "pt": "Comparar", "hi": "तुलना", "mr": "तुलना", "te": "పోల్చండి",
    },
    "side_by_side": {
        "en": "Side by side", "ms": "Bersebelahan", "ms-arab": "برسبلاهن", "ar": "جنباً إلى جنب",
        "fr": "Côte à côte", "es": "Lado a lado", "pt": "Lado a lado", "hi": "आमने-सामने",
        "mr": "शेजारी शेजारी", "te": "పక్కపక్కన",
    },
    "only_second_has": {
        "en": "Only the second has:", "ms": "Hanya yang kedua ada:", "ms-arab": "هاڽ يڠ کدوا اد:",
        "ar": "الثاني فقط يحتوي على:", "fr": "Seul le second possède :", "es": "Solo el segundo tiene:",
        "pt": "Só o segundo tem:", "hi": "केवल दूसरे में है:", "mr": "फक्त दुसऱ्यात आहे:",
        "te": "రెండవదానిలో మాత్రమే ఉంది:",
    },
    "shared": {
        "en": "Shared:", "ms": "Dikongsi:", "ms-arab": "دکوڠسي:", "ar": "مشترك:", "fr": "En commun :",
        "es": "En común:", "pt": "Em comum:", "hi": "साझा:", "mr": "सामायिक:", "te": "ఉమ్మడి:",
    },
    "name_each": {
        "en": "Name each structure", "ms": "Namakan setiap struktur", "ms-arab": "ناماکن ستياڤ ستروکتور",
        "ar": "سمِّ كل جزء", "fr": "Nommez chaque structure", "es": "Nombra cada estructura",
        "pt": "Nomeie cada estrutura", "hi": "प्रत्येक संरचना का नाम बताइए",
        "mr": "प्रत्येक रचनेचे नाव सांगा", "te": "ప్రతి నిర్మాణానికి పేరు పెట్టండి",
    },
    "continued": {
        "en": "(continued)", "ms": "(sambungan)", "ms-arab": "(سمبوڠن)", "ar": "(تابع)", "fr": "(suite)",
        "es": "(continuación)", "pt": "(continuação)", "hi": "(जारी)", "mr": "(पुढे चालू)",
        "te": "(కొనసాగింపు)",
    },
    "ready": {
        "en": "Ready to teach.", "ms": "Sedia untuk mengajar.", "ms-arab": "سديا اونتوق مڠاجر.",
        "ar": "جاهز للتدريس.", "fr": "Prêt à enseigner.", "es": "Listo para enseñar.",
        "pt": "Pronto para ensinar.", "hi": "पढ़ाने के लिए तैयार।", "mr": "शिकवण्यास तयार.",
        "te": "బోధించడానికి సిద్ధం.",
    },
    "notes_line": {
        "en": "Every slide's speaker notes carry the narration.",
        "ms": "Nota penceramah setiap slaid membawa narasi.",
        "ms-arab": "نوتا ڤنچرامه ستياڤ سلايد ممباوا ناراسي.",
        "ar": "ملاحظات المتحدث في كل شريحة تحمل السرد.",
        "fr": "Les notes du présentateur de chaque diapositive contiennent la narration.",
        "es": "Las notas del orador de cada diapositiva contienen la narración.",
        "pt": "As notas do apresentador de cada slide contêm a narração.",
        "hi": "हर स्लाइड के वक्ता नोट्स में कथन है।",
        "mr": "प्रत्येक स्लाइडच्या वक्ता टिपांमध्ये निवेदन आहे.",
        "te": "ప్రతి స్లైడ్ స్పీకర్ నోట్స్‌లో కథనం ఉంది.",
    },
    "answer": {
        "en": "Answer", "ms": "Jawapan", "ms-arab": "جواڤن", "ar": "الإجابة", "fr": "Réponse",
        "es": "Respuesta", "pt": "Resposta", "hi": "उत्तर", "mr": "उत्तर", "te": "సమాధానం",
    },
    "answer_not_given": {
        "en": "Answer not given.", "ms": "Jawapan tidak diberikan.", "ms-arab": "جواڤن تيدق دبريکن.",
        "ar": "الإجابة غير محددة.", "fr": "Réponse non indiquée.", "es": "Respuesta no indicada.",
        "pt": "Resposta não indicada.", "hi": "उत्तर नहीं दिया गया।", "mr": "उत्तर दिलेले नाही.",
        "te": "సమాధానం ఇవ్వలేదు.",
    },
    "labelled_here": {
        "en": "Labelled here:", "ms": "Dilabel di sini:", "ms-arab": "دلابل دسيني:", "ar": "المسمّى هنا:",
        "fr": "Étiqueté ici :", "es": "Etiquetado aquí:", "pt": "Rotulado aqui:", "hi": "यहाँ लेबल किया गया:",
        "mr": "येथे लेबल केलेले:", "te": "ఇక్కడ లేబుల్ చేయబడింది:",
    },
    "answers": {
        "en": "Answers:", "ms": "Jawapan:", "ms-arab": "جواڤن:", "ar": "الإجابات:", "fr": "Réponses :",
        "es": "Respuestas:", "pt": "Respostas:", "hi": "उत्तर:", "mr": "उत्तरे:", "te": "సమాధానాలు:",
    },
}

# Keys the document builders already localise; the deck says the same thing
# the worksheet does rather than inventing a second translation of it.
_FROM_DOCS = {
    "objectives": "learning_objectives",
    "key_terms": "key_vocabulary",
    "term": "term",
    "meaning": "definition",
}

LANGS = ("en", "ms", "ms-arab", "ar", "fr", "es", "pt", "hi", "mr", "te")


def T(lang: str | None, key: str, **fmt) -> str:
    code = (lang or "en").strip().lower()
    if key in _FROM_DOCS:
        text = _doc_t(_FROM_DOCS[key], code)
    else:
        row = _STRINGS[key]
        text = row.get(code) or row["en"]
    return text.format(**fmt) if fmt else text
