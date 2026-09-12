"""Offline: a teacher-route deck in four scripts, to LOOK at, not to test.

Builds the deck a teacher gets beside the video (video-shaped script: headings,
narration, visuals, no points) plus the authored extras, in English, Arabic,
Hindi and Telugu. No model call. Output in spike_out/routes/.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from agent5_slides import deck_generator as dg           # noqa: E402
from agent5_slides.deck_storyboard import storyboard, summarise  # noqa: E402
from tests.test_deck_routes import ANALYSIS_WITH_DEFS, _video_script  # noqa: E402

OUT = ROOT / "spike_out" / "routes"

EN = {}
AR = {"title": "الخلية", "h1": "ما هي الخلية؟", "n1": "كل كائن حي مبني من خلايا. لننظر داخل خلية واحدة ونتعرف على أجزائها الرئيسية وما تقوم به كل منها.",
      "h2": "كيف تُطلق الطاقة", "n2": "يلتقي الجلوكوز والأكسجين في الميتوكوندريا.", "f1": "الجلوكوز", "f2": "الأكسجين", "f3": "الطاقة", "cap": "في الميتوكوندريا",
      "h3": "أي جزء يحمل الحمض النووي؟", "n3": "فكّر قبل أن تجيب.", "o1": "الغشاء", "o2": "النواة", "o3": "الميتوكوندريا",
      "h4": "ما يجب تذكّره", "n4": "ثلاثة أشياء نحتفظ بها.", "t1": "الخلية هي وحدة الحياة", "t2": "النواة تحمل الحمض النووي"}
HI = {"title": "कोशिका", "h1": "कोशिका क्या है?", "n1": "हर जीवित वस्तु कोशिकाओं से बनी है। आइए एक कोशिका के अंदर देखें और उसके मुख्य भागों को पहचानें।",
      "h2": "ऊर्जा कैसे मुक्त होती है", "n2": "ग्लूकोज़ और ऑक्सीजन माइटोकॉन्ड्रिया में मिलते हैं।", "f1": "ग्लूकोज़", "f2": "ऑक्सीजन", "f3": "ऊर्जा", "cap": "माइटोकॉन्ड्रिया में",
      "h3": "कौन सा भाग डीएनए रखता है?", "n3": "उत्तर देने से पहले सोचें।", "o1": "झिल्ली", "o2": "केन्द्रक", "o3": "माइटोकॉन्ड्रिया",
      "h4": "क्या याद रखें", "n4": "तीन बातें।", "t1": "कोशिका जीवन की इकाई है", "t2": "केन्द्रक डीएनए रखता है"}
TE = {"title": "కణం", "h1": "కణం అంటే ఏమిటి?", "n1": "ప్రతి జీవి కణాలతో నిర్మించబడింది. ఒక కణం లోపల చూద్దాం మరియు దాని ముఖ్య భాగాలను గుర్తిద్దాం.",
      "h2": "శక్తి ఎలా విడుదల అవుతుంది", "n2": "గ్లూకోజ్ మరియు ఆక్సిజన్ మైటోకాండ్రియాలో కలుస్తాయి.", "f1": "గ్లూకోజ్", "f2": "ఆక్సిజన్", "f3": "శక్తి", "cap": "మైటోకాండ్రియాలో",
      "h3": "ఏ భాగం DNA ను కలిగి ఉంటుంది?", "n3": "సమాధానం చెప్పే ముందు ఆలోచించండి.", "o1": "పొర", "o2": "కేంద్రకం", "o3": "మైటోకాండ్రియా",
      "h4": "గుర్తుంచుకోవాల్సినవి", "n4": "మూడు విషయాలు.", "t1": "కణం జీవితం యొక్క యూనిట్", "t2": "కేంద్రకం DNA ను కలిగి ఉంటుంది"}

EXTRAS = {
    "en": {"objectives": ["Describe the cell as the basic unit of life.", "Explain how the mitochondria release energy."],
           "misconceptions": [{"misconception": "All cells are the same size.", "correction": "Cells vary enormously; a nerve cell can be a metre long."}],
           "worked_examples": [{"problem": "A cell has no nucleus. Prokaryote or eukaryote?", "solution": "Prokaryote: only eukaryotic cells keep their DNA inside a nucleus."}]},
    "ar": {"objectives": ["وصف الخلية بوصفها الوحدة الأساسية للحياة.", "شرح كيفية إطلاق الميتوكوندريا للطاقة."],
           "misconceptions": [{"misconception": "كل الخلايا بالحجم نفسه.", "correction": "تختلف الخلايا كثيراً؛ قد يبلغ طول الخلية العصبية متراً."}],
           "worked_examples": []},
    "hi": {"objectives": ["कोशिका को जीवन की मूल इकाई के रूप में वर्णित करना।"],
           "misconceptions": [{"misconception": "सभी कोशिकाएँ एक ही आकार की होती हैं।", "correction": "कोशिकाएँ बहुत भिन्न होती हैं; एक तंत्रिका कोशिका एक मीटर लंबी हो सकती है।"}],
           "worked_examples": []},
    "te": {"objectives": ["కణాన్ని జీవితం యొక్క ప్రాథమిక యూనిట్‌గా వివరించండి."],
           "misconceptions": [{"misconception": "అన్ని కణాలు ఒకే పరిమాణంలో ఉంటాయి.", "correction": "కణాలు చాలా భిన్నంగా ఉంటాయి; ఒక నాడీ కణం ఒక మీటర్ పొడవు ఉండవచ్చు."}],
           "worked_examples": []},
}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for code, text, direction in (("en", EN, "ltr"), ("ar", AR, "rtl"), ("hi", HI, "ltr"), ("te", TE, "ltr")):
        # Objectives etc. in the same language: the extras above are per code.
        model = dg.model_from_script(ANALYSIS_WITH_DEFS if code == "en" else {}, _video_script(text), EXTRAS[code], language=code)
        slides = storyboard(model)
        path = dg.build_lesson_deck(model, OUT / f"teacher_{code}.pptx", direction=direction)
        print(f"{code}: {len(slides):>2} slides  {summarise(slides)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
