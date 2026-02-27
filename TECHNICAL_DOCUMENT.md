# SketchCast AI — Technical Architecture Document

**Version:** 1.0
**Date:** 27 February 2026
**Repository:** github.com/muqtadar1984-code/sketchcast-ai
**Live:** https://sketchcast.streamlit.app/

---

## 1. System Overview

SketchCast AI is a multi-agent pipeline that transforms PDF textbooks into interactive, narrated whiteboard-style video lessons. A teacher uploads a PDF; the system extracts content, analyses it with Claude AI, writes a Socratic narration script, generates hand-drawn sketch animations, synthesises speech with ElevenLabs, and assembles everything into a browser-based player — all triggered by a single click.

### Technology Stack

| Layer | Technology |
|-------|-----------|
| UI | Streamlit (Python) |
| AI / LLM | Anthropic Claude API (claude-sonnet-4) |
| Text-to-Speech | ElevenLabs API (eleven_turbo_v2) |
| PDF Extraction | Docling (primary), PyMuPDF (fallback/TOC) |
| Animation | Rough.js (hand-drawn SVG), SVG filters |
| Audio Processing | pydub, audioop-lts |
| Data Models | Pydantic v2 |
| Database | SQLAlchemy + SQLite |
| API Layer | FastAPI (per-agent, ports 8003-8006) |
| Hosting | Streamlit Cloud |

---

## 2. Agent Pipeline — What Has Been Built

### 2.1 Pipeline Summary

| Agent | Name | Status | What It Does |
|-------|------|--------|-------------|
| 1 | Library & Ingestion | Complete | PDF upload, text/image extraction, chapter structuring |
| 2 | Content Analysis | Complete | Claude-driven concept extraction, difficulty analysis, visual opportunity detection |
| 3 | Script Generation | Complete | Socratic narration scripts with TTS markup and sketch cues |
| 4 | Sketch Animation | Complete | SVG whiteboard drawings with Rough.js hand-drawn rendering |
| 5 | Audio Generation | Complete | ElevenLabs TTS per segment, stitched into master MP3 |
| 6 | Live Playback Engine | Complete | Browser player syncing audio + animations in real time |
| 7 | YouTube Export | Disabled | Code not yet written; page locked via feature flag |
| 8 | Q&A Interjection | Disabled | Code built but locked; question bank with fuzzy matching |

### 2.2 Auto-Chain Execution

One click on "Run Analysis" triggers the full pipeline sequentially:

```
PDF Upload (manual)
    |
    v
Agent 1: Ingestion (manual upload)
    |
    v
Agent 2: Analysis -----> triggered by "Run Analysis" button
    |
    v
Agent 3: Script --------> auto-triggered after analysis
    |
    v
Agent 4: Animations ----> auto-triggered after script
    |
    v
Agent 5: Audio ----------> auto-triggered after animations (skips if no ElevenLabs keys)
    |
    v
Agent 6: Player ---------> auto-triggered after audio
```

Each step has its own progress indicator. If one agent fails, results from prior agents are preserved.

---

## 3. Agent 1 — Library & Ingestion

**Location:** `agent1_ingestion/`
**Purpose:** Accept PDF textbook uploads, extract all content, and structure it into a navigable hierarchy.

### How It Works

**PDF Text Extraction** (`extractor.py`):
- **Primary backend:** Docling with OCR enabled. Uses semantic item labels (TITLE, SECTION_HEADER, PARAGRAPH, TABLE, LIST_ITEM, CAPTION, PICTURE) to classify each content block.
- **Fallback backend:** PyMuPDF (fitz) when Docling is unavailable. Uses font-size frequency analysis — the most common font size is classified as body text; larger sizes become headings.
- **Caching:** MD5 hash of first 4MB + file size used as cache key, enabling fast re-runs on Streamlit Cloud's ephemeral filesystem.
- **Output:** `ExtractionResult` containing `DocItem[]` (each with item_type, text, page_num, level), `TOCItem[]`, total_pages, and readability_score.

**Image Extraction** (`image_extractor.py`):
- Uses PyMuPDF `page.get_images(full=True)` to find all embedded images by xref.
- Filters by minimum dimensions (50x50px).
- Extracts surrounding text as `context_label` for each image.
- Includes a blank-image filter: pre-API pixel variance check (PIL, threshold=50) and post-API keyword filter ("completely black", "no visible content") to exclude empty/solid images.

**Book Structuring** (`structurer.py`):
- Converts flat DocItem list into `StructuredBook` with chapters, sections, subsections.
- **Chapter detection** (priority): PDF TOC bookmarks (level=1) > Level-1 heading DocItems > treat entire document as 1 chapter.
- **Section detection:** Level 2 DocItems become Sections; Level 3 become Subsections nested under them; Level 0 (body text) accumulates into the current section.
- **Key box detection:** Regex matching on known patterns (activity, definition, info, exercise, quote) to tag special content blocks.

### Data Models

```
StructuredBook
  ├── book_id, title, author, isbn
  ├── total_pages, total_chapters, readability_score
  ├── table_of_contents: TOCEntry[]
  └── chapters: ChapterContent[]
        ├── chapter_num, title, start_page, end_page
        ├── sections: Section[]
        │     ├── section_title, content, page_num
        │     └── subsections: Subsection[]
        ├── images: ImageInfo[]
        └── key_boxes: KeyBox[] (activity, definition, info, exercise, quote)
```

### Deduplication

Before processing, the system checks for duplicates using:
1. ISBN exact match
2. Title + author fuzzy match (rapidfuzz, threshold 85%)
3. File hash exact match (SHA-256)

---

## 4. Agent 2 — Content Analysis

**Location:** `agent2_analysis/`
**Purpose:** Deep educational analysis of chapter content using Claude AI.

### How It Works

`run_full_analysis()` makes exactly **2 Claude API calls** per chapter:

**Call 1 — Full Chapter Analysis:**
- Sends chapter text (capped at 15K characters) + structured metadata.
- Claude returns a single JSON containing:
  - **Concepts** (id, name, definition, importance: foundational/supporting/application, dependencies, prerequisites)
  - **Difficulty assessments** per section (score 1-10, vocabulary load, pacing recommendation, suggested analogies)
  - **Visual opportunities** (trigger_text, visual_type, animation_sequence with step-by-step actions, sketch_elements, estimated_duration, complexity)

**Call 2 — Batch Image Analysis:**
- Sends all chapter images in a single multi-image Claude Vision call.
- Claude returns per-image: visual_type, description, key_elements, educational_value, can_be_recreated_as_sketch, sketch_recreation_notes.

**Episode Segmentation** (mechanical, no API call):
- Builds a single-episode plan per chapter based on sections covered, concepts introduced, and visual opportunities available.
- Estimates ~600 words per 5-minute episode.

### Output: MasterAnalysis

```
MasterAnalysis
  ├── analysis_id, book_id, chapter_num, chapter_title
  ├── difficulty_level_requested (primary/middle/high school)
  ├── token_usage (input, output, total, estimated_cost_usd)
  ├── concepts: ConceptResult
  │     ├── concepts: Concept[]
  │     ├── dependencies: Dependency[]
  │     └── prerequisites: Prerequisite[]
  ├── difficulty_assessments: DifficultyAssessment[]
  ├── visual_opportunities: VisualOpportunity[]
  │     └── animation_sequence: AnimationStep[] (step, action, details, duration_ms)
  ├── image_analyses: ImageAnalysis[]
  └── episodes: EpisodePlan
```

---

## 5. Agent 3 — Script & Dialogue Generation

**Location:** `agent3_scripts/`
**Purpose:** Generate Socratic narration scripts with ElevenLabs TTS markup, sketch cues, and question hooks.

### How It Works

`generate_chapter_scripts_from_analysis()` takes the full MasterAnalysis and produces one script per episode:

1. Builds an episode context string from the analysis (concepts, visuals, teaching notes).
2. Sends to Claude with the `EPISODE_SCRIPT_PROMPT`.
3. Parses response into typed `ScriptSegment` objects.

### Script Structure (mandatory segment order)

| Segment Type | Duration | Purpose |
|-------------|----------|---------|
| `hook` | ~30s | Surprising real-world question to grab attention |
| `activate` | ~45s | Bridge from known to unknown ("You've probably noticed...") |
| `explore` (1+) | 60-120s each | One per major concept, Socratic question-driven |
| `question_hook` | ~20s | Natural pause for student reflection |
| `synthesis` | ~45s | "Let's collect what we discovered..." |
| `preview` | ~20s | Tease next episode |

### Key Fields Per Segment

- `text` — plain narrator text
- `elevenlabs_text` — same text with `<break time="Xs"/>` TTS markup (0.3s micro, 0.5s short, 1s medium, 2s thinking)
- `sketch_cue` — `{action, element, timing}` telling Agent 4 what to draw and when
  - action: draw, highlight, label, clear, point, annotate
  - timing: before, during, or after the narration
- `pause_for_question` — boolean flag for Agent 6/8 integration
- `estimated_duration_seconds` — word-count-based estimate

### Output: EpisodeScript

```
EpisodeScript
  ├── script_id, book_id, chapter_num, episode_num, episode_title
  ├── narrator_persona: "Socratic"
  ├── segments: ScriptSegment[]
  │     ├── segment_id: "s001", "s002", ...
  │     ├── type: hook | activate | explore | question_hook | synthesis | preview
  │     ├── text, elevenlabs_text
  │     ├── sketch_cue: {action, element, timing} | null
  │     ├── pause_for_question: bool
  │     └── estimated_duration_seconds
  ├── total_estimated_duration_seconds
  └── question_hook_count
```

---

## 6. Agent 4 — Sketch Animation

**Location:** `agent4_animation/`
**Purpose:** Generate SVG whiteboard drawings with hand-drawn style and Rough.js rendering.

### How It Works

`generate_episode_animations_from_script()` processes every segment:

1. **Segments with `sketch_cue`** — Claude plans the sketch, then it's rendered:
   - `_get_sketch_plan()` — Claude designs the layout: which elements (circles, rectangles, arrows, text, mind maps, process flows, timelines, pyramids, Venn diagrams, trees, grids), their positions, sizes, colours, and draw order.
   - `execute_sketch_plan()` — `SVGCanvas` renders the plan into an SVG string (1280x720, #FAFAFA background).
   - `build_animation()` — Creates frame-by-frame timing (progressive reveal based on draw_order).
   - `generate_roughjs_html()` — Produces a self-contained HTML file using the Rough.js library for true hand-drawn rendering.

2. **Segments without `sketch_cue`** — Marked as blank canvas (no animation).

### SVG Canvas Capabilities

The `SVGCanvas` class supports:
- **Primitives:** circle, rectangle, line, arrow, text
- **Compounds:** grid, timeline, tree, mind_map, pyramid, venn, process_flow
- **Hand-drawn effects:** SVG `feTurbulence` + `feDisplacementMap` filter for wobbly lines, seeded stroke-width variation (±0.6px), paper texture overlay via Perlin noise

### Rough.js HTML Template

Each animated segment produces a standalone HTML file that:
- Loads the Rough.js CDN library
- Reads the sketch plan + animation timing as embedded JSON
- Draws elements progressively using `requestAnimationFrame`
- Default rough.js options: `roughness: 1.4, bowing: 0.8, strokeWidth: 2.2, fillStyle: 'hachure'`
- Exposes `window.startAnimation()` for external control

### Output: AnimationManifest

```
AnimationManifest
  ├── manifest_id, script_id, book_id, chapter_num, episode_num
  ├── canvas_size: {width: 1280, height: 720}
  ├── total_segments, animated_segments, blank_segments
  └── segments: ManifestSegment[]
        ├── segment_id, type
        ├── has_animation: bool
        ├── svg_path: "storage/animations/.../s003_sketch.svg"
        ├── animation_path: "storage/animations/.../s003_animation.json"
        ├── roughjs_html_path: "storage/animations/.../s003_roughjs.html"
        ├── estimated_duration_seconds
        └── sketch_cue_timing: before | during | after
```

---

## 7. Agent 5 — Audio Generation

**Location:** `agent5_audio/`
**Purpose:** Synthesise narration audio using ElevenLabs TTS and stitch into a master MP3.

### How It Works

`generate_episode_audio()` runs a 3-step pipeline:

**Step 1 — TTS Generation** (`tts_client.py`):
- Iterates through script segments sequentially (no parallel calls).
- Sends each segment's `elevenlabs_text` (with `<break>` markup) to ElevenLabs.
- Configuration: model `eleven_turbo_v2`, output `mp3_44100_128`, voice ID from `st.secrets`.
- 0.5s delay between API calls to respect rate limits.
- Retry once with exponential backoff on failure.
- Each segment saved as `{segment_id}.mp3`.

**Step 2 — Stitching** (`stitcher.py`):
- Uses pydub to concatenate all segment MP3s in order.
- Inserts 300ms silence gaps between segments.
- Exports as single `master.mp3` at 128kbps.
- Returns `(total_duration_seconds, [per_segment_durations])`.

**Step 3 — Manifest Building** (`manifest_builder.py`):
- Calculates cumulative `master_start_seconds` / `master_end_seconds` from actual measured durations + 300ms gaps.
- These are the timestamps Agent 6 uses for sync — never estimated durations.

### Output: AudioManifest

```
AudioManifest
  ├── audio_manifest_id, script_id, book_id, chapter_num, episode_num
  ├── voice_id, model
  ├── master_audio_path: "storage/audio/{book_id}/chapter_{n}/master.mp3"
  ├── total_duration_seconds (actual measured)
  └── segments: SegmentAudio[]
        ├── segment_id
        ├── audio_path: "storage/audio/.../s001.mp3"
        ├── actual_duration_seconds
        ├── master_start_seconds (cumulative position in master)
        ├── master_end_seconds
        ├── pause_for_question: bool
        └── pause_point: bool
```

### Duration Discrepancy Note

Agent 3 estimates duration from word count (~130 words/minute). ElevenLabs `eleven_turbo_v2` speaks significantly faster — actual durations are typically ~46% of estimated. The pipeline uses actual measured durations for all sync calculations.

---

## 8. Agent 6 — Live Playback Engine

**Location:** `agent6_player/`
**Purpose:** Synchronise master MP3 audio with Rough.js sketch animations in a browser-based player.

### How It Works

`build_player_package()` runs 3 steps:

**Step 1 — Timeline Merging** (`sync_engine.py`):
- Joins audio manifest (Agent 5) and animation manifest (Agent 4) by `segment_id`.
- Calculates `animation_trigger` from `sketch_cue_timing`:
  - `"before"` → `max(0, audio_start - animation_duration)`
  - `"during"` → `audio_start`
  - `"after"` → `audio_end`
- Carries forward `pause_at_second = audio_end` for question_hook segments.

**Step 2 — Asset Embedding**:
- Reads each animated segment's Rough.js HTML content.
- Encodes master MP3 as base64 data URI (for Streamlit iframe embedding).

**Step 3 — HTML Assembly** (`player_builder.py`):
- Produces a single self-contained HTML file with all CSS, JS, timeline JSON, animation content, and audio embedded inline.
- Embedded in Streamlit via `st.components.v1.html(player_html, width=1280, height=800)`.

### Player Features (player.js)

| Feature | Implementation |
|---------|---------------|
| Audio playback | HTML5 `<audio>` element with play/pause/seek/volume |
| Animation sync | `requestAnimationFrame` loop checks `audio.currentTime` against `animation_trigger` timestamps; loads Rough.js HTML into `<iframe>` via `srcdoc` |
| Segment tracking | Segment title bar updates with current segment type and narration text preview |
| Progress | Red progress bar + coloured dots at each segment boundary (orange = pause point) |
| Question pause | **Currently disabled.** `checkPausePoints()` is a no-op. When re-enabled, pauses audio at `pause_at_second` and shows question overlay. |

### Output: UnifiedTimeline

```
UnifiedTimeline
  ├── timeline_id, script_id, book_id, chapter_num, episode_num, episode_title
  ├── total_duration_seconds (from actual audio)
  ├── master_audio_path
  └── segments: TimelineSegment[]
        ├── segment_id, type
        ├── audio_start, audio_end (seconds in master)
        ├── has_animation: bool
        ├── roughjs_html_path
        ├── animation_trigger (calculated second)
        ├── sketch_cue_timing: before | during | after
        ├── pause_for_question: bool
        ├── pause_at_second
        └── segment_text
```

---

## 9. Agent 7 — YouTube Export (Disabled)

**Status:** Locked via `config.py → YOUTUBE_EXPORT_ENABLED = False`
**Streamlit page:** Shows "YouTube export is not enabled in this version."
**Code:** Not yet written. Will use Remotion for MP4 rendering from the unified timeline.

---

## 10. Agent 8 — Q&A Interjection Handler (Disabled)

**Location:** `agent8_qa/`
**Status:** Code fully built but page locked and interjection disabled in the player.

### What's Built (Ready to Re-Enable)

- **Question Bank** (`question_bank.py`): JSON-backed storage with rapidfuzz fuzzy matching (85% threshold). Repeated questions served from cache at zero LLM cost. Questions with 10+ uses auto-marked "verified".
- **Answer Generator** (`answer_generator.py`): Claude streaming in the Socratic narrator voice, 2-4 sentences max, contextually aware of current segment and chapter.
- **Streaming TTS** (`streaming_tts.py`): Buffers Claude text into sentences, sends each complete sentence to ElevenLabs streaming immediately. Target: audio starts within 1.5s.
- **Question Handler** (`question_handler.py`): Orchestrator — bank check first, then Claude + TTS fallback, then auto-bank new answers.
- **FastAPI SSE Endpoint** (`main.py`): `POST /ask/{script_id}/{segment_id}` returns Server-Sent Events with `text` and `audio` events.

---

## 11. Data Flow Between Agents

```
┌─────────────────────────────────────────────────────────────────┐
│                        AGENT 1                                   │
│  PDF → Docling/PyMuPDF → ExtractionResult → StructuredBook      │
│  PDF → PyMuPDF images → ExtractedImage[]                        │
└────────────────────────┬────────────────────────────────────────┘
                         │ StructuredBook.chapters[n]
                         v
┌─────────────────────────────────────────────────────────────────┐
│                        AGENT 2                                   │
│  ChapterContent + Claude → MasterAnalysis                        │
│    (concepts, difficulty, visuals, image analyses, episode plan) │
└────────────────────────┬────────────────────────────────────────┘
                         │ MasterAnalysis (full dict)
                         v
┌─────────────────────────────────────────────────────────────────┐
│                        AGENT 3                                   │
│  MasterAnalysis + Claude → EpisodeScript                         │
│    (segments with text, elevenlabs_text, sketch_cues)           │
└──────────┬─────────────────────────────┬────────────────────────┘
           │ sketch_cue per segment       │ elevenlabs_text per segment
           v                              v
┌─────────────────────────┐  ┌────────────────────────────────────┐
│        AGENT 4          │  │            AGENT 5                  │
│  sketch_cue + Claude    │  │  elevenlabs_text + ElevenLabs TTS  │
│  → SketchPlan           │  │  → segment MP3s                    │
│  → SVG + animation.json │  │  → master.mp3 (pydub stitch)      │
│  → Rough.js HTML        │  │  → AudioManifest (actual timings)  │
│  → AnimationManifest    │  │                                    │
└──────────┬──────────────┘  └──────────────┬─────────────────────┘
           │ AnimationManifest               │ AudioManifest
           └──────────┬─────────────────────┘
                      │ merge by segment_id
                      v
┌─────────────────────────────────────────────────────────────────┐
│                        AGENT 6                                   │
│  AudioManifest + AnimationManifest → UnifiedTimeline             │
│  UnifiedTimeline + embedded assets → player.html                │
│    (self-contained HTML/JS/CSS with base64 audio + animations)  │
└─────────────────────────────────────────────────────────────────┘
```

### Integration Keys

- **Agent 1 → 2:** `chapter_content` dict (sections, images, key_boxes)
- **Agent 2 → 3:** `MasterAnalysis` dict (concepts, visuals, difficulty, episode plan)
- **Agent 3 → 4:** `ScriptSegment.sketch_cue` (action, element, timing)
- **Agent 3 → 5:** `ScriptSegment.elevenlabs_text` (with `<break>` markup)
- **Agent 4 + 5 → 6:** Matched by `segment_id` across both manifests; sync uses **actual audio timestamps only**

---

## 12. Storage Layout

```
storage/
├── books/                    # Raw/cached PDF data (Agent 1)
├── extracted_images/         # Chapter images (Agent 1)
│   └── {book_id}/
├── processed/                # StructuredBook JSON (Agent 1)
│   └── {book_id}.json
├── analysis/                 # MasterAnalysis JSON (Agent 2)
│   └── {book_id}/
│       └── chapter_{n}.json
├── scripts/                  # EpisodeScript JSON (Agent 3)
│   └── {book_id}/
│       └── chapter_{n}_episode_{m}.json
├── animations/               # SVG + timing + Rough.js HTML (Agent 4)
│   └── {book_id}/
│       └── chapter_{n}/
│           ├── s001_sketch.svg
│           ├── s001_animation.json
│           ├── s001_roughjs.html
│           ├── ...
│           └── manifest.json
├── audio/                    # MP3 files + audio manifest (Agent 5)
│   └── {book_id}/
│       └── chapter_{n}/
│           ├── s001.mp3
│           ├── s002.mp3
│           ├── ...
│           ├── master.mp3
│           └── audio_manifest.json
├── player/                   # Player HTML + timeline (Agent 6)
│   └── {book_id}/
│       └── chapter_{n}/
│           ├── timeline.json
│           └── player.html
└── question_bank/            # Cached Q&A pairs (Agent 8)
    └── {book_id}/
        └── chapter_{n}_bank.json
```

---

## 13. Database Schema

```sql
books           -- id, title, author, isbn, file_hash, pages, chapters, readability, status
chapters        -- id, book_id (FK), chapter_num, title, start/end page
images          -- id, book_id (FK), chapter_id (FK), filename, page_num, context, path
chapter_analyses-- id, book_id (FK), chapter_num, difficulty_level, status, tokens, cost
episode_audio   -- id, script_id, book_id (FK), chapter_num, status, voice_id, model, duration
episode_player  -- id, script_id, book_id (FK), chapter_num, status, timeline_path, built_at
question_bank   -- id, book_id (FK), chapter_num, segment_id, question, answer, usage_count, verified
question_log    -- id, book_id (FK), chapter_num, student_id, question, served_from_cache, latency
```

---

## 14. Streamlit UI Pages

| # | Page | Agent | Purpose |
|---|------|-------|---------|
| 1 | Upload Book | 1 | PDF drag-and-drop with dedup check |
| 2 | Library | 1 | All uploaded books with metadata |
| 3 | Book Viewer | 1 | Chapter/section reader with ToC |
| 4 | Analyse Chapter | 2-6 | Triggers full pipeline (single button) |
| 5 | Analysis Results | 2 | Concepts, visuals, episodes, images, script tabs |
| 6 | Scripts | 3 | Segment-by-segment narration viewer with ElevenLabs markup |
| 7 | Animations | 4 | SVG + Rough.js previews per segment |
| 8 | Audio | 5 | Master + segment MP3 players, actual vs estimated duration |
| 9 | Player | 6 | Embedded interactive player (audio + animations synced) |
| 10 | YouTube Export | 7 | Locked ("Coming Soon") |
| 11 | Question Bank | 8 | Locked ("Coming Soon") |

---

## 15. Configuration & Secrets

**Streamlit Cloud Secrets:**
```toml
ANTHROPIC_API_KEY = "sk-ant-..."
ELEVENLABS_API_KEY = "sk_..."
ELEVENLABS_VOICE_ID = "SDNKIYEpTz0h56jQX8rA"
```

**Feature Flags** (`config.py`):
```python
YOUTUBE_EXPORT_ENABLED = False   # Set True when ready for YouTube launch
```

**Dependencies** (`requirements.txt`):
```
fastapi>=0.104.0, uvicorn>=0.24.0, pymupdf>=1.23.0, sqlalchemy>=2.0.0,
pydantic>=2.0.0, rapidfuzz>=3.0.0, python-multipart>=0.0.6, python-dotenv>=1.0.0,
aiofiles>=23.0.0, streamlit>=1.30.0, anthropic>=0.20.0, pillow>=10.0.0,
elevenlabs>=1.0.0, pydub>=0.25.0, audioop-lts>=0.2.1
```

**System packages** (`packages.txt`): `ffmpeg`

---

## 16. Proposed Refactor — Scribe / Speed Paint Visual Style

The next evolution of the pipeline replaces the current Rough.js iframe-based animation with a "Scribe" (speed-paint) visual style. This is a CSS `stroke-dashoffset` animation where a virtual hand draws SVG paths in real time, synchronised to the narrator's voice.

### 16.1 What Changes

| Component | Current | Proposed |
|-----------|---------|----------|
| Agent 3 output | Script segments with `sketch_cue` | Director's Manifest with `visual_action` markers (DRAW_START, DRAW_CONTINUE, GHOST_ONLY) and PAUSE_FOR_QUESTION markers |
| Agent 4 SVG | Primitives (rect, circle, line) + Rough.js rendering | All primitives converted to `<path>` elements with computed `pathLength`. Two layers per element: Ghost (opacity 0.1, dashed) and Ink (opacity 1.0) |
| Agent 6 player | Rough.js HTML in iframe, frame-based reveal | CSS `stroke-dashoffset` animation from L to 0 synced to audio duration. `requestAnimationFrame` loop positions a follower hand (marker_hand.png) at the current draw point |
| Playback experience | Rough.js elements appear in batches per frame | Continuous pen-drawing effect with visible hand tracing each path |

### 16.2 Agent 3 — The Socratic Director

The script prompt will be rewritten to output a "Director's Manifest" where each segment includes:

```json
{
  "segment_id": "s003",
  "type": "explore",
  "visual_action": "DRAW_START",
  "narration": "...",
  "elevenlabs_text": "... <break time='300ms'/> ...",
  "sketch_cue": { "action": "draw", "element": "...", "timing": "during" },
  "pause_for_question": false
}
```

- `DRAW_START` — begin drawing the sketch for this segment
- `DRAW_CONTINUE` — continue drawing from where the last segment left off
- `GHOST_ONLY` — show the ghost outline but don't ink it yet
- `PAUSE_FOR_QUESTION` — stop audio, freeze hand, trigger Q&A modal

ElevenLabs `<break time="300ms"/>` tags are inserted between segments so the Speed Paint hand can move between canvas areas without overlapping audio.

### 16.3 Agent 4 — SVG Path & Ghost Sketch Logic

**Path Conversion** (`agent_4_svg_utils.py`):
- All SVG primitives (rect, circle, line) converted to `<path d="...">` elements.
- For each path, total length L is computed (Python `svgpathtools` or JS `getTotalLength()`).

**Dual-Layer Rendering:**
- **Ghost Layer:** `stroke-opacity: 0.1`, `stroke-dasharray: "5,5"` — shows the full sketch outline immediately on segment start (student sees where the drawing is going).
- **Ink Layer:** `stroke-opacity: 1.0` — drawn progressively via CSS `stroke-dashoffset` animation, perfectly synced to audio duration.

**Manifest addition per path:**
```json
{
  "path_id": "p001",
  "d": "M 100 200 L 300 400 ...",
  "total_length": 542.7,
  "ghost_style": { "stroke-opacity": 0.1, "stroke-dasharray": "5,5" },
  "ink_style": { "stroke-opacity": 1.0 }
}
```

### 16.4 Agent 6 — The Scribe Playback Engine

**New player component** (`scribe_player.html`):

**The Animation:**
```css
.ink-path {
  stroke-dasharray: L;          /* total path length */
  stroke-dashoffset: L;         /* starts fully hidden */
  animation: draw Xs linear;    /* X = segment audio duration */
}
@keyframes draw {
  to { stroke-dashoffset: 0; }  /* fully visible */
}
```

**The Follower Hand:**
- A `marker_hand.png` overlay positioned via absolute CSS.
- `requestAnimationFrame` loop reads `audio.currentTime`, calculates draw progress, calls `path.getPointAtLength(progress * L)` to get (x, y), and positions the hand image there.

**State Machine:**
1. **On segment start:** Show Ghost sketch for the full chapter immediately.
2. **During narration:** Animate Ink path and move Hand along the draw point.
3. **On question hook:** Stop audio, freeze Hand at current (x, y), trigger Q&A modal.
4. **On resume:** Continue drawing from frozen position.

### 16.5 Pipeline Manifest (`pipeline_manifest.py`)

A new utility that merges:
- Agent 3's Director's Manifest (segment order, visual_actions, narration)
- Agent 4's path lengths (per-path L values, ghost/ink layer data)
- Agent 5's audio timestamps (actual measured durations per segment)

Into a single **Master Timeline** that the Scribe player reads:

```json
{
  "master_timeline": [
    {
      "segment_id": "s003",
      "audio_start": 82.1,
      "audio_end": 150.3,
      "visual_action": "DRAW_START",
      "paths": [
        {
          "path_id": "p001",
          "d": "M 100 200 ...",
          "total_length": 542.7,
          "draw_start_offset": 82.1,
          "draw_duration": 68.2
        }
      ]
    }
  ]
}
```

### 16.6 Technical Deliverables for Scribe Refactor

| File | Purpose |
|------|---------|
| `pipeline_manifest.py` | Merge Agent 3 script + Agent 4 paths + Agent 5 timestamps into Master Timeline |
| `scribe_player.html` | Frontend JS/CSS for stroke-dashoffset animation + follower hand |
| `agent_4_svg_utils.py` | Convert basic SVG primitives to animated `<path>` elements with length metadata |
| Updated Agent 3 prompts | Director's Manifest output format with visual_action markers |
| Updated Agent 4 renderer | Ghost + Ink dual-layer SVG output |
| Updated Agent 6 builder | Build Scribe player instead of Rough.js iframe player |

---

## 17. Key Architectural Decisions

1. **Actual durations only.** Agent 6 never uses Agent 3's estimated durations. All sync is based on Agent 5's measured MP3 lengths. This prevents audio/visual desync.

2. **Single-click pipeline.** The entire pipeline triggers from one "Run Analysis" button. Each agent auto-chains to the next. Manual pages exist for re-runs.

3. **Graceful degradation.** If ElevenLabs keys aren't configured, audio and player steps skip with a warning. Prior results (analysis, script, animations) are still saved.

4. **Agent 7 disabled by design.** MP4 rendering is separated from the student experience. Students see live browser playback (Agent 6); MP4 is only for YouTube distribution (Agent 7, future).

5. **Question bank flywheel.** Agent 8's cost structure improves over time — early users pay LLM cost, their answers get banked, later users get instant cached responses at zero cost.

6. **Embedded mode.** The entire pipeline runs in-process within Streamlit — no separate API server needed for deployment. FastAPI endpoints exist but are optional.
