# SketchCast AI — Technical Document

_Last updated: 2026-06-30. Reflects the current production architecture (web app on Vercel, worker on Railway, Supabase backend), the native video engine, and the student↔teacher + analytics system._

---

## 1. What SketchCast is

SketchCast turns a **textbook chapter (PDF)** into a **narrated lesson** plus a set of teaching materials, and lets teachers **assign** that content to students and **track** their progress.

For each chapter a teacher can generate:

- a **narrated lesson video** — slides whose objects *animate on* in teaching order (title writes on, divider grows, bullet points and diagrams draw in), with a free text-to-speech voiceover;
- an **editable slide deck** (`.pptx`) with the spoken Socratic narration in the speaker notes;
- teacher **documents** (`.docx`): lesson plan, class activities, worksheet, exam/test paper, case study.

Students assigned a chapter watch the lesson in-app (it counts as complete at 100%), and complete worksheets/exams either as an **interactive in-app quiz** (auto-graded) or by **uploading an answer file**. Teachers see completion, revisions, and scores in an **analytics dashboard**.

It is launching as a public **freemium beta**. The free tier deliberately uses zero-cost generation (free TTS, no AI images, deterministic native video). Paid upsells (AI images, premium voices, AI whiteboard video) are planned.

---

## 2. High-level architecture

Three independently-deployed pieces share one Supabase project as the source of truth:

```
            ┌─────────────────────────┐
  Teacher → │  Web app (Next.js)      │  Vercel · app.sketchcast.app
  Student → │  - auth, dashboards     │
            │  - upload, assign       │
            │  - serve results        │
            └───────────┬─────────────┘
                        │  reads/writes (RLS) + service-role ops
            ┌───────────▼─────────────┐
            │  Supabase               │  Postgres + Auth + Storage
            │  - tables + RLS         │
            │  - job queue (triggers) │
            │  - storage buckets      │
            └───────────▲─────────────┘
                        │  polls job queue, uploads artifacts (service role)
            ┌───────────┴─────────────┐
            │  Worker (Python)        │  Railway
            │  - agents 1–8 pipeline  │
            │  - docgen               │
            └─────────────────────────┘
```

**End-to-end flow**

1. Teacher uploads a chapter PDF → web app inserts a `books` row.
2. A DB trigger enqueues an `index_book` job → the worker extracts the chapter list and writes it back onto the book.
3. Teacher clicks **Generate** for a chapter/kind → app inserts a `generations` row.
4. A DB trigger enqueues a job → the worker runs the pipeline → uploads artifacts (deck, video, docx, questions.json) and marks the job done.
5. The app serves results via signed Storage URLs; teachers assign chapters to classes; students consume them; progress flows back.

The client **never** writes to the `jobs` table or talks to the worker directly — it only inserts `generations`/`books` rows, and DB triggers create the jobs. This keeps the queue authoritative and RLS-safe.

---

## 3. Repositories & hosting

| Repo | Contents | Host | Branch |
|------|----------|------|--------|
| `muqtadar1984-code/sketchcast-app` | Next.js web app + Supabase migrations (`supabase/migrations/`) | **Vercel** (`app.sketchcast.app`), auto-deploy | `main` |
| `muqtadar1984-code/sketchcast-ai` | Python worker, agents 1–8, `docgen`, legacy Streamlit monolith | **Railway** (web + worker split), auto-deploy | `master` |

- **Domain**: `sketchcast.app`, registered at **Cloudflare** (free DNS/SSL/CDN; `.app` forces HTTPS). The web app runs on the `app.` subdomain.
- **Supabase** provides Postgres, Auth, and Storage for both repos.
- A legacy **Streamlit** app (`streamlit_app.py`) in the worker repo is the original monolith that ran the agents in-process; the same agent modules are now driven by the headless worker.

---

## 4. The generation pipeline (agents)

The worker reuses a set of "agent" modules, each a stage of the pipeline. Orchestrated by `worker/process.py`.

| Agent | Module | Responsibility |
|-------|--------|----------------|
| **1 — Ingestion** | `agent1_ingestion/` | Extract text (PyMuPDF), extract images, and `structure_book()` → detect the chapter list `[{num, title}]`. Also renders a page-1 cover thumbnail. |
| **2 — Analysis** | `agent2_analysis/` | Claude analyzes a chapter → key concepts, difficulty assessment, "visual opportunities", and an episode breakdown. Detects grade + subject from the title/chapter list. |
| **3 — Scripts** | `agent3_scripts/` | Claude writes the **Socratic narration** per segment, plus the on-screen `slide_heading`, `slide_points`, an optional composable `slide_visual` (diagram), and optional `visual_request` (for paid AI imagery). |
| **4 — Images** | `agent4_image_gen/` | *(Free tier: skipped.)* Gemini "Nano Banana" image generation for the paid tier. |
| **5 — Slides** | `agent5_slides/` | `compose_slide()` renders the canonical 1280×720 slide (heading + bullets, **or** a diagram) and exports a PNG + a combined editable **PPTX** (with narration in speaker notes). |
| **6 — Video** | `agent6_animation/` | The **native renderer**: animates the slide's objects writing on, paced to the narration, with free **Edge-TTS** voiceover, then muxes audio. |
| **8 — Render** | `agent8_render/` | Concatenates per-segment MP4s into one chapter video with ffmpeg (stream-copy concat, memory-flat). |
| **QA** | `agent8_qa/`, `quality_checker.py` | Background quality checks. |

Claude calls go through `shared/claude_client.py`. Models default to the latest Claude family (cost-leaning for the free tier).

**Document generators** (`docgen/`) are dispatched by `generation_kind` for the non-video outputs: `lesson_plan`, `activity`, `worksheet`, `exam_paper`, `case_study`. Each asks Claude for structured JSON and renders it to a branded `.docx` (python-pptx/python-docx). Worksheet/exam **also** emit a `questions.json` for the interactive quiz player.

---

## 5. The native video engine (the differentiator)

The free-tier lesson video is rendered **deterministically, on-device, for $0** — no third-party video API. Built in three phases (all shipped):

- **Phase 1 — object renderer.** Instead of looping a flat slide image, the slide's *objects* animate on in teaching order (context line → title → divider grows → bullets write on, pen at the frontier). `agent5_slides/slide_builder.compose_slide()` is the single layout source; it returns the rendered image **plus ordered "reveal boxes"** which the renderer animates. The write-on phase is sized to fit inside the narration; the finished slide is then frozen (ffmpeg `tpad`) for the remainder, so the clip length equals the audio length. Perfect text fidelity, multilingual, instant.
- **Phase 2 — composable diagrams.** A small, deterministic diagram vocabulary (`flow`, `cycle`, `hierarchy`, `compare`) via `agent5_slides/diagram_builder.render_diagram()`. It draws into the slide and returns reveal-boxes, so diagrams animate object-by-object with the **same** animation mechanism — no new code path.
- **Phase 3 — icon objects.** An `icons` slide kind: labelled icon tiles. ~34 crisp **DejaVu symbol glyphs** + 6 hand-drawn PIL primitives (lightbulb, book, target, globe, search, clock), with an alias map and a generic fallback.

Claude (Agent 3) chooses when to emit a `slide_visual` diagram/icons block for structural concepts. Output codecs are uniform (libx264 / yuv420p / 1280×720 / 24 fps / aac) so Agent 8 can stream-copy the concat.

**Paid video options** were evaluated (see Roadmap): Gemini Nano Banana (static art), Google Veo (B-roll), HeyGen/D-ID (AI presenter), and **Golpo AI** (AI whiteboard explainer) — the leading candidate for the paid "speedpaint" tier.

---

## 6. Data model (Supabase Postgres)

Defined in `sketchcast-app/supabase/migrations/` (0001 → 0008). RLS is enabled on every table.

### Core tables

| Table | Purpose |
|-------|---------|
| `schools` | Optional org; independent teachers have `school_id = NULL`. |
| `profiles` | One row per `auth.users` user. `role` (school_admin/teacher/student), `full_name`, `school_id`, `username` (student login ID), `parent_email`, `must_reset_password`. |
| `classes` | A class owned by a teacher; has a `join_code`. |
| `enrollments` | Student ↔ class (many-to-many). |
| `books` | Uploaded source PDFs (shared library within a school). Holds the auto-detected `chapters` jsonb, `grade`, `subject`, `cover_path`. |
| `generations` | Teacher-owned generated outputs. `kind` (presentation / lesson_plan / worksheet / exam_paper / case_study / activity), `book_id`, `chapter_ref`, `params` jsonb, `status`. |
| `artifacts` | Files produced for a generation (`deck_pptx`, `video_mp4`, `docx`, `questions_json`, …) → paths in the `artifacts` bucket. |
| `jobs` | The worker's queue. Created by triggers, never by the client. |
| `generation_shares` | A teacher assigns a generation to a class (with `due_at`). The assignment primitive. |
| `branding` | A teacher's uploaded `.docx`/`.pptx` school templates. |
| `student_progress` | Per (generation × student) completion lifecycle: `assigned → in_progress → completed → revised`, `revision_count`, `progress_pct`, timestamps. |
| `submissions` | A student's worksheet/exam answer: `mode` (file/interactive), `answers` jsonb, `file_path`, `auto_score`/`max_score`, `teacher_score`/`feedback`, `grade_status`. |

### Enums
`user_role`, `book_kind`, `generation_kind`, `job_status`, `artifact_kind`, `progress_status`.

### Storage buckets (private)
- `uploads` — source PDFs + school branding templates (user manages own `{uid}/…` folder).
- `artifacts` — generated outputs (owner manages; served via signed URLs).
- `submissions` — student answer-file uploads (`{uid}/{genId}/…`).

### Triggers / functions
- `handle_new_user()` — creates a `profiles` row on signup (role from sign-up metadata).
- `create_job_for_generation()` / `create_index_job_for_book()` — enqueue worker jobs on insert.
- `touch_updated_at()` — maintains `updated_at`.

### Row-Level Security model
Every table is RLS-isolated. Cross-table checks use **SECURITY DEFINER helper functions** (which bypass RLS internally) to stay safe and non-recursive: `current_role_val()`, `current_school_id()`, `shared_to_me(gen)`, and (added in 0008) `owns_class(cls)`, `enrolled_in_class(cls)`, `teaches_student(stu)`.

Representative policies: teachers manage their own books/generations/classes; students read content **shared to a class they're enrolled in** (`shared_to_me`); a teacher reads the profiles + progress + submissions of students they teach; school admins get read-only visibility within their school.

> **Important RLS lesson (migration 0008):** never write a policy whose `USING` subqueries a table whose own policy subqueries back — Postgres raises *"infinite recursion detected in policy"* and authenticated reads silently return nothing (while the SQL editor, running as `postgres`, bypasses RLS and looks fine). Use a SECURITY DEFINER helper instead.

### Migrations (apply in order, in the Supabase SQL editor)
`0001_init` (schema + RLS) · `0002_book_chapters` · `0003_grade_subject_docs` · `0004_branding` · `0005_student_profiles` · `0006_progress_submissions` · `0007_questions_artifact` · `0008_fix_rls_recursion`.

---

## 7. Web app (Next.js)

**Stack:** Next.js 16 (App Router, Turbopack), React 19, Tailwind v4, `@supabase/ssr`. (Note: this Next version renamed `middleware.ts` → `proxy.ts`; `cookies()` is async.)

**Auth & routing**
- `src/proxy.ts` + `utils/supabase/proxy.ts` refresh the Supabase session on every request and guard `/dashboard*` (unauthenticated → `/login`).
- `utils/supabase/{server,client,admin}.ts` — server (cookie-bound, RLS) client, browser client, and a **service-role admin** client (server-only).
- Login accepts a teacher **email** or a student **ID** (an ID with no `@` is mapped to `username@students.sketchcast.app`).

**Teacher dashboard** (`src/app/dashboard/page.tsx`, role-gated)
- **Library** — books grouped Grade → Subject; per-chapter content via `ChapterGenerate` (checkboxes per document type + a single "Generate (N)" button, plus "Assign chapter").
- **Classes & students** (`classes-card.tsx`) — create classes, provision students (→ login IDs + temp passwords), roster, join code, "Show progress".
- **School branding**, **Upload**, and the **Analytics** nav.

**Student dashboard** ("My lessons") — assigned chapters grouped by class; each item is interactive: the **lesson plays in-app** and completes at 100% (re-opening a finished one → *revised*); worksheets/exams offer **Take quiz** (interactive) or **Submit file**.

**Analytics** (`dashboard/analytics/page.tsx`) — metric cards (classes / students / assignments / completion % / overdue / to-grade), per-class completion bars, "most revised" hotspots, and a **grading queue** (`grade-list.tsx`).

**API routes**
- `POST /api/students` — teacher-only student provisioning (service role): creates an auth user per student (synthetic email, temp password), fills the profile, enrolls them, and returns the credentials.
- `GET /api/submission-url` — teacher-only signed URL for a student's uploaded submission file (RLS gates the read; service role signs the object).

**Design system ("Warm Scholarly")** — central tokens in `globals.css` (cream `#FBF6EC`, forest green `#2E6B4E`, warm amber `#C77F2A`, ink), **Fraunces** (serif headings) + **Inter** (body) via `next/font`, and reusable component classes authored as Tailwind v4 `@utility` (`card`, `btn-primary`, `field`, `chip`, …). (Tailwind v4 note: custom classes must be `@utility`, not `@layer components`, to be reliably emitted.)

---

## 8. Student ↔ teacher system

Built in three phases on top of the existing `classes`/`enrollments`/`generation_shares` foundation.

- **Phase A — identity + assignment.** Student provisioning (school-issued **ID + password** given to parents; the **parent's email** is stored for communication, not as the login, since siblings can share one); join-by-code; "Assign chapter" shares all *student-facing* materials (lesson video/deck, worksheet, exam, activity, case study — **never** the teacher lesson plan) to a class with a due date; the read-only student dashboard.
- **Phase B — completion + reverse feedback.** `student_progress` lifecycle; lesson video completes at 100%, reopening → *revised*; worksheets/exams via answer-file upload; the teacher's per-class roster (Completed / Revised / Incomplete / Overdue).
- **Phase C — interactive quizzes + analytics.** The worker emits `questions.json`; the in-app **quiz player** auto-grades objective questions (fill-blank, true/false, match) and stores answers + score; subjective/short answers are flagged for the teacher; the analytics dashboard + grading queue.

**Completion rules (decided):** *Completed = 100%* for everything. *Revised = any re-open* of a completed item. Tests/worksheets support **both** an interactive auto-graded path **and** a file-upload path; grading is hybrid (auto where objective, manual otherwise).

---

## 9. Security & auth

- **Supabase Auth** — Google + email/password, with email verification + forgot-password.
- **Students** — for minors, school/teacher-provisioned accounts: name-derived **username** (`first.last`, numeric suffix on collision), a synthetic unique login email, a random temp password, and the parent's email for comms. Public self-signup is gated to 18+.
- **RLS everywhere** — each user sees only their own data + what's explicitly shared (see §6).
- **Service-role key** (`SUPABASE_SERVICE_ROLE_KEY`) — used **server-side only** (the provisioning route, and signing students' entitled artifacts/submissions). Stored in Vercel + Railway env, never shipped to the browser.

---

## 10. Deployment & operations

- **Web app** → push to `main` → Vercel auto-builds/deploys. (Build gates on TypeScript, not ESLint.)
- **Worker** → push to `master` → Railway auto-deploys.
- **Supabase migrations** are applied **manually** in the SQL editor, in order, and **must precede** the matching app deploy (a migration-dependent query against a missing column errors). Migration `0007` (an `ALTER TYPE ... ADD VALUE`) is run as its own statement.

**Environment variables**

| Var | Where | Purpose |
|-----|-------|---------|
| `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_ANON_KEY` | Vercel (public) | Browser/SSR Supabase client. |
| `SUPABASE_SERVICE_ROLE_KEY` | Vercel + Railway (secret) | Server-only admin ops (provisioning, signing). |
| `GOOGLE_AI_API_KEY` | Railway | Gemini (paid image gen). |
| `SKETCHCAST_TTS_VOICE` | Railway (optional) | Edge-TTS voice override. |

**Generation cost levers (free tier):** free Edge/Google TTS (no ElevenLabs), no AI images, deterministic native video, a cheap Claude model, per-teacher daily generation caps (planned, enforced DB-side), and a watermark.

---

## 11. Tech stack summary

| Layer | Technology |
|-------|-----------|
| Web app | Next.js 16, React 19, TypeScript, Tailwind v4 |
| Backend / DB | Supabase (Postgres + RLS, Auth, Storage) |
| Worker | Python (FastAPI for the legacy API; headless job processor), PyMuPDF, Pillow, python-pptx/docx, ffmpeg (imageio-ffmpeg), Edge-TTS |
| AI | Anthropic Claude (scripts, analysis, documents); Google Gemini (paid images) |
| Hosting | Vercel (app), Railway (worker), Cloudflare (DNS), Supabase (data) |

---

## 12. Key design decisions & gotchas

- **Object-animation video** beats a flat slide loop at $0 and is the product's visual identity; diagrams + icons reuse the same reveal-box animation.
- **Derive the assigned set, record only activity** — the "what's assigned" set is `generation_shares ⋈ enrollments` (auto-adjusts to enrollment changes); `student_progress` only records actual student activity; "incomplete/overdue" falls out by absence.
- **RLS recursion** — cross-table policies must use SECURITY DEFINER helpers (migration 0008 fixed a recursion that silently nulled authenticated reads).
- **Tailwind v4** — custom component classes must be `@utility`; `@theme` (non-inline) emits the CSS vars used by them.
- **React 19 purity lint** — `Date.now()` / `Math.random()` in render are flagged; compute time-based values server-side and order options deterministically.
- **Migrations before deploys** — a migration-dependent query against a not-yet-applied column will error; the app degrades gracefully where possible.

---

## 13. Roadmap

- **Paid video tier** — **Golpo AI** (AI whiteboard explainer; REST API; ~$2/min; accepts our `custom_script` + our own narration audio) is the leading candidate, with HeyGen/D-ID (AI presenter) and Veo (B-roll) as later layers. Weigh cost, data-egress/copyright, and latency before committing.
- **Paid feature gates** — AI images (Nano Banana), premium voices (ElevenLabs), the whiteboard video.
- **Per-teacher daily generation caps** — enforced DB-side (a `BEFORE INSERT` trigger / RPC on `generations`) so the client can't bypass.
- **Parent communications** — wire `parent_email` to a provider (Resend/SES) for assignment/completion notifications + password resets.
- **Native mobile apps** (Android + iOS) using the `.app` universal links.
- **Forced password reset** on first student login; a delete-class UI; richer diagram/icon objects.
