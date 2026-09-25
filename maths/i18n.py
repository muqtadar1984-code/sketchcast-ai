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
