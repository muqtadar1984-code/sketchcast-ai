"""Agent 4: Rough.js Renderer — generates self-contained HTML sketches.

For each animated segment, this module produces an HTML file that:
  • Loads Rough.js from CDN (genuine hand-drawn canvas rendering)
  • Reads the sketch plan JSON produced by Agent 4's sketch generator
  • Draws each element progressively using the animation timing JSON
  • Exposes window.startAnimation() for Playwright (Agent 6) control

Agent 6 will use Playwright to capture these HTML files as video frames.
"""

import json
from pathlib import Path

# ── Rough.js HTML template ────────────────────────────────────────────
# Placeholders:
#   __SKETCH_PLAN__    replaced with JSON.stringify'd sketch plan dict
#   __ANIMATION_JSON__ replaced with JSON.stringify'd animation dict

_ROUGHJS_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: #FAFAFA; overflow: hidden; }
  canvas { display: block; }
</style>
</head>
<body>
<canvas id="sketch" width="1280" height="720"></canvas>
<script src="https://cdnjs.cloudflare.com/ajax/libs/rough.js/4.6.6/rough.min.js"
        crossorigin="anonymous"></script>
<script>
/* ── global state ────────────────────────────────────────────── */
const canvas = document.getElementById('sketch');
const ctx    = canvas.getContext('2d');
const rc     = rough.canvas(canvas);

window._animationComplete = false;

/* ── sketch data injected by Python ─────────────────────────── */
const sketchPlan     = __SKETCH_PLAN__;
const animationFrames = __ANIMATION_JSON__;

/* ── Rough.js defaults (hand-drawn feel) ─────────────────────── */
const defaultOpts = {
  roughness: 1.4,
  bowing:    0.8,
  stroke:    '#2C3E50',
  strokeWidth: 2.2,
  fillStyle: 'hachure',
  fillWeight: 1.2,
  hachureAngle: -41,
  hachureGap:   8,
};

/* ── paper texture background ────────────────────────────────── */
function drawBackground() {
  ctx.fillStyle = '#FAFAFA';
  ctx.fillRect(0, 0, 1280, 720);
  // subtle warm grain overlay
  const imgData = ctx.getImageData(0, 0, 1280, 720);
  const d = imgData.data;
  for (let i = 0; i < d.length; i += 4) {
    const noise = (Math.random() - 0.5) * 8;
    d[i]   = Math.max(0, Math.min(255, d[i]   + noise * 0.6));
    d[i+1] = Math.max(0, Math.min(255, d[i+1] + noise * 0.5));
    d[i+2] = Math.max(0, Math.min(255, d[i+2] + noise * 0.3));
  }
  ctx.putImageData(imgData, 0, 0);
}

/* ── element → Rough.js renderer ────────────────────────────── */
function makeOpts(p) {
  const opts = Object.assign({}, defaultOpts);
  if (p.color)  opts.stroke = p.color;
  if (p.stroke_width) opts.strokeWidth = p.stroke_width;
  if (p.fill && p.fill !== 'none') {
    opts.fill      = p.fill;
    opts.fillStyle = 'hachure';
  } else {
    opts.fill = undefined;
  }
  return opts;
}

function drawElement(element) {
  const p    = element.params || {};
  const opts = makeOpts(p);

  switch (element.type) {

    case 'circle':
      rc.circle(p.cx, p.cy, (p.r || 50) * 2, opts);
      break;

    case 'rectangle':
      rc.rectangle(p.x, p.y, p.width, p.height, opts);
      break;

    case 'line':
      rc.line(p.x1, p.y1, p.x2, p.y2, opts);
      break;

    case 'arrow': {
      rc.line(p.x1, p.y1, p.x2, p.y2, opts);
      const angle = Math.atan2(p.y2 - p.y1, p.x2 - p.x1);
      const sz    = 14;
      rc.line(p.x2, p.y2,
        p.x2 - sz * Math.cos(angle - 0.4),
        p.y2 - sz * Math.sin(angle - 0.4), opts);
      rc.line(p.x2, p.y2,
        p.x2 - sz * Math.cos(angle + 0.4),
        p.y2 - sz * Math.sin(angle + 0.4), opts);
      break;
    }

    case 'text': {
      const fs     = p.font_size || 16;
      const weight = p.bold ? 'bold' : 'normal';
      ctx.font        = `${weight} ${fs}px 'Patrick Hand', 'Segoe UI', Arial, sans-serif`;
      ctx.fillStyle   = p.color || '#2C3E50';
      ctx.textAlign   = ({'left':'left','right':'right'})[p.align] || 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(p.text || '', p.x, p.y);
      break;
    }

    case 'mind_map': {
      // center node
      rc.circle(p.cx, p.cy, (p.radius || 55) * 0.8, Object.assign({}, opts, {fill: p.color || '#4A90D9'}));
      ctx.font = 'bold 14px Arial, sans-serif';
      ctx.fillStyle = '#FFFFFF';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText((p.center_label || '').substring(0, 18), p.cx, p.cy);
      // branches
      const branches = p.branches || [];
      const n = branches.length || 1;
      const branchColors = ['#4A90D9','#E67E22','#27AE60','#9B59B6','#E74C3C','#1ABC9C'];
      branches.forEach(function(label, i) {
        const angle = (2 * Math.PI * i / n) - Math.PI / 2;
        const bx = p.cx + (p.radius || 200) * Math.cos(angle);
        const by = p.cy + (p.radius || 200) * Math.sin(angle);
        const bc = branchColors[i % branchColors.length];
        rc.line(p.cx + 28 * Math.cos(angle), p.cy + 28 * Math.sin(angle), bx, by,
          Object.assign({}, opts, {stroke: bc}));
        rc.circle(bx, by, 76, Object.assign({}, opts, {fill: bc, stroke: bc}));
        ctx.font = '12px Arial, sans-serif';
        ctx.fillStyle = '#FFFFFF';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(String(label).substring(0, 14), bx, by);
      });
      break;
    }

    case 'process_flow': {
      const steps = p.steps || [];
      const n     = steps.length || 1;
      const gap   = 18;
      const bw    = (p.width - gap * (n - 1)) / n;
      const bh    = 52;
      steps.forEach(function(step, i) {
        const bx = p.x + i * (bw + gap);
        rc.rectangle(bx, p.y, bw, bh, Object.assign({}, opts, {fill: '#4A90D9', stroke: p.color || '#2C3E50'}));
        ctx.font = 'bold 12px Arial, sans-serif';
        ctx.fillStyle = '#FFFFFF';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(String(step).substring(0, 20), bx + bw / 2, p.y + bh / 2);
        if (i < n - 1) {
          const ax = bx + bw;
          const ay = p.y + bh / 2;
          rc.line(ax, ay, ax + gap - 2, ay, opts);
        }
      });
      break;
    }

    case 'pyramid': {
      const levels = p.levels || [];
      const n      = levels.length || 1;
      const lh     = p.height / n;
      const colors = ['#4A90D9','#E67E22','#27AE60','#9B59B6','#E74C3C'];
      levels.forEach(function(label, i) {
        const ratio = (i + 1) / n;
        const lw    = p.width * ratio;
        const lx    = p.x + (p.width - lw) / 2;
        const ly    = p.y + i * lh;
        const bc    = colors[i % colors.length];
        rc.rectangle(lx, ly, lw, lh, Object.assign({}, opts, {fill: bc, stroke: '#FFFFFF'}));
        ctx.font = 'bold 13px Arial, sans-serif';
        ctx.fillStyle = '#FFFFFF';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(String(label).substring(0, 28), p.x + p.width / 2, ly + lh / 2);
      });
      break;
    }

    case 'venn': {
      const circles = p.circles || [];
      const offsets = [[-65,0],[65,0],[0,-55]];
      const r       = 110;
      const vColors = ['#4A90D9','#E67E22','#27AE60'];
      circles.slice(0, 3).forEach(function(c, i) {
        const [ox, oy] = offsets[i] || [0, 0];
        const col = c.color || vColors[i % vColors.length];
        rc.circle(p.cx + ox, p.cy + oy, r * 2,
          Object.assign({}, opts, {fill: col, fillStyle: 'solid', stroke: col, fillWeight: 0, fillOpacity: 0.3}));
        ctx.font = 'bold 13px Arial, sans-serif';
        ctx.fillStyle = '#2C3E50';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(String(c.label || '').substring(0, 14), p.cx + ox, p.cy + oy);
      });
      break;
    }

    case 'timeline': {
      const events = p.events || [];
      const n      = events.length || 1;
      rc.line(p.x, p.y, p.x + p.width, p.y, opts);
      events.forEach(function(ev, i) {
        const ex = p.x + (i + 1) * p.width / (n + 1);
        rc.line(ex, p.y - 8, ex, p.y + 8, opts);
        ctx.font = 'bold 13px Arial, sans-serif';
        ctx.fillStyle = p.color || '#2C3E50';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(String(ev.label || ''), ex, p.y - 22);
        if (ev.year) ctx.fillText(String(ev.year), ex, p.y + 24);
      });
      break;
    }

    default:
      // Unknown element type — skip gracefully
      break;
  }
}

/* ── progressive reveal animation ───────────────────────────── */
const frames      = (animationFrames && animationFrames.frames) ? animationFrames.frames : [];
const elementMap  = {};
(sketchPlan.elements || []).forEach(function(e) { elementMap[e.id] = e; });

let frameIndex   = 0;
const drawnIds   = new Set();

function revealNextFrame() {
  if (frameIndex >= frames.length) {
    window._animationComplete = true;
    return;
  }
  const frame = frames[frameIndex++];
  (frame.elements_to_reveal || []).forEach(function(eid) {
    if (!drawnIds.has(eid) && elementMap[eid]) {
      drawElement(elementMap[eid]);
      drawnIds.add(eid);
    }
  });
  const dur = Math.max(
    ((frame.end_second || 0) - (frame.start_second || 0)) * 1000,
    100
  );
  setTimeout(revealNextFrame, dur);
}

/* ── public API (used by Playwright / Agent 6) ───────────────── */
window.startAnimation = function() {
  drawBackground();
  frameIndex   = 0;
  drawnIds.clear();
  window._animationComplete = false;
  revealNextFrame();
};

// Auto-start on load
window.startAnimation();
</script>
</body>
</html>
"""


def generate_roughjs_html(
    segment_id: str,
    sketch_plan: dict,
    animation_json: dict,
    output_dir: Path,
) -> str:
    """Generate a self-contained Rough.js HTML animation file for one segment.

    Parameters
    ----------
    segment_id:    ID string (e.g. "s003"), used as filename prefix.
    sketch_plan:   Dict produced by Agent 4's Claude sketch plan (contains "elements").
    animation_json: Dict produced by animation_builder.build_animation().model_dump().
    output_dir:    Directory where the HTML file will be saved.

    Returns
    -------
    Absolute path to the generated HTML file (str).
    """
    html_content = (
        _ROUGHJS_TEMPLATE
        .replace("__SKETCH_PLAN__", json.dumps(sketch_plan, ensure_ascii=False))
        .replace("__ANIMATION_JSON__", json.dumps(animation_json, ensure_ascii=False))
    )

    html_path = output_dir / f"{segment_id}_roughjs.html"
    html_path.write_text(html_content, encoding="utf-8")
    return str(html_path)
