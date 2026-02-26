/**
 * SketchCast Player Engine
 *
 * Synchronises master MP3 audio with Rough.js sketch animations,
 * handles pause-for-question interactions, and connects to Agent 8
 * for live Q&A.
 *
 * Expects three globals injected by player.html:
 *   TIMELINE  — unified timeline JSON (from sync_engine)
 *   ANIMATIONS — { segment_id: roughjs_html_string }
 *   AUDIO_SOURCE — data URI or file path for master MP3
 */

(function () {
  "use strict";

  // ── DOM refs ──────────────────────────────────────────────
  const playBtn = document.getElementById("play-btn");
  const timeDisplay = document.getElementById("time-display");
  const progressBar = document.getElementById("progress-bar");
  const progressContainer = document.getElementById("progress-container");
  const progressDots = document.getElementById("progress-dots");
  const segTitle = document.getElementById("segment-title");
  const segIcon = document.getElementById("segment-icon");
  const segCounter = document.getElementById("segment-counter");
  const volumeSlider = document.getElementById("volume-slider");
  const volumeIcon = document.getElementById("volume-icon");
  const animFrame = document.getElementById("animation-frame");
  const blankCanvas = document.getElementById("blank-canvas");
  const questionOverlay = document.getElementById("question-overlay");
  const questionInput = document.getElementById("question-input");
  const askBtn = document.getElementById("ask-btn");
  const continueBtn = document.getElementById("continue-btn");
  const resumeBtn = document.getElementById("resume-btn");
  const answerArea = document.getElementById("answer-area");
  const answerText = document.getElementById("answer-text");
  const questionInputArea = document.getElementById("question-input-area");

  // ── State ─────────────────────────────────────────────────
  const audio = new Audio();
  let isPlaying = false;
  let currentSegmentIdx = -1;
  let pausedForQuestion = false;
  let animationShowing = false;

  const segments = TIMELINE.segments || [];
  const totalDuration = TIMELINE.total_duration_seconds || 0;

  const TYPE_ICONS = {
    hook: "\uD83E\uDE9D",
    activate: "\u26A1",
    explore: "\uD83D\uDD0D",
    question_hook: "\u2753",
    synthesis: "\uD83C\uDFAF",
    preview: "\uD83D\uDC49",
  };

  // ── Init ──────────────────────────────────────────────────
  function init() {
    audio.src = AUDIO_SOURCE;
    audio.preload = "auto";
    audio.volume = parseFloat(volumeSlider.value);

    // Build progress dots for segment boundaries
    buildProgressDots();

    // Event listeners
    playBtn.addEventListener("click", togglePlay);
    volumeSlider.addEventListener("input", onVolumeChange);
    volumeIcon.addEventListener("click", toggleMute);
    progressContainer.addEventListener("click", onProgressClick);
    continueBtn.addEventListener("click", onContinue);
    askBtn.addEventListener("click", onAsk);
    resumeBtn.addEventListener("click", onResume);
    questionInput.addEventListener("keydown", function (e) {
      if (e.key === "Enter") onAsk();
    });

    audio.addEventListener("timeupdate", onTimeUpdate);
    audio.addEventListener("ended", onAudioEnded);
    audio.addEventListener("loadedmetadata", function () {
      updateTimeDisplay(0, audio.duration || totalDuration);
    });

    updateTimeDisplay(0, totalDuration);
    segCounter.textContent = segments.length + " sections";
  }

  // ── Playback controls ────────────────────────────────────
  function togglePlay() {
    if (pausedForQuestion) return;
    if (isPlaying) {
      audio.pause();
      isPlaying = false;
      playBtn.innerHTML = "&#9654;";
    } else {
      audio.play().catch(function () {});
      isPlaying = true;
      playBtn.innerHTML = "&#10074;&#10074;";
    }
  }

  function onVolumeChange() {
    audio.volume = parseFloat(volumeSlider.value);
    volumeIcon.innerHTML = audio.volume === 0 ? "&#128263;" : "&#128266;";
  }

  function toggleMute() {
    if (audio.volume > 0) {
      volumeSlider.dataset.prev = audio.volume;
      audio.volume = 0;
      volumeSlider.value = 0;
      volumeIcon.innerHTML = "&#128263;";
    } else {
      audio.volume = parseFloat(volumeSlider.dataset.prev || 0.8);
      volumeSlider.value = audio.volume;
      volumeIcon.innerHTML = "&#128266;";
    }
  }

  function onProgressClick(e) {
    if (pausedForQuestion) return;
    var rect = progressContainer.getBoundingClientRect();
    var pct = (e.clientX - rect.left) / rect.width;
    var dur = audio.duration || totalDuration;
    audio.currentTime = pct * dur;
  }

  // ── Time update loop ─────────────────────────────────────
  function onTimeUpdate() {
    var t = audio.currentTime;
    var dur = audio.duration || totalDuration;

    // Update progress bar
    var pct = dur > 0 ? (t / dur) * 100 : 0;
    progressBar.style.width = pct + "%";

    updateTimeDisplay(t, dur);

    // Find current segment
    var newIdx = findSegmentAt(t);
    if (newIdx !== currentSegmentIdx) {
      currentSegmentIdx = newIdx;
      onSegmentChange(newIdx);
    }

    // Check for animation triggers
    checkAnimationTriggers(t);

    // Check for pause points
    checkPausePoints(t);
  }

  function findSegmentAt(t) {
    for (var i = 0; i < segments.length; i++) {
      if (t >= segments[i].audio_start && t < segments[i].audio_end) {
        return i;
      }
    }
    // If past all segments, return last
    if (segments.length > 0 && t >= segments[segments.length - 1].audio_start) {
      return segments.length - 1;
    }
    return 0;
  }

  function onSegmentChange(idx) {
    if (idx < 0 || idx >= segments.length) return;
    var seg = segments[idx];
    var icon = TYPE_ICONS[seg.type] || "\u2022";
    segIcon.textContent = icon;

    // Show segment text preview
    var text = seg.segment_text || seg.type;
    segTitle.textContent = text.substring(0, 80) + (text.length > 80 ? "..." : "");
    segCounter.textContent = "Section " + (idx + 1) + " of " + segments.length;
  }

  // ── Animation sync ────────────────────────────────────────
  var activeAnimSegId = null;

  function checkAnimationTriggers(t) {
    for (var i = 0; i < segments.length; i++) {
      var seg = segments[i];
      if (!seg.has_animation || !seg.animation_trigger) continue;

      // Trigger animation when current time crosses trigger point
      if (
        t >= seg.animation_trigger &&
        t < seg.audio_end + 0.5 &&
        activeAnimSegId !== seg.segment_id
      ) {
        showAnimation(seg.segment_id);
        return;
      }
    }

    // If we're in a segment without animation, show blank
    if (currentSegmentIdx >= 0 && currentSegmentIdx < segments.length) {
      var curSeg = segments[currentSegmentIdx];
      if (!curSeg.has_animation && animationShowing) {
        showBlank();
      }
    }
  }

  function showAnimation(segmentId) {
    var htmlContent = ANIMATIONS[segmentId];
    if (!htmlContent) {
      showBlank();
      return;
    }

    activeAnimSegId = segmentId;
    animationShowing = true;
    blankCanvas.style.display = "none";
    animFrame.style.display = "block";

    // Write animation HTML into iframe
    animFrame.srcdoc = htmlContent;
  }

  function showBlank() {
    activeAnimSegId = null;
    animationShowing = false;
    animFrame.style.display = "none";
    blankCanvas.style.display = "flex";
    animFrame.srcdoc = "";
  }

  // ── Pause for question ────────────────────────────────────
  var handledPauses = {};

  function checkPausePoints(t) {
    // Interjection disabled — play through question segments without pausing
    return;
  }

  function triggerQuestionPause(seg) {
    audio.pause();
    isPlaying = false;
    pausedForQuestion = true;
    playBtn.innerHTML = "&#9654;";

    // Show question overlay
    questionOverlay.style.display = "flex";
    questionInputArea.style.display = "flex";
    answerArea.style.display = "none";
    questionInput.value = "";
    questionInput.focus();
  }

  function onContinue() {
    questionOverlay.style.display = "none";
    pausedForQuestion = false;
    audio.play().catch(function () {});
    isPlaying = true;
    playBtn.innerHTML = "&#10074;&#10074;";
  }

  function onAsk() {
    var q = questionInput.value.trim();
    if (!q) {
      onContinue();
      return;
    }

    // Show answer area, hide input
    questionInputArea.style.display = "none";
    answerArea.style.display = "block";
    answerText.textContent = "Thinking...";

    // Call Agent 8 endpoint
    var scriptId = TIMELINE.script_id || "";
    var segId = segments[currentSegmentIdx]
      ? segments[currentSegmentIdx].segment_id
      : "";
    var bookId = TIMELINE.book_id || "";
    var chapterNum = TIMELINE.chapter_num || 0;

    // Try SSE streaming from Agent 8
    try {
      var url =
        "/ask/" + scriptId + "/" + segId +
        "?q=" + encodeURIComponent(q) +
        "&book_id=" + encodeURIComponent(bookId) +
        "&chapter_num=" + chapterNum;

      var eventSource = new EventSource(url);
      answerText.textContent = "";

      eventSource.addEventListener("text", function (e) {
        answerText.textContent += e.data;
      });

      eventSource.addEventListener("audio", function (e) {
        // Audio chunk handling (base64 encoded)
        try {
          var audioChunk = atob(e.data);
          // Play audio chunk if Web Audio API available
        } catch (err) {
          // Ignore audio decode errors
        }
      });

      eventSource.addEventListener("complete", function () {
        eventSource.close();
      });

      eventSource.addEventListener("error", function () {
        if (answerText.textContent === "") {
          answerText.textContent =
            "I could not connect to the Q&A service right now. " +
            "Tap Resume to continue the episode.";
        }
        eventSource.close();
      });
    } catch (err) {
      answerText.textContent =
        "Q&A service not available. Tap Resume to continue.";
    }
  }

  function onResume() {
    questionOverlay.style.display = "none";
    pausedForQuestion = false;
    audio.play().catch(function () {});
    isPlaying = true;
    playBtn.innerHTML = "&#10074;&#10074;";
  }

  // ── Progress dots ─────────────────────────────────────────
  function buildProgressDots() {
    progressDots.innerHTML = "";
    if (totalDuration <= 0) return;

    for (var i = 0; i < segments.length; i++) {
      var seg = segments[i];
      var pct = (seg.audio_start / totalDuration) * 100;
      var dot = document.createElement("div");
      dot.className = "progress-dot" + (seg.pause_for_question ? " pause-dot" : "");
      dot.style.left = pct + "%";
      dot.title = seg.type + (seg.pause_for_question ? " (pause)" : "");
      progressDots.appendChild(dot);
    }
  }

  // ── Helpers ───────────────────────────────────────────────
  function updateTimeDisplay(current, total) {
    timeDisplay.textContent = formatTime(current) + " / " + formatTime(total);
  }

  function formatTime(s) {
    var m = Math.floor(s / 60);
    var sec = Math.floor(s % 60);
    return m + ":" + (sec < 10 ? "0" : "") + sec;
  }

  function onAudioEnded() {
    isPlaying = false;
    playBtn.innerHTML = "&#9654;";
    showBlank();
    segTitle.textContent = "Episode complete";
    segCounter.textContent = "";
  }

  // ── Boot ──────────────────────────────────────────────────
  init();
})();
