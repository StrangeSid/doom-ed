# Future Work — Adaptive Practice and Teaching Loop

**Status:** Living roadmap
**Baseline:** commit 4664f23, 190 tests passing
**Date:** 2026-10-27
**Related:** [HOME_FEED.md](HOME_FEED.md), [DOCS.md](../DOCS.md), [ARCHITECTURE.md](../ARCHITECTURE.md)

> This is the execution backlog, not a second design doc. The design and acceptance details for the feed live in HOME_FEED.md. This file tracks what is left, in what order, and how we will know it is done.

---

## 1. Purpose

The adaptive practice loop is now wired end to end:

- The practice API and planner exist,
- The home feed renders and grades cards,
- A card can open a lesson with its practice evidence,
- Lesson quizzes update the same mastery state,
- The LMS is optional and plugin-shaped.

The loop isn't yet useful for a real student because the item bank is empty. The next phase is therefore content-first. Everything else waits on a verified pool of skills and items.

This plan answers three questions:

1. What must ship before a student can use the feed for a week?
2. What must ship before the loop is trustworthy enough to leave on by default?
3. What can wait until there is real usage data?

---

## 2. Current state

### 2.1 Shipped

- **Practice schema and migrations:** skills, items, item stats, student skill state, attempts, feed plans/cards/events, checkpoints tables, reports, owner settings.
- **Engine:** deterministic planner, BKT mastery, Elo-style ability calibration, FSRS-lite scheduling, answer checking for MCQ and numeric items, owner-scoped attempt ledger.
- **API:** feed, attempts, events, progress, teach, feedback, settings, reset. All feature-flagged.
- **Home UI:** scroll feed, plan header, reason disclosures, answered-card summaries, numeric form, skip/report, teach handoff, cursor pagination, dwell/impression events, mobile layout.
- **Teaching bridge:** the practice brief reaches the teacher prompt; lesson quizzes carry skill ids; lesson answers update practice state.
- **LMS plugin layer:** optional Indus adapter, registry, status endpoint, no LMS import inside the practice engine.
- **Tests:** 190 pass, including 5 home-feed browser tests.

### 2.2 Missing or thin

- No skill catalog seed.
- No item authoring or generation pipeline.
- No verified item pool, so the feed returns the calibration state.
- No checkpoint runner or checkpoint UI.
- No progress view.
- No after-lesson retrieval card in the feed.
- Numeric answers are graded and rendered, but no browser test covers the numeric form.
- The legacy /api/study route and chat-turn triage still call the LMS bridge directly.
- Rate limiting is coarse; generation limits aren't implemented.
- Existing quiz_states still keep correct answers in plaintext columns, while new practice items encrypt their solutions.
- Documentation for authoring content doesn't exist yet.

### 2.3 Feature flags

| Flag | Default | Current effect |
|---|---|---|
| DOOMED_PRACTICE_ENGINE | 0 | Exposes the practice API; off means 404 |
| DOOMED_HOME_FEED | 0 | Routes authenticated students to the feed |
| DOOMED_ITEM_GENERATION | 0 | Reserved; no generator is wired |
| DOOMED_LMS_PLUGIN | 1 | Enables the built-in Indus adapter |

---

## 3. Constraints

These aren't negotiable for this phase:

1. **No fake student data.** Curriculum metadata is fine; fake attempts, fake mastery, and fake course data aren't.
2. **Verified content only.** A draft or unverified item must never enter the feed.
3. **Answer keys stay server-side.** No correct index, explanation, or solution hash in a feed response.
4. **Owner scoping on every practice table.** Two students on one machine never share state.
5. **Formative only.** No live test ingestion, no proctoring, no school grade implications.
6. **LMS remains optional.** Removing the plugin mustn't break the feed, lessons, or local study.
7. **No build step.** Vanilla HTML, CSS, and ES modules.
8. **No hot-path LLM.** Feed and attempt responses mustn't wait on a model call.

---

## 4. Workstreams

### 4.1 Content foundation (C)

The critical path. The feed is only as good as the item bank.

- Load a skill catalog from a versioned JSON file or a small local admin command.
- Ship a minimal DP subject/unit seed that is curriculum metadata, not student data.
- Provide a content import path for verified items with strict validation.
- Add a background authoring pipeline and an independent verifier.
- Track provenance, source references, verification state, and retirement.
- Report coverage per subject and skill so gaps are visible.

### 4.2 Practice engine completion (P)

- Checkpoint runs: 5–8 items, delayed feedback, 80% pass, remediation, 24-hour cooldown.
- Repair cards: turn a missed check into a scaffolded card or a teach handoff.
- After-lesson retrieval: schedule a card when a lesson quiz lands, then surface it in the next plan.
- Planner tuning: warm-up card, stretch share, interleaving, format caps, and calibration.
- Timezone and day-boundary edge cases.
- Concurrency hardening around attempt tokens and plan creation.

### 4.3 Teaching loop completion (T)

- Report skill-id coverage from lesson quizzes and flag unmapped checks.
- Map a missing skill id to the thread dependency map when confidence is high.
- Record node completion, not just individual quiz grades.
- Improve the practice brief with misconceptions and confidence.
- Add a post-lesson feed card and a "what changed" summary.

### 4.4 Home feed experience (U)

- Progress view: subject, unit, skill bands, due counts, checkpoint history.
- Checkpoint UI: one submit, no per-item feedback until the end.
- Numeric browser test and keyboard/accessibility pass.
- Calibration and empty-bank admin action ("prepare practice").
- Feed performance at 1,000 items and 50 skills.
- Fresh desktop/mobile/dark screenshots for the README.

### 4.5 LMS addon (I)

- Turn optional deadlines into feed context cards through LearningContext.
- Migrate /api/study and chat-turn triage to the integration boundary and practice-first local context.
- Add a second plugin template or a documented adapter contract.
- Keep plugin capability checks explicit: auth, context, files, writes.

### 4.6 Security and operations (O)

- Encrypt legacy quiz_states answer material or migrate it.
- Rate-limit attempts, events, reports, and generation per owner.
- Add request size caps for events and generation payloads.
- Log planner reasons, generation cost, item reports, and p95 timings without logging answer keys.
- Add data export and a clearer reset confirmation.
- Define event retention and aggregation.

### 4.7 Quality and documentation (Q, D)

- Full test suite in CI, including browser tests.
- A numeric-answer browser test.
- Migration test from a pre-practice database.
- Security regression suite for answer leaks, cross-owner reads, live-test refusal, and XSS.
- Content authoring guide with examples.
- Checkpoint and progress API docs.
- Deployment and operations runbook.

---

## 5. Prioritized backlog

Priority meanings: P0 is required before daily student use; P1 is required before turning the feature on by default; P2 can follow real usage.

| ID | Priority | Workstream | Item | Acceptance |
|---|---|---|---|---|
| C1 | P0 | Content | Skill catalog loader and minimal DP seed | Catalog loads from a versioned file; subjects map to the enrolled session; no duplicate ids |
| C2 | P0 | Content | Verified item import command | Strict validation; encrypted solution; content-hash dedupe; dry-run and report modes |
| C3 | P0 | Content | Authoring prompt and structured output | JSON schema enforced; 2–5 options; one correct answer; explanation present; no answer leak |
| C4 | P0 | Content | Independent verifier and quarantine | A second pass solves the item; failed items stay draft; two reports retire an item |
| C5 | P0 | Content | Coverage report and cold-start calibration | Verified counts per skill; calibration set; feed never invents items |
| C6 | P1 | Content | Provenance and copyright checks | Source reference recorded; no verbatim past-paper text; near-duplicate rejection |
| P1 | P0 | Practice | Checkpoint service and API | 5–8 items; delayed feedback; pass threshold; remediation; cooldown |
| P2 | P1 | Practice | Repair-card insertion | Two misses produce a repair card; repeated miss produces a teach card |
| P3 | P1 | Practice | After-lesson retrieval | Lesson quiz schedules a due card; feed shows it with an after-lesson reason |
| P4 | P1 | Practice | Numeric browser test and unit rules | Numeric form tested end to end; tolerance and units documented |
| P5 | P1 | Practice | Planner tuning and snapshots | Warm-up, stretch, interleaving, and caps asserted by tests |
| P6 | P1 | Practice | Timezone and plan-resume edges | Day rollover, travel, and reload preserve a coherent plan |
| P7 | P1 | Practice | Token and plan concurrency | Parallel attempts cannot double-grade or double-count mastery |
| T1 | P1 | Teaching | Skill-id coverage report | Unmapped quizzes visible; mapping fallback from dependency map |
| T2 | P1 | Teaching | Node completion signal | Thread records taught nodes; feed can schedule retrieval |
| T3 | P2 | Teaching | Better practice brief | Misconceptions, confidence, and last attempts formatted and tested |
| U1 | P1 | UI | Progress view | Subject and skill bands, due counts, checkpoint history; no rankings |
| U2 | P1 | UI | Checkpoint UI | Answer all, submit once, result panel, remediation list |
| U3 | P1 | UI | Accessibility and keyboard pass | Keyboard navigation, focus, live regions, reduced motion, contrast |
| U4 | P1 | UI | Calibration action | A student can request practice preparation and see progress |
| U5 | P2 | UI | Feed performance | p95 within budget at 1,000 items; DOM stays bounded |
| I1 | P2 | LMS | Deadline and context cards | Optional plugin context appears as cards, never as fake practice |
| I2 | P2 | LMS | Legacy triage migration | /api/study and chat triage no longer depend on the bridge directly |
| O1 | P0 | Ops | Encrypt legacy quiz answers | Correct data encrypted or migrated; tests prove no plaintext leak |
| O2 | P1 | Ops | Rate limits and size caps | Per-owner limits; 429 on abuse; payload caps enforced |
| O3 | P1 | Ops | Metrics and logs | Planner reasons, cost, reports, p95; no secrets or answer keys |
| O4 | P2 | Ops | Export and reset | Owner can export or clear practice data with confirmation |
| Q1 | P0 | Quality | CI for the full suite | Unit, integration, and browser tests run on every change |
| Q2 | P1 | Quality | Security regression suite | Answer leak, cross-owner, live-test, and XSS cases stay covered |
| D1 | P0 | Docs | Content authoring guide | Schema, validation, verification, and a worked example |
| D2 | P1 | Docs | Checkpoint and progress API docs | Endpoint contracts and error semantics documented |
| D3 | P2 | Docs | Operations runbook | Flags, backup, migration, generation cost, and rollback |

---

## 6. Milestones

### M1 — Verified content seed (1–2 weeks)

Scope: C1–C5.

Exit criteria:

- A subject catalog loads for the default standalone subjects.
- At least 3 verified items exist for each seeded skill used by calibration.
- The import command rejects malformed, duplicate, or unverified content.
- The feed serves real cards for a seeded owner and never fabricates a question.
- A coverage report lists missing skills.

### M2 — Checkpoint and progress (1 week)

Scope: P1, U1, U2, P4.

Exit criteria:

- A checkpoint can be started, answered without feedback, and submitted once.
- Pass and fail paths schedule the correct follow-up.
- The progress view shows mastery bands and due counts from real attempts.
- The numeric form has a browser test.

### M3 — Teaching loop completion (1 week)

Scope: P2, P3, T1, T2.

Exit criteria:

- A lesson quiz with skill ids updates mastery and schedules retrieval.
- The next feed contains an after-lesson card.
- Unmapped quizzes are visible to the developer without changing mastery.
- A missed feed card produces a repair or teach follow-up.

### M4 — Hardening (1 week)

Scope: O1, O2, O3, Q1, Q2, U3.

Exit criteria:

- Legacy quiz answers are encrypted or migrated.
- Rate limits and payload caps are enforced.
- CI runs unit, integration, and browser tests.
- The security regression suite passes with zero answer leaks.

### M5 — Optional LMS context and calibration (after launch)

Scope: I1, I2, C6, U5, O4, D3.

Exit criteria:

- A connected LMS can add deadline cards without becoming a dependency.
- Legacy triage is behind the integration boundary.
- Hosted calibration shares item statistics, never student data.

---

## 7. Next two weeks

### Week 1

1. C1: add the skill catalog file format, loader, and a small DP seed.
2. C2: build the verified item import command with dry-run and validation.
3. C3: draft the authoring prompt and strict output schema.
4. C4: build the validator and the independent verifier skeleton.
5. C5: add the coverage report.
6. D1: document the content contract and one worked example.

### Week 2

1. C6: add provenance and near-duplicate checks.
2. P1: implement checkpoint runs and the API.
3. U2: add the checkpoint UI.
4. U1: add the progress view.
5. P4: add the numeric browser test.
6. Enable both flags locally with a seeded catalog and review the first real feed.

### Explicitly deferred

- Free-response grading.
- Timed tests.
- Social or leaderboard features.
- Hosted multi-user calibration.
- A native mobile app.
- Generative hints in the feed request path.

---

## 8. Metrics

### Learning

- First-attempt accuracy on due reviews.
- Seven-day retention of skills.
- Checkpoint pass rate and error recovery.
- Mastery gain per subject per week.
- Percentage of enrolled units with at least three attempts.

### Loop

- Lessons started from a feed card.
- Feed cards created after a lesson.
- Lesson quizzes with recorded skill ids.
- Feed-to-lesson-to-feed round trips.

### Content quality

- Verified items per skill.
- Verifier pass rate.
- Item report rate.
- Ambiguity reports resolved.
- Answer-leak incidents, target zero.

### Performance

- Feed p95 under 200 ms locally.
- Attempt p95 under 120 ms.
- First card visible under 1 second on a warm server.
- No unbounded DOM growth while scrolling.

### Wellbeing

- Median session length.
- Skip rate.
- End-of-plan completion.
- No dark-pattern streak loss.

---

## 9. Risks and dependencies

| Risk | Impact | Mitigation |
|---|---|---|
| No verified content at launch | High | Seed a small catalog; show calibration; do not invent items |
| Generated items are wrong or ambiguous | High | Deterministic validator, independent verifier, reports, retirement |
| Content copyright | High | Original items, references only, no verbatim past papers |
| Planner overfits one student | Medium | Exploration share, coverage checkpoints, local tuning only |
| Answer leakage | High | Server-side solutions, encrypted at rest, response tests |
| LLM cost or latency | Medium | Background batches, cache, no hot-path calls, circuit breaker |
| Browser regression | Medium | Existing Playwright suites, numeric test, accessibility pass |
| Migration breaks an old database | Medium | Additive schema, guarded columns, migration test, backup |
| LMS coupling returns | Medium | Plugin boundary tests; practice imports no LMS module |
| Scope creep into proctoring | High | Formative-only scope; no school grade integration |

Dependencies:

- Content authoring needs a working model endpoint and a verifier model.
- Checkpoints need enough verified items per skill.
- After-lesson cards need lesson quizzes to carry skill ids.
- Progress view needs real attempt history.
- A default-on rollout needs M1–M4 complete.

---

## 10. Open decisions

| # | Decision | Recommendation |
|---|---|---|
| 1 | Seed catalog source | Public subject guides; store only topic titles and source refs |
| 2 | Authoring model | Use the configured teacher/maker model; keep model choice in config |
| 3 | Verification threshold | Independent model pass plus two-report quarantine |
| 4 | Minimum pool per skill | 3 verified items for practice, 8 for checkpoints |
| 5 | Default daily goal | 12 cards; keep it adjustable |
| 6 | Checkpoint timing | After 6 attempts across 2 skills, or on student request |
| 7 | Feed default | Keep opt-in until M1–M4 exit criteria pass |
| 8 | Free response | Defer until deterministic or rubric-graded items are proven |
| 9 | Hosted item sharing | Verified, ownerless items only; aggregate stats, no student data |
| 10 | Human review | Start model-verified; add human review for reported items |

---

## 11. Out of scope

- Proctoring or high-stakes assessment.
- Exam-score prediction.
- Social feeds, leaderboards, or study groups.
- A native mobile client.
- Cloud sync in this phase.
- Full IRT calibration with large cohorts.
- Audio or handwriting input.
- Replacing the Socratic teacher with a feed-only experience.

---

## 12. Definition of done

A work item is done when:

1. It has tests that fail before the change and pass after it.
2. Owner scoping and answer-key rules are unchanged or stronger.
3. It works with the LMS plugin disabled.
4. It respects the feature flags and doesn't change the default experience.
5. The relevant docs and changelog are updated.
6. Browser behavior is checked at desktop and mobile widths.
7. No new synthetic student data enters production paths.

---

## Appendix A — First content contract

A skill record needs:

- id, subject, level, unit, title, description
- prereq_ids, exam_weight
- source_kind, source_ref

An item record needs:

- id, skill_ids, format, stem, options
- correct indices or numeric value and tolerance
- explanation, difficulty, guessing
- source_kind, source_ref, verification

Validation rules:

- 2–5 options for MCQ; exactly one correct for single-select.
- No answer or explanation in the stem.
- No duplicate options.
- No HTML or script; Markdown and LaTeX are allowed and sanitized at render.
- A content hash must be unique.
- An unverified item is never eligible for the feed.

---

## Appendix B — Useful commands

Unit and integration suite:

    pytest --ignore=tests/test_e2e_playwright.py --ignore=tests/test_home_e2e.py

Browser suite:

    pytest tests/test_e2e_playwright.py tests/test_home_e2e.py

Full suite:

    pytest -q

Local feed:

    DOOMED_PRACTICE_ENGINE=1 DOOMED_HOME_FEED=1 uvicorn app.main:app --host 127.0.0.1 --port 8000

With no verified items, the feed correctly returns the calibration state.
