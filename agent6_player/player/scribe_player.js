/**
 * SketchCast Scribe Player Engine
 *
 * Replaces the iframe-based Rough.js player with native SVG path animation.
 * Uses stroke-dashoffset for the "drawing" effect and a marker hand that
 * follows the path tip using getPointAtLength().
 *
 * Expects three globals injected by the HTML template:
 *   TIMELINE     — unified timeline JSON (from sync_engine)
 *   SCRIBE_DATA  — { segment_id: { svg_content: string, keyframes: [...] } }
 *   AUDIO_SOURCE — data URI or file path for master MP3
 */

(function () {
  "use strict";

  // ── DOM refs ──────────────────────────────────────────────
  var playBtn = document.getElementById("play-btn");
  var timeDisplay = document.getElementById("time-display");
  var progressBar = document.getElementById("progress-bar");
  var progressContainer = document.getElementById("progress-container");
  var progressDots = document.getElementById("progress-dots");
  var segTitle = document.getElementById("segment-title");
  var segIcon = document.getElementById("segment-icon");
  var segCounter = document.getElementById("segment-counter");
  var volumeSlider = document.getElementById("volume-slider");
  var volumeIcon = document.getElementById("volume-icon");
  var drawingBoard = document.getElementById("drawing-board");
  var markerHand = document.getElementById("marker-hand");
  var blankCanvas = document.getElementById("blank-canvas");
  var questionOverlay = document.getElementById("question-overlay");
  var questionInput = document.getElementById("question-input");
  var askBtn = document.getElementById("ask-btn");
  var continueBtn = document.getElementById("continue-btn");
  var resumeBtn = document.getElementById("resume-btn");
  var answerArea = document.getElementById("answer-area");
  var answerText = document.getElementById("answer-text");
  var questionInputArea = document.getElementById("question-input-area");

  // ── State ─────────────────────────────────────────────────
  var audio = new Audio();
  var isPlaying = false;
  var currentSegmentIdx = -1;
  var pausedForQuestion = false;
  var activeAnimSegId = null;
  var rafId = null;

  var segments = TIMELINE.segments || [];
  var totalDuration = TIMELINE.total_duration_seconds || 0;

  var TYPE_ICONS = {
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

    buildProgressDots();

    // Event listeners — controls
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

    // Audio events — use rAF loop for smooth 60fps animation
    audio.addEventListener("play", startAnimLoop);
    audio.addEventListener("pause", stopAnimLoop);
    audio.addEventListener("ended", onAudioEnded);
    audio.addEventListener("loadedmetadata", function () {
      updateTimeDisplay(0, audio.duration || totalDuration);
    });
    audio.addEventListener("seeked", function () {
      // On seek, immediately sync scribe state
      var t = audio.currentTime;
      updateSegment(t);
      checkAnimationTriggers(t);
      syncScribeToTime(t);
    });

    updateTimeDisplay(0, totalDuration);
    segCounter.textContent = segments.length + " sections";
  }

  // ── Playback controls ─────────────────────────────────────
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

  // ── Animation loop (60fps via requestAnimationFrame) ──────
  function startAnimLoop() {
    if (!rafId) rafId = requestAnimationFrame(animLoop);
  }

  function stopAnimLoop() {
    if (rafId) {
      cancelAnimationFrame(rafId);
      rafId = null;
    }
  }

  function animLoop() {
    var t = audio.currentTime;

    // Update progress bar
    var dur = audio.duration || totalDuration;
    var pct = dur > 0 ? (t / dur) * 100 : 0;
    progressBar.style.width = pct + "%";
    updateTimeDisplay(t, dur);

    // Segment tracking
    updateSegment(t);

    // Animation triggers
    checkAnimationTriggers(t);

    // Scribe animation core
    updateScribeAnimation(t);

    if (isPlaying) {
      rafId = requestAnimationFrame(animLoop);
    }
  }

  // ── Segment tracking ──────────────────────────────────────
  function updateSegment(t) {
    var newIdx = findSegmentAt(t);
    if (newIdx !== currentSegmentIdx) {
      currentSegmentIdx = newIdx;
      onSegmentChange(newIdx);
    }
  }

  function findSegmentAt(t) {
    for (var i = 0; i < segments.length; i++) {
      if (t >= segments[i].audio_start && t < segments[i].audio_end) {
        return i;
      }
    }
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

    var text = seg.segment_text || seg.type;
    segTitle.textContent = text.substring(0, 80) + (text.length > 80 ? "..." : "");
    segCounter.textContent = "Section " + (idx + 1) + " of " + segments.length;
  }

  // ── Scribe animation triggers ─────────────────────────────
  function checkAnimationTriggers(t) {
    for (var i = 0; i < segments.length; i++) {
      var seg = segments[i];
      if (!seg.has_animation || seg.animation_trigger == null) continue;

      if (
        t >= seg.animation_trigger &&
        t < seg.audio_end + 0.5 &&
        activeAnimSegId !== seg.segment_id
      ) {
        showSegmentSketch(seg.segment_id);
        return;
      }
    }

    // If current segment has no animation, show blank
    if (currentSegmentIdx >= 0 && currentSegmentIdx < segments.length) {
      var curSeg = segments[currentSegmentIdx];
      if (!curSeg.has_animation && activeAnimSegId) {
        showBlank();
      }
    }
  }

  // ── Scribe SVG loading ────────────────────────────────────
  function showSegmentSketch(segmentId) {
    var data = SCRIBE_DATA[segmentId];
    if (!data) {
      showBlank();
      return;
    }

    activeAnimSegId = segmentId;
    blankCanvas.style.display = "none";
    drawingBoard.style.display = "block";
    drawingBoard.innerHTML = data.svg_content;
    markerHand.style.display = "block";

    // Initialize all ink-layer paths to fully hidden
    var inkPaths = drawingBoard.querySelectorAll(".ink-layer");
    for (var i = 0; i < inkPaths.length; i++) {
      var len = parseFloat(inkPaths[i].getAttribute("data-length"));
      inkPaths[i].style.strokeDasharray = len;
      inkPaths[i].style.strokeDashoffset = len;
    }

    // Hide all text elements initially
    var textEls = drawingBoard.querySelectorAll(".text-element");
    for (var j = 0; j < textEls.length; j++) {
      textEls[j].style.opacity = "0";
    }
  }

  function showBlank() {
    activeAnimSegId = null;
    drawingBoard.style.display = "none";
    drawingBoard.innerHTML = "";
    blankCanvas.style.display = "flex";
    markerHand.style.opacity = "0";
  }

  // ── Scribe animation core ─────────────────────────────────
  function updateScribeAnimation(t) {
    if (!activeAnimSegId) return;
    var data = SCRIBE_DATA[activeAnimSegId];
    if (!data || !data.keyframes) return;

    var handX = -100;
    var handY = -100;
    var anyActive = false;

    for (var i = 0; i < data.keyframes.length; i++) {
      var kf = data.keyframes[i];

      // Handle text elements — fade in
      if (kf.element_type === "text") {
        var textEl = document.getElementById("text-" + kf.path_id);
        if (textEl) {
          if (t >= kf.audio_end) {
            textEl.style.opacity = "1";
          } else if (t >= kf.audio_start) {
            var textProgress = (t - kf.audio_start) / (kf.audio_end - kf.audio_start);
            textEl.style.opacity = String(Math.min(textProgress * 2, 1));
          } else {
            textEl.style.opacity = "0";
          }
        }
        continue;
      }

      // Handle path elements — stroke-dashoffset animation
      var pathEl = document.getElementById(kf.path_id);
      if (!pathEl) continue;

      var len = parseFloat(pathEl.getAttribute("data-length"));
      if (!len || len <= 0) continue;

      if (t < kf.audio_start) {
        // Not started drawing yet
        pathEl.style.strokeDashoffset = String(len);
      } else if (t >= kf.audio_end) {
        // Finished drawing
        pathEl.style.strokeDashoffset = "0";
      } else {
        // Actively drawing — this is where the magic happens
        anyActive = true;

        var duration = kf.audio_end - kf.audio_start;
        var elapsed = t - kf.audio_start;
        var progress = elapsed / duration;

        // Reveal the ink layer progressively
        var currentOffset = len * (1 - progress);
        pathEl.style.strokeDashoffset = String(currentOffset);

        // Move the hand to the exact coordinate on the path
        var currentPointLength = len * progress;
        try {
          var point = pathEl.getPointAtLength(currentPointLength);
          handX = point.x;
          handY = point.y;
        } catch (e) {
          // getPointAtLength can fail on empty/degenerate paths
        }
      }
    }

    // Position the marker hand
    if (anyActive && handX > -50) {
      markerHand.style.opacity = "1";
      markerHand.style.transform =
        "translate(" + (handX - 12) + "px, " + (handY - 24) + "px)";
    } else {
      markerHand.style.opacity = "0";
    }
  }

  /**
   * Sync all scribe paths to an arbitrary time (used after seeking).
   * Instantly snaps all paths to their correct state without animation.
   */
  function syncScribeToTime(t) {
    if (!activeAnimSegId) return;
    var data = SCRIBE_DATA[activeAnimSegId];
    if (!data || !data.keyframes) return;

    for (var i = 0; i < data.keyframes.length; i++) {
      var kf = data.keyframes[i];

      if (kf.element_type === "text") {
        var textEl = document.getElementById("text-" + kf.path_id);
        if (textEl) {
          textEl.style.opacity = t >= kf.audio_end ? "1" : "0";
        }
        continue;
      }

      var pathEl = document.getElementById(kf.path_id);
      if (!pathEl) continue;

      var len = parseFloat(pathEl.getAttribute("data-length"));
      if (t >= kf.audio_end) {
        pathEl.style.strokeDashoffset = "0";
      } else if (t >= kf.audio_start) {
        var progress = (t - kf.audio_start) / (kf.audio_end - kf.audio_start);
        pathEl.style.strokeDashoffset = String(len * (1 - progress));
      } else {
        pathEl.style.strokeDashoffset = String(len);
      }
    }
  }

  // ── Pause for question ─────────────────────────────────────
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
    questionInputArea.style.display = "none";
    answerArea.style.display = "block";
    answerText.textContent = "Thinking...";

    var scriptId = TIMELINE.script_id || "";
    var segId =
      segments[currentSegmentIdx]
        ? segments[currentSegmentIdx].segment_id
        : "";
    var bookId = TIMELINE.book_id || "";
    var chapterNum = TIMELINE.chapter_num || 0;

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

  // ── Progress dots ──────────────────────────────────────────
  function buildProgressDots() {
    progressDots.innerHTML = "";
    if (totalDuration <= 0) return;

    for (var i = 0; i < segments.length; i++) {
      var seg = segments[i];
      var pct = (seg.audio_start / totalDuration) * 100;
      var dot = document.createElement("div");
      dot.className =
        "progress-dot" + (seg.pause_for_question ? " pause-dot" : "");
      dot.style.left = pct + "%";
      dot.title = seg.type + (seg.pause_for_question ? " (pause)" : "");
      progressDots.appendChild(dot);
    }
  }

  // ── Helpers ────────────────────────────────────────────────
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
    stopAnimLoop();
    playBtn.innerHTML = "&#9654;";
    markerHand.style.opacity = "0";
    segTitle.textContent = "Episode complete";
    segCounter.textContent = "";
  }

  // ── Boot ───────────────────────────────────────────────────
  init();
})();
