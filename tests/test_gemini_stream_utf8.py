"""A streamed Gemini reply keeps its non-ASCII characters (2026-10-30: the books runner's review data read
"x â 7" for "x − 7"). Vertex answers :streamGenerateContent?alt=sse as text/event-stream without a charset, which
makes requests decode it as ISO-8859-1; the client must read it as UTF-8."""
import io
import json

import requests

from shared.gemini_client import GeminiClient


def _sse_response(text: str) -> requests.Response:
    chunk = {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}],
             "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 4}}
    res = requests.Response()
    res.status_code = 200
    res.headers["Content-Type"] = "text/event-stream"          # no charset, as Vertex sends it
    res.raw = io.BytesIO(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
    res.encoding = requests.utils.get_encoding_from_headers(res.headers)    # what requests itself would set: ISO-8859-1
    return res


def test_a_streamed_reply_keeps_minus_signs_superscripts_and_arabic():
    text = "x − 7 · 5x² · Ø 3 mm · الخلية"
    out = GeminiClient._drain_stream(_sse_response(text))
    got = out["candidates"][0]["content"]["parts"][0]["text"]
    assert got == text, got
