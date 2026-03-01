(function() {
    const audio = new Audio(AUDIO_SOURCE);
    const svg = document.getElementById("drawing-board");
    const hand = document.getElementById("marker-hand");
    const playBtn = document.getElementById("play-btn");
    const progressBar = document.getElementById("progress-bar");
    const progressContainer = document.getElementById("progress-container");
    const title = document.getElementById("segment-title");

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
            title.innerText = seg.segment_text || seg.type;

            // If DRAW_START, clear canvas and load new paths
            if (seg.visual_action === "DRAW_START" && seg.paths) {
                svg.innerHTML = ""; // Clear board
                currentPaths = [];

                // Inject SVG elements
                seg.paths.forEach(p => {
                    // Ghost
                    const g = document.createElementNS("http://www.w3.org/2000/svg", "path");
                    g.setAttribute("d", p.d);
                    g.setAttribute("style", p.ghost_style);
                    svg.appendChild(g);

                    // Ink
                    const i = document.createElementNS("http://www.w3.org/2000/svg", "path");
                    i.setAttribute("id", "ink_" + p.path_id);
                    i.setAttribute("d", p.d);
                    i.setAttribute("style", p.ink_style);
                    // Store metadata for animation
                    i._totalLength = p.total_length;
                    svg.appendChild(i);

                    currentPaths.push(i);
                });
            }
        }

        // 3. Animate Paths based on Segment Progress
        if (seg.visual_action === "DRAW_START" || seg.visual_action === "DRAW_CONTINUE") {
            const segDuration = seg.audio_end - seg.audio_start;
            const segProgress = Math.max(0, (t - seg.audio_start) / segDuration);

            // Distribute progress across all paths in this segment
            // Simple approach: Draw paths sequentially based on total length ratio
            let totalCanvasLength = currentPaths.reduce((sum, p) => sum + p._totalLength, 0);
            let currentDrawDistance = totalCanvasLength * segProgress;

            let covered = 0;
            let handPos = null;

            currentPaths.forEach(path => {
                const len = path._totalLength;
                const startAt = covered;
                const endAt = covered + len;

                if (currentDrawDistance >= endAt) {
                    // Path fully drawn
                    path.style.strokeDashoffset = 0;
                } else if (currentDrawDistance > startAt) {
                    // Path partially drawn
                    const localDraw = currentDrawDistance - startAt;
                    path.style.strokeDashoffset = len - localDraw;

                    // Update Hand Position
                    try {
                        const pt = path.getPointAtLength(localDraw);
                        handPos = pt;
                    } catch(e){}
                } else {
                    // Path not started
                    path.style.strokeDashoffset = len;
                }
                covered += len;
            });

            // Move Hand
            if (handPos) {
                hand.style.display = "block";
                // Offset by -10, -300 to align pen tip (tweak based on image)
                hand.style.transform = "translate(" + handPos.x + "px, " + (handPos.y - 300) + "px)";
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
            document.getElementById("time-display").innerText =
                Math.floor(t/60) + ":" + Math.floor(t%60).toString().padStart(2, '0');
        }
    }
})();
