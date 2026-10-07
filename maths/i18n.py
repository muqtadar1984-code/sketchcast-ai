"""The maths lesson's own words, in every lesson language.

Two tables, one mechanism (the same shape as docgen/strings.py):

* ``BOARD`` — the few fixed strings the algebra board writes or falls back
  to: the "check" note, the try-it invitation, the closing line, the card's
  default title. The model writes every real spoken line in the lesson's
  language; these are the belt behind that.
* ``WORDS`` — how notation is SPOKEN when it slips into a spoken line
  ("x^2" -> "x squared" / "x تربيع" / "x का वर्ग"). ``maths.speech`` reads
  the table for the lesson language; English is the reference.

Language set mirrors shared/languages.py without importing it: en, ms,
ms-arab, ar, fr, es, pt, hi, mr, te. Unknown codes -> English. Digits stay
Western in every language (every TTS provider reads them). ms-arab is Jawi:
Arabic script, Malay vocabulary.
"""

from __future__ import annotations

LANGS = ("en", "ms", "ms-arab", "ar", "fr", "es", "pt", "hi", "mr", "te")


def norm_lang(lang: str | None) -> str:
    code = (lang or "en").strip().lower()
    if code in ("ms-arab-my", "jawi"):
        return "ms-arab"
    if "-" in code and code not in ("ms-arab",):
        code = code.split("-", 1)[0]
    return code if code in LANGS else "en"


BOARD: dict[str, dict[str, str]] = {
    "check": {
        "en": "check", "ms": "semak", "ar": "تحقق", "fr": "vérification", "es": "comprobación",
        "pt": "verificação", "hi": "जाँच", "mr": "पडताळणी", "te": "సరిచూడు", "ms-arab": "سيمق",
    },
    "set_up": {
        "en": "set up", "ms": "bentuk persamaan", "ar": "صياغة", "fr": "mise en équation",
        "es": "planteamiento", "pt": "montagem", "hi": "समीकरण बनाएँ", "mr": "समीकरण मांडा",
        "te": "సమీకరణం రాయండి", "ms-arab": "بنتوق ڤرسماءن",
    },
    "not_allowed": {
        "en": "not allowed: {why}", "ms": "tidak dibenarkan: {why}", "ar": "غير مسموح: {why}",
        "fr": "interdit : {why}", "es": "no permitido: {why}", "pt": "não permitido: {why}",
        "hi": "गलत: {why}", "mr": "चूक: {why}", "te": "తప్పు: {why}", "ms-arab": "تيدق دبنرکن: {why}",
    },
    "try_it": {
        "en": "Try it", "ms": "Cuba sendiri", "ar": "جرّب بنفسك", "fr": "À vous", "es": "Inténtalo",
        "pt": "Tente você", "hi": "अब आप कीजिए", "mr": "तुम्ही करून पाहा", "te": "మీరు ప్రయత్నించండి",
        "ms-arab": "چوبا سنديري",
    },
    "not_to_scale": {
        "en": "not drawn to scale", "ms": "tidak dilukis mengikut skala", "ar": "الرسم ليس بمقياس",
        "fr": "figure non à l'échelle", "es": "no está a escala", "pt": "sem escala",
        "hi": "पैमाने के अनुसार नहीं", "mr": "प्रमाणानुसार नाही", "te": "స్కేలు ప్రకారం కాదు",
        "ms-arab": "تيدق دلوکيس مڠيکوت سکالا",
    },
    "pause_line": {
        "en": "Pause the video and try it", "ms": "Jeda video dan cuba", "ar": "أوقف الفيديو وجرّب",
        "fr": "Mettez la vidéo en pause et essayez", "es": "Pausa el video e inténtalo",
        "pt": "Pause o vídeo e tente", "hi": "वीडियो रोकें और हल करें", "mr": "व्हिडिओ थांबवा आणि सोडवा",
        "te": "వీడియో ఆపి ప్రయత్నించండి", "ms-arab": "جدا ۏيديو دان چوبا",
    },
    "common_mistakes": {
        "en": "Common mistakes", "ms": "Kesilapan lazim", "ar": "أخطاء شائعة", "fr": "Erreurs fréquentes",
        "es": "Errores comunes", "pt": "Erros comuns", "hi": "आम गलतियाँ", "mr": "सामान्य चुका",
        "te": "సాధారణ తప్పులు", "ms-arab": "کسيلاڤن لازيم",
    },
    "method": {
        "en": "METHOD", "ms": "KAEDAH", "ar": "الطريقة", "fr": "MÉTHODE", "es": "MÉTODO", "pt": "MÉTODO",
        "hi": "विधि", "mr": "पद्धत", "te": "పద్ధతి", "ms-arab": "قاعده",
    },
    "try_it_speech": {
        "en": "Try this one yourself: {problem}. Pause the video and work it out.",
        "ms": "Cuba yang ini sendiri: {problem}. Jeda video dan selesaikannya.",
        "ar": "جرّب هذه بنفسك: {problem}. أوقف الفيديو وحلّها.",
        "fr": "Essayez celle-ci vous-même : {problem}. Mettez la vidéo en pause et résolvez-la.",
        "es": "Intenta esta tú mismo: {problem}. Pausa el video y resuélvela.",
        "pt": "Tente esta sozinho: {problem}. Pause o vídeo e resolva.",
        "hi": "इसे खुद हल कीजिए: {problem}। वीडियो रोकें और हल करें।",
        "mr": "हे स्वतः सोडवा: {problem}. व्हिडिओ थांबवा आणि सोडवा.",
        "te": "దీన్ని మీరే ప్రయత్నించండి: {problem}. వీడియో ఆపి పరిష్కరించండి.",
        "ms-arab": "چوبا يڠ اين سنديري: {problem}. جدا ۏيديو دان سلسايکنڽ.",
    },
    "solution_intro": {
        "en": "Let us go through it together.", "ms": "Mari kita selesaikan bersama.",
        "ar": "هيا نحلّها معًا.", "fr": "Corrigeons-la ensemble.", "es": "Resolvámosla juntos.",
        "pt": "Vamos resolver juntos.", "hi": "आइए इसे साथ मिलकर हल करें।", "mr": "चला, आपण मिळून सोडवूया.",
        "te": "రండి, కలిసి పరిష్కరిద్దాం.", "ms-arab": "ماري کيت سلسايکن برسام.",
    },
    "closing": {
        "en": "I hope you now have a better understanding of {topic}. Try a few more on your own, "
              "and see you in the next lesson.",
        "ms": "Saya harap anda kini lebih memahami {topic}. Cuba beberapa lagi sendiri, "
              "dan jumpa dalam pelajaran seterusnya.",
        "ar": "أرجو أن تكون قد فهمت {topic} بشكل أفضل الآن. جرّب بعض التمارين بنفسك، وأراك في الدرس القادم.",
        "fr": "J'espère que vous comprenez mieux {topic} maintenant. Entraînez-vous encore un peu, "
              "et à la prochaine leçon.",
        "es": "Espero que ahora entiendas mejor {topic}. Practica algunos más por tu cuenta, "
              "y nos vemos en la próxima lección.",
        "pt": "Espero que agora você entenda melhor {topic}. Pratique mais alguns sozinho, "
              "e até a próxima aula.",
        "hi": "आशा है अब आपको {topic} बेहतर समझ आ गया होगा। कुछ और सवाल खुद हल कीजिए, अगले पाठ में मिलते हैं।",
        "mr": "आशा आहे की आता तुम्हाला {topic} अधिक चांगले समजले असेल. आणखी काही स्वतः सोडवा, पुढील पाठात भेटू.",
        "te": "ఇప్పుడు మీకు {topic} బాగా అర్థమైందని ఆశిస్తున్నాను. మరికొన్ని మీరే ప్రయత్నించండి, "
              "తదుపరి పాఠంలో కలుద్దాం.",
        "ms-arab": "ساي هارڤ اندا کيني لبيه ممهمي {topic}. چوبا ببراڤ لاݢي سنديري، دان جومڤا دالم ڤلاجرن ستروسڽ.",
    },
    "method_intro": {
        "en": "Here is the method we will use.", "ms": "Inilah kaedah yang akan kita gunakan.",
        "ar": "هذه هي الطريقة التي سنستخدمها.", "fr": "Voici la méthode que nous allons utiliser.",
        "es": "Este es el método que usaremos.", "pt": "Este é o método que vamos usar.",
        "hi": "यह है वह विधि जो हम इस्तेमाल करेंगे।", "mr": "ही आहे आपण वापरणार असलेली पद्धत.",
        "te": "మనం ఉపయోగించే పద్ధతి ఇదే.", "ms-arab": "اينيله قاعده يڠ اکن کيت ݢوناکن.",
    },
    "example_intro": {
        "en": "Here is {label}: {problem}.", "ms": "Ini {label}: {problem}.", "ar": "إليك {label}: {problem}.",
        "fr": "Voici {label} : {problem}.", "es": "Aquí está {label}: {problem}.",
        "pt": "Aqui está {label}: {problem}.", "hi": "यह रहा {label}: {problem}।",
        "mr": "हे आहे {label}: {problem}.", "te": "ఇదిగో {label}: {problem}.", "ms-arab": "اين {label}: {problem}.",
    },
    "try_it_label": {
        "en": "the try-it question", "ms": "soalan cubaan", "ar": "سؤال التدريب", "fr": "l'exercice à faire",
        "es": "el ejercicio para ti", "pt": "o exercício para você", "hi": "अभ्यास प्रश्न", "mr": "सराव प्रश्न",
        "te": "అభ్యాస ప్రశ్న", "ms-arab": "سوءالن چوباءن",
    },
    "next_example": {
        "en": "the next example", "ms": "contoh seterusnya", "ar": "المثال التالي", "fr": "l'exemple suivant",
        "es": "el siguiente ejemplo", "pt": "o próximo exemplo", "hi": "अगला उदाहरण", "mr": "पुढील उदाहरण",
        "te": "తదుపరి ఉదాహరణ", "ms-arab": "چونتوه ستروسڽ",
    },
    "today": {
        "en": "Today we learn {topic}.", "ms": "Hari ini kita belajar {topic}.", "ar": "اليوم نتعلم {topic}.",
        "fr": "Aujourd'hui, nous apprenons {topic}.", "es": "Hoy aprendemos {topic}.",
        "pt": "Hoje aprendemos {topic}.", "hi": "आज हम {topic} सीखेंगे।", "mr": "आज आपण {topic} शिकणार आहोत.",
        "te": "ఈరోజు మనం {topic} నేర్చుకుందాం.", "ms-arab": "هاري اين کيت بلاجر {topic}.",
    },
    "recap_intro": {
        "en": "Let us recap the method.", "ms": "Mari kita imbas kembali kaedah ini.", "ar": "لنراجع الطريقة.",
        "fr": "Récapitulons la méthode.", "es": "Repasemos el método.", "pt": "Vamos recapitular o método.",
        "hi": "आइए विधि को दोहराएँ।", "mr": "चला, पद्धतीची उजळणी करूया.", "te": "పద్ధతిని మరోసారి చూద్దాం.",
        "ms-arab": "ماري کيت ايمبس کمبالي قاعده اين.",
    },
}


def board_text(key: str, lang: str | None, **fmt) -> str:
    """A board string in the lesson language (English when the language
    is unknown), with its placeholders filled."""
    table = BOARD[key]
    text = table.get(norm_lang(lang)) or table["en"]
    return text.format(**fmt) if fmt else text


# ── spoken notation ─────────────────────────────────────────────────────
# Templates: {l}/{r} the sides of a relation, {n}/{d} a fraction, {b}/{e} a
# power's base and exponent, {x} a function's argument or a negated term.
# "sq_all"/"cube_all"/"pow_all" speak a BRACKETED base ("(x+1), all
# squared"); a language without the idiom falls back to the plain form with
# the base set off by a comma.

_EN = {
    "plus": "plus", "minus": "minus", "times": "times", "neg": "negative {x}",
    "frac": "{n} over {d}",
    "sq": "{b} squared", "cube": "{b} cubed", "pow": "{b} to the power of {e}",
    "sq_all": "{b}, all squared", "cube_all": "{b}, all cubed", "pow_all": "{b}, all to the power of {e}",
    "rel_eq": "{l} equals {r}", "rel_lt": "{l} is less than {r}", "rel_le": "{l} is less than or equal to {r}",
    "rel_gt": "{l} is greater than {r}", "rel_ge": "{l} is greater than or equal to {r}",
    "rel_ne": "{l} is not equal to {r}",
    "sqrt": "the square root of {x}", "abs": "the absolute value of {x}", "sin": "sine of {x}",
    "cos": "cosine of {x}", "tan": "tan of {x}", "log": "log of {x}", "ln": "the natural log of {x}",
    "exp": "e to the power of {x}", "func": "{f} of {x}",
    "lhs": "the left-hand side", "rhs": "the right-hand side",
    "fractions": {("1", "2"): "a half", ("1", "3"): "a third", ("2", "3"): "two thirds",
                  ("1", "4"): "a quarter", ("3", "4"): "three quarters", ("1", "5"): "a fifth",
                  ("1", "10"): "a tenth"},
}

WORDS: dict[str, dict] = {
    "en": _EN,
    "ms": {
        "plus": "tambah", "minus": "tolak", "times": "darab", "neg": "negatif {x}", "frac": "{n} per {d}",
        "sq": "{b} kuasa dua", "cube": "{b} kuasa tiga", "pow": "{b} kuasa {e}",
        "rel_eq": "{l} sama dengan {r}", "rel_lt": "{l} kurang daripada {r}",
        "rel_le": "{l} kurang daripada atau sama dengan {r}", "rel_gt": "{l} lebih daripada {r}",
        "rel_ge": "{l} lebih daripada atau sama dengan {r}", "rel_ne": "{l} tidak sama dengan {r}",
        "sqrt": "punca kuasa dua {x}", "abs": "nilai mutlak {x}", "sin": "sin {x}", "cos": "kos {x}",
        "tan": "tan {x}", "log": "log {x}", "ln": "log asli {x}", "exp": "e kuasa {x}", "func": "{f} {x}",
        "lhs": "sebelah kiri", "rhs": "sebelah kanan",
        "fractions": {("1", "2"): "setengah", ("1", "4"): "suku", ("3", "4"): "tiga suku"},
    },
    "ms-arab": {
        "plus": "تمبه", "minus": "تولق", "times": "دراب", "neg": "نيݢاتيف {x}", "frac": "{n} ڤر {d}",
        "sq": "{b} کواس دوا", "cube": "{b} کواس تيݢ", "pow": "{b} کواس {e}",
        "rel_eq": "{l} سام دڠن {r}", "rel_lt": "{l} کورڠ درڤد {r}", "rel_le": "{l} کورڠ درڤد اتاو سام دڠن {r}",
        "rel_gt": "{l} لبيه درڤد {r}", "rel_ge": "{l} لبيه درڤد اتاو سام دڠن {r}", "rel_ne": "{l} تيدق سام دڠن {r}",
        "sqrt": "ڤونچا کواس دوا {x}", "abs": "نيلاي مطلق {x}", "sin": "سين {x}", "cos": "کوس {x}",
        "tan": "تن {x}", "log": "لوݢ {x}", "ln": "لوݢ اصلي {x}", "exp": "e کواس {x}", "func": "{f} {x}",
        "lhs": "سبله کيري", "rhs": "سبله کانن",
        "fractions": {("1", "2"): "ستڠه", ("1", "4"): "سوکو", ("3", "4"): "تيݢ سوکو"},
    },
    "ar": {
        "plus": "زائد", "minus": "ناقص", "times": "ضرب", "neg": "سالب {x}", "frac": "{n} على {d}",
        "sq": "{b} تربيع", "cube": "{b} تكعيب", "pow": "{b} أس {e}",
        "rel_eq": "{l} يساوي {r}", "rel_lt": "{l} أصغر من {r}", "rel_le": "{l} أصغر من أو يساوي {r}",
        "rel_gt": "{l} أكبر من {r}", "rel_ge": "{l} أكبر من أو يساوي {r}", "rel_ne": "{l} لا يساوي {r}",
        "sqrt": "الجذر التربيعي لـ {x}", "abs": "القيمة المطلقة لـ {x}", "sin": "جيب {x}",
        "cos": "جيب تمام {x}", "tan": "ظل {x}", "log": "لوغاريتم {x}", "ln": "اللوغاريتم الطبيعي لـ {x}",
        "exp": "هـ أس {x}", "func": "{f} {x}",
        "lhs": "الطرف الأيسر", "rhs": "الطرف الأيمن",
        "fractions": {("1", "2"): "نصف", ("1", "3"): "ثلث", ("2", "3"): "ثلثان", ("1", "4"): "ربع",
                      ("3", "4"): "ثلاثة أرباع", ("1", "5"): "خمس", ("1", "10"): "عشر"},
    },
    "fr": {
        "plus": "plus", "minus": "moins", "times": "fois", "neg": "moins {x}", "frac": "{n} sur {d}",
        "sq": "{b} au carré", "cube": "{b} au cube", "pow": "{b} puissance {e}",
        "sq_all": "{b}, le tout au carré", "cube_all": "{b}, le tout au cube",
        "pow_all": "{b}, le tout puissance {e}",
        "rel_eq": "{l} égale {r}", "rel_lt": "{l} est inférieur à {r}", "rel_le": "{l} est inférieur ou égal à {r}",
        "rel_gt": "{l} est supérieur à {r}", "rel_ge": "{l} est supérieur ou égal à {r}",
        "rel_ne": "{l} est différent de {r}",
        "sqrt": "racine carrée de {x}", "abs": "valeur absolue de {x}", "sin": "sinus de {x}",
        "cos": "cosinus de {x}", "tan": "tangente de {x}", "log": "log de {x}",
        "ln": "logarithme népérien de {x}", "exp": "e puissance {x}", "func": "{f} de {x}",
        "lhs": "le membre de gauche", "rhs": "le membre de droite",
        "fractions": {("1", "2"): "un demi", ("1", "3"): "un tiers", ("2", "3"): "deux tiers",
                      ("1", "4"): "un quart", ("3", "4"): "trois quarts", ("1", "5"): "un cinquième",
                      ("1", "10"): "un dixième"},
    },
    "es": {
        "plus": "más", "minus": "menos", "times": "por", "neg": "menos {x}", "frac": "{n} sobre {d}",
        "sq": "{b} al cuadrado", "cube": "{b} al cubo", "pow": "{b} elevado a {e}",
        "sq_all": "{b}, todo al cuadrado", "cube_all": "{b}, todo al cubo", "pow_all": "{b}, todo elevado a {e}",
        "rel_eq": "{l} es igual a {r}", "rel_lt": "{l} es menor que {r}", "rel_le": "{l} es menor o igual que {r}",
        "rel_gt": "{l} es mayor que {r}", "rel_ge": "{l} es mayor o igual que {r}",
        "rel_ne": "{l} es distinto de {r}",
        "sqrt": "raíz cuadrada de {x}", "abs": "valor absoluto de {x}", "sin": "seno de {x}",
        "cos": "coseno de {x}", "tan": "tangente de {x}", "log": "logaritmo de {x}",
        "ln": "logaritmo natural de {x}", "exp": "e elevado a {x}", "func": "{f} de {x}",
        "lhs": "el lado izquierdo", "rhs": "el lado derecho",
        "fractions": {("1", "2"): "un medio", ("1", "3"): "un tercio", ("2", "3"): "dos tercios",
                      ("1", "4"): "un cuarto", ("3", "4"): "tres cuartos", ("1", "5"): "un quinto",
                      ("1", "10"): "un décimo"},
    },
    "pt": {
        "plus": "mais", "minus": "menos", "times": "vezes", "neg": "menos {x}", "frac": "{n} sobre {d}",
        "sq": "{b} ao quadrado", "cube": "{b} ao cubo", "pow": "{b} elevado a {e}",
        "sq_all": "{b}, tudo ao quadrado", "cube_all": "{b}, tudo ao cubo", "pow_all": "{b}, tudo elevado a {e}",
        "rel_eq": "{l} é igual a {r}", "rel_lt": "{l} é menor que {r}", "rel_le": "{l} é menor ou igual a {r}",
        "rel_gt": "{l} é maior que {r}", "rel_ge": "{l} é maior ou igual a {r}", "rel_ne": "{l} é diferente de {r}",
        "sqrt": "raiz quadrada de {x}", "abs": "valor absoluto de {x}", "sin": "seno de {x}",
        "cos": "cosseno de {x}", "tan": "tangente de {x}", "log": "logaritmo de {x}",
        "ln": "logaritmo natural de {x}", "exp": "e elevado a {x}", "func": "{f} de {x}",
        "lhs": "o lado esquerdo", "rhs": "o lado direito",
        "fractions": {("1", "2"): "um meio", ("1", "3"): "um terço", ("2", "3"): "dois terços",
                      ("1", "4"): "um quarto", ("3", "4"): "três quartos", ("1", "5"): "um quinto",
                      ("1", "10"): "um décimo"},
    },
    "hi": {
        "plus": "जमा", "minus": "घटा", "times": "गुणा", "neg": "ऋण {x}", "frac": "{n} बटा {d}",
        "sq": "{b} का वर्ग", "cube": "{b} का घन", "pow": "{b} की घात {e}",
        "rel_eq": "{l} बराबर {r}", "rel_lt": "{l}, {r} से छोटा है", "rel_le": "{l}, {r} से छोटा या बराबर है",
        "rel_gt": "{l}, {r} से बड़ा है", "rel_ge": "{l}, {r} से बड़ा या बराबर है", "rel_ne": "{l}, {r} के बराबर नहीं है",
        "sqrt": "{x} का वर्गमूल", "abs": "{x} का निरपेक्ष मान", "sin": "{x} की साइन", "cos": "{x} की कोसाइन",
        "tan": "{x} की टैन", "log": "{x} का लॉग", "ln": "{x} का प्राकृतिक लॉग", "exp": "e की घात {x}",
        "func": "{x} का {f}",
        "lhs": "बायाँ पक्ष", "rhs": "दायाँ पक्ष",
        "fractions": {("1", "2"): "आधा", ("1", "3"): "एक तिहाई", ("2", "3"): "दो तिहाई",
                      ("1", "4"): "एक चौथाई", ("3", "4"): "तीन चौथाई"},
    },
    "mr": {
        "plus": "अधिक", "minus": "उणे", "times": "गुणिले", "neg": "ऋण {x}", "frac": "{n} भागिले {d}",
        "sq": "{b} चा वर्ग", "cube": "{b} चा घन", "pow": "{b} चा {e} वा घात",
        "rel_eq": "{l} बरोबर {r}", "rel_lt": "{l}, {r} पेक्षा लहान आहे", "rel_le": "{l}, {r} पेक्षा लहान किंवा बरोबर आहे",
        "rel_gt": "{l}, {r} पेक्षा मोठा आहे", "rel_ge": "{l}, {r} पेक्षा मोठा किंवा बरोबर आहे",
        "rel_ne": "{l}, {r} च्या बरोबर नाही",
        "sqrt": "{x} चे वर्गमूळ", "abs": "{x} चे निरपेक्ष मूल्य", "sin": "{x} चा साइन", "cos": "{x} चा कोसाइन",
        "tan": "{x} चा टॅन", "log": "{x} चा लॉग", "ln": "{x} चा नैसर्गिक लॉग", "exp": "e चा {x} वा घात",
        "func": "{x} चा {f}",
        "lhs": "डावी बाजू", "rhs": "उजवी बाजू",
        "fractions": {("1", "2"): "अर्धा", ("1", "3"): "एक तृतीयांश", ("2", "3"): "दोन तृतीयांश",
                      ("1", "4"): "एक चतुर्थांश", ("3", "4"): "तीन चतुर्थांश"},
    },
    "te": {
        "plus": "ప్లస్", "minus": "మైనస్", "times": "ఇంటూ", "neg": "ఋణ {x}", "frac": "{n} బై {d}",
        "sq": "{b} వర్గం", "cube": "{b} ఘనం", "pow": "{b} యొక్క {e} వ ఘాతం",
        "rel_eq": "{l} సమానం {r}", "rel_lt": "{l}, {r} కంటే తక్కువ", "rel_le": "{l}, {r} కంటే తక్కువ లేదా సమానం",
        "rel_gt": "{l}, {r} కంటే ఎక్కువ", "rel_ge": "{l}, {r} కంటే ఎక్కువ లేదా సమానం", "rel_ne": "{l}, {r} కి సమానం కాదు",
        "sqrt": "{x} యొక్క వర్గమూలం", "abs": "{x} యొక్క పరమ మూల్యం", "sin": "{x} యొక్క సైన్",
        "cos": "{x} యొక్క కొసైన్", "tan": "{x} యొక్క టాన్", "log": "{x} యొక్క లాగ్", "ln": "{x} యొక్క సహజ లాగ్",
        "exp": "e యొక్క {x} వ ఘాతం", "func": "{x} యొక్క {f}",
        "lhs": "ఎడమ వైపు", "rhs": "కుడి వైపు",
        "fractions": {("1", "2"): "సగం", ("1", "4"): "పావు", ("3", "4"): "ముప్పావు",
                      ("1", "3"): "మూడో వంతు", ("2", "3"): "మూడింట రెండు వంతులు"},
    },
}


# ── theorem reasons, as the answer key and the board print them ───────────
# Keyed by maths.geometry.theorems' ids. English is the reference; a
# theorem missing a language falls back to English. The ids are the
# engine's and never change; these are only their names in each language.
REASONS: dict[str, dict[str, str]] = {
    "angles_on_line": {
        "en": "angles on a straight line add up to 180°",
        "ms": "sudut-sudut pada garis lurus berjumlah 180°",
        "ms-arab": "سودوت-سودوت ڤد ݢاريس لوروس برجمله 180 درجه",
        "ar": "مجموع الزوايا على خط مستقيم يساوي 180 درجة",
        "fr": "les angles sur une droite font 180°",
        "es": "los ángulos sobre una recta suman 180°",
        "pt": "os ângulos numa reta somam 180°",
        "hi": "एक सरल रेखा पर बने कोणों का योग 180 डिग्री होता है",
        "mr": "सरळ रेषेवरील कोनांची बेरीज 180 अंश असते",
        "te": "సరళరేఖపై కోణాల మొత్తం 180 డిగ్రీలు",
    },
    "angles_at_point": {
        "en": "angles at a point add up to 360°",
        "ms": "sudut-sudut pada satu titik berjumlah 360°",
        "ms-arab": "سودوت-سودوت ڤد ساتو تيتيق برجمله 360 درجه",
        "ar": "مجموع الزوايا حول نقطة يساوي 360 درجة",
        "fr": "les angles autour d'un point font 360°",
        "es": "los ángulos alrededor de un punto suman 360°",
        "pt": "os ângulos em torno de um ponto somam 360°",
        "hi": "एक बिंदु पर बने कोणों का योग 360 डिग्री होता है",
        "mr": "एका बिंदूभोवतीच्या कोनांची बेरीज 360 अंश असते",
        "te": "ఒక బిందువు చుట్టూ కోణాల మొత్తం 360 డిగ్రీలు",
    },
    "vertically_opposite": {
        "en": "vertically opposite angles are equal",
        "ms": "sudut bertentang bucu adalah sama",
        "ms-arab": "سودوت برتنتڠ بوچو اداله سام",
        "ar": "الزاويتان المتقابلتان بالرأس متساويتان",
        "fr": "les angles opposés par le sommet sont égaux",
        "es": "los ángulos opuestos por el vértice son iguales",
        "pt": "os ângulos opostos pelo vértice são iguais",
        "hi": "शीर्षाभिमुख कोण बराबर होते हैं",
        "mr": "विरुद्ध कोन समान असतात",
        "te": "శీర్షాభిముఖ కోణాలు సమానం",
    },
    "angle_addition": {
        "en": "the whole angle is the sum of its parts",
        "ms": "sudut keseluruhan ialah jumlah bahagian-bahagiannya",
        "ms-arab": "سودوت کسلوروهن اياله جمله بهاݢين-بهاݢينڽ",
        "ar": "الزاوية الكلية هي مجموع أجزائها",
        "fr": "l'angle entier est la somme de ses parties",
        "es": "el ángulo completo es la suma de sus partes",
        "pt": "o ângulo inteiro é a soma das suas partes",
        "hi": "पूरा कोण अपने भागों का योग होता है",
        "mr": "पूर्ण कोन हा त्याच्या भागांची बेरीज असतो",
        "te": "మొత్తం కోణం దాని భాగాల మొత్తం",
    },
    "triangle_angle_sum": {
        "en": "angles in a triangle add up to 180°",
        "ms": "sudut-sudut dalam segi tiga berjumlah 180°",
        "ms-arab": "سودوت-سودوت دالم سݢي تيݢ برجمله 180 درجه",
        "ar": "مجموع زوايا المثلث يساوي 180 درجة",
        "fr": "les angles d'un triangle font 180°",
        "es": "los ángulos de un triángulo suman 180°",
        "pt": "os ângulos de um triângulo somam 180°",
        "hi": "त्रिभुज के कोणों का योग 180 डिग्री होता है",
        "mr": "त्रिकोणाच्या कोनांची बेरीज 180 अंश असते",
        "te": "త్రిభుజం కోణాల మొత్తం 180 డిగ్రీలు",
    },
    "quadrilateral_angle_sum": {
        "en": "angles in a quadrilateral add up to 360°",
        "ms": "sudut-sudut dalam sisi empat berjumlah 360°",
        "ms-arab": "سودوت-سودوت دالم سيسي امڤت برجمله 360 درجه",
        "ar": "مجموع زوايا الشكل الرباعي يساوي 360 درجة",
        "fr": "les angles d'un quadrilatère font 360°",
        "es": "los ángulos de un cuadrilátero suman 360°",
        "pt": "os ângulos de um quadrilátero somam 360°",
        "hi": "चतुर्भुज के कोणों का योग 360 डिग्री होता है",
        "mr": "चौकोनाच्या कोनांची बेरीज 360 अंश असते",
        "te": "చతుర్భుజం కోణాల మొత్తం 360 డిగ్రీలు",
    },
    "polygon_interior_sum": {
        "en": "interior angles of an n-sided polygon add up to (n − 2) × 180°",
        "ms": "sudut pedalaman poligon n sisi berjumlah (n − 2) × 180°",
        "ms-arab": "سودوت ڤدالمن ڤوليݢون n سيسي برجمله (n - 2) دارب 180 درجه",
        "ar": "مجموع الزوايا الداخلية لمضلع ذي n ضلعًا يساوي (n - 2) ضرب 180 درجة",
        "fr": "les angles intérieurs d'un polygone à n côtés font (n − 2) × 180°",
        "es": "los ángulos interiores de un polígono de n lados suman (n − 2) × 180°",
        "pt": "os ângulos internos de um polígono de n lados somam (n − 2) × 180°",
        "hi": "n भुजाओं वाले बहुभुज के अंतःकोणों का योग (n − 2) × 180 डिग्री होता है",
        "mr": "n बाजूंच्या बहुभुजाच्या आंतरकोनांची बेरीज (n − 2) × 180 अंश असते",
        "te": "n భుజాల బహుభుజి అంతర కోణాల మొత్తం (n − 2) × 180 డిగ్రీలు",
    },
    "polygon_exterior_sum": {
        "en": "exterior angles of a polygon add up to 360°",
        "ms": "sudut peluaran poligon berjumlah 360°",
        "ms-arab": "سودوت ڤلوارن ڤوليݢون برجمله 360 درجه",
        "ar": "مجموع الزوايا الخارجية للمضلع يساوي 360 درجة",
        "fr": "les angles extérieurs d'un polygone font 360°",
        "es": "los ángulos exteriores de un polígono suman 360°",
        "pt": "os ângulos externos de um polígono somam 360°",
        "hi": "बहुभुज के बहिष्कोणों का योग 360 डिग्री होता है",
        "mr": "बहुभुजाच्या बाह्यकोनांची बेरीज 360 अंश असते",
        "te": "బహుభుజి బాహ్య కోణాల మొత్తం 360 డిగ్రీలు",
    },
    "isosceles_base_angles": {
        "en": "base angles of an isosceles triangle are equal",
        "ms": "sudut tapak segi tiga sama kaki adalah sama",
        "ms-arab": "سودوت تاڤق سݢي تيݢ سام کاکي اداله سام",
        "ar": "زاويتا قاعدة المثلث متساوي الساقين متساويتان",
        "fr": "les angles à la base d'un triangle isocèle sont égaux",
        "es": "los ángulos de la base de un triángulo isósceles son iguales",
        "pt": "os ângulos da base de um triângulo isósceles são iguais",
        "hi": "समद्विबाहु त्रिभुज के आधार कोण बराबर होते हैं",
        "mr": "समद्विभुज त्रिकोणाचे पायाचे कोन समान असतात",
        "te": "సమద్విబాహు త్రిభుజం భూకోణాలు సమానం",
    },
    "equilateral_angles": {
        "en": "every angle of an equilateral triangle is 60°",
        "ms": "setiap sudut segi tiga sama sisi ialah 60°",
        "ms-arab": "ستياڤ سودوت سݢي تيݢ سام سيسي اياله 60 درجه",
        "ar": "كل زاوية في المثلث متساوي الأضلاع تساوي 60 درجة",
        "fr": "chaque angle d'un triangle équilatéral vaut 60°",
        "es": "cada ángulo de un triángulo equilátero mide 60°",
        "pt": "cada ângulo de um triângulo equilátero mede 60°",
        "hi": "समबाहु त्रिभुज का प्रत्येक कोण 60 डिग्री होता है",
        "mr": "समभुज त्रिकोणाचा प्रत्येक कोन 60 अंश असतो",
        "te": "సమబాహు త్రిభుజంలో ప్రతి కోణం 60 డిగ్రీలు",
    },
    "exterior_angle_triangle": {
        "en": "the exterior angle of a triangle equals the sum of the two opposite interior angles",
        "ms": "sudut peluaran segi tiga sama dengan jumlah dua sudut pedalaman bertentangan",
        "ms-arab": "سودوت ڤلوارن سݢي تيݢ سام دڠن جمله دوا سودوت ڤدالمن برتنتڠن",
        "ar": "الزاوية الخارجية للمثلث تساوي مجموع الزاويتين الداخليتين المقابلتين",
        "fr": "l'angle extérieur d'un triangle est égal à la somme des deux angles intérieurs opposés",
        "es": "el ángulo exterior de un triángulo es igual a la suma de los dos ángulos interiores opuestos",
        "pt": "o ângulo externo de um triângulo é igual à soma dos dois ângulos internos opostos",
        "hi": "त्रिभुज का बहिष्कोण दो सम्मुख अंतःकोणों के योग के बराबर होता है",
        "mr": "त्रिकोणाचा बाह्यकोन समोरील दोन आंतरकोनांच्या बेरजेइतका असतो",
        "te": "త్రిభుజం బాహ్య కోణం ఎదుటి రెండు అంతర కోణాల మొత్తానికి సమానం",
    },
    "alternate_angles": {
        "en": "alternate angles are equal",
        "ms": "sudut selang-seli adalah sama",
        "ms-arab": "سودوت سلڠ-سلي اداله سام",
        "ar": "الزاويتان المتبادلتان متساويتان",
        "fr": "les angles alternes-internes sont égaux",
        "es": "los ángulos alternos son iguales",
        "pt": "os ângulos alternos são iguais",
        "hi": "एकांतर कोण बराबर होते हैं",
        "mr": "व्युत्क्रम कोन समान असतात",
        "te": "ఏకాంతర కోణాలు సమానం",
    },
    "corresponding_angles": {
        "en": "corresponding angles are equal",
        "ms": "sudut sepadan adalah sama",
        "ms-arab": "سودوت سڤادن اداله سام",
        "ar": "الزاويتان المتناظرتان متساويتان",
        "fr": "les angles correspondants sont égaux",
        "es": "los ángulos correspondientes son iguales",
        "pt": "os ângulos correspondentes são iguais",
        "hi": "संगत कोण बराबर होते हैं",
        "mr": "संगत कोन समान असतात",
        "te": "సదృశ కోణాలు సమానం",
    },
    "cointerior_angles": {
        "en": "co-interior angles add up to 180°",
        "ms": "sudut pedalaman sesisi berjumlah 180°",
        "ms-arab": "سودوت ڤدالمن سسيسي برجمله 180 درجه",
        "ar": "مجموع الزاويتين الداخليتين في جهة واحدة يساوي 180 درجة",
        "fr": "les angles co-intérieurs font 180°",
        "es": "los ángulos conjugados internos suman 180°",
        "pt": "os ângulos colaterais internos somam 180°",
        "hi": "सह-अंतःकोणों का योग 180 डिग्री होता है",
        "mr": "सह-आंतरकोनांची बेरीज 180 अंश असते",
        "te": "సహ అంతర కోణాల మొత్తం 180 డిగ్రీలు",
    },
    "pythagoras": {
        "en": "Pythagoras' theorem",
        "ms": "teorem Pythagoras",
        "ms-arab": "تيوريم ڤيثاݢوراس",
        "ar": "نظرية فيثاغورس",
        "fr": "théorème de Pythagore",
        "es": "teorema de Pitágoras",
        "pt": "teorema de Pitágoras",
        "hi": "पाइथागोरस प्रमेय",
        "mr": "पायथागोरसचे प्रमेय",
        "te": "పైథాగరస్ సిద్ధాంతం",
    },
    "perimeter": {
        "en": "the perimeter is the sum of the sides",
        "ms": "perimeter ialah jumlah semua sisi",
        "ms-arab": "ڤريميتر اياله جمله سموا سيسي",
        "ar": "المحيط هو مجموع الأضلاع",
        "fr": "le périmètre est la somme des côtés",
        "es": "el perímetro es la suma de los lados",
        "pt": "o perímetro é a soma dos lados",
        "hi": "परिमाप सभी भुजाओं का योग होता है",
        "mr": "परिमिती म्हणजे सर्व बाजूंची बेरीज",
        "te": "చుట్టుకొలత అంటే భుజాల మొత్తం",
    },
    "area_rectangle": {
        "en": "area of a rectangle = length × width",
        "ms": "luas segi empat tepat = panjang × lebar",
        "ms-arab": "لواس سݢي امڤت تڤت = ڤنجڠ دارب ليبر",
        "ar": "مساحة المستطيل = الطول ضرب العرض",
        "fr": "aire d'un rectangle = longueur × largeur",
        "es": "área de un rectángulo = largo × ancho",
        "pt": "área de um retângulo = comprimento × largura",
        "hi": "आयत का क्षेत्रफल = लंबाई × चौड़ाई",
        "mr": "आयताचे क्षेत्रफळ = लांबी × रुंदी",
        "te": "దీర్ఘచతురస్రం వైశాల్యం = పొడవు × వెడల్పు",
    },
    "area_triangle": {
        "en": "area of a triangle = ½ × base × height",
        "ms": "luas segi tiga = ½ × tapak × tinggi",
        "ms-arab": "لواس سݢي تيݢ = ستڠه دارب تاڤق دارب تيڠݢي",
        "ar": "مساحة المثلث = نصف ضرب القاعدة ضرب الارتفاع",
        "fr": "aire d'un triangle = ½ × base × hauteur",
        "es": "área de un triángulo = ½ × base × altura",
        "pt": "área de um triângulo = ½ × base × altura",
        "hi": "त्रिभुज का क्षेत्रफल = 1/2 × आधार × ऊँचाई",
        "mr": "त्रिकोणाचे क्षेत्रफळ = 1/2 × पाया × उंची",
        "te": "త్రిభుజం వైశాల్యం = 1/2 × భూమి × ఎత్తు",
    },
    "area_parallelogram": {
        "en": "area of a parallelogram = base × height",
        "ms": "luas segi empat selari = tapak × tinggi",
        "ms-arab": "لواس سݢي امڤت سلاري = تاڤق دارب تيڠݢي",
        "ar": "مساحة متوازي الأضلاع = القاعدة ضرب الارتفاع",
        "fr": "aire d'un parallélogramme = base × hauteur",
        "es": "área de un paralelogramo = base × altura",
        "pt": "área de um paralelogramo = base × altura",
        "hi": "समांतर चतुर्भुज का क्षेत्रफल = आधार × ऊँचाई",
        "mr": "समांतरभुज चौकोनाचे क्षेत्रफळ = पाया × उंची",
        "te": "సమాంతర చతుర్భుజం వైశాల్యం = భూమి × ఎత్తు",
    },
    "area_trapezium": {
        "en": "area of a trapezium = ½ × (a + b) × h",
        "ms": "luas trapezium = ½ × (a + b) × h",
        "ms-arab": "لواس تراڤيزيوم = ستڠه دارب (a + b) دارب h",
        "ar": "مساحة شبه المنحرف = نصف ضرب (a + b) ضرب h",
        "fr": "aire d'un trapèze = ½ × (a + b) × h",
        "es": "área de un trapecio = ½ × (a + b) × h",
        "pt": "área de um trapézio = ½ × (a + b) × h",
        "hi": "समलंब का क्षेत्रफल = 1/2 × (a + b) × h",
        "mr": "समलंब चौकोनाचे क्षेत्रफळ = 1/2 × (a + b) × h",
        "te": "ట్రెపీజియం వైశాల్యం = 1/2 × (a + b) × h",
    },
    "area_circle": {
        "en": "area of a circle = πr²",
        "ms": "luas bulatan = πr²",
        "ms-arab": "لواس بولتن = ڤاي دارب r دارب r",
        "ar": "مساحة الدائرة = باي ضرب نصف القطر ضرب نصف القطر",
        "fr": "aire d'un disque = πr²",
        "es": "área de un círculo = πr²",
        "pt": "área de um círculo = πr²",
        "hi": "वृत्त का क्षेत्रफल = पाई × r × r",
        "mr": "वर्तुळाचे क्षेत्रफळ = पाय × r × r",
        "te": "వృత్తం వైశాల్యం = పై × r × r",
    },
    "circumference": {
        "en": "circumference = 2πr",
        "ms": "lilitan = 2πr",
        "ms-arab": "ليليتن = 2 دارب ڤاي دارب r",
        "ar": "محيط الدائرة = 2 ضرب باي ضرب نصف القطر",
        "fr": "circonférence = 2πr",
        "es": "circunferencia = 2πr",
        "pt": "circunferência = 2πr",
        "hi": "परिधि = 2 × पाई × r",
        "mr": "परीघ = 2 × पाय × r",
        "te": "పరిధి = 2 × పై × r",
    },
    "area_composite": {
        "en": "the area of a compound shape is the sum of its parts",
        "ms": "luas bentuk gabungan ialah jumlah luas bahagian-bahagiannya",
        "ms-arab": "لواس بنتوق ݢابوڠن اياله جمله لواس بهاݢين-بهاݢينڽ",
        "ar": "مساحة الشكل المركب هي مجموع مساحات أجزائه",
        "fr": "l'aire d'une figure composée est la somme des aires de ses parties",
        "es": "el área de una figura compuesta es la suma de las áreas de sus partes",
        "pt": "a área de uma figura composta é a soma das áreas das suas partes",
        "hi": "संयुक्त आकृति का क्षेत्रफल उसके भागों के क्षेत्रफलों का योग होता है",
        "mr": "संयुक्त आकृतीचे क्षेत्रफळ तिच्या भागांच्या क्षेत्रफळांची बेरीज असते",
        "te": "సంయుక్త ఆకారం వైశాల్యం దాని భాగాల వైశాల్యాల మొత్తం",
    },
}


def reason_text(theorem: str, lang: str | None) -> str:
    """A theorem's reason in the lesson language; English when the language
    or the theorem is unknown here; "" when the id is unknown everywhere."""
    table = REASONS.get(theorem) or {}
    return table.get(norm_lang(lang)) or table.get("en") or ""


def words_for(lang: str | None) -> dict:
    """The spoken-notation table for a language: every key present, the
    language's own words over English."""
    code = norm_lang(lang)
    # the "all squared" idiom is not inherited: a language without it sets
    # the bracketed base off with a comma instead (maths.speech)
    table = dict(_EN) if code == "en" else {k: v for k, v in _EN.items() if not k.endswith("_all")}
    table.update(WORDS.get(code, {}))
    return table


__all__ = ["LANGS", "BOARD", "WORDS", "norm_lang", "board_text", "words_for"]
