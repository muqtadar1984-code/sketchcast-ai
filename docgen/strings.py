"""User-visible document strings, localized for every lesson language.

ONE mechanism for every string a builder or the style layer emits into a
document: the ``_STRINGS`` table (key → {lang → text}) + ``_t(key, lang)``.
Grew out of the style layer's original five-key table in docx_builder.py; it
moved here when the six builders were localized (2026-08-18) — docx_builder
re-exports ``_t``/``letters`` so builders keep a single import (``dx``).

Language set mirrors shared/languages.py WITHOUT importing it (docgen must stay
importable in isolation for tests): en, ms, ms-arab, ar, fr, es, pt, hi, mr, te.
Unknown codes → English.

Rules that hold for every entry (founder-approved previews):
· ENGLISH IS PINNED — the ``en`` value of every key is byte-identical to the
  literal the builders emitted before localization (test_worksheet.py and
  test_docx_numbering.py assert the exact strings).
· ms-arab is STRICT Jawi: Arabic script, MALAY vocabulary, the Jawi house
  letters ڤ ݢ ڠ چ ڽ (never Arabic vocabulary — a Jawi doc says بنر, not صواب).
· Digits stay WESTERN (0-9) in every language — no Arabic-Indic numerals.
· Enumeration letters: Arabic-script docs (ar, ms-arab) use the abjad sequence
  (أ ب ج د هـ و …); everything else uses A B C D. ``letters(lang)`` is the ONLY
  source — section heads, the marks table, match-table options, MCQ options,
  and every answer key draw from the same sequence, or keys become
  un-gradeable against their papers.
· True/False: one pair per language ("true"/"false" keys) shared by the T/F
  cue line, the section headings, and the answer-key values.
"""

from __future__ import annotations

import string

# RTL lesson languages (mirrors shared/languages.py without importing it).
RTL_LANGS = {"ar", "ms-arab"}

# Abjad enumeration for Arabic-script documents (أبجد هوز order). 28 letters —
# comfortably above MAX_MATCH (26) and any section/MCQ count.
_ABJAD = [
    "أ", "ب", "ج", "د", "هـ", "و", "ز", "ح", "ط", "ي", "ك", "ل", "م", "ن",
    "س", "ع", "ف", "ص", "ق", "ر", "ش", "ت", "ث", "خ", "ذ", "ض", "ظ", "غ",
]


def letters(lang: str) -> list[str]:
    """The enumeration sequence for a language: abjad for Arabic-script docs,
    A–Z otherwise. Use EVERYWHERE letters appear (sections, marks table,
    match options, MCQ options, answer keys) so paper and key always agree."""
    if (lang or "en").strip().lower() in RTL_LANGS:
        return list(_ABJAD)
    return list(string.ascii_uppercase)


_STRINGS: dict[str, dict[str, str]] = {
    # ── style-layer strings (the original table) ─────────────────────────────
    "section": {
        "en": "Section", "ms": "Bahagian", "ar": "القسم", "fr": "Section",
        "es": "Sección", "pt": "Seção", "te": "విభాగం", "mr": "विभाग",
        "hi": "खंड", "ms-arab": "بهاݢين",
    },
    "marks": {
        "en": "Marks", "ms": "Markah", "ar": "الدرجات", "fr": "Points",
        "es": "Puntos", "pt": "Pontos", "te": "మార్కులు", "mr": "गुण",
        "hi": "अंक", "ms-arab": "مرکه",
    },
    "total": {
        "en": "Total", "ms": "Jumlah", "ar": "المجموع", "fr": "Total",
        "es": "Total", "pt": "Total", "te": "మొత్తం", "mr": "एकूण",
        "hi": "कुल", "ms-arab": "جومله",
    },
    # Template: {t}/{f} are filled with this table's "true"/"false" values, so
    # the cue and the answer key can never drift apart.
    "tf_box_cue": {
        "en": "Write {t} or {f} in each box.",
        "ms": "Tulis {t} atau {f} dalam setiap petak.",
        "ar": "اكتب {t} أو {f} في كل مربع.",
        "fr": "Écrivez {t} ou {f} dans chaque case.",
        "es": "Escribe {t} o {f} en cada casilla.",
        "pt": "Escreva {t} ou {f} em cada caixa.",
        "te": "ప్రతి పెట్టెలో {t} లేదా {f} అని రాయండి.",
        "mr": "प्रत्येक चौकटीत {t} किंवा {f} लिहा.",
        "hi": "प्रत्येक बॉक्स में {t} या {f} लिखें।",
        "ms-arab": "توليس {t} اتاو {f} دالم ستياڤ ڤتق.",
    },
    "end_of_paper": {
        "en": "END OF PAPER", "ms": "KERTAS SOALAN TAMAT",
        "ar": "نهاية الورقة", "fr": "FIN DE L'ÉPREUVE",
        "es": "FIN DEL EXAMEN", "pt": "FIM DA PROVA",
        "te": "పరీక్షా పత్రం ముగిసింది", "mr": "प्रश्नपत्रिका समाप्त",
        "hi": "प्रश्न पत्र समाप्त", "ms-arab": "کرتس سوءالن تامت",
    },

    # ── True/False pair (cue line + section heading + answer-key values) ─────
    "true": {
        "en": "True", "ms": "Benar", "ar": "صواب", "fr": "Vrai",
        "es": "Verdadero", "pt": "Verdadeiro", "te": "నిజం", "mr": "खरे",
        "hi": "सही", "ms-arab": "بنر",
    },
    "false": {
        "en": "False", "ms": "Palsu", "ar": "خطأ", "fr": "Faux",
        "es": "Falso", "pt": "Falso", "te": "అబద్ధం", "mr": "खोटे",
        "hi": "गलत", "ms-arab": "ڤلسو",
    },

    # ── Section-kind headings ("Section {letter} — <kind>") ──────────────────
    "sec_mcq": {
        "en": "Multiple choice", "ms": "Aneka pilihan", "ar": "اختيار من متعدد",
        "fr": "Choix multiples", "es": "Opción múltiple", "pt": "Múltipla escolha",
        "te": "బహుళ ఎంపిక", "mr": "बहुपर्यायी प्रश्न", "hi": "बहुविकल्पीय प्रश्न",
        "ms-arab": "انيکا ڤيليهن",
    },
    "sec_fill_blank": {
        "en": "Fill in the blanks", "ms": "Isi tempat kosong",
        "ar": "املأ الفراغات", "fr": "Remplissez les blancs",
        "es": "Completa los espacios", "pt": "Preencha as lacunas",
        "te": "ఖాళీలను పూరించండి", "mr": "रिकाम्या जागा भरा",
        "hi": "रिक्त स्थान भरें", "ms-arab": "ايسي تمڤت کوسوڠ",
    },
    "sec_true_false": {
        "en": "True or False", "ms": "Benar atau Palsu", "ar": "صواب أو خطأ",
        "fr": "Vrai ou Faux", "es": "Verdadero o Falso",
        "pt": "Verdadeiro ou Falso", "te": "నిజం లేదా అబద్ధం",
        "mr": "खरे किंवा खोटे", "hi": "सही या गलत", "ms-arab": "بنر اتاو ڤلسو",
    },
    "sec_match": {
        "en": "Match the columns", "ms": "Padankan lajur",
        "ar": "صل بين العمودين", "fr": "Reliez les colonnes",
        "es": "Relaciona las columnas", "pt": "Relacione as colunas",
        "te": "నిలువు వరుసలను జతపరచండి", "mr": "स्तंभांची जुळणी करा",
        "hi": "स्तंभों का मिलान करें", "ms-arab": "ڤادنکن لاجور",
    },
    "sec_short": {
        "en": "Short answer", "ms": "Jawapan pendek", "ar": "الإجابة القصيرة",
        "fr": "Réponse courte", "es": "Respuesta corta", "pt": "Resposta curta",
        "te": "సంక్షిప్త జవాబు", "mr": "लघु उत्तर", "hi": "लघु उत्तर",
        "ms-arab": "جواڤن ڤندق",
    },
    "sec_long": {
        "en": "Long answer", "ms": "Jawapan panjang", "ar": "الإجابة المطولة",
        "fr": "Réponse longue", "es": "Respuesta larga", "pt": "Resposta longa",
        "te": "వివరణాత్మక జవాబు", "mr": "दीर्घ उत्तर", "hi": "दीर्घ उत्तर",
        "ms-arab": "جواڤن ڤنجڠ",
    },

    # ── Answer key / match exercise ──────────────────────────────────────────
    "answer_key": {
        "en": "Answer Key", "ms": "Skema Jawapan", "ar": "نموذج الإجابة",
        "fr": "Corrigé", "es": "Clave de respuestas", "pt": "Gabarito",
        "te": "జవాబుల పట్టిక", "mr": "उत्तरसूची", "hi": "उत्तर कुंजी",
        "ms-arab": "سکيما جواڤن",
    },
    "column": {  # {letter} ← letters(lang)
        "en": "Column {letter}", "ms": "Lajur {letter}", "ar": "العمود {letter}",
        "fr": "Colonne {letter}", "es": "Columna {letter}", "pt": "Coluna {letter}",
        "te": "నిలువు వరుస {letter}", "mr": "स्तंभ {letter}",
        "hi": "स्तंभ {letter}", "ms-arab": "لاجور {letter}",
    },
    "match_instruction": {  # {a}/{b} ← column_label(lang, 0/1)
        "en": "Match each item in {a} to the correct item in {b}.",
        "ms": "Padankan setiap item dalam {a} dengan item yang betul dalam {b}.",
        "ar": "صل كل عنصر في {a} بالعنصر الصحيح في {b}.",
        "fr": "Reliez chaque élément de la {a} à l'élément correct de la {b}.",
        "es": "Relaciona cada elemento de la {a} con el elemento correcto de la {b}.",
        "pt": "Relacione cada item da {a} com o item correto da {b}.",
        "te": "{a} లోని ప్రతి అంశాన్ని {b} లోని సరైన అంశంతో జతపరచండి.",
        "mr": "{a} मधील प्रत्येक घटक {b} मधील योग्य घटकाशी जुळवा.",
        "hi": "{a} के प्रत्येक मद का मिलान {b} के सही मद से करें।",
        "ms-arab": "ڤادنکن ستياڤ ايتم دالم {a} دڠن ايتم يڠ بتول دالم {b}.",
    },
    "marks_bracket": {  # {n} stays a WESTERN digit in every language
        "en": "[{n} marks]", "ms": "[{n} markah]", "ar": "[{n} درجات]",
        "fr": "[{n} points]", "es": "[{n} puntos]", "pt": "[{n} pontos]",
        "te": "[{n} మార్కులు]", "mr": "[{n} गुण]", "hi": "[{n} अंक]",
        "ms-arab": "[{n} مرکه]",
    },

    # ── Default document titles ("<kind> — <chapter>") ───────────────────────
    "doc_worksheet": {
        "en": "Worksheet", "ms": "Lembaran Kerja", "ar": "ورقة عمل",
        "fr": "Fiche d'exercices", "es": "Hoja de ejercicios",
        "pt": "Folha de exercícios", "te": "అభ్యాస పత్రం", "mr": "कार्यपत्रिका",
        "hi": "अभ्यास पत्रक", "ms-arab": "لمبارن کرجا",
    },
    "doc_test_paper": {
        "en": "Test Paper", "ms": "Kertas Ujian", "ar": "ورقة اختبار",
        "fr": "Épreuve", "es": "Examen", "pt": "Prova",
        "te": "పరీక్షా పత్రం", "mr": "प्रश्नपत्रिका", "hi": "प्रश्न पत्र",
        "ms-arab": "کرتس اوجين",
    },
    "doc_exam": {
        "en": "Exam", "ms": "Peperiksaan", "ar": "امتحان", "fr": "Examen",
        "es": "Examen", "pt": "Exame", "te": "పరీక్ష", "mr": "परीक्षा",
        "hi": "परीक्षा", "ms-arab": "ڤڤريقساءن",
    },
    "doc_lesson_plan": {
        "en": "Lesson Plan", "ms": "Rancangan Pengajaran", "ar": "خطة درس",
        "fr": "Plan de leçon", "es": "Plan de clase", "pt": "Plano de aula",
        "te": "పాఠ ప్రణాళిక", "mr": "पाठ आराखडा", "hi": "पाठ योजना",
        "ms-arab": "رنچاڠن ڤڠاجرن",
    },
    "doc_activities": {
        "en": "Class Activities", "ms": "Aktiviti Kelas", "ar": "أنشطة صفية",
        "fr": "Activités de classe", "es": "Actividades de clase",
        "pt": "Atividades de sala", "te": "తరగతి కార్యకలాపాలు",
        "mr": "वर्ग उपक्रम", "hi": "कक्षा गतिविधियाँ", "ms-arab": "اکتيۏيتي کلس",
    },
    "doc_case_study": {
        "en": "Case Study", "ms": "Kajian Kes", "ar": "دراسة حالة",
        "fr": "Étude de cas", "es": "Estudio de caso", "pt": "Estudo de caso",
        "te": "సందర్భ అధ్యయనం", "mr": "प्रकरण अभ्यास", "hi": "केस अध्ययन",
        "ms-arab": "کاجين کيس",
    },
    "chapter": {  # fallback when a chapter has no title
        "en": "Chapter", "ms": "Bab", "ar": "الفصل", "fr": "Chapitre",
        "es": "Capítulo", "pt": "Capítulo", "te": "అధ్యాయం", "mr": "प्रकरण",
        "hi": "अध्याय", "ms-arab": "باب",
    },
    "quiz": {  # questions.json fallback title
        "en": "Quiz", "ms": "Kuiz", "ar": "اختبار قصير", "fr": "Quiz",
        "es": "Cuestionario", "pt": "Questionário", "te": "క్విజ్",
        "mr": "प्रश्नमंजुषा", "hi": "प्रश्नोत्तरी", "ms-arab": "کويز",
    },

    # ── Subtitles / meta lines ───────────────────────────────────────────────
    "difficulty": {
        "en": "Difficulty", "ms": "Kesukaran", "ar": "مستوى الصعوبة",
        "fr": "Difficulté", "es": "Dificultad", "pt": "Dificuldade",
        "te": "కఠినత", "mr": "काठिण्य", "hi": "कठिनाई", "ms-arab": "کسوکرن",
    },
    "diff_easy": {
        "en": "easy", "ms": "mudah", "ar": "سهل", "fr": "facile",
        "es": "fácil", "pt": "fácil", "te": "సులభం", "mr": "सोपे",
        "hi": "सरल", "ms-arab": "موده",
    },
    "diff_medium": {
        "en": "medium", "ms": "sederhana", "ar": "متوسط", "fr": "moyen",
        "es": "medio", "pt": "médio", "te": "మధ్యస్థం", "mr": "मध्यम",
        "hi": "मध्यम", "ms-arab": "سدرهانا",
    },
    "diff_hard": {
        "en": "hard", "ms": "sukar", "ar": "صعب", "fr": "difficile",
        "es": "difícil", "pt": "difícil", "te": "కఠినం", "mr": "कठीण",
        "hi": "कठिन", "ms-arab": "سوکر",
    },
    "diff_mixed": {
        "en": "mixed", "ms": "campuran", "ar": "متنوع", "fr": "mixte",
        "es": "mixto", "pt": "misto", "te": "మిశ్రమం", "mr": "मिश्र",
        "hi": "मिश्रित", "ms-arab": "چمڤورن",
    },
    "duration": {
        "en": "Duration", "ms": "Tempoh", "ar": "المدة", "fr": "Durée",
        "es": "Duración", "pt": "Duração", "te": "వ్యవధి", "mr": "कालावधी",
        "hi": "अवधि", "ms-arab": "تمڤوه",
    },
    "n_minutes": {  # {n} stays a WESTERN digit
        "en": "{n} minutes", "ms": "{n} minit", "ar": "{n} دقيقة",
        "fr": "{n} minutes", "es": "{n} minutos", "pt": "{n} minutos",
        "te": "{n} నిమిషాలు", "mr": "{n} मिनिटे", "hi": "{n} मिनट",
        "ms-arab": "{n} مينيت",
    },
    "paper_covers": {
        "en": "This paper covers:", "ms": "Kertas ini merangkumi:",
        "ar": "تغطي هذه الورقة:", "fr": "Cette épreuve couvre :",
        "es": "Este examen abarca:", "pt": "Esta prova abrange:",
        "te": "ఈ పత్రం పరిధి:", "mr": "या प्रश्नपत्रिकेत समाविष्ट:",
        "hi": "इस प्रश्न पत्र में शामिल:", "ms-arab": "کرتس اين مرڠکومي:",
    },
    "teacher_only": {
        "en": "For the teacher only — not for distribution to students.",
        "ms": "Untuk guru sahaja — bukan untuk diedarkan kepada murid.",
        "ar": "للمعلم فقط — لا يوزع على الطلاب.",
        "fr": "Réservé à l'enseignant — ne pas distribuer aux élèves.",
        "es": "Solo para el docente — no distribuir a los alumnos.",
        "pt": "Apenas para o professor — não distribuir aos alunos.",
        "te": "ఉపాధ్యాయుల కోసం మాత్రమే — విద్యార్థులకు పంపిణీ చేయరాదు.",
        "mr": "फक्त शिक्षकांसाठी — विद्यार्थ्यांना वाटू नये.",
        "hi": "केवल शिक्षक के लिए — विद्यार्थियों को वितरित न करें।",
        "ms-arab": "اونتوق ݢورو سهاج — بوکن اونتوق دايدرکن کڤد موريد.",
    },

    # ── Lesson plan ──────────────────────────────────────────────────────────
    "learning_objectives": {
        "en": "Learning objectives", "ms": "Objektif pembelajaran",
        "ar": "أهداف التعلم", "fr": "Objectifs d'apprentissage",
        "es": "Objetivos de aprendizaje", "pt": "Objetivos de aprendizagem",
        "te": "అభ్యసన లక్ష్యాలు", "mr": "अध्ययन उद्दिष्टे",
        "hi": "सीखने के उद्देश्य", "ms-arab": "اوبجيکتيف ڤمبلاجرن",
    },
    "materials": {
        "en": "Materials", "ms": "Bahan", "ar": "المواد", "fr": "Matériel",
        "es": "Materiales", "pt": "Materiais", "te": "సామగ్రి",
        "mr": "साहित्य", "hi": "सामग्री", "ms-arab": "بهن",
    },
    "key_vocabulary": {
        "en": "Key vocabulary", "ms": "Kosa kata utama",
        "ar": "المفردات الأساسية", "fr": "Vocabulaire clé",
        "es": "Vocabulario clave", "pt": "Vocabulário-chave",
        "te": "ముఖ్య పదజాలం", "mr": "मुख्य शब्दसंग्रह", "hi": "मुख्य शब्दावली",
        "ms-arab": "کوسا کات اوتام",
    },
    "lesson_flow": {
        "en": "Lesson flow", "ms": "Aliran pengajaran", "ar": "سير الدرس",
        "fr": "Déroulement de la leçon", "es": "Desarrollo de la clase",
        "pt": "Desenvolvimento da aula", "te": "పాఠ ప్రవాహం",
        "mr": "पाठ प्रवाह", "hi": "पाठ प्रवाह", "ms-arab": "اليرن ڤڠاجرن",
    },
    "assessment": {
        "en": "Assessment", "ms": "Pentaksiran", "ar": "التقويم",
        "fr": "Évaluation", "es": "Evaluación", "pt": "Avaliação",
        "te": "మూల్యాంకనం", "mr": "मूल्यमापन", "hi": "मूल्यांकन",
        "ms-arab": "ڤنتقسيرن",
    },
    "homework": {
        "en": "Homework", "ms": "Kerja rumah", "ar": "الواجب المنزلي",
        "fr": "Devoirs", "es": "Tarea", "pt": "Dever de casa",
        "te": "ఇంటి పని", "mr": "गृहपाठ", "hi": "गृहकार्य",
        "ms-arab": "کرجا روماه",
    },
    "differentiation": {
        "en": "Differentiation", "ms": "Pembezaan", "ar": "التمايز",
        "fr": "Différenciation", "es": "Diferenciación", "pt": "Diferenciação",
        "te": "వైవిధ్యీకరణ", "mr": "विभेदीकरण", "hi": "विभेदीकरण",
        "ms-arab": "ڤمبزاءن",
    },
    "term": {
        "en": "Term", "ms": "Istilah", "ar": "المصطلح", "fr": "Terme",
        "es": "Término", "pt": "Termo", "te": "పదం", "mr": "संज्ञा",
        "hi": "शब्द", "ms-arab": "اصطلاح",
    },
    "definition": {
        "en": "Definition", "ms": "Definisi", "ar": "التعريف",
        "fr": "Définition", "es": "Definición", "pt": "Definição",
        "te": "నిర్వచనం", "mr": "व्याख्या", "hi": "परिभाषा",
        "ms-arab": "ديفينيسي",
    },
    "phase": {
        "en": "Phase", "ms": "Fasa", "ar": "المرحلة", "fr": "Phase",
        "es": "Fase", "pt": "Fase", "te": "దశ", "mr": "टप्पा",
        "hi": "चरण", "ms-arab": "فاسا",
    },
    "min_col": {  # narrow lesson-flow column header
        "en": "Min", "ms": "Min", "ar": "دقائق", "fr": "Min",
        "es": "Min", "pt": "Min", "te": "నిమి.", "mr": "मिनिटे",
        "hi": "मिनट", "ms-arab": "مينيت",
    },
    "teacher_does": {
        "en": "Teacher does", "ms": "Tindakan guru", "ar": "دور المعلم",
        "fr": "Rôle de l'enseignant", "es": "Acción del docente",
        "pt": "Ação do professor", "te": "ఉపాధ్యాయుడు చేసేది",
        "mr": "शिक्षकाची कृती", "hi": "शिक्षक की भूमिका",
        "ms-arab": "تيندقن ݢورو",
    },
    "students_do": {
        "en": "Students do", "ms": "Tindakan murid", "ar": "دور الطلاب",
        "fr": "Rôle des élèves", "es": "Acción de los alumnos",
        "pt": "Ação dos alunos", "te": "విద్యార్థులు చేసేది",
        "mr": "विद्यार्थ्यांची कृती", "hi": "विद्यार्थियों की भूमिका",
        "ms-arab": "تيندقن موريد",
    },
    "support": {
        "en": "Support", "ms": "Sokongan", "ar": "الدعم", "fr": "Soutien",
        "es": "Apoyo", "pt": "Apoio", "te": "మద్దతు", "mr": "साहाय्य",
        "hi": "सहायता", "ms-arab": "سوکوڠن",
    },
    "challenge": {
        "en": "Challenge", "ms": "Cabaran", "ar": "التحدي", "fr": "Défi",
        "es": "Desafío", "pt": "Desafio", "te": "సవాలు", "mr": "आव्हान",
        "hi": "चुनौती", "ms-arab": "چابرن",
    },

    # ── Activities ───────────────────────────────────────────────────────────
    "activity": {
        "en": "Activity", "ms": "Aktiviti", "ar": "النشاط", "fr": "Activité",
        "es": "Actividad", "pt": "Atividade", "te": "కార్యకలాపం",
        "mr": "उपक्रम", "hi": "गतिविधि", "ms-arab": "اکتيۏيتي",
    },
    "objective": {
        "en": "Objective", "ms": "Objektif", "ar": "الهدف", "fr": "Objectif",
        "es": "Objetivo", "pt": "Objetivo", "te": "లక్ష్యం", "mr": "उद्दिष्ट",
        "hi": "उद्देश्य", "ms-arab": "اوبجيکتيف",
    },
    "grouping": {
        "en": "Grouping", "ms": "Kumpulan", "ar": "تنظيم المجموعات",
        "fr": "Regroupement", "es": "Agrupación", "pt": "Agrupamento",
        "te": "గుంపు విధానం", "mr": "गटरचना", "hi": "समूहन",
        "ms-arab": "کومڤولن",
    },
    "steps": {
        "en": "Steps", "ms": "Langkah", "ar": "الخطوات", "fr": "Étapes",
        "es": "Pasos", "pt": "Passos", "te": "దశలు", "mr": "पायऱ्या",
        "hi": "चरण", "ms-arab": "لڠکه",
    },
    "teacher_facilitation": {
        "en": "Teacher facilitation", "ms": "Pemudahcaraan guru",
        "ar": "إشراف المعلم", "fr": "Animation par l'enseignant",
        "es": "Facilitación del docente", "pt": "Facilitação do professor",
        "te": "ఉపాధ్యాయుని పర్యవేక్షణ", "mr": "शिक्षक मार्गदर्शन",
        "hi": "शिक्षक मार्गदर्शन", "ms-arab": "ڤمودهچاراءن ݢورو",
    },
    "success_looks_like": {
        "en": "Success looks like", "ms": "Tanda kejayaan",
        "ar": "مؤشرات النجاح", "fr": "Signes de réussite",
        "es": "Señales de éxito", "pt": "Sinais de sucesso",
        "te": "విజయ సూచికలు", "mr": "यशाची लक्षणे", "hi": "सफलता के संकेत",
        "ms-arab": "تندا کجاياءن",
    },

    # ── Case study ───────────────────────────────────────────────────────────
    "background": {
        "en": "Background", "ms": "Latar belakang", "ar": "الخلفية",
        "fr": "Contexte", "es": "Antecedentes", "pt": "Contexto",
        "te": "నేపథ్యం", "mr": "पार्श्वभूमी", "hi": "पृष्ठभूमि",
        "ms-arab": "لاتر بلاکڠ",
    },
    "scenario": {
        "en": "Scenario", "ms": "Senario", "ar": "السيناريو", "fr": "Scénario",
        "es": "Escenario", "pt": "Cenário", "te": "సన్నివేశం",
        "mr": "प्रसंग", "hi": "परिदृश्य", "ms-arab": "سيناريو",
    },
    "discussion_questions": {
        "en": "Discussion questions", "ms": "Soalan perbincangan",
        "ar": "أسئلة للنقاش", "fr": "Questions de discussion",
        "es": "Preguntas de discusión", "pt": "Questões para discussão",
        "te": "చర్చా ప్రశ్నలు", "mr": "चर्चेचे प्रश्न", "hi": "चर्चा प्रश्न",
        "ms-arab": "سوءالن ڤربينچڠن",
    },
    "concepts_applied": {
        "en": "Concepts applied", "ms": "Konsep yang digunakan",
        "ar": "المفاهيم المطبقة", "fr": "Concepts appliqués",
        "es": "Conceptos aplicados", "pt": "Conceitos aplicados",
        "te": "అన్వయించిన భావనలు", "mr": "वापरलेल्या संकल्पना",
        "hi": "प्रयुक्त अवधारणाएँ", "ms-arab": "کونسيڤ يڠ دݢوناکن",
    },
    "teacher_notes": {
        "en": "Teacher notes — answer guidance",
        "ms": "Nota guru — panduan jawapan",
        "ar": "ملاحظات المعلم — إرشادات الإجابة",
        "fr": "Notes pour l'enseignant — guide de réponse",
        "es": "Notas para el docente — guía de respuestas",
        "pt": "Notas do professor — orientações de resposta",
        "te": "ఉపాధ్యాయ గమనికలు — జవాబు మార్గదర్శకత్వం",
        "mr": "शिक्षक टिपा — उत्तर मार्गदर्शन",
        "hi": "शिक्षक टिप्पणियाँ — उत्तर मार्गदर्शन",
        "ms-arab": "نوتا ݢورو — ڤندوان جواڤن",
    },

    # ── Lesson plan teaching modes (catalogue kits, Phase 3 decision 11) ─────
    # "Mode A — Full lesson": the letter comes from letters(lang), like sections.
    "mode": {
        "en": "Mode", "ms": "Mod", "ar": "النمط", "fr": "Mode",
        "es": "Modalidad", "pt": "Modo", "te": "విధానం", "mr": "पद्धत",
        "hi": "मोड", "ms-arab": "مود",
    },
    "mode_full_lesson": {
        "en": "Full lesson", "ms": "Pelajaran penuh", "ar": "الدرس الكامل",
        "fr": "Leçon complète", "es": "Clase completa", "pt": "Aula completa",
        "te": "పూర్తి పాఠం", "mr": "संपूर्ण पाठ", "hi": "पूर्ण पाठ",
        "ms-arab": "ڤلاجرن ڤنوه",
    },
    "mode_micro_clips": {
        "en": "In-class micro-clips", "ms": "Klip pendek dalam kelas",
        "ar": "مقاطع قصيرة داخل الصف", "fr": "Micro-extraits en classe",
        "es": "Microclips en clase", "pt": "Microclipes em sala",
        "te": "తరగతిలో చిన్న క్లిప్‌లు", "mr": "वर्गातील लघु क्लिप",
        "hi": "कक्षा में लघु क्लिप", "ms-arab": "کليڤ ڤندق دالم کلس",
    },
    "mode_flipped": {
        "en": "Flipped / pre-watch", "ms": "Terbalik / tonton dahulu",
        "ar": "الصف المعكوس / مشاهدة مسبقة",
        "fr": "Classe inversée / visionnage préalable",
        "es": "Aula invertida / visionado previo",
        "pt": "Sala invertida / pré-visualização",
        "te": "ఫ్లిప్డ్ / ముందుగా చూడటం", "mr": "फ्लिप्ड / आधी पाहणे",
        "hi": "फ़्लिप्ड / पहले देखें", "ms-arab": "ترباليق / تونتون دهولو",
    },
    "pre_watch": {
        "en": "Before class", "ms": "Sebelum kelas", "ar": "قبل الحصة",
        "fr": "Avant le cours", "es": "Antes de la clase", "pt": "Antes da aula",
        "te": "తరగతికి ముందు", "mr": "वर्गापूर्वी", "hi": "कक्षा से पहले",
        "ms-arab": "سبلوم کلس",
    },

    # ── Question-bank worksheet (composed sets, Phase 3 decision 9) ──────────
    "sec_assertion_reason": {
        "en": "Assertion and reason", "ms": "Pernyataan dan alasan",
        "ar": "التوكيد والسبب", "fr": "Assertion et raison",
        "es": "Afirmación y razón", "pt": "Afirmação e razão",
        "te": "వాక్యం మరియు కారణం", "mr": "विधान आणि कारण",
        "hi": "अभिकथन और कारण", "ms-arab": "ڤرنياتاءن دان الاسن",
    },
    "sec_numerical": {
        "en": "Numerical", "ms": "Berangka", "ar": "مسائل حسابية",
        "fr": "Numérique", "es": "Numérico", "pt": "Numérico",
        "te": "సంఖ్యాత్మక", "mr": "संख्यात्मक", "hi": "संख्यात्मक",
        "ms-arab": "برڠک",
    },
    "sec_diagram_label": {
        "en": "Label the diagram", "ms": "Labelkan rajah",
        "ar": "سمِّ أجزاء الرسم", "fr": "Légendez le schéma",
        "es": "Rotula el diagrama", "pt": "Identifique o diagrama",
        "te": "చిత్రానికి పేర్లు రాయండి", "mr": "आकृतीला नावे द्या",
        "hi": "आरेख को नामांकित करें", "ms-arab": "لابيلکن راجه",
    },
    "marking_scheme": {
        "en": "Marking scheme", "ms": "Skema pemarkahan", "ar": "مخطط التصحيح",
        "fr": "Barème", "es": "Criterios de calificación",
        "pt": "Critérios de correção", "te": "మార్కుల పథకం",
        "mr": "गुणदान योजना", "hi": "अंकन योजना", "ms-arab": "سکيما ڤمرکهن",
    },
    "explanation": {
        "en": "Explanation", "ms": "Penjelasan", "ar": "التفسير",
        "fr": "Explication", "es": "Explicación", "pt": "Explicação",
        "te": "వివరణ", "mr": "स्पष्टीकरण", "hi": "व्याख्या",
        "ms-arab": "ڤنجلسن",
    },
    "total_marks": {
        "en": "Total marks", "ms": "Jumlah markah", "ar": "مجموع الدرجات",
        "fr": "Total des points", "es": "Puntos totales", "pt": "Total de pontos",
        "te": "మొత్తం మార్కులు", "mr": "एकूण गुण", "hi": "कुल अंक",
        "ms-arab": "جومله مرکه",
    },
    "n_questions": {  # {n} stays a WESTERN digit
        "en": "{n} questions", "ms": "{n} soalan", "ar": "{n} سؤالًا",
        "fr": "{n} questions", "es": "{n} preguntas", "pt": "{n} questões",
        "te": "{n} ప్రశ్నలు", "mr": "{n} प्रश्न", "hi": "{n} प्रश्न",
        "ms-arab": "{n} سوءالن",
    },
}


def _t(key: str, lang: str) -> str:
    table = _STRINGS[key]
    return table.get((lang or "en").strip().lower()) or table["en"]


# ── Composed helpers (single source for every letter/pair that must agree
#    between a paper and its answer key) ──────────────────────────────────────

def section_heading(lang: str, index: int, kind_key: str) -> str:
    """"Section A — Fill in the blanks" / "القسم أ — املأ الفراغات". The SAME
    string names the paper section and its answer-key section."""
    return f"{_t('section', lang)} {letters(lang)[index]} — {_t(kind_key, lang)}"


def column_label(lang: str, index: int) -> str:
    """"Column A" / "العمود أ" — header of the match table; letter drawn from
    letters(lang) so it matches the option letters in the rows."""
    return _t("column", lang).format(letter=letters(lang)[index])


def match_instruction(lang: str) -> str:
    return _t("match_instruction", lang).format(
        a=column_label(lang, 0), b=column_label(lang, 1))


def tf_word(value, lang: str) -> str:
    """The localized True/False pair — used by BOTH the paper's cue line and
    the answer key's values, so they can never disagree."""
    return _t("true" if value else "false", lang)


def marks_bracket(n, lang: str) -> str:
    """"[5 marks]" / "[5 درجات]" — same string wherever marks are repeated."""
    return _t("marks_bracket", lang).format(n=n)


def difficulty_label(value, lang: str) -> str:
    """Localized easy/medium/hard/mixed. English (and any unrecognized value)
    passes through UNTOUCHED so today's English output stays byte-identical."""
    lang = (lang or "en").strip().lower()
    key = {"easy": "diff_easy", "medium": "diff_medium",
           "hard": "diff_hard", "mixed": "diff_mixed"}.get(str(value).strip().lower())
    if lang in ("", "en") or key is None:
        return str(value)
    return _t(key, lang)
