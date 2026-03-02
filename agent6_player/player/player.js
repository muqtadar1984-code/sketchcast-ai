(function() {
    const audio = new Audio(AUDIO_SOURCE);
    const svg = document.getElementById("drawing-board");
    const hand = document.getElementById("marker-hand");
    const playBtn = document.getElementById("play-btn");
    const progressBar = document.getElementById("progress-bar");
    const progressContainer = document.getElementById("progress-container");
    const title = document.getElementById("segment-title");
    const timeDisplay = document.getElementById("time-display");

    let isPlaying = false;
    let currentPaths = []; // Active paths for current segment

    // --- SETUP ---
    playBtn.onclick = () => isPlaying ? audio.pause() : audio.play();
    audio.onplay = () => { isPlaying = true; playBtn.innerText = "||"; requestAnimationFrame(loop); };
    audio.onpause = () => { isPlaying = false; playBtn.innerText = "\u25B6"; };

    progressContainer.onclick = (e) => {
        const rect = progressContainer.getBoundingClientRect();
        const pos = (e.clientX - rect.left) / rect.width;
        audio.currentTime = pos * audio.duration;
    };

    // --- MAIN LOOP ---
    function loop() {
        if (!isPlaying) return;
        const t = audio.currentTime;
        updateUI(t);
        renderFrame(t);
        requestAnimationFrame(loop);
    }

    // --- RENDER LOGIC ---
    let lastSegmentId = null;

    function renderFrame(t) {
        // 1. Find active segment
        const seg = TIMELINE.segments.find(s => t >= s.audio_start && t < s.audio_end);
        if (!seg) return;

        // 2. Setup Canvas if Segment Changed
        if (seg.segment_id !== lastSegmentId) {
            lastSegmentId = seg.segment_id;
            title.innerText = seg.segment_text ? seg.segment_text.substring(0, 50) + "..." : seg.type;

            // Only clear canvas on DRAW_START — DRAW_CONTINUE and GHOST_ONLY
            // keep the existing drawn state visible.
            if (seg.visual_action === "DRAW_START") {
                svg.innerHTML = "";
                currentPaths = [];

                if (seg.paths && seg.paths.length > 0) {
                    seg.paths.sort((a,b) => (a.draw_order || 0) - (b.draw_order || 0)).forEach(p => {
                        if (p.element_type === 'text' && p.text_params) {
                            const txt = document.createElementNS("http://www.w3.org/2000/svg", "text");
                            txt.setAttribute("x", p.text_params.x);
                            txt.setAttribute("y", p.text_params.y);
                            txt.setAttribute("fill", p.text_params.color || "#2C3E50");
                            txt.setAttribute("font-size", p.text_params.font_size || "16");
                            txt.setAttribute("font-weight", p.text_params.bold ? "bold" : "normal");
                            txt.textContent = p.text_params.text || "";
                            txt.style.opacity = "0";
                            txt._id = p.path_id;
                            svg.appendChild(txt);
                            currentPaths.push({el: txt, type: 'text', id: p.path_id});
                        } else {
                            // GHOST (Faint outline)
                            const ghost = document.createElementNS("http://www.w3.org/2000/svg", "path");
                            ghost.setAttribute("d", p.d);
                            ghost.setAttribute("stroke", p.stroke_color || "#2C3E50");
                            ghost.setAttribute("stroke-width", p.stroke_width || 2);
                            ghost.setAttribute("fill", "none");
                            ghost.style.opacity = "0.1";
                            svg.appendChild(ghost);

                            // INK (Actual drawing)
                            const ink = document.createElementNS("http://www.w3.org/2000/svg", "path");
                            ink.setAttribute("d", p.d);
                            ink.setAttribute("stroke", p.stroke_color || "#2C3E50");
                            ink.setAttribute("stroke-width", p.stroke_width || 2);
                            ink.setAttribute("fill", "none");
                            ink.setAttribute("stroke-dasharray", p.total_length);
                            ink.setAttribute("stroke-dashoffset", p.total_length);
                            ink._totalLength = p.total_length;
                            ink._id = p.path_id;
                            svg.appendChild(ink);
                            currentPaths.push({el: ink, type: 'path', len: p.total_length, id: p.path_id});
                        }
                    });
                }
            } else if (seg.visual_action === "GHOST_ONLY") {
                // Hide hand but keep existing canvas
                hand.style.display = "none";
            }
            // DRAW_CONTINUE: keep canvas and currentPaths as-is
        }

        // 3. Animate Elements based on Keyframes
        if (seg.scribe_keyframes) {
            let activeHandPos = null;

            seg.scribe_keyframes.forEach(kf => {
                const target = currentPaths.find(x => x.id === kf.path_id);
                if (!target) return;

                // Check where we are in this specific keyframe's timeline
                if (t >= kf.audio_end) {
                    // Completed
                    if (target.type === 'text') {
                        target.el.style.opacity = "1";
                    } else {
                        target.el.style.strokeDashoffset = "0";
                    }
                } else if (t < kf.audio_start) {
                    // Not started
                    if (target.type === 'text') {
                        target.el.style.opacity = "0";
                    } else {
                        target.el.style.strokeDashoffset = target.len;
                    }
                } else {
                    // IN PROGRESS
                    const duration = kf.audio_end - kf.audio_start;
                    const progress = (t - kf.audio_start) / duration;

                    if (target.type === 'text') {
                        target.el.style.opacity = progress; // Fade in text
                    } else {
                        // Draw path
                        const drawLen = target.len * (1 - progress);
                        target.el.style.strokeDashoffset = drawLen;

                        // Move Hand
                        try {
                            const point = target.el.getPointAtLength(target.len * progress);
                            activeHandPos = point;
                        } catch(e) {}
                    }
                }
            });

            // Update Hand
            if (activeHandPos) {
                hand.style.display = "block";
                hand.style.transform = `translate(${activeHandPos.x}px, ${activeHandPos.y}px) rotate(-10deg)`;
            } else {
                hand.style.display = "none";
            }
        } else if (seg.paths && seg.paths.length > 0) {
            // FALLBACK: No keyframes — use simple progressive animation
            const segDuration = seg.audio_end - seg.audio_start;
            const segProgress = Math.max(0, (t - seg.audio_start) / segDuration);

            let totalCanvasLength = currentPaths.reduce((sum, p) => sum + (p.len || 0), 0);
            let currentDrawDistance = totalCanvasLength * segProgress;
            let covered = 0;
            let handPos = null;

            currentPaths.forEach(cp => {
                if (cp.type === 'text') {
                    cp.el.style.opacity = segProgress;
                    return;
                }
                const len = cp.len;
                const startAt = covered;
                const endAt = covered + len;

                if (currentDrawDistance >= endAt) {
                    cp.el.style.strokeDashoffset = 0;
                } else if (currentDrawDistance > startAt) {
                    const localDraw = currentDrawDistance - startAt;
                    cp.el.style.strokeDashoffset = len - localDraw;
                    try {
                        const pt = cp.el.getPointAtLength(localDraw);
                        handPos = pt;
                    } catch(e) {}
                } else {
                    cp.el.style.strokeDashoffset = len;
                }
                covered += len;
            });

            if (handPos) {
                hand.style.display = "block";
                hand.style.transform = `translate(${handPos.x}px, ${handPos.y}px) rotate(-10deg)`;
            } else {
                hand.style.display = "none";
            }
        } else {
            hand.style.display = "none"; // Hide hand during GHOST_ONLY
        }
    }

    function updateUI(t) {
        if (audio.duration) {
            const pct = (t / audio.duration) * 100;
            progressBar.style.width = pct + "%";
            timeDisplay.innerText = Math.floor(t/60) + ":" + Math.floor(t%60).toString().padStart(2, '0');
        }
    }
})();
