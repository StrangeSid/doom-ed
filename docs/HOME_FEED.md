# Home Feed & Adaptive Practice — Feature Plan

**Status:** Approved; Phase 0/1 backend implemented
**Date:** 2026-10-25
**Baseline:** 176 unit/integration tests pass (pytest -q --ignore=tests/test_e2e_playwright.py)
**Scope:** feed-first home, adaptive practice engine, bidirectional teaching loop
**New constraint:** the LMS is an optional integration plugin, never a core dependency.

> This plan is additive. The current chat, viewer, triage endpoints, and security model stay. The feed ships behind feature flags, and the lesson view remains one tap away.

---

## 1. Executive summary

Doom-ed currently opens on a three-pane triage + chat surface. The student must decide what to study, then start a lesson to get a graded check. Practice only exists inside a lesson, and quiz state is bound to a thread (**quiz_states.thread_id**). Nothing schedules retrieval, nothing models mastery per skill, and nothing feeds the results of practice back into teaching.

This change makes the home page a scrollable study feed. The feed is a sequence of small, server-graded practice cards, checkpoint tests, repair cards, and teach handoffs, ordered by an adaptive planner. Feed and teacher share one evidence stream: feed attempts become the teacher's brief, and lesson quizzes update the same skill state and schedule.

Key outcomes:

- One screen answers "what should I do right now?" with evidence, not just a static list.
- Retrieval practice, spacing, interleaving, and delayed feedback become the default mode of study.
- Every lesson is grounded in the student's recent practice, and every lesson changes what appears next.
- The student can see why a card was chosen and can hand any card to the teacher.
- No live school test is ever ingested or answered; the engine is formative-only.
- The LMS is one optional addon among others. With no plugin installed, local study still works end to end.

### 1.1 Implementation status

Done on 2026-10-25:

- Additive practice schema and guarded migrations in **app/db.py**.
- **app/practice/** core: config, models, grading/scheduling, deterministic planner, owner-scoped store, feed service, and feature-flagged API.
- **app/integrations/** plugin contract and registry, with the existing Indus bridge wrapped as the built-in adapter.
- Feed, attempt, event, progress, teach, settings, feedback, and reset endpoints, all off by default behind **DOOMED_PRACTICE_ENGINE**.
- 19 new practice tests; 176 unit/integration tests pass.

Still to build:

- Skill catalog seed and the generated/verified item bank.
- The scrollable home UI behind **DOOMED_HOME_FEED**.
- Teaching bridge injection: practice brief in the teacher prompt, lesson quiz skill ids, after-lesson cards.
- Checkpoints, progress view, and item quality reports UI.

---

## 2. What exists today

### 2.1 UI

- **web/index.html** defines a two-column shell: left triage pane and center chat pane; lesson log and file viewer are slide-over drawers.
- **web/assets/app.js** owns auth, triage loading, SSE chat, cards, citations, and the log.
- **web/assets/cards.js** already has a reusable, server-graded quiz card (**renderQuizCard**) with a pinned "I don't know" option. This is the natural rendering primitive for feed items.
- No home feed, no scroll controller, no cursor pagination, no view routing.

### 2.2 Practice and data

- Practice is chat-only. The teacher calls the **quiz** tool in **app/orchestrator.py**; the card pauses the turn and **submit_card_reply** grades it.
- **quiz_states** in **app/db.py** is thread-bound and stores question, shuffled options, correct indices, explanation, and answer. Correct answers stay server-side until submission.
- There is no skill graph, item bank, student mastery state, scheduling queue, attempt ledger, checkpoint run, or feed plan.
- The only "weak topic" source is **app/lms_bridge.py build_study_triage**: graded EOL/FA results when LMS is linked, plus chat quiz misses; standalone mode uses a static curriculum fallback.
- There is no signal pipeline for dwell, skip, confidence, reveal, or teach clicks.

### 2.3 Content

- The IB client searches 53k+ resource metadata and pairs papers with markschemes. It does not provide question items or answers.
- The file reader can extract text and rasterize pages. It is a reference pipeline, not an item bank.
- Item content must therefore be generated, verified, and cached locally, or referenced from student-owned documents. Verbatim redistribution of past-paper questions should be avoided.

### 2.4 Constraints that shape the design

- Vanilla HTML/CSS/ES modules; no build step.
- Backend-for-frontend only; browser never calls LMS, IB catalog, or LLM directly.
- Owner scoping is mandatory on every read/write: owner = tenant:uid.
- Zero synthetic student data. Generated practice content is real content only when grounded, labelled, and verified; it is not fake student activity.
- Academic integrity: never answer or submit a live/unsubmitted LMS test; **assert_not_live_test** remains non-negotiable.
- Local-first storage, Fernet at rest, 0600 SQLite, SSRF-guarded fetches, DOMPurify on render.
- Existing endpoints and tests must keep passing while the feed is flagged off.

### 2.5 Implication

This is not a UI reskin. It adds a second pedagogy surface with its own state machine, and it changes the contract between practice and teaching. The plan therefore separates:

1. curriculum and item content,
2. student evidence and scheduling,
3. feed planning and API,
4. UI,
5. teaching bridge,
6. content generation and verification.

### 2.6 LMS integration is an optional plugin

The core study loop must not import or require a school LMS. The new boundary is:

- **app/integrations/base.py** defines **LMSPlugin**, **LearningContext**, and integration exceptions.
- **app/integrations/registry.py** registers plugins and resolves the active one for a session. With no plugin, **build_learning_context** returns an empty local context.
- **app/integrations/indus.py** is the built-in adapter around the existing **app/lms_bridge.py**. The bridge stays supported, but it is now behind an optional plugin.
- **app/practice/** imports no LMS module. Feed planning, attempts, scheduling, and progress work with zero plugins.
- **/api/login** and **/api/session** resolve the plugin through the registry. **/api/integrations** reports installed/connected plugins without exposing credentials.
- The teacher tools **search_lms_files**, **browse_lms_folder**, and **confirm_lms_write** resolve the active plugin and return the standalone fallback when none is connected.
- **DOOMED_LMS_PLUGIN=0**, or a missing upstream package, disables the plugin without breaking local study.
- Legacy **/api/study** and the chat-turn triage still use the bridge as a compatibility path. They are scheduled to move to the practice-first local context in phase 3; until then, the legacy route is explicitly not a dependency of the feed.

Rule: if removing the plugin breaks the home feed, the plugin boundary is wrong.

---

## 3. Goals and non-goals

### Goals

- **Retrieval-first home.** The default action is answering a calibrated question, not opening a blank chat.
- **Adaptive but explainable.** The planner targets the student's frontier, but every card carries a reason code and a one-line explanation.
- **Closed loop.** Feed attempts improve teaching; lesson outcomes improve the feed.
- **Exam-aware.** Subject weighting and countdown shape the mix without turning the app into a cram machine.
- **Works standalone and linked.** No LMS is required; linked LMS adds deadlines and graded evidence, never live test content.
- **Fast.** Feed requests are database/planner calls, not LLM calls. First card renders immediately.

### Non-goals

- Replacing the Socratic teacher or the file viewer.
- Answering, submitting, or simulating a live school assessment.
- Proctoring, grades, or claims that a checkpoint predicts an exam score.
- Leaderboards, public profiles, or points-first gamification.
- Infinite doomscrolling as the only mode. The default is a finite daily plan; "keep going" is opt-in.
- A native mobile app or a cloud sync service in this change.
- Adding a frontend framework, bundler, or external ML service.

---

## 4. Product experience

### 4.1 Views and navigation

A small hash router keeps the zero-build constraint and avoids server catch-all routes:

- **#/home** — scrollable study feed, default for authenticated students.
- **#/lesson/<thread_id>** — existing chat lesson, log, and viewer.
- **#/progress** — mastery map and review queue (phase 2).
- **#/practice** — optional full-screen "extra practice" stream.

Navigation:

- Left rail: brand, Home, Lessons, Progress, account.
- On desktop, the existing triage pane can remain as the left rail in phase 1; in phase 4 it becomes a "Today" right rail.
- On mobile, a bottom bar: Home, Practice, Coach, Progress.
- The lesson composer, lesson log, and viewer keep their current behavior.

Unauthenticated home: sign-in and "Start Independent IB Study" remain the only actions, plus a short preview of how the feed works. No feed data is fetched without a session.

### 4.2 Feed anatomy

- Sticky header: greeting, exam countdown, daily progress ring, streak, settings.
- "Today's plan" strip: answered/goal, due reviews, weak skills, next checkpoint.
- Card stack: one idea per card, generous spacing, subject color, skill label, difficulty band, estimated time.
- End-of-plan state: recap, what improved, what is due tomorrow, "Extra practice" and "Open a lesson".
- A "Load more" button always exists as a fallback to IntersectionObserver infinite scroll.
- A sticky "Ask the coach" action on mobile; each card has "Ask coach" too.

### 4.3 Card types

| Type | Purpose | Interaction | Grading |
|---|---|---|---|
| retrieval | Due review of a previously seen skill | MCQ/numeric, immediate feedback | Server-side, updates mastery/schedule |
| practice | New or weak-skill practice | MCQ/numeric, immediate feedback | Server-side |
| repair | Re-teach a missed prerequisite with a short scaffold | Short explanation + one easier item | Server-side |
| checkpoint | 5–8 item formative test across a skill cluster | Answer all, submit once | Delayed feedback, score + remediation |
| teach | Handoff when explanation is needed | Opens lesson with seeded brief | Ungraded |
| application | IB-style multi-step question | MCQ or numeric, step hints optional | Server-side; phase 3 free response |
| recap | End-of-plan summary | Read, rate confidence | Ungraded |
| deadline | LMS due work surfaced as context | Opens lesson or viewer | Ungraded |
| reflection | "What felt shaky?" | One tap or text | Feeds next plan with low weight |

### 4.4 Card mechanics

- **Answer commit.** Selecting an option does not reveal correctness; submit grades server-side. This prevents probing and supports checkpoint mode.
- **Immediate feedback** for retrieval/practice: correct/incorrect, the correct option, a concise explanation, and "why this card" remains available.
- **"I don't know" is first-class.** It is pinned as the first option, never shuffled, and is treated as an unknown signal, not a misconception. Two unknowns in one skill insert a teach card.
- **Confidence** (optional, one tap: low/medium/high) after submitting. It adjusts the strength of mastery updates and helps distinguish lucky guesses.
- **Skip** does not grade. Three skips on one skill suppress that skill for the session and show a gentler "not now" state.
- **Report** flags ambiguous, wrong, outdated, too easy, or too hard. Two reports quarantine an item.
- **Bookmark** adds the skill to a personal review queue.
- **Ask coach** creates a lesson seeded with this item's attempt evidence.
- **Keyboard**: 1–5 select, Enter submit, J/K next/previous card, R reveal explanation after grading. Focus is never stolen by auto-advance.
- **No auto-advance.** The student controls the scroll; this preserves reflection time and avoids a slot-machine feel.

### 4.5 Scroll behavior

- Cursor pagination: server returns next_cursor; the client requests the next page when the sentinel is within 800px.
- The daily plan is persisted server-side, so reload returns the same sequence and preserves answers.
- The client renders at most 30 cards in the DOM; older cards are collapsed to summary rows if needed.
- Dwell is measured only while the card is at least 50% visible and the tab is active. It is batched, capped, and never used as a mastery signal.
- Infinite "Extra practice" mode continues with softer caps; it is clearly labelled and can be ended at any time.
- Reduced motion is respected; skeletons are static; no parallax or autoplay.

### 4.6 Checkpoints ("tests")

- Triggered when a skill cluster has enough evidence (see §7.6) or when the student taps "Check my understanding".
- 5–8 items across 2–4 skills, mostly medium difficulty, no feedback until submit.
- Optional soft timer, off by default. The timer never force-submits; it shows elapsed time.
- Pass threshold 80%. On pass: schedule maintenance reviews at 7 and 21 days and mark the cluster checkpoint-passed.
- On fail: per-item remediation items are queued; the same checkpoint cannot be retaken for 24 hours.
- The UI calls it a "checkpoint", not an exam, and states that it is formative and not sent to school.

### 4.7 Teach handoff

- Any practice, repair, or checkpoint card has "Teach me this".
- The client posts skill ids, attempt ids, and an intent (teach, deepen, fix_misconception).
- The server creates a normal lesson thread with origin = practice, stores a practice_brief in thread metadata, and returns a seed message.
- The client navigates to #/lesson/<id> and sends the seed message through the existing SSE endpoint. No parallel chat implementation.
- The teacher opens with the weak node, not with "what do you want to learn?".

### 4.8 Progress view

- Subject → unit → skill mastery bands: solid, learning, weak, unseen.
- Due counts, recent errors, checkpoint history, streak, minutes practiced.
- "Review now" filters the feed to due items.
- No public ranking, no comparative percentiles, no exam-score prediction.

### 4.9 Onboarding and calibration

- Existing profile (subjects, grade, target session) is reused.
- If no skill state exists, the feed starts with a short calibration set: 6 items per subject, 2 easy / 2 medium / 2 hard, from verified items.
- If the item bank is below the minimum, the feed shows a "preparing your practice" state and uses teach cards / IB reference reading instead of inventing questions. No fake mastery.
- The student can adjust daily goal and subject focus in settings.

### 4.10 Example session

1. Home shows 12 cards, 4 done, 5 due reviews, one weak skill.
2. Warm-up retrieval on kinematics: correct, explanation, next due in 4 days.
3. Vector components: wrong; the distractor reveals a sign error.
4. Repair card: a short coordinate-axis scaffold plus an easier item; still wrong.
5. Teach card: "Teach me vector components" → lesson opens with the brief and two recent attempts.
6. Teacher probes, teaches, and quizzes with skill_ids = ["phys.kin.vectors"]; the quiz updates the same skill state.
7. Back home: an after_lesson retrieval card appears; next day it is due again. The checkpoint becomes eligible after two more skills reach 0.6 mastery.

---

## 5. The closed loop

~~~mermaid
flowchart TB
  subgraph Home["Home feed (scroll)"]
    FEED["Feed cards<br/>practice · repair · checkpoint · teach"]
    EVENTS["Scroll signals<br/>dwell · skip · confidence · report"]
  end
  subgraph Engine["Adaptive practice engine"]
    PLAN["Planner<br/>due + weak + prereq + exam"]
    MODEL["Mastery + scheduler<br/>BKT · Elo · FSRS-lite"]
    BANK["Item bank<br/>verified, versioned, encrypted answers"]
    ATT["Attempt ledger<br/>owner-scoped evidence"]
  end
  subgraph Teach["Teaching surface (lesson)"]
    BRIEF["Practice brief"]
    TEACHER["Socratic teacher<br/>probe → plan → teach"]
    QUIZ["Lesson quiz<br/>skill_ids + grade"]
  end
  subgraph LMS["Optional LMS triage"]
    TRIAGE["Graded EOL/FA weak topics<br/>deadlines · calendar"]
  end

  FEED --> PLAN
  PLAN --> BANK
  BANK --> FEED
  FEED --> ATT
  EVENTS --> ATT
  ATT --> MODEL
  MODEL --> PLAN
  ATT --> BRIEF
  BRIEF --> TEACHER
  TEACHER --> QUIZ
  QUIZ --> ATT
  QUIZ --> MODEL
  TRIAGE --> PLAN
  TRIAGE --> BRIEF
~~~

### 5.1 Feed → teaching

The feed supplies:

- the exact item, selected option, correct option, and explanation the student just saw;
- whether the student said "I don't know", skipped, or reported the item;
- latency, confidence, and whether the explanation was revealed;
- the skill ids implied by the item and the planner's reason;
- a suggested entry node (the weakest prerequisite of the failed skill).

The teacher brief is generated on the server and injected into the system prompt. The browser never invents the brief.

### 5.2 Teaching → feed

The lesson supplies:

- every quiz grade, with skill_ids attached by the teacher;
- whether the student answered "I don't know";
- the thread phase (probe/plan/teach) and the dependency map;
- goal changes and plan approval;
- time spent and diagrams viewed (low weight).

These update the same student_skill_state, practice_attempts, and scheduler as feed attempts. A lesson quiz is not a separate universe.

### 5.3 Signal contract

| Signal | Produced by | Stored in | Used for | Weight |
|---|---|---|---|---|
| Correct / incorrect | Feed attempt, lesson quiz | practice_attempts, quiz_states | Mastery, schedule, brief | High |
| Selected distractor | Same | practice_attempts.selected_json | Misconception map, teach entry | High |
| "I don't know" | Same | dont_know | Unknown queue, teach trigger | Medium |
| Confidence | Feed card | confidence | BKT update strength | Medium |
| Latency | Feed card | latency_ms | Guess detection only | Low |
| Skip | Feed events | feed_events | Presentation penalty, not mastery | Low |
| Dwell | Feed events | feed_events | Engagement, card design | Very low |
| Reveal explanation | Feed events | feed_events | Explanation dependence | Low |
| Report | Feed events | item_reports | Item quarantine | Quality only |
| Bookmark | Feed events | feed_events | Review queue | Medium |
| Teach click | Feed | threads.origin_json | Entry node, loop metric | High |
| Node taught | Lesson quiz skill_ids | student_skill_state | Schedule retrieval | High |
| Graded LMS weakness | Triage | Cached triage | Seed skills, feed cards | Medium |
| Deadline | Triage | Cached triage | Deadline card | Context only |

### 5.4 Loop guardrails

- **No single signal owns mastery.** Only graded attempts change mastery; dwell and skips never do.
- **Exploration is mandatory.** At least 10% of cards come from skills outside the current weak set, so the teacher and planner do not collapse into a self-confirming loop.
- **Coverage checks.** A weekly checkpoint samples across enrolled subjects, including skills not recently practiced.
- **Teacher prose is not evidence.** The brief may suggest an entry node, but only quiz attempts update state.
- **Unknowns are distinct from errors.** "I don't know" triggers explanation and teaching; a wrong distractor triggers misconception repair.

---

## 6. Feature breakdown

| ID | Feature | Priority | Acceptance |
|---|---|---|---|
| F1 | Skill graph and IB subject catalog | P0 | Skills have subject, level, prereqs, source; owner-agnostic; queryable by enrolled subject |
| F2 | Item bank with provenance and verification | P0 | Items are immutable/versioned; answers encrypted; no answer in feed payload; draft items excluded |
| F3 | Attempt ledger and grading | P0 | Owner-scoped; idempotent; MCQ/multi/numeric; explanation only after submit |
| F4 | Mastery, Elo, and scheduler | P0 | Deterministic updates; due queue; tests for edge cases |
| F5 | Adaptive planner | P0 | Prereq-safe, diverse, capped, seeded/deterministic; reason codes |
| F6 | Feed API and daily plan | P0 | Cursor-stable; persisted across reload; p95 < 200ms locally |
| F7 | Scrollable home UI | P0 | Cards render, answer, feedback, load more, mobile, keyboard; no XSS |
| F8 | Teach handoff and practice brief | P0 | Creates a normal thread; brief in prompt; seed message; no duplicate chat stack |
| F9 | Lesson → algorithm | P0 | quiz accepts skill_ids; card reply updates practice state; feed changes next load |
| F10 | Checkpoints | P1 | 5–8 items, delayed feedback, remediation, 24h cooldown |
| F11 | Progress view | P1 | Mastery map, due counts, history; no rankings |
| F12 | Item generation and verification | P1 | Structured JSON, validator, independent verify pass, dedupe, cost cap |
| F13 | Feed events and quality reports | P1 | Batched, validated, capped, owner-scoped; reports quarantine |
| F14 | Settings, reset, and deletion | P1 | Daily goal, timezone, focus subjects; reset clears practice state |
| F15 | Triage integration | P2 | Practice misses appear in weak topics; deadlines appear as context cards |
| F16 | Hosted calibration and experiments | P2 | Shared verified items, aggregate item stats, no student data sharing |

---

## 7. Algorithm specification

### 7.1 State

Per student and skill:

- ability theta (Elo-style, logistic scale);
- mastery probability p (BKT);
- stability S in days (FSRS-lite);
- attempts, correct, unknowns, streak, last seen, due at;
- checkpoint eligibility.

Per item:

- difficulty b, discrimination a (used later for IRT-style targeting), guessing g;
- skill ids, format, source, verification, version.

Defaults: theta = 0, p = 0.35, S = 1.0, g = 1/option_count (0.25 for 4 options), slip = 0.10, transition = 0.15. These are priors, not claims about the student; calibration replaces them with evidence.

### 7.2 Grading and mastery update

Expected success:

~~~text
P = 1 / (1 + exp(-(theta - b)))
theta' = theta + K_s * (y - P)
b'     = b - K_i * (y - P)
~~~

- y is 1 for correct, 0 for incorrect or unknown.
- K_s decays from 0.30 to 0.06 as skill attempts grow: K_s = max(0.06, 0.30 / (1 + attempts/8)).
- K_i is small and decays faster: K_i = max(0.01, 0.08 / (1 + item_attempts/20)); item calibration is aggregate-only and never stores owner ids.
- Confidence modulates the learner update: low confidence x0.6, medium x1.0, high x1.25. This makes a lucky guess worth less and a confident error worth more.
- Latency only flags "fast correct" on hard items as a possible guess; it is capped at 5 minutes and not used in the primary update.

BKT posterior, with slip s_slip = 0.10, guess G, transition T = 0.15:

~~~text
prior = p + (1 - p) * T
correct:  post = prior * (1 - s_slip) / (prior * (1 - s_slip) + (1 - prior) * G)
incorrect: post = prior * s_slip / (prior * s_slip + (1 - prior) * (1 - G))
unknown:  use slip 0.35 in the incorrect branch, and flag unknown
~~~

Then blend theta and p into a band:

- solid: p >= 0.75 and at least 3 attempts across 2 days;
- learning: 0.40 <= p < 0.75;
- weak: p < 0.40 with at least 2 attempts;
- unseen: no attempts.

The bands are for UI and gating; the planner uses the continuous values.

Grading rules:

- mcq_single: the selected set must equal exactly one correct index; no partial credit.
- mcq_multi: exact set match; partial selections are incorrect; "I don't know" is recorded separately.
- numeric: parse a finite number; accept a relative tolerance of 1e-3 or an absolute tolerance of 1e-6. Units are stored on the item and are not parsed from free text.
- Unknown or malformed selections return 422 and do not create an attempt.

### 7.3 Scheduling

Use a simplified FSRS curve, not a black box:

~~~text
R(t, S) = (1 + t / (9S))^(-1)
I = 9S * (1 / R_target - 1)      # days
~~~

- R_target default 0.90; configurable 0.85–0.92.
- On correct: S' = min(365, S * (1 + 0.7 * (1 - p))).
- On incorrect: S' = max(0.5, 0.3 * S) and a repair item is due in the same session (10 minutes) or next session.
- On unknown: S' = max(0.5, 0.5 * S); schedule a teach card before the next retrieval item.
- Clamp the interval to 1–180 days. A failed item cannot become "due tomorrow" and disappear; it re-enters the current session if the student is still active.
- Each skill has an independent due time. The feed does not use a single global daily review queue.

### 7.4 Candidate filtering (hard constraints)

An item is eligible only if:

- it is not retired, reported twice, or unverified (unless the request is an explicit diagnostics pool);
- its skills belong to the student's enrolled subjects and level;
- its prerequisites have mastery >= 0.50, unless it is a repair/teach card;
- it was not served in the last 3 cards or the last 24 hours, unless it is due;
- the per-skill daily cap (3 attempts) and per-format cap (4) are not exceeded;
- the daily new-skill cap (2) and new-item cap (5) are not exceeded;
- the student has not skipped the skill 3 times this session;
- it is not derived from a live/unsubmitted LMS assessment;
- the item's content version is current.

If the pool is empty, return a teach card or a deadline card; never fabricate an item.

### 7.5 Ranking

For each candidate, compute a normalized priority:

~~~text
priority =
  1.00 * due_pressure        # 1 if overdue, decays to 0 at 7 days
+ 0.90 * mastery_gap         # max(0, target - p)
+ 0.60 * exam_weight         # subject proximity/weight, clipped 0..1
+ 0.40 * teach_signal        # recent lesson node or teach click
+ 0.30 * misconception       # unresolved wrong pattern
+ 0.20 * novelty             # not seen in 14 days
- 0.50 * repetition         # same skill/format in last 3 cards
- 0.30 * skip_penalty
~~~

- Sample from the top 10 candidates with a softmax temperature of 0.35 early and 0.15 within 30 days of the exam. This is exploration, not randomness.
- Seed the RNG with hash(owner, day, position) so the same plan is reproducible; tests can pin the seed.
- Re-rank with a diversity pass: no more than 2 consecutive cards from the same subject or skill; alternate formats where possible.
- The highest contributing term becomes the reason_code; a template turns it into one sentence ("You last saw this 4 days ago" or "This prerequisite unlocks your next unit").

### 7.6 Plan composition and checkpoints

Daily default: 12 cards / about 20 minutes. Mix:

- 50% due reviews (oldest due first, but with a warm-up);
- 30% weak-skill practice (p < 0.6, prerequisites met);
- 15% new skill introduction;
- 5% stretch (success target ~0.55).

Rules:

- First card is a short, likely-correct retrieval item to build momentum.
- After 2 consecutive misses on one skill: insert a repair card; if the next item is also missed, insert a teach card.
- After 2 unknowns on one skill: insert a teach card.
- Checkpoint eligibility when: a cluster has >= 6 attempts, >= 2 skills, and at least one skill at p >= 0.60; or the student taps "Check my understanding"; or 7 days have passed since the last checkpoint for that subject.
- Max 1 checkpoint per day and 1 per subject per week.
- End-of-plan recap always appears; it is not a graded card.
- "Extra practice" continues with the same rules but relaxes the daily goal and caps new items at 2 per extra page.

### 7.7 Cold start

- No skill state: run calibration.
- Item pool below 3 verified items for a skill: show a teach card or a sourced reading card; queue generation in the background.
- No generated items at all: the feed can still run on verified seed items or lesson quizzes; it must not show fake attempts or fake mastery.
- If the student has LMS graded EOL/FA results, use the missed questions as topic seeds (never as copied items) and map them to skills.

### 7.8 Determinism and explainability

- Planner is pure given (state snapshot, candidate pool, seed, config). Same inputs → same plan. This is testable.
- Every card stores its reason code and the score components that produced it; the UI can show "Why this card?" without another model call.
- The student can dismiss a reason ("not now") and the planner records it; dismissal is temporary, not a mastery change.
- No hidden "engagement optimization" that overrides due reviews.

### 7.9 Worked example

Student: Physics HL, weak in vector components, exam in 180 days.

1. Due review: kinematics item, last seen 4 days ago. Correct. p 0.72→0.79, S 3→5.2 days, due in 5 days.
2. Weak practice: vector components, difficulty medium. Wrong with a sign error; p 0.41→0.29, S 2→0.6, repair due now.
3. Repair card: coordinate axes and component signs, one easy item. Correct; p 0.29→0.44, S 0.6→1.0, due tomorrow.
4. Next item still wrong (different distractor). Two misses → teach card. Brief lists both attempts and the sign pattern.
5. Lesson: teacher probes, teaches components, and quizzes with skill_ids = ["phys.kin.vectors"]. Correct; p→0.62; retrieval scheduled for tomorrow and 3 days.
6. Home next day: after_lesson retrieval appears first; a checkpoint becomes eligible after the second skill crosses 0.60.

### 7.10 Tuning and guardrails

- Config defaults in one place (**app/practice/config.py**): weights, caps, target retention, daily goal, slip/guess/transition, checkpoint thresholds.
- Do not tune on a single student's data in production; expose values for tests and local debugging.
- Log reason codes and score components (not student answers) for offline inspection.
- A planner regression test asserts the distribution of card types over a synthetic state matrix (not synthetic student data in production).

---

## 8. Data model

### 8.1 New tables (additive)

All student tables carry owner. All writes happen through the existing Database connection/lock. Answer material is encrypted with the existing Cipher.

~~~sql
CREATE TABLE IF NOT EXISTS skills (
  id TEXT PRIMARY KEY,
  subject TEXT NOT NULL,
  level TEXT,
  unit TEXT,
  title TEXT NOT NULL,
  description TEXT,
  ib_code TEXT,
  prereq_ids_json TEXT NOT NULL DEFAULT '[]',
  exam_weight REAL NOT NULL DEFAULT 1.0,
  source_kind TEXT,
  source_ref TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
  id TEXT PRIMARY KEY,
  owner TEXT,                         -- NULL = shared, verified bank
  version INTEGER NOT NULL DEFAULT 1,
  skill_ids_json TEXT NOT NULL,
  format TEXT NOT NULL,               -- mcq_single | mcq_multi | numeric
  stem TEXT NOT NULL,
  options_json TEXT,                  -- labels/values only; never the answer
  solution_enc BLOB NOT NULL,         -- Fernet: correct indices + explanation
  difficulty REAL NOT NULL DEFAULT 0.0,
  discrimination REAL NOT NULL DEFAULT 1.0,
  guessing REAL NOT NULL DEFAULT 0.25,
  source_kind TEXT NOT NULL,          -- generated | syllabus | ib_reference | lms_graded
  source_ref TEXT,
  source_sha256 TEXT,
  verification TEXT NOT NULL DEFAULT 'draft', -- draft | verified | retired
  model TEXT,
  prompt_version TEXT,
  content_hash TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  retired_at REAL
);

CREATE TABLE IF NOT EXISTS item_stats (
  item_id TEXT PRIMARY KEY,
  attempts INTEGER NOT NULL DEFAULT 0,
  correct INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS student_skill_state (
  owner TEXT NOT NULL,
  skill_id TEXT NOT NULL,
  ability REAL NOT NULL DEFAULT 0.0,
  mastery REAL NOT NULL DEFAULT 0.35,
  attempts INTEGER NOT NULL DEFAULT 0,
  correct INTEGER NOT NULL DEFAULT 0,
  unknowns INTEGER NOT NULL DEFAULT 0,
  streak INTEGER NOT NULL DEFAULT 0,
  stability REAL NOT NULL DEFAULT 1.0,
  due_at REAL,
  last_seen REAL,
  checkpoint_eligible INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL,
  PRIMARY KEY (owner, skill_id)
);

CREATE TABLE IF NOT EXISTS practice_attempts (
  id TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  attempt_token TEXT NOT NULL,
  item_id TEXT NOT NULL,
  item_version INTEGER NOT NULL,
  skill_ids_json TEXT NOT NULL,
  selected_json TEXT,
  correct INTEGER NOT NULL DEFAULT 0,
  dont_know INTEGER NOT NULL DEFAULT 0,
  latency_ms INTEGER,
  confidence INTEGER,
  source TEXT NOT NULL,               -- feed | lesson | checkpoint
  thread_id TEXT,
  card_id TEXT,
  explanation_shown INTEGER NOT NULL DEFAULT 0,
  planner_reason TEXT,
  created_at REAL NOT NULL,
  UNIQUE (owner, attempt_token)
);

CREATE TABLE IF NOT EXISTS feed_plans (
  id TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  day TEXT NOT NULL,                  -- local date in owner timezone
  timezone TEXT NOT NULL,
  goal_cards INTEGER NOT NULL,
  state TEXT NOT NULL DEFAULT 'active',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE (owner, day)
);

CREATE TABLE IF NOT EXISTS feed_cards (
  id TEXT PRIMARY KEY,
  plan_id TEXT NOT NULL,
  owner TEXT NOT NULL,
  position INTEGER NOT NULL,
  kind TEXT NOT NULL,
  item_id TEXT,
  checkpoint_id TEXT,
  skill_ids_json TEXT NOT NULL DEFAULT '[]',
  reason_code TEXT,
  reason_text TEXT,
  state TEXT NOT NULL DEFAULT 'pending', -- pending | answered | skipped
  attempt_id TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE (plan_id, position)
);

CREATE TABLE IF NOT EXISTS feed_events (
  id TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  plan_id TEXT,
  card_id TEXT,
  event_type TEXT NOT NULL,
  value_json TEXT,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS checkpoint_runs (
  id TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  plan_id TEXT,
  skill_ids_json TEXT NOT NULL,
  item_ids_json TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'open', -- open | submitted
  score INTEGER,
  total INTEGER,
  time_limit_s INTEGER,
  started_at REAL NOT NULL,
  submitted_at REAL
);

CREATE TABLE IF NOT EXISTS checkpoint_answers (
  checkpoint_id TEXT NOT NULL,
  item_id TEXT NOT NULL,
  selected_json TEXT,
  correct INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (checkpoint_id, item_id)
);

CREATE TABLE IF NOT EXISTS item_reports (
  id TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  item_id TEXT NOT NULL,
  kind TEXT NOT NULL,                 -- ambiguous | wrong | outdated | too_easy | too_hard
  note TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS owner_settings (
  owner TEXT PRIMARY KEY,
  timezone TEXT NOT NULL DEFAULT 'UTC',
  daily_cards INTEGER NOT NULL DEFAULT 12,
  target_retention REAL NOT NULL DEFAULT 0.90,
  focus_subjects_json TEXT,
  checkpoints_enabled INTEGER NOT NULL DEFAULT 1,
  difficulty_pref TEXT NOT NULL DEFAULT 'balanced',
  updated_at REAL NOT NULL
);
~~~

Indexes:

- student_skill_state(owner, due_at), (owner, mastery).
- practice_attempts(owner, created_at), (owner, item_id).
- feed_cards(plan_id, position), (owner, state).
- feed_events(owner, created_at).
- items(content_hash), (owner, verification).
- item_reports(item_id, status).

### 8.2 Existing table changes

Use a guarded **ensure_column** helper in **app/db.py**, because the current schema is created with CREATE TABLE IF NOT EXISTS and old databases exist.

- **threads**: add origin TEXT NOT NULL DEFAULT 'manual' and origin_json TEXT.
- **quiz_states**: add skill_ids_json TEXT, item_id TEXT, source TEXT NOT NULL DEFAULT 'lesson'.
- No destructive changes, no table rebuilds.

### 8.3 Ownership and encryption

- Every student query joins on owner; a missing owner is a programming error, not an empty result.
- items.owner NULL means shared verified content. Private generated items are owner-scoped until verified and explicitly promoted.
- Item content and its solution are immutable. Calibration uses items.difficulty as the prior and item_stats for aggregate evidence; observed difficulty is blended only when item_stats.attempts >= 20. item_stats never stores owner ids.
- solution_enc uses the same Fernet cipher as file pages. The feed response never decrypts it.
- stem/options are not secrets; explanations are revealed only after an attempt or checkpoint submit.
- practice_attempts stores only selections and indices, not copied question text.
- feed_events values are capped and validated; no free-form page HTML.

### 8.4 Retention and deletion

- feed_events retained 90 days by default, then aggregated to counts; configurable.
- **POST /api/practice/reset** deletes attempts, state, plans, cards, events, checkpoint runs, and owner settings for that owner. Items generated privately are retired.
- Deleting a lesson thread sets practice_attempts.thread_id = NULL but keeps the evidence (the attempt happened); the thread itself is removed as today.
- Item reports and retired shared items are retained for quality, without owner links after aggregation.

### 8.5 Migration

- Add tables in _init_schema; add columns via ensure_column.
- Run on startup and in tests with :memory: and temp file databases.
- Take a sqlite3 .backup before the first production migration; additive changes are low risk, but the plan includes a restore test.
- Feature flag off means no practice tables are read; tables can exist harmlessly.

---

## 9. API contract

All endpoints are under the existing session/cookie auth and owner scope. JSON only. Errors use the existing FastAPI style and status codes.

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | /api/home/feed | Next page of the daily plan | Cursor, limit, optional date hint; no LLM |
| POST | /api/home/events | Batched scroll/interaction events | Max 50, enum-validated |
| POST | /api/practice/attempts | Grade one feed attempt | Idempotent via attempt_token |
| GET | /api/practice/progress | Mastery bands and due counts | Aggregated, no answers |
| POST | /api/practice/checkpoints/{id}/start | Create/lock a checkpoint | Server picks items |
| POST | /api/practice/checkpoints/{id}/submit | Submit all answers | Delayed feedback |
| POST | /api/practice/teach | Create a lesson from evidence | Returns thread id + seed |
| POST | /api/practice/feedback | Report an item | Quarantine threshold |
| POST | /api/practice/settings | Daily goal, timezone, focus | Owner settings |
| POST | /api/practice/reset | Delete practice state | Explicit confirmation |
| POST | /api/practice/bookmarks | Add/remove review bookmark | Phase 2 |

### 9.1 Feed

~~~http
GET /api/home/feed?day=2026-10-25&cursor=0&limit=6
~~~

~~~json
{
  "status": "ok",
  "plan": {
    "id": "plan_...",
    "day": "2026-10-25",
    "goal_cards": 12,
    "answered": 4,
    "streak_days": 5,
    "exam_countdown_days": 198,
    "checkpoint_due": false
  },
  "cards": [
    {
      "card_id": "fc_...",
      "kind": "practice",
      "position": 4,
      "item": {
        "id": "it_...",
        "version": 1,
        "format": "mcq_single",
        "stem": "A ball is thrown ...",
        "options": [
          {"index": 1, "label": "12 m", "value": "12"},
          {"index": 2, "label": "18 m", "value": "18"}
        ],
        "skill_labels": ["Projectile motion"],
        "difficulty_band": "medium",
        "estimated_seconds": 90
      },
      "reason": {
        "code": "due_review",
        "text": "You last saw this 4 days ago."
      },
      "state": "pending"
    }
  ],
  "next_cursor": 6,
  "has_more": true
}
~~~

Rules:

- The payload never contains correct indices, explanations, solution hashes, or model provenance that could leak the answer.
- The plan is created on first request and reused until the local day changes or the student explicitly refreshes.
- If cursor is beyond the plan, return has_more: false and the recap card.
- limit is capped at 10 to keep the client DOM light.

### 9.2 Attempt

~~~http
POST /api/practice/attempts
~~~

~~~json
{
  "card_id": "fc_...",
  "item_id": "it_...",
  "item_version": 1,
  "attempt_token": "uuid-or-hmac",
  "selected": [2],
  "confidence": 1,
  "latency_ms": 48000
}
~~~

~~~json
{
  "status": "ok",
  "correct": false,
  "dont_know": false,
  "correct_options": [3],
  "explanation": "...",
  "mastery": {"before": 0.41, "after": 0.29, "band": "weak"},
  "next_due_at": 1760000000,
  "follow_up": {"kind": "repair", "reason": "Sign error in components"},
  "attempt_id": "pa_..."
}
~~~

- Replaying the same attempt_token returns the same result without a second mastery update.
- Reusing a token for a different card/item returns 409.
- selected must be a list of integers within the item's options, or the __dont_know__ sentinel. Validation is server-side.
- Explanation is returned only after a recorded attempt; never from GET /feed.

### 9.3 Events

~~~http
POST /api/home/events
~~~

~~~json
{
  "events": [
    {"card_id": "fc_...", "type": "dwell", "value": {"ms": 4200, "visible": 0.8}},
    {"card_id": "fc_...", "type": "skip"},
    {"card_id": "fc_...", "type": "teach_click"}
  ]
}
~~~

Allowed types: impression, dwell, skip, reveal, bookmark, report, teach_click, checkpoint_start, checkpoint_submit, feed_end, reflection. Unknown types are dropped, not stored. Batch cap 50; dwell capped at 10 minutes; payload size cap 8 KB.

The report UI calls POST /api/practice/feedback; the report event is telemetry only and does not itself quarantine an item.

### 9.4 Teach

~~~http
POST /api/practice/teach
~~~

~~~json
{
  "skill_ids": ["phys.kin.vectors"],
  "attempt_ids": ["pa_..."],
  "intent": "fix_misconception"
}
~~~

~~~json
{
  "status": "ok",
  "thread_id": "...",
  "seed_message": "I need to fix vector components. Start from the foundations and use my recent practice evidence.",
  "brief_summary": {"skills": 1, "attempts": 2}
}
~~~

The server stores the brief in threads.origin_json, sets origin = practice, and returns. The client sends the seed through the existing /api/chat/send SSE endpoint.

### 9.5 Error semantics

| Status | When |
|---|---|
| 401 | No session or expired cookie |
| 403 | Owner mismatch, live-test reference, or reset without confirmation |
| 404 | Unknown card/item/plan |
| 409 | Item version retired or attempt token reused with different payload |
| 422 | Invalid selection, bad event type, bad settings |
| 429 | Attempt/event/generation rate limit |
| 502/503 | Upstream generation/verification unavailable; feed still serves the existing plan |

### 9.6 Performance budgets

- Feed response p95 < 200 ms local with 1,000 items and 50 skills; no LLM call.
- Attempt response p95 < 120 ms; one transaction.
- Event ingestion is fire-and-forget for the UI; p95 < 80 ms.
- No endpoint returns more than 10 cards; no endpoint returns all attempts.
- Query count per feed page <= 6, enforced by a test using SQLite trace logs.

---

## 10. Content pipeline and item quality

### 10.1 Sources

1. **Verified generated items.** Generator model authors original items from skill specs and optional source references. Two-pass verification. This is the primary pool.
2. **Syllabus-derived skills.** Subject guides from the IB catalog or student-attached syllabus are parsed into topic nodes. Use **app/ib_client.py map_course_to_ib_params** to map enrolled course names to IB subject/level, then extract units and topics. Skills store a source ref; no copyrighted text is copied into the item bank.
3. **Graded LMS evidence.** Missed questions from graded EOL/FA results become topic seeds and misconception hints, not copied items. assert_not_live_test runs before any EOL detail fetch.
4. **Student-owned documents.** A student can attach a handout; items can reference a page in the viewer. The item text is generated, not copied wholesale.
5. **Seed catalog.** For cold start, ship a small curated list of IB DP subject/unit titles from public subject guides. This is curriculum metadata, not student data and not fake activity.

Explicitly not a source: unsubmitted/live tests, answer keys from a live assessment, or another student's work.

### 10.2 Generation

- Triggered in the background when a skill has fewer than 3 verified items, or manually from a debug/admin view.
- Batch size 20 per skill per run, cost cap configurable; never in the feed request path.
- Prompt inputs: skill title, description, subject/level, prerequisites, optional source excerpt (wrapped as untrusted data), target format, difficulty band.
- The generator must return strict JSON:

~~~json
{
  "stem": "...",
  "format": "mcq_single",
  "options": [{"label": "...", "value": "..."}],
  "correct": [1],
  "explanation": "...",
  "difficulty": "medium",
  "skill_ids": ["..."],
  "misconception": "sign error in components",
  "source_note": "aligned to subject guide 1.2"
}
~~~

Prompt rules (reuse the construction procedure in **app/prompts.py**):

- correct option written first, distractors by mutating one concrete element;
- no reasoning in options; all reasoning in the explanation;
- no answer or explanation in the stem;
- no "all/none of the above" unless logically required;
- 2–5 options, unique labels, one correct for single-select;
- no PII, no live assessment references, no instructions embedded in source excerpts;
- original wording; cite sources in source_note.

### 10.3 Validation and verification

Deterministic validator:

- schema and type checks;
- option count 2–5, unique labels, non-empty stem/explanation;
- correct indices in range; single-select has exactly one;
- no duplicate options after normalization;
- no HTML/script; Markdown/LaTeX allowed but sanitized at render;
- length caps (stem 1–600 chars, option 1–160, explanation 1–800);
- skill ids exist, belong to the subject, and are not blocked;
- no source text overlap above a threshold (copyright check);
- content hash and near-duplicate check (trigram Jaccard >= 0.9 is rejected).

Independent verifier:

- A second model call receives the item and skill spec, solves it without seeing the author's reasoning, and returns pass/fail plus issues.
- Checks: unique answer, no ambiguity, correct difficulty, alignment with skill, distractor plausibility, explanation correctness.
- Only verification = verified items enter the feed. draft items are visible only in a debug view.
- A human can promote/retire; for a solo local app, the verifier plus report quarantine is the baseline.

### 10.4 Dedup, versioning, retirement

- Items are immutable. Edits create a new version/id; old versions are retired.
- content_hash is SHA-256 of normalized stem + sorted options + correct + skill ids.
- Reports: 2 reports across any owners → retired; the feed replaces the card with an alternative. Reports never delete the audit row.
- Item calibration aggregates only item statistics; no owner ids leave the local database.
- Quality dashboard (debug only): pass rate, report rate, ambiguity flags, generation cost.

### 10.5 Copyright and prompt injection

- Do not store or serve verbatim past-paper questions or markscheme text.
- source_ref points to the catalog or the student's own file; the viewer opens the original for reference.
- All source text is untrusted data: the generator prompt states that instructions inside a document must be ignored.
- Generated item content is untrusted too: it is sanitized on render and never interpreted as a tool call.

### 10.6 Cost and operations

- Assume no generation if a verified pool already meets the minimum.
- Batch, cache, and retire aggressively.
- Log model, prompt version, token counts, and verifier result; never log API keys.
- A generation circuit breaker stops after N consecutive failures.
- Manual "generate practice for this skill" action for local debugging, behind the loopback admin check.

---

## 11. Frontend implementation

### 11.1 New and changed files

| File | Change |
|---|---|
| web/index.html | Add hash-router views, home feed container, header, sentinel, nav; keep existing chat, log, viewer, templates |
| web/assets/home.js | New: feed controller, pagination, view switching, keyboard, progress header |
| web/assets/practice-cards.js | New: feed card renderers (practice wrapper, checkpoint, teach, recap, deadline, reason line) |
| web/assets/feed-events.js | New: dwell/visibility observer, batched events, sendBeacon on pagehide |
| web/assets/app.js | Add showView, hash routing, feed boot, teach handoff, move triage into the Today rail; keep existing chat functions |
| web/assets/cards.js | Reuse renderQuizCard; add optional onGraded hook if needed; no breaking changes |
| web/assets/style.css | Keep tokens; add a small import for home styles or append feed section |
| web/assets/home.css | New: feed layout, cards, skeletons, progress ring, responsive, reduced motion |
| web/assets/progress.js | New (phase 2): mastery map and review queue |
| web/assets/renderer.js | Reuse renderMarkdown; no change unless a renderInline helper is needed |

### 11.2 Shell and routing

- Keep a single page. Do not add a framework or a bundler.
- app.js starts the vendor loader, checks /api/session, then routes:
  - no session → current sign-in/independent CTA;
  - session and #/lesson/<id> → lesson view;
  - session and #/progress → progress view;
  - otherwise home.
- showView toggles hidden/aria-hidden on the home and lesson containers and updates nav aria-current.
- Hash changes are listened to; pushState is used for nav clicks so back/forward works.
- The lesson view is the existing center pane content, extracted into a wrapper; chat code stays in app.js for phase 1, then can be split if it grows.

### 11.3 Feed rendering

- Use safe DOM construction, not innerHTML, for item text.
- Markdown/LaTeX goes through renderMarkdown (Marked + KaTeX + DOMPurify). If the vendor is missing, render plain text and show a non-blocking banner.
- Reuse renderQuizCard for single/multi MCQ. The adapter maps a feed item to the existing card shape:
  - card_type = quiz, call_id = card_id, question = stem, options with server-assigned indices, multi_select.
  - onReply posts to /api/practice/attempts and returns the grade shape the card expects.
- New renderers:
  - checkpoint: question list, no per-item grade, single submit, result panel;
  - teach: skill label, reason, "Open lesson" button, no answer;
  - recap: counts, mastery deltas, next due, "Extra practice";
  - deadline: title, due date, subject, "Open lesson" / "View file";
  - reason line: a disclosure with the one-sentence reason and "not now".
- Numeric input: a single text field with unit label and a server-side tolerance; no client-side correctness.
- Card state is reflected in data-state for CSS; answered cards remain in the feed with a compact summary, not removed abruptly.

### 11.4 Scroll and events

- IntersectionObserver at 0.5 threshold for impressions and dwell; pause on visibilitychange.
- Batch events every 5 seconds or 5 events; flush on pagehide with navigator.sendBeacon.
- Cap dwell at 10 minutes; do not send scroll coordinates.
- Infinite scroll sentinel plus a visible "Load more" button for keyboard/screen-reader users.
- Preserve scroll position when returning from a lesson; restore the last card.
- Prefetch the next page when 3 cards remain.

### 11.5 Accessibility

- main landmark; each card is an article with an accessible name (subject + skill).
- Feedback uses aria-live="polite"; the correct answer is announced once, not repeatedly.
- Options are buttons with visible focus; keyboard shortcuts are documented and can be disabled.
- Color is not the only signal for correct/incorrect; icons and text labels are present.
- Reduced motion; no auto-scroll; no time pressure by default.
- "Load more" is a real button; infinite scroll is an enhancement.
- Contrast meets WCAG AA in both themes.

### 11.6 Performance

- First feed page: 6 cards, no images, no LLM; skeleton under 100 ms.
- Max 30 card nodes; collapse answered cards older than 10 positions.
- No layout shift from async explanations; reserve space.
- Defer progress view and generation controls until needed.
- Keep vendor loading behavior unchanged; do not add new third-party scripts.

---

## 12. Backend implementation

### 12.1 Module layout

~~~text
app/
  llm.py                      # extracted call_llm_chat/_stream_message (no behavior change)
  integrations/
    base.py                   # LMSPlugin, LearningContext, integration errors
    registry.py               # optional plugin lookup; empty context when absent
    indus.py                  # built-in adapter around lms_bridge
    __init__.py               # registers the built-in plugin, exports the facade
  practice/
    __init__.py
    config.py                 # weights, caps, defaults
    models.py                 # dataclasses for Skill, Item, Attempt, PlanCard
    skills.py                 # catalog load/seed, subject mapping, prerequisite traversal
    planner.py                # pure candidate scoring + plan composition
    grading.py                # answer checking, BKT, Elo, scheduler
    store.py                  # owner-scoped SQL over the existing Database
    service.py                # plan_feed, grade_attempt, teach_brief, checkpoints
    generation.py             # author/verify pipeline, dedupe, retirement
    prompts.py                # generation/verifier prompts
    api.py                    # APIRouter mounted by main.py
~~~

### 12.2 Touchpoints

| File | Change |
|---|---|
| app/db.py | New tables in _init_schema; ensure_column for threads and quiz_states; no destructive changes |
| app/main.py | Include practice.api.router before the static mount; startup ensures the skill catalog; no route removals |
| app/orchestrator.py | Add skill_ids to the quiz tool; accept/load a practice brief; call practice.service.record_lesson_attempt after grading |
| app/orchestrator.py (fallback) | If the teacher omits skill_ids, map the quiz to the thread's current dependency-map node by title. If no confident match exists, store source = lesson_unmapped and do not update mastery; the attempt still appears in the brief. |
| app/prompts.py | Add practice_brief formatting and instructions to build_teacher_system_prompt |
| app/lms_bridge.py | Merge practice misses into weak_topics; keep API shape; no live-test changes |
| app/config.py | Add feed/practice env knobs and feature flags |
| app/security.py | Reuse assert_not_live_test, Cipher, sanitize_filename; add a small owner rate limiter if needed |
| tests/ | New suites and updates to existing API/e2e/XSS tests |

### 12.3 Refactor: extract the LLM client

- call_llm_chat and _stream_message currently live in **app/orchestrator.py** (lines ~317–416). Generation must not import the orchestrator (circular import).
- Move them to **app/llm.py**, import them back into orchestrator under the same names. Existing tests patch **app.orchestrator.call_llm_chat**, so the patch target keeps working if the imported name is used.
- No behavior change in this refactor; a focused test proves streaming and non-streaming shapes.

### 12.4 Feature flags and config

- DOOMED_HOME_FEED (default 0 until phase 2 ships; 1 for local dev).
- DOOMED_PRACTICE_ENGINE (default 0; 1 enables plan/attempt endpoints).
- DOOMED_ITEM_GENERATION (default 0; 1 enables background authoring).
- DOOMED_DAILY_CARDS (default 12).
- DOOMED_TARGET_RETENTION (default 0.90).
- DOOMED_PRACTICE_RATE_LIMIT (attempts per minute per owner).
- DOOMED_FEED_DEBUG (reason components in the API for local debugging only; never in hosted mode).
- All defaults live in **app/practice/config.py**; feature flags checked at the router boundary so the engine can be disabled without removing tables.

### 12.5 Startup

- Ensure the schema (existing behavior).
- Load the skill catalog if present; no network call on startup.
- Do not start a generation thread that could delay boot; use a lazy background worker with a lock, or an explicit local admin action.
- If the catalog is missing, the feed returns a needs_calibration state with a readable message; it does not crash.

### 12.6 Timezones and day boundaries

- Store the student's IANA timezone in owner_settings; default UTC.
- The server computes the plan day from that timezone. The client day hint is ignored if it disagrees beyond one day.
- This avoids a plan resetting at the wrong local hour and keeps schedules stable across travel.

---

## 13. Security, privacy, and academic integrity

### 13.1 Invariants

1. **No answer in the feed.** No correct indices, explanation, solution blob, or answer hash in GET /api/home/feed.
2. **Owner scoping on every row.** Every practice query includes owner = ?; new tables get tests with two owners.
3. **Encrypted solutions.** items.solution_enc is Fernet-encrypted; decrypted only inside grading.
4. **No live test ingestion.** Graded EOL/FA results only; assert_not_live_test before any detail fetch; tests assert the refusal path.
5. **No LLM in the hot path.** Feed and attempt endpoints do not call a model; generation is background and flag-gated.
6. **Untrusted content stays untrusted.** Item text, explanations, and source excerpts are data; sanitized on render; never executed or treated as instructions.
7. **Idempotent grading.** Replays cannot double-count mastery; tokens are unique per owner.
8. **Rate limited.** Attempts, events, reports, and generation have per-owner limits.
9. **Deletable.** Reset removes practice state; generated private items are retired.
10. **No secret or internal prompt leaves the server.** Item provenance shown to the student is a short source label, not model prompts or keys.

### 13.2 Threat table

| Threat | Mitigation |
|---|---|
| Student probes the API for answers | No answer in feed; grade only on attempt; explanation after submission; no endpoint returns solution blobs |
| Cross-student feed leak | Owner in every query; two-owner tests; shared items contain no student data |
| Live exam leak | assert_not_live_test; only graded results used; no LMS test content in prompts |
| Prompt injection in source docs | Wrapped as data; generator prompt says ignore instructions; verifier sees only item + skill spec |
| XSS via generated math/Markdown | renderMarkdown + DOMPurify; no innerHTML for item text; CSP unchanged |
| Scraping/cost abuse | Rate limits, pools, background generation cap, local-only admin |
| Answer key at rest | Fernet-encrypted solution column; DB 0600; key from DOOMED_SECRET_KEY |
| Analytics surveillance | No third-party analytics; owner-scoped local events; dwell capped and never used for mastery |
| Accidental data loss on reset | Require explicit confirmation; return counts; keep lessons unless separately deleted |

---

## 14. Testing plan

### 14.1 Unit

- **tests/test_practice_grading.py**: single/multi/numeric; "I don't know"; confidence weighting; tampered indices; item version mismatch; no explanation before attempt.
- **tests/test_practice_model.py**: BKT posterior bounds; Elo update direction; scheduler interval monotonicity; failure reset; target retention.
- **tests/test_practice_planner.py**: deterministic seed; prerequisites; caps; interleaving; warm-up; reason code; empty pool fallback; no live-test items.
- **tests/test_practice_store.py**: owner isolation; idempotency; encryption at rest; plan persistence; checkpoint state; reset.
- **tests/test_practice_migrations.py**: old schema → new; existing rows intact; ensure_column idempotent; 0600 permissions.

### 14.2 API and integration

- **tests/test_practice_api.py**: feed shape and no answer leak; attempt/replay/409; events validation; teach creates thread + brief; progress; settings; reset; 401/403/422/429.
- **tests/test_practice_teaching.py**: brief appears in the system prompt; lesson quiz with skill_ids updates state; feed after lesson; triage merges practice misses; thread origin.
- **tests/test_api.py** additions: feed disabled by flag → 404 or 503 consistent with the flag; existing endpoints unchanged.
- **tests/test_security_hardening.py** additions: answer key never serialized; cross-owner 403; generated XSS payload sanitized.
- **tests/test_lms_bridge.py** additions: practice misses appear in weak topics; live test still blocked.

### 14.3 Browser

- **tests/test_home_e2e.py** (Playwright):
  - feed loads after local session;
  - first card answers and shows feedback;
  - reload preserves answered state;
  - infinite scroll loads the next page;
  - "Load more" works with keyboard;
  - teach handoff opens a lesson and sends the seed;
  - mobile viewport reaches the composer;
  - no console errors;
  - dark/light theme;
  - generated item with a script tag renders inert.
- Extend **tests/test_frontend_xss.py** for item stems/explanations.
- Keep existing Playwright tests passing; the home view must not break auth, profile, log, viewer, or theme.

### 14.4 Performance and quality

- SQL trace test: feed page <= 6 queries; attempt <= 3.
- Seed 1,000 items / 50 skills / 10k attempts in a temp DB and assert p95 under budget locally.
- Property test: planner never selects an item whose prerequisites are unmet; never exceeds caps; always returns at most limit cards.
- A fuzz test for selection validation (negative, out of range, duplicates, strings) returns 422, not a crash.

### 14.5 Commands

~~~bash
.venv/bin/python -m pytest -q --ignore=tests/test_e2e_playwright.py
.venv/bin/python -m pytest -q tests/test_home_e2e.py tests/test_frontend_xss.py
~~~

The baseline at plan time is 157 unit/integration tests passing. New tests are additive; no existing test is deleted or weakened.

---

## 15. Rollout plan

| Phase | Scope | Deliverable | Estimate | Exit criteria |
|---|---|---|---|---|
| 0 | Schema, flags, skill catalog, planner skeleton | Tables + pure planner tests | 2–3 days | Migration tests pass; flag off has no behavior change |
| 1 | Grading, scheduler, store, feed/attempt APIs | Feed JSON + attempt grading | 4–5 days | API tests pass; no answer leak; p95 budget met |
| 2 | Home UI and practice cards | Scrollable feed behind flag | 4–5 days | Playwright pass; mobile/keyboard; existing chat intact |
| 3 | Teaching bridge | Brief + skill_ids + after-lesson cards | 3–4 days | Loop integration tests pass; lesson can start from a card |
| 4 | Checkpoints + generation/verification | Checkpoint flow + verified pool | 4–6 days | Checkpoint tests; verifier pass rate; report quarantine |
| 5 | Progress, triage integration, polish | Progress view + docs/screenshot | 3–4 days | Accessibility pass; docs updated; flag on by default locally |

Rough total: 3–4 weeks for one developer, assuming content generation is the long pole. The feed MVP (phases 0–3) is usable without automatic generation if a seed pool exists.

PR sequence:

1. feat(db): practice schema and migrations
2. feat(practice): skill catalog and item store
3. feat(practice): grading, mastery, and scheduler
4. feat(practice): planner and feed composition
5. feat(api): feed, attempt, event, teach endpoints
6. feat(web): home feed shell and practice cards
7. feat(practice): teaching brief and lesson signal capture
8. feat(practice): checkpoints and item generation
9. feat(web): progress view and accessibility polish
10. docs: home feed plan, API reference, changelog

Each PR: feature flag, tests, no secrets, no synthetic student data, owner isolation, docs.

---

## 16. Metrics

### Learning

- First-attempt accuracy on due reviews (target: +10–15% after 4 weeks).
- Checkpoint pass rate and error recovery rate (missed item later correct).
- 7-day retention of skills (due review success after >= 7 days).
- Mastery gain per subject per week.
- Coverage: percentage of enrolled subject units with at least 3 attempts.

### Loop

- Share of lessons started from a feed card (target >= 40% by week 4).
- Share of feed cards generated after a lesson (after_lesson/review).
- Percentage of lesson quizzes with recorded skill ids (target >= 80%).
- Feed-to-lesson-to-feed round trips per week.

### Quality and safety

- Item report rate < 2%; ambiguity reports resolved within 24h.
- Verifier pass rate 70–85% (too high suggests weak verification; too low suggests bad prompts).
- Answer-leak incidents: 0.
- Live-test refusals still 403: 100% regression tests.
- p95 feed < 200 ms local; p95 attempt < 120 ms; first card < 1 s on a warm local server.
- Median session 12–25 minutes; skip rate < 25%; no dark-pattern streak loss.

For a single-user local install, show personal progress, not comparative cohort metrics.

---

## 17. Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Item pool too small at launch | High | Medium | Seed verified items; calibrate; degrade to teach/reading cards; background generation |
| Generated items wrong or ambiguous | Medium | High | Two-pass verification; deterministic validator; reports; retire; no mastery update from unverified |
| Answer leakage | Low | High | No answer in feed; encrypted solution; per-owner; tests; rate limits |
| Live-test ingestion | Low | High | Only graded results; assert_not_live_test; explicit source checks; regression tests |
| Over-testing / anxiety | Medium | Medium | Finite daily plan; no timer by default; confidence and reflection; opt-out |
| Infinite scroll compulsion | Medium | Medium | Default finite plan; extra practice opt-in; end-of-plan recap; break nudge |
| LLM cost/latency | Medium | Medium | Batch generation; cache; no hot-path model; local proxy; circuit breaker |
| Copyright | Medium | High | Original items; references only; no verbatim past-paper text; source notes |
| Scroll jank / DOM bloat | Medium | Medium | Cap cards; pagination; IntersectionObserver; no new heavy libraries |
| Accessibility regressions | Medium | Medium | Keyboard, focus, live regions, reduced motion; Playwright + manual checks |
| Migration breaks data | Low | High | Additive tables; guarded columns; backup; migration tests |
| Cross-owner leak | Low | High | Owner in every query; two-owner tests; code review checklist |
| Scope creep | High | Medium | Phase gates; flags; MVP without generation; keep chat intact |

---

## 18. Decisions locked (2026-10-25)

| # | Decision | Locked choice | Why |
|---|---|---|---|
| D1 | Is home the default for authenticated students? | Yes, feed-first; lesson one tap away | The request is a home experience; chat remains for depth |
| D2 | Finite plan or infinite feed? | Finite daily plan + opt-in "Extra practice" | Better learning and wellbeing; still scrollable |
| D3 | Are tests timed? | No; no timer by default | Formative, low-stress; timing can be an experiment later |
| D4 | Free-response grading in MVP? | No; MCQ/multi/numeric first; free response in phase 4 | Deterministic, testable, no grading ambiguity |
| D5 | Automatic item generation? | Background, flag-gated, when pool < 3 per skill | Avoid hot-path latency and cost |
| D6 | Shared item bank? | Private by default; shared only verified, ownerless | Privacy; content reuse later |
| D7 | Keep the left triage pane? | Keep in phase 1; move to a "Today" right rail in phase 4 | Minimizes disruption; lets users compare |
| D8 | What happens to existing quiz_states? | Keep; add skill_ids and practice link; don't migrate | Backward compatible; lesson quizzes remain |
| D9 | Daily goal default | 12 cards / ~20 minutes, adjustable | Enough retrieval without a 3-hour session |
| D10 | Timezone/day boundary | Store IANA timezone; server computes day | Stable plans across travel |
| D11 | Should the feed call the LLM for hints? | No in MVP; hints are pre-authored per item or generated in background | Latency and answer-leak control |
| D12 | Reset behavior | Explicit reset clears practice state; lessons stay unless deleted | Predictability and safety |

---

## 19. Out of scope (later)

- Native mobile apps, push notifications, and calendar integration beyond existing LMS data.
- Social features, leaderboards, study groups, and teacher dashboards.
- Proctoring, high-stakes grading, or exam-score prediction.
- Audio lessons, speech input, and handwriting OCR.
- Cloud sync, hosted multi-user calibration, and shared item marketplaces.
- Full IRT calibration with large cohorts; the MVP uses Elo/BKT and a simple scheduler.

---

## 20. Immediate next steps

1. Seed a skill catalog for the enrolled subjects and build the verified item pipeline (phase 0 content).
2. Build the scrollable home UI and feed cards behind **DOOMED_HOME_FEED=0**.
3. Wire the teaching bridge: inject the practice brief into the teacher prompt, add **skill_ids** to the quiz tool, and record lesson attempts in the same state.
4. Add checkpoints and the progress view.
5. Run the browser suite, then enable the flags locally and review the first real feed.
6. Tune weights with local data only; do not tune on a single student's production data.

---

## Appendix A: Planner defaults

~~~text
daily_cards = 12
limit_per_page = 6
target_success = 0.75
stretch_success = 0.55
repair_success = 0.85
target_retention = 0.90
max_new_skills_per_day = 2
max_new_items_per_day = 5
max_attempts_per_skill_per_day = 3
max_checkpoints_per_day = 1
min_verified_items_per_skill = 3
recent_duplicate_window = 3 cards / 24 hours
~~~

## Appendix B: File map summary

- New backend: **app/practice/**, **app/integrations/**, **app/llm.py** (planned extraction).
- New frontend: **web/assets/home.js**, **web/assets/practice-cards.js**, **web/assets/feed-events.js**, **web/assets/home.css**, **web/assets/progress.js**.
- Changed: **app/db.py**, **app/main.py**, **app/orchestrator.py**, **app/prompts.py**, **app/lms_bridge.py**, **app/config.py**, **web/index.html**, **web/assets/app.js**, **web/assets/style.css**, **tests/**, docs.
- Unchanged: viewer, viz-engine, log-export, file reader, LMS client, security core.

---

**End of plan.**
