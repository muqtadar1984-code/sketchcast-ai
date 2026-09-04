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
timepoints returned, marks dropped, clips whose duration had to be estimated.

QUOTA. The API allows 1,000 requests a minute per project. Chirp is one
request per SENTENCE, the worker renders eight lessons at once and each
lesson's segments on a four-thread pool, so nothing stopped 32 threads from
asking for ~1,500 requests a minute and turning a whole batch of lessons free
through the pre-flight. A process-wide sliding-window limiter
(GOOGLE_TTS_RPM, default 600) now paces every thread in the process.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import random
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from ..chunks import (billable_chars, chunks, family, interpolate_words, language_code,
                      ssml_for, words_from_marks, words_of)

logger = logging.getLogger("shared.tts.google")

_ENDPOINT = "https://texttospeech.googleapis.com/v1beta1/text:synthesize"
_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)")
_PARALLEL = int(os.getenv("GOOGLE_TTS_PARALLEL", "4"))
_TRIES = 4
_RETRYABLE = frozenset({429, 500, 502, 503, 504})

_creds = None
_creds_lock = threading.Lock()
# Set the first time Google answers 403 to the billing-project header — some
# service accounts lack serviceusage.services.use on the project, and the
# Vertex path this worker already runs never needed the header.
_skip_user_project = False


class GoogleTTSError(RuntimeError):
    """An answer from Google that ended the call: status, Google's own
    error.status/message (RESOURCE_EXHAUSTED, PERMISSION_DENIED,
    INVALID_ARGUMENT …), and whether waiting could have helped."""

    def __init__(self, status: int, detail: str, retryable: bool):
        super().__init__(f"Google TTS {status}: {detail}" if status else f"Google TTS: {detail}")
        self.status = status
        self.detail = detail
        self.retryable = retryable


from shared.ratelimit import RateLimiter as _RateLimiter  # noqa: E402 — shared with the image path


_limiter = _RateLimiter(int(os.getenv("GOOGLE_TTS_RPM", "600")))


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


def _error_detail(r) -> str:
    """Google's error.status + error.message when the body is JSON, else the
    first 300 characters — the difference between 'quota' and 'permission' is
    the whole diagnosis, and str(HTTPError) carried neither."""
    try:
        e = (r.json() or {}).get("error") or {}
        s = " ".join(str(x) for x in (e.get("status"), e.get("message")) if x)
        if s:
            return s
    except Exception:  # noqa: BLE001
        pass
    return (getattr(r, "text", "") or "")[:300]


def _retry_after(r) -> float | None:
    try:
        v = (getattr(r, "headers", None) or {}).get("Retry-After")
        return min(60.0, float(v)) if v else None
    except (TypeError, ValueError):
        return None


def _jitter(delay: float) -> float:
    return delay * (0.5 + random.random())


def _post(body: dict) -> dict:
    """One synthesize call. 429/5xx and network errors are retried with
    jittered backoff (Retry-After honoured); every other 4xx raises at once
    with Google's error detail — a malformed request will not get better by
    waiting, and the previous shape retried 400/401/403 four times because
    HTTPError is a RequestException. A 403 on the billing-project header is
    retried once without the header, then remembered."""
    import requests
    global _skip_user_project
    token, project = _token()
    delay = 2.0
    last: Exception | None = None
    for attempt in range(_TRIES):
        headers = {"Authorization": f"Bearer {token}"}
        if project and not _skip_user_project:
            headers["x-goog-user-project"] = project
        _limiter.acquire()
        try:
            r = requests.post(_ENDPOINT, headers=headers, json=body, timeout=120)
        except requests.RequestException as exc:  # network-level only
            last = exc
            if attempt < _TRIES - 1:
                time.sleep(_jitter(delay))
                delay *= 2
                continue
            break
        if r.status_code < 400:
            return r.json()
        detail = _error_detail(r)
        if (r.status_code == 403 and project and not _skip_user_project
                and ("serviceusage" in detail.lower() or "user project" in detail.lower()
                     or "USER_PROJECT" in detail)):
            logger.warning("Google TTS 403 on x-goog-user-project (%s); retrying without the header", detail)
            _skip_user_project = True
            last = GoogleTTSError(403, detail, False)
            continue
        if r.status_code in _RETRYABLE and attempt < _TRIES - 1:
            wait = _retry_after(r) or _jitter(delay)
            logger.warning("Google TTS %s (%s); retrying in %.1fs (%d/%d)",
                           r.status_code, detail, wait, attempt + 1, _TRIES - 1)
            last = GoogleTTSError(r.status_code, detail, True)
            time.sleep(wait)
            delay *= 2
            continue
        raise GoogleTTSError(r.status_code, detail, r.status_code in _RETRYABLE)
    raise GoogleTTSError(0, f"failed after {_TRIES} attempts: {last}", True)


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


def _estimate_secs(piece: str) -> float:
    """When ffmpeg cannot read a clip's duration: ~2.7 words a second. Better
    than 0.0, which collapsed the sentence to an instant and pulled every
    later sentence early by its true length."""
    return max(0.6, len(words_of(piece)) / 2.7)


def _concat_entry(p: Path) -> str:
    """One line of an ffmpeg concat list. The demuxer's single-quoted form
    escapes an apostrophe as '\\'' — a storage path with one in it used to
    break the list and turn every multi-sentence segment into an Edge
    fallback on that machine."""
    return "file '" + p.as_posix().replace("'", "'\\''") + "'\n"


def _concat(parts: list[Path], out: Path, ffmpeg: str) -> None:
    """Same mechanism the dialogue path uses: ffmpeg's concat demuxer, stream
    copy. Google's clips share one encoder, so no re-encode is needed."""
    if len(parts) == 1:
        shutil.copyfile(parts[0], out)
        return
    lst = out.with_suffix(".concat.txt")
    lst.write_text("".join(_concat_entry(p) for p in parts), encoding="utf-8")
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


def _synth_all(ssmls: list[tuple[str, int, int]], ref_voice: str, lang: str,
               marks: bool, fam: str) -> list[tuple[bytes, list[dict]]]:
    """Every chunk on the pool. On the first failure the chunks not yet started
    are cancelled (a 40-sentence segment whose third sentence failed used to
    make — and pay for — the other 37 before falling back), the chunks Google
    already answered are recorded against the runaway cap (Google billed
    them even though the segment is about to fall back to Edge), and the
    failure is raised."""
    results: list = [None] * len(ssmls)
    failed: Exception | None = None
    done_chars = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, _PARALLEL)) as ex:
        futs = {ex.submit(_synth_chunk, s, ref_voice, lang, marks): i for i, (s, _, _) in enumerate(ssmls)}
        for fut in concurrent.futures.as_completed(futs):
            i = futs[fut]
            try:
                results[i] = fut.result()
                done_chars += billable_chars(ssmls[i][0])
            except concurrent.futures.CancelledError:
                continue
            except Exception as exc:  # noqa: BLE001
                if failed is None:
                    failed = exc
                    for f in futs:
                        f.cancel()
    if failed is not None:
        if done_chars:
            from .. import cost
            cost.record(done_chars, "google", fam)
            logger.warning("Google TTS: %d chars were billed on the chunks that succeeded before the failure",
                           done_chars)
        raise failed
    return results


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

    results = _synth_all(ssmls, ref_voice, lang, marks, fam)

    ffmpeg = _ffmpeg()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmpdir = Path(tempfile.mkdtemp(prefix="gtts_", dir=str(out_path.parent)))
    words: list[dict] = []
    cursor = 0.0
    dropped = 0
    estimated = 0
    billable = 0
    try:
        clip_paths: list[Path] = []
        for i, ((audio, tps), piece, (ssml, first_mark, n_words)) in enumerate(zip(results, pieces, ssmls)):
            p = tmpdir / f"c{i:04d}.mp3"
            p.write_bytes(audio)
            clip_paths.append(p)
            dur = _duration(p, ffmpeg)
            if dur <= 0.0:
                dur = _estimate_secs(piece)
                estimated += 1
                logger.warning("Google TTS: clip %d duration unreadable; estimated %.1fs", i, dur)
            # Google exempts <mark> from the character count; everything else
            # in the SSML is billed.
            billable += billable_chars(ssml)
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
            "duration_estimated": estimated, "audio_secs": round(cursor, 3)}
