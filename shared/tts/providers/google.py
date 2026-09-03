"""Google Cloud Text-to-Speech provider — premium, gated + metered.

Transport is raw REST with the worker's Application Default Credentials, the
same chain spike/scene_engine/raster_assets.py uses for Vertex: no API key
(the org forbids them) and no new dependency. Bills the credited GCP project.

Contract matches the other providers: ``synthesize(text, out_path, ref_voice,
boundaries_out=None)`` writes an MP3 and, when asked, a ``words.json`` in the
exact shape Edge writes and timing.py reads — ``[{"t": sec, "w": word}]``.

Two paths, chosen by the voice family (see shared/tts/chunks.py for why):
  classic (Standard / WaveNet / Neural2): whole-sentence chunks under the
      request cap, a <mark> before every word, timepoints back → exact words.
  chirp (Chirp 3 HD): one sentence per request, each clip measured with
      ffmpeg, clips concatenated — sentence starts exact, words interpolated.

Returns a small stats dict the caller can fold into its report: billable
characters (SSML minus marks, which Google exempts), requests made,
timepoints returned, marks dropped.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from ..chunks import (chunks, family, interpolate_words, language_code, ssml_for,
                      words_from_marks)

logger = logging.getLogger("shared.tts.google")

_ENDPOINT = "https://texttospeech.googleapis.com/v1beta1/text:synthesize"
_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)")
_PARALLEL = int(os.getenv("GOOGLE_TTS_PARALLEL", "4"))
_TRIES = 4

_creds = None
_creds_lock = __import__("threading").Lock()


def _token() -> tuple[str, str]:
    """(bearer token, project) from ADC; refreshed when expired.

    Under a lock, and the token is read INSIDE it. Chunks are synthesized on a
    thread pool, and the first live smoke returned 401: one thread assigned
    the credentials object while another read its still-empty token and sent
    'Bearer None'. Single-threaded, the same code returned 200."""
    global _creds
    from shared.claude_client import _ensure_google_credentials
    import google.auth
    import google.auth.transport.requests
    with _creds_lock:
        if _creds is None:
            _ensure_google_credentials()
            _creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        if not _creds.valid:
            _creds.refresh(google.auth.transport.requests.Request())
        token = _creds.token
        project = os.getenv("VERTEX_PROJECT_ID", "").strip() or getattr(_creds, "project_id", "") or ""
    if not token:
        raise RuntimeError("google tts: no access token after refresh")
    return token, project


def _post(body: dict) -> dict:
    """One synthesize call with backoff on 429/5xx. Other 4xx raise at once —
    a malformed request will not get better by waiting."""
    import requests
    token, project = _token()
    headers = {"Authorization": f"Bearer {token}"}
    if project:
        headers["x-goog-user-project"] = project
    delay = 2.0
    last: Exception | None = None
    for attempt in range(_TRIES):
        try:
            r = requests.post(_ENDPOINT, headers=headers, json=body, timeout=120)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < _TRIES - 1:
                logger.warning("Google TTS %s; retrying in %.0fs (%d/%d)", r.status_code, delay, attempt + 1, _TRIES - 1)
                time.sleep(delay)
                delay *= 2
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:  # network-level; retry too
            last = exc
            if attempt < _TRIES - 1:
                time.sleep(delay)
                delay *= 2
    raise RuntimeError(f"Google TTS failed after {_TRIES} attempts: {last}")


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _duration(path: Path, ffmpeg: str) -> float:
    proc = subprocess.run([ffmpeg, "-i", str(path)], capture_output=True, text=True)
    m = _DUR_RE.search(proc.stderr or "")
    if not m:
        return 0.0
    h, mnt, s = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + float(s)


def _concat(parts: list[Path], out: Path, ffmpeg: str) -> None:
    """Same mechanism the dialogue path uses: ffmpeg's concat demuxer, stream
    copy. Google's clips share one encoder, so no re-encode is needed."""
    if len(parts) == 1:
        shutil.copyfile(parts[0], out)
        return
    lst = out.with_suffix(".concat.txt")
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
    subprocess.run([ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                    "-c", "copy", str(out)], capture_output=True)
    lst.unlink(missing_ok=True)
    if not out.exists() or out.stat().st_size == 0:
        raise RuntimeError("google tts concat produced no audio")


def _synth_chunk(ssml: str, ref_voice: str, lang: str, marks: bool) -> tuple[bytes, list[dict]]:
    import base64
    body = {"input": {"ssml": ssml},
            "voice": {"languageCode": lang, "name": ref_voice},
            "audioConfig": {"audioEncoding": "MP3"}}
    if marks:
        body["enableTimePointing"] = ["SSML_MARK"]
    j = _post(body)
    audio = base64.b64decode(j.get("audioContent") or "")
    if not audio:
        raise RuntimeError("google tts returned no audio")
    return audio, list(j.get("timepoints") or [])


def synthesize(text: str, out_path: Path, ref_voice: str,
               boundaries_out: Path | None = None) -> dict:
    """Render `text` (may carry <break/> markup) to MP3 at out_path. When
    `boundaries_out` is given, words.json is written beside it."""
    clean = (text or "").strip()
    if not clean:
        raise ValueError("empty text for TTS")
    fam = family(ref_voice)
    lang = language_code(ref_voice)
    marks = fam == "classic" and boundaries_out is not None
    pieces = chunks(clean, one_sentence_each=(fam == "chirp"), marks=marks)
    if not pieces:
        raise ValueError("no sentences to synthesize")

    # number marks across chunks so names never collide when chunks are
    # synthesized in parallel
    ssmls: list[tuple[str, int, int]] = []       # (ssml, first_mark, n_words)
    first = 0
    for piece in pieces:
        s, n = ssml_for(piece, marks=marks, mark_offset=first)
        ssmls.append((s, first, n))
        first += n

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, _PARALLEL)) as ex:
        results = list(ex.map(lambda t: _synth_chunk(t[0], ref_voice, lang, marks), ssmls))

    ffmpeg = _ffmpeg()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmpdir = Path(tempfile.mkdtemp(prefix="gtts_", dir=str(out_path.parent)))
    words: list[dict] = []
    cursor = 0.0
    dropped = 0
    billable = 0
    try:
        clip_paths: list[Path] = []
        for i, ((audio, tps), piece, (ssml, first_mark, n_words)) in enumerate(zip(results, pieces, ssmls)):
            p = tmpdir / f"c{i:04d}.mp3"
            p.write_bytes(audio)
            clip_paths.append(p)
            dur = _duration(p, ffmpeg)
            # Google exempts <mark> from the character count; everything else
            # in the SSML is billed.
            billable += len(re.sub(r'<mark name="w\d+"/>', "", ssml))
            if boundaries_out is not None:
                if marks:
                    ws, miss = words_from_marks(piece, tps, chunk_start=cursor,
                                                chunk_duration=dur, mark_offset=first_mark)
                    dropped += miss
                else:
                    ws = interpolate_words(piece, cursor, dur)
                words.extend(ws)
            cursor += dur
        _concat(clip_paths, out_path, ffmpeg)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError("google tts produced no audio")
    if boundaries_out is not None and words:
        try:
            Path(boundaries_out).write_text(json.dumps(words, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass  # timing degrades to char-midpoint; audio is intact
    if dropped:
        logger.warning("Google TTS: %d mark(s) returned no timepoint; interpolated", dropped)
    return {"provider": "google", "family": fam, "requests": len(pieces), "chars": billable,
            "timepoints": sum(len(t) for _, t in results), "marks_dropped": dropped,
            "audio_secs": round(cursor, 3)}
