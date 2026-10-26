// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * Home feed controller.
 *
 * Fetches the cursor-paginated daily plan, renders practice cards, posts
 * grades to /api/practice/attempts, and batches presentation events
 * (impression / dwell / skip / report / teach_click) to /api/home/events.
 *
 * Item stems, options, reasons, and explanations are untrusted. They are
 * painted as text first and only upgraded through renderMarkdown(), which
 * runs Marked + KaTeX + DOMPurify (and fails closed without either).
 *
 * The controller is inert until app.js calls initHome(); the module is also
 * loaded directly by index.html, so importing it twice is harmless.
 */

import { renderQuizCard } from "./cards.js";
import { renderMarkdown } from "./renderer.js";

const PAGE_LIMIT = 6;
const EVENT_BATCH_MAX = 5;
const EVENT_FLUSH_MS = 5000;
const MAX_DWELL_MS = 600000;
const DONT_KNOW = "__dont_know__";

const KIND_LABELS = {
  practice: "Practice",
  retrieval: "Review",
  repair: "Repair",
  checkpoint: "Checkpoint",
  teach: "Teach",
  recap: "Recap",
  deadline: "Deadline",
  reflection: "Reflection",
  application: "Application",
};

const BAND_LABELS = { easy: "Easy", medium: "Medium", hard: "Hard" };

const REPORT_KINDS = [
  ["ambiguous", "Ambiguous wording"],
  ["wrong", "Wrong answer"],
  ["outdated", "Outdated"],
  ["too_easy", "Too easy"],
  ["too_hard", "Too hard"],
];

// ---------------------------------------------------------------------------
// Small DOM helpers
// ---------------------------------------------------------------------------

function byId(id) {
  return document.getElementById(id);
}

function el(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}

/** Literal text first, then renderer.js sanitized Markdown/LaTeX in place. */
function setRichText(element, source) {
  const text = String(source ?? "");
  element.textContent = text;
  if (!text) return;
  try {
    const html = renderMarkdown(text);
    if (html) element.innerHTML = html; // renderMarkdown escapes/sanitizes.
  } catch (err) {
    element.textContent = text;
  }
}

function num(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function makeToken() {
  try {
    if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
      return crypto.randomUUID();
    }
  } catch (err) {
    /* insecure context: fall through */
  }
  return "attempt-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 12);
}

function safeDomId(value) {
  return String(value || "card").replace(/[^a-zA-Z0-9_-]+/g, "-");
}

function timeGreeting() {
  const hour = new Date().getHours();
  if (hour < 12) return "Good morning";
  if (hour < 17) return "Good afternoon";
  return "Good evening";
}

function prettyDay(day) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(day || ""));
  if (!match) return "Today's plan";
  try {
    const value = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]), 12, 0, 0);
    return value.toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" });
  } catch (err) {
    return "Today's plan";
  }
}

function formatSeconds(seconds) {
  const value = num(seconds, 0);
  if (value <= 0) return "";
  if (value < 60) return "~" + Math.round(value) + "s";
  return "~" + Math.max(1, Math.round(value / 60)) + " min";
}

function formatDue(dueSeconds) {
  const stamp = Number(dueSeconds);
  if (!Number.isFinite(stamp)) return "";
  const diff = stamp * 1000 - Date.now();
  if (diff <= 0) return "now";
  if (diff < 3600000) return "in " + Math.max(1, Math.round(diff / 60000)) + " min";
  if (diff < 86400000) return "in " + Math.round(diff / 3600000) + " h";
  const days = Math.ceil(diff / 86400000);
  return "in " + days + " day" + (days === 1 ? "" : "s");
}

function masteryNote(result) {
  if (!result || !result.mastery) return null;
  const mastery = result.mastery;
  const after = Number(mastery.after);
  if (!Number.isFinite(after)) return null;
  const band = String(mastery.band || "").trim();
  const label = band ? band.charAt(0).toUpperCase() + band.slice(1) : "Updated";
  const bits = ["Mastery " + label + " · " + Math.round(after * 100) + "%"];
  if (result.next_due_at) {
    const due = formatDue(result.next_due_at);
    if (due) bits.push("next review " + due);
  }
  return el("p", "feed-mastery-note", bits.join(" · "));
}

function followUpNote(result) {
  const followUp = result && result.follow_up;
  if (!followUp || !followUp.reason) return null;
  return el("p", "feed-follow-up", followUp.reason);
}

function errorMessage(err) {
  return (err && (err.message || err.detail)) || String(err || "Something went wrong.");
}

// ---------------------------------------------------------------------------
// Controller
// ---------------------------------------------------------------------------

class HomeFeed {
  constructor(api, refs) {
    this.api = api || {};
    this.refs = refs;
    this.records = new Map();
    this.plan = null;
    this.planId = null;
    this.cursor = 0;
    this.hasMore = false;
    this.loading = false;
    this.loaded = false;
    this.generation = 0;
    this.eventQueue = [];
    this.eventTimer = null;
    this.scrollPos = 0;
    this.observer = null;
    this.sentinelObserver = null;
    this.feedEndSent = false;
    this.reportMenus = [];

    this.bindUi();
    this.initObservers();
    this.resetHeader();
  }

  // -- wiring ---------------------------------------------------------------

  bindUi() {
    const refs = this.refs;
    if (refs.loadMore) {
      refs.loadMore.addEventListener("click", () => this.loadMore());
    }
    if (refs.endLessons) {
      refs.endLessons.addEventListener("click", () => this.call("onLessons"));
    }
    if (refs.endRefresh) {
      refs.endRefresh.addEventListener("click", () => this.reload());
    }

    // renderQuizCard dispatches this after painting the grade banner. Make the
    // announcement polite and once, and add the mastery line next to it.
    if (refs.cards) {
      refs.cards.addEventListener("doomed:quiz-graded", (event) => {
        const detail = event.detail || {};
        const record = this.records.get(String(detail.cardId || ""));
        const grade = record ? record.article.querySelector(".dcard-grade") : null;
        if (grade) {
          grade.setAttribute("role", "status");
          grade.setAttribute("aria-live", "polite");
        }
        if (record && !record.masteryRendered) {
          record.masteryRendered = true;
          const result = record.lastResult;
          if (result) {
            const mount = record.article.querySelector(".feed-card-mount");
            if (mount) {
              const note = masteryNote(result);
              if (note) mount.appendChild(note);
              const follow = followUpNote(result);
              if (follow) mount.appendChild(follow);
            }
          }
        }
      });
    }

    // Reporting menus are inline popovers; close them on outside click/Escape.
    document.addEventListener("click", (event) => {
      for (const entry of this.reportMenus) {
        if (!entry.menu || entry.menu.hidden) continue;
        if (!entry.wrap.contains(event.target)) {
          entry.menu.hidden = true;
          if (entry.button) entry.button.setAttribute("aria-expanded", "false");
        }
      }
    });
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      for (const entry of this.reportMenus) {
        if (!entry.menu || entry.menu.hidden) continue;
        entry.menu.hidden = true;
        if (entry.button) {
          entry.button.setAttribute("aria-expanded", "false");
          entry.button.focus();
        }
      }
    });

    document.addEventListener("visibilitychange", () => {
      if (document.hidden) {
        this.stopAllDwell();
        this.flushEvents({ beacon: true });
      } else {
        for (const record of this.records.values()) {
          if (record.ratio >= 0.5 && !record.answered && !record.skipped) this.startDwell(record);
        }
      }
    });
    window.addEventListener("pagehide", () => {
      this.stopAllDwell();
      this.flushEvents({ beacon: true });
    });
  }

  initObservers() {
    if (typeof IntersectionObserver !== "function") return;
    this.observer = new IntersectionObserver(
      (entries) => this.handleIntersections(entries),
      { threshold: [0, 0.5, 1] }
    );
    if (this.refs.sentinel) {
      this.sentinelObserver = new IntersectionObserver(
        (entries) => {
          if (entries.some((entry) => entry.isIntersecting)) this.loadMore();
        },
        { rootMargin: "800px 0px" }
      );
      this.sentinelObserver.observe(this.refs.sentinel);
    }
  }

  // -- public surface used by app.js ---------------------------------------

  activate() {
    if (this.api.hasSession && !this.api.hasSession()) {
      this.renderAuthState();
      return;
    }
    if (this.scrollPos && this.refs.view) {
      const target = this.scrollPos;
      requestAnimationFrame(() => {
        if (this.refs.view) this.refs.view.scrollTop = target;
      });
    }
    if (!this.loaded && !this.loading) {
      this.loadPage();
    } else {
      this.syncLoadMore();
    }
  }

  deactivate() {
    if (this.refs.view) this.scrollPos = this.refs.view.scrollTop;
    this.stopAllDwell();
  }

  reload() {
    this.generation += 1;
    this.loading = false;
    this.loaded = false;
    this.feedEndSent = false;
    this.disabledNotified = false;
    this.clearCards();
    this.cursor = 0;
    this.hasMore = false;
    this.refs.empty.hidden = true;
    this.refs.empty.replaceChildren();
    this.refs.end.hidden = true;
    this.refs.status.textContent = "";
    this.refs.skeleton.hidden = false;
    this.setBusy(false);
    this.loadPage();
  }

  reset() {
    this.generation += 1;
    this.loading = false;
    this.loaded = false;
    this.feedEndSent = false;
    this.clearCards();
    this.eventQueue = [];
    if (this.eventTimer) {
      clearTimeout(this.eventTimer);
      this.eventTimer = null;
    }
    this.plan = null;
    this.planId = null;
    this.cursor = 0;
    this.hasMore = false;
    this.scrollPos = 0;
    if (this.refs.view) this.refs.view.scrollTop = 0;
    if (this.refs.empty) {
      this.refs.empty.hidden = true;
      this.refs.empty.replaceChildren();
    }
    if (this.refs.end) this.refs.end.hidden = true;
    if (this.refs.status) this.refs.status.textContent = "";
    if (this.refs.skeleton) this.refs.skeleton.hidden = true;
    this.syncLoadMore();
    this.resetHeader();
  }

  // -- loading -------------------------------------------------------------

  async loadPage() {
    if (this.loading) return;
    if (this.api.hasSession && !this.api.hasSession()) {
      this.renderAuthState();
      return;
    }
    const generation = this.generation;
    const cursor = this.cursor;
    this.loading = true;
    this.setBusy(true);
    if (!this.loaded) {
      this.refs.skeleton.hidden = false;
      this.refs.status.textContent = "";
    } else {
      this.refs.status.textContent = "Loading more cards…";
    }
    this.syncLoadMore();

    const params = new URLSearchParams({ cursor: String(cursor), limit: String(PAGE_LIMIT) });
    try {
      const response = await fetch("/api/home/feed?" + params.toString(), {
        headers: { Accept: "application/json" },
        credentials: "same-origin",
      });
      if (generation !== this.generation) return;

      if (response.status === 401) {
        this.renderAuthState();
        this.notifyUnauthorized();
        return;
      }
      if (response.status === 404) {
        this.renderDisabledState();
        this.notifyDisabled();
        return;
      }
      if (!response.ok) {
        let detail = "HTTP " + response.status;
        try {
          const body = await response.json();
          detail = body.detail || detail;
        } catch (err) {
          /* non-JSON error body */
        }
        throw new Error(detail);
      }

      const data = await response.json();
      if (generation !== this.generation) return;
      this.applyPage(data);
      this.loaded = true;
    } catch (err) {
      if (generation !== this.generation) return;
      if (!this.loaded) {
        this.renderErrorState(err);
      } else {
        this.refs.status.textContent = "Could not load more cards: " + errorMessage(err);
      }
    } finally {
      if (generation === this.generation) {
        this.loading = false;
        this.setBusy(false);
        this.refs.skeleton.hidden = true;
        this.syncLoadMore();
      }
    }
  }

  loadMore() {
    if (this.loading || !this.hasMore || !this.loaded) return;
    this.loadPage();
  }

  applyPage(data) {
    this.plan = data.plan || this.plan;
    if (this.plan) {
      this.planId = this.plan.id || this.planId;
      this.renderPlan(this.plan);
    }

    const cards = Array.isArray(data.cards) ? data.cards : [];
    let added = 0;
    for (const card of cards) {
      if (this.renderCard(card)) added += 1;
    }

    this.cursor = Number.isFinite(Number(data.next_cursor)) ? Number(data.next_cursor) : null;
    this.hasMore = Boolean(data.has_more) && this.cursor !== null;
    this.syncLoadMore();
    this.refs.skeleton.hidden = true;

    if (this.records.size === 0) {
      this.refs.end.hidden = true;
      if (this.plan && this.plan.needs_calibration) {
        this.renderCalibrationState();
      } else if (!this.hasMore) {
        this.renderEmptyPlanState();
      }
      return;
    }

    this.refs.empty.hidden = true;
    this.refs.empty.replaceChildren();
    if (!this.hasMore) {
      this.refs.end.hidden = false;
      this.refs.status.textContent = "";
      if (!this.feedEndSent) {
        this.feedEndSent = true;
        this.queueEvent("feed_end", null);
      }
    } else {
      this.refs.end.hidden = true;
      this.refs.status.textContent = added === 0 ? "No new cards in this page." : "";
    }
  }

  // -- plan header ---------------------------------------------------------

  resetHeader() {
    const refs = this.refs;
    if (refs.greeting) refs.greeting.textContent = timeGreeting();
    if (refs.eyebrow) refs.eyebrow.textContent = "Today's plan";
    if (refs.subline) refs.subline.textContent = "A few small checks, chosen for what you need next.";
    if (refs.checkpoint) refs.checkpoint.hidden = true;
    if (refs.streak) refs.streak.textContent = "—";
    if (refs.exam) refs.exam.textContent = "—";
    if (refs.due) refs.due.textContent = "—";
    this.renderProgress(0, 12);
  }

  renderPlan(plan) {
    if (!plan) return;
    const refs = this.refs;
    const student = this.call("getStudent");
    const fullName = student && student.name ? String(student.name).trim() : "";
    const firstName = fullName ? fullName.split(/\s+/)[0] : "";
    if (refs.greeting) {
      refs.greeting.textContent = timeGreeting() + (firstName ? ", " + firstName : "");
    }
    if (refs.eyebrow) refs.eyebrow.textContent = prettyDay(plan.day);

    const streak = Math.max(0, num(plan.streak_days, 0));
    if (refs.streak) refs.streak.textContent = streak + (streak === 1 ? " day" : " days");

    const examDays = plan.exam_countdown_days;
    if (refs.exam) {
      if (typeof examDays === "number" && Number.isFinite(examDays)) {
        refs.exam.textContent = Math.max(0, examDays) + (examDays === 1 ? " day" : " days");
      } else {
        refs.exam.textContent = "—";
      }
    }
    if (refs.due) refs.due.textContent = String(Math.max(0, num(plan.due_reviews, 0)));
    if (refs.checkpoint) refs.checkpoint.hidden = !plan.checkpoint_due;

    const answered = Math.max(0, num(plan.answered, 0));
    const goal = Math.max(1, num(plan.goal_cards, 12));
    this.renderProgress(answered, goal);

    if (refs.subline) {
      let text = "A short adaptive set, chosen for what you need next. “I don't know” is always free.";
      if (plan.needs_calibration) {
        text = "No verified practice items are ready for your subjects yet. A lesson still works.";
      } else if (plan.checkpoint_due) {
        text = "A checkpoint is ready when you are — formative, and never sent to school.";
      } else if (answered >= goal) {
        text = "You hit today's goal. Extra practice is optional — stop while it still feels easy.";
      } else if (streak > 1) {
        text = "Day " + streak + " of the streak. Small and often is the whole trick.";
      }
      refs.subline.textContent = text;
    }
  }

  renderProgress(answered, goal) {
    const refs = this.refs;
    const total = Math.max(1, num(goal, 12));
    const done = Math.max(0, num(answered, 0));
    if (refs.progressCount) refs.progressCount.textContent = done + " / " + total;
    if (refs.progressFill) {
      const pct = Math.max(0, Math.min(100, Math.round((done / total) * 100)));
      refs.progressFill.style.width = pct + "%";
    }
    if (refs.progressBar) {
      refs.progressBar.setAttribute("aria-valuemax", String(total));
      refs.progressBar.setAttribute("aria-valuenow", String(Math.min(done, total)));
    }
  }

  // -- cards ---------------------------------------------------------------

  renderCard(card) {
    if (!card || !card.card_id || this.records.has(String(card.card_id))) return false;
    const item = card.item && typeof card.item === "object" ? card.item : null;
    const kind = String(card.kind || "practice");
    const kindLabel = KIND_LABELS[kind] || "Practice";
    const skillLabels = item && Array.isArray(item.skill_labels) ? item.skill_labels.filter(Boolean) : [];
    const skillLabel = skillLabels.length ? skillLabels.join(" · ") : "Adaptive practice";

    const article = el("article", "feed-card");
    article.dataset.cardId = String(card.card_id);
    article.dataset.kind = kind;
    const answered = String(card.state || "pending") === "answered";
    article.dataset.state = answered ? "answered" : "pending";
    if (answered) article.classList.add("is-answered");
    article.setAttribute("aria-label", kindLabel + ": " + skillLabel);

    const record = {
      data: card,
      article,
      answered,
      skipped: false,
      attemptId: null,
      lastResult: null,
      mastered: false,
      submitting: false,
      teachBusy: false,
      impressed: false,
      visibleSince: null,
      dwellTotal: 0,
      ratio: 0,
      shownAt: (typeof performance !== "undefined" ? performance.now() : Date.now()),
    };
    this.records.set(String(card.card_id), record);

    article.appendChild(this.buildCardHead(card, item, kindLabel, skillLabel, record));

    if (!item) {
      article.appendChild(this.buildInfoBody(card));
    } else if (answered) {
      article.appendChild(this.buildAnsweredBody(item));
    } else if (item.format === "numeric") {
      article.appendChild(this.buildNumericBody(record, item));
    } else if (item.format === "mcq_single" || item.format === "mcq_multi") {
      article.appendChild(this.buildQuizBody(record, item));
    } else {
      article.appendChild(this.buildInfoBody(card));
    }

    article.appendChild(this.buildWhy(card));
    article.appendChild(this.buildActions(record, item));

    this.refs.cards.appendChild(article);
    if (this.observer) this.observer.observe(article);
    return true;
  }

  buildCardHead(card, item, kindLabel, skillLabel, record) {
    const head = el("header", "feed-card-head");
    const kind = el("span", "feed-kind", kindLabel);
    const skill = el("span", "feed-skill", skillLabel);
    skill.title = skillLabel;
    head.append(kind, skill);

    if (item && item.difficulty_band) {
      const band = el("span", "feed-band", BAND_LABELS[item.difficulty_band] || String(item.difficulty_band));
      band.dataset.band = String(item.difficulty_band);
      head.appendChild(band);
    }
    if (item && num(item.estimated_seconds, 0) > 0) {
      head.appendChild(el("span", "feed-time", formatSeconds(item.estimated_seconds)));
    }

    const chip = el("span", "feed-state-chip", record.answered ? "Answered" : "");
    chip.hidden = !record.answered;
    record.chip = chip;
    head.appendChild(chip);
    return head;
  }

  buildAnsweredBody(item) {
    const fragment = document.createDocumentFragment();
    const stem = el("div", "feed-stem prose");
    setRichText(stem, item.stem || "");
    const row = el("div", "feed-answered");
    const mark = el("span", "feed-answered-mark", "✓");
    mark.setAttribute("aria-hidden", "true");
    row.append(mark, el("span", "feed-answered-text", "Answer recorded — this card is done for today."));
    fragment.append(stem, row);
    return fragment;
  }

  buildInfoBody(card) {
    const fragment = document.createDocumentFragment();
    const stem = el("div", "feed-stem prose");
    setRichText(stem, (card.reason && card.reason.text) || "This card is a pointer into a lesson.");
    const row = el("div", "feed-answered");
    row.appendChild(el("span", "feed-answered-text", "Open a lesson to work through this with the coach."));
    fragment.append(stem, row);
    return fragment;
  }

  buildQuizBody(record, item) {
    const mount = el("div", "feed-card-mount");
    const quiz = renderQuizCard(
      {
        call_id: record.data.card_id,
        question: item.stem || "Check your understanding.",
        options: Array.isArray(item.options) ? item.options : [],
        multi_select: item.format === "mcq_multi",
      },
      (reply) => this.submitQuiz(record, reply)
    );
    mount.appendChild(quiz);
    return mount;
  }

  buildNumericBody(record, item) {
    const mount = el("div", "feed-card-mount");
    const form = el("form", "feed-numeric");
    form.noValidate = true;
    const inputId = "feed-num-" + safeDomId(record.data.card_id);
    const hintId = inputId + "-hint";

    const stem = el("div", "feed-stem prose");
    setRichText(stem, item.stem || "");
    mount.appendChild(stem);

    const label = el("label", "feed-numeric-label", "Your answer");
    label.htmlFor = inputId;

    const row = el("div", "feed-numeric-row");
    const input = el("input", "feed-numeric-input");
    input.id = inputId;
    input.type = "text";
    input.inputMode = "decimal";
    input.autocomplete = "off";
    input.placeholder = "e.g. 4.2";
    input.setAttribute("aria-describedby", hintId);

    const check = el("button", "btn sm", "Check answer");
    check.type = "submit";
    const dontKnow = el("button", "btn ghost sm", "I don't know");
    dontKnow.type = "button";
    row.append(input, check, dontKnow);

    const hint = el("p", "feed-numeric-hint", "Graded on the server. Units are optional unless the question asks for them.");
    hint.id = hintId;
    const error = el("p", "feed-numeric-error");
    error.hidden = true;

    form.append(label, row, hint, error);

    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const value = input.value.trim();
      if (!value) {
        error.textContent = "Type a number first.";
        error.hidden = false;
        input.focus();
        return;
      }
      this.submitNumeric(record, value, { form, input, check, dontKnow, error });
    });
    dontKnow.addEventListener("click", () => {
      this.submitNumeric(record, DONT_KNOW, { form, input, check, dontKnow, error });
    });

    mount.appendChild(form);
    return mount;
  }

  buildWhy(card) {
    const details = el("details", "feed-why");
    const summary = el("summary", null, "Why this card?");
    details.appendChild(summary);
    const reason = card.reason && card.reason.text ? card.reason.text : "Chosen to keep this skill in view.";
    details.appendChild(el("p", "feed-why-text", reason));
    return details;
  }

  buildActions(record, item) {
    const actions = el("footer", "feed-card-actions");

    if (item && Array.isArray(item.skill_ids) && item.skill_ids.length) {
      const teach = el("button", "feed-action primary", "Teach me this");
      teach.type = "button";
      teach.dataset.action = "teach";
      teach.addEventListener("click", () => this.teach(record, teach));
      actions.appendChild(teach);
    }

    if (!record.answered) {
      const skip = el("button", "feed-action", "Skip");
      skip.type = "button";
      skip.dataset.action = "skip";
      skip.addEventListener("click", () => this.skip(record));
      actions.appendChild(skip);
    }

    const reportWrap = el("div", "feed-report");
    const reportButton = el("button", "feed-action", "Report");
    reportButton.type = "button";
    reportButton.setAttribute("aria-haspopup", "true");
    reportButton.setAttribute("aria-expanded", "false");

    const menu = el("div", "feed-report-menu");
    menu.hidden = true;
    menu.appendChild(el("p", "feed-report-title", "What's wrong?"));
    for (const pair of REPORT_KINDS) {
      const choice = el("button", "feed-action", pair[1]);
      choice.type = "button";
      choice.dataset.reportKind = pair[0];
      choice.addEventListener("click", () => this.report(record, pair[0], menu, reportButton));
      menu.appendChild(choice);
    }

    reportButton.addEventListener("click", () => {
      const open = menu.hidden;
      menu.hidden = !open;
      reportButton.setAttribute("aria-expanded", String(open));
      if (!open) reportButton.focus();
    });
    reportWrap.append(reportButton, menu);
    actions.appendChild(reportWrap);
    this.reportMenus.push({ wrap: reportWrap, menu, button: reportButton });
    return actions;
  }

  // -- attempts ------------------------------------------------------------

  async submitQuiz(record, reply) {
    const selected = reply && Array.isArray(reply.selected) ? reply.selected : [];
    try {
      const result = await this.postAttempt(record, selected);
      record.lastResult = result;
      this.recordAttempt(record, result);
      return result;
    } catch (err) {
      if (err.status === 401) this.expireSession();
      if (err.status === 409) this.markAlreadyAnswered(record);
      throw err;
    }
  }

  async submitNumeric(record, value, parts) {
    if (record.submitting || record.answered) return;
    record.submitting = true;
    parts.check.disabled = true;
    parts.dontKnow.disabled = true;
    parts.input.disabled = true;
    parts.error.hidden = true;
    parts.check.textContent = "Checking…";
    try {
      const result = await this.postAttempt(record, [value]);
      record.lastResult = result;
      this.recordAttempt(record, result);
      this.showNumericFeedback(record, result);
    } catch (err) {
      parts.check.disabled = false;
      parts.dontKnow.disabled = false;
      parts.input.disabled = false;
      parts.check.textContent = "Check answer";
      if (err.status === 401) {
        this.expireSession();
      } else if (err.status === 409) {
        this.markAlreadyAnswered(record);
      } else {
        parts.error.textContent = errorMessage(err);
        parts.error.hidden = false;
      }
    } finally {
      record.submitting = false;
    }
  }

  async postAttempt(record, selected) {
    const item = record.data.item || {};
    const body = {
      card_id: record.data.card_id,
      item_id: item.id,
      item_version: num(item.version, 1),
      selected: Array.isArray(selected) ? selected : [selected],
      attempt_token: makeToken(),
      latency_ms: Math.max(0, Math.min(300000, Math.round((typeof performance !== "undefined" ? performance.now() : Date.now()) - record.shownAt))),
    };
    const response = await fetch("/api/practice/attempts", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      credentials: "same-origin",
      body: JSON.stringify(body),
    });
    if (response.status === 401) {
      const err = new Error("Your session expired. Sign in again to keep practising.");
      err.status = 401;
      throw err;
    }
    if (!response.ok) {
      let detail = "HTTP " + response.status;
      try {
        const payload = await response.json();
        detail = payload.detail || detail;
      } catch (err) {
        /* non-JSON error body */
      }
      const failure = new Error(detail);
      failure.status = response.status;
      throw failure;
    }
    return response.json();
  }

  recordAttempt(record, result) {
    if (!record || record.answered || !result) return;
    record.answered = true;
    record.article.dataset.state = "answered";
    record.article.classList.add("is-answered");
    if (record.chip) {
      record.chip.hidden = false;
      record.chip.textContent = "Answered";
    }
    record.attemptId = result.attempt_id || null;
    record.lastResult = result;
    if (this.plan) {
      this.plan.answered = Math.max(0, num(this.plan.answered, 0)) + 1;
      this.renderProgress(this.plan.answered, this.plan.goal_cards);
    }
    this.stopDwell(record);
    this.queueEvent("reveal", record.data.card_id);
  }

  markAlreadyAnswered(record) {
    if (!record) return;
    record.answered = true;
    record.article.dataset.state = "answered";
    record.article.classList.add("is-answered");
    if (record.chip) {
      record.chip.hidden = false;
      record.chip.textContent = "Answered";
    }
    const form = record.article.querySelector(".feed-numeric");
    if (form) {
      const error = record.article.querySelector(".feed-numeric-error");
      if (error) {
        error.textContent = "This card has already been answered. Reload to see the saved plan.";
        error.hidden = false;
      }
    }
  }

  showNumericFeedback(record, result) {
    const mount = record.article.querySelector(".feed-card-mount");
    if (!mount) return;
    const kind = result.dont_know ? "dk" : result.correct ? "ok" : "bad";
    const panel = el("div", "feed-feedback " + kind);
    panel.setAttribute("role", "status");
    panel.setAttribute("aria-live", "polite");
    const title = result.dont_know ? "— Not known yet" : result.correct ? "✓ Correct" : "✗ Not yet";
    panel.appendChild(el("div", "feed-feedback-title", title));
    if (result.explanation) {
      const body = el("div", "feed-feedback-body prose");
      setRichText(body, result.explanation);
      panel.appendChild(body);
    }
    const note = masteryNote(result);
    if (note) panel.appendChild(note);
    const follow = followUpNote(result);
    if (follow) panel.appendChild(follow);
    mount.replaceChildren(panel);
  }

  // -- card actions --------------------------------------------------------

  skip(record) {
    if (!record || record.answered) return;
    record.skipped = true;
    record.article.hidden = true;
    record.article.classList.add("is-skipped");
    this.stopDwell(record);
    if (this.observer) this.observer.unobserve(record.article);
    this.queueEvent("skip", record.data.card_id);
    this.refs.status.textContent = "Skipped. It stays out of the way until you reload — no grade was recorded.";
  }

  async report(record, kind, menu, button) {
    const item = record.data.item;
    if (!item) return;
    menu.hidden = true;
    button.setAttribute("aria-expanded", "false");
    try {
      const response = await fetch("/api/practice/feedback", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ item_id: item.id, kind, note: "" }),
      });
      if (response.status === 401) {
        this.expireSession();
        return;
      }
      if (!response.ok) throw new Error("HTTP " + response.status);
      this.queueEvent("report", record.data.card_id, { kind });
      button.textContent = "Reported";
      button.disabled = true;
      this.toast("Thanks — this item was flagged for review.", "success");
    } catch (err) {
      this.toast("Could not send that report: " + errorMessage(err), "error");
    }
  }

  async teach(record, button) {
    if (!record || record.teachBusy) return;
    const item = record.data.item || {};
    const skillIds = Array.isArray(item.skill_ids) ? item.skill_ids.filter(Boolean) : [];
    if (!skillIds.length) {
      this.toast("This card is not linked to a skill yet.", "info");
      return;
    }
    record.teachBusy = true;
    button.disabled = true;
    const label = button.textContent;
    button.textContent = "Opening lesson…";
    try {
      const response = await fetch("/api/practice/teach", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({
          skill_ids: skillIds,
          attempt_ids: record.attemptId ? [record.attemptId] : [],
          intent: "teach",
        }),
      });
      if (response.status === 401) {
        this.expireSession();
        return;
      }
      if (response.status === 404) {
        this.notifyDisabled();
        return;
      }
      if (!response.ok) {
        let detail = "HTTP " + response.status;
        try {
          const payload = await response.json();
          detail = payload.detail || detail;
        } catch (err) {
          /* non-JSON error body */
        }
        throw new Error(detail);
      }
      const data = await response.json();
      if (!data.thread_id) throw new Error("The server did not return a lesson.");
      this.queueEvent("teach_click", record.data.card_id);
      await this.flushEvents();
      await this.callAsync("onTeach", {
        threadId: data.thread_id,
        seedMessage: data.seed_message || "Teach me this.",
        cardId: record.data.card_id,
        skillIds,
      });
    } catch (err) {
      if (err.status === 401) this.expireSession();
      this.toast("Could not open a lesson: " + errorMessage(err), "error");
    } finally {
      record.teachBusy = false;
      button.disabled = false;
      button.textContent = label;
    }
  }

  // -- states --------------------------------------------------------------

  renderAuthState() {
    this.refs.skeleton.hidden = true;
    this.showState({
      title: "Sign in to see your plan",
      text: "Your daily practice plan is private to your account. Sign in with Indus LMS, or start an independent IB study session.",
      actions: [
        { label: "Sign in", primary: true, onClick: () => this.call("onSignIn") },
        { label: "Start independent study", onClick: () => this.call("onStartLocal") },
      ],
      note: "No practice data is fetched without a session.",
    });
  }

  renderCalibrationState() {
    this.refs.skeleton.hidden = true;
    this.showState({
      title: "Preparing your practice",
      text: "No verified practice items are ready for your subjects yet. Start a lesson and the coach will teach from what you already know; the feed fills in as content is verified.",
      actions: [
        { label: "Open a lesson", primary: true, onClick: () => this.call("onLessons") },
        { label: "Check again", onClick: () => this.reload() },
      ],
      note: "No placeholder questions are shown — practice appears only when content is verified.",
    });
  }

  renderEmptyPlanState() {
    this.refs.skeleton.hidden = true;
    this.showState({
      title: "Nothing due right now",
      text: "Your plan is clear for today. Open a lesson to go deeper on anything you just practised, or check again after a break.",
      actions: [
        { label: "Open a lesson", primary: true, onClick: () => this.call("onLessons") },
        { label: "Check again", onClick: () => this.reload() },
      ],
    });
  }

  renderErrorState(err) {
    this.refs.skeleton.hidden = true;
    this.showState({
      title: "Could not load your plan",
      text: errorMessage(err) + ". Lessons still work while the feed is unavailable.",
      actions: [
        { label: "Try again", primary: true, onClick: () => this.reload() },
        { label: "Open a lesson", onClick: () => this.call("onLessons") },
      ],
    });
  }

  renderDisabledState() {
    this.refs.skeleton.hidden = true;
    this.showState({
      title: "Home feed is off",
      text: "This server has the practice engine disabled. Lessons, files, and study triage still work exactly as before.",
      actions: [{ label: "Open a lesson", primary: true, onClick: () => this.call("onLessons") }],
    });
  }

  showState({ title, text, actions, note }) {
    const empty = this.refs.empty;
    if (!empty) return;
    empty.replaceChildren();
    empty.hidden = false;
    const mark = el("div", "home-empty-mark", "d");
    mark.setAttribute("aria-hidden", "true");
    empty.append(mark, el("h2", null, title), el("p", null, text));
    if (Array.isArray(actions) && actions.length) {
      const row = el("div", "home-empty-actions");
      for (const action of actions) {
        const button = el("button", action.primary ? "btn" : "btn ghost", action.label);
        button.type = "button";
        if (typeof action.onClick === "function") button.addEventListener("click", action.onClick);
        row.appendChild(button);
      }
      empty.appendChild(row);
    }
    if (note) empty.appendChild(el("p", "home-empty-note", note));
    this.refs.end.hidden = true;
  }

  clearCards() {
    this.stopAllDwell();
    for (const record of this.records.values()) {
      if (this.observer) this.observer.unobserve(record.article);
    }
    this.records.clear();
    this.reportMenus = [];
    if (this.refs.cards) this.refs.cards.replaceChildren();
  }

  syncLoadMore() {
    if (!this.refs.loadMore) return;
    this.refs.loadMore.hidden = !this.hasMore;
    this.refs.loadMore.disabled = this.loading;
    this.refs.loadMore.textContent = this.loading ? "Loading…" : "Load more cards";
  }

  setBusy(busy) {
    if (this.refs.feed) this.refs.feed.setAttribute("aria-busy", busy ? "true" : "false");
  }

  // -- presentation events -------------------------------------------------

  queueEvent(type, cardId, value) {
    if (this.api.hasSession && !this.api.hasSession()) return;
    if (!type) return;
    this.eventQueue.push({
      type,
      card_id: cardId || null,
      plan_id: this.planId || null,
      value: value === undefined ? null : value,
    });
    if (this.eventQueue.length >= EVENT_BATCH_MAX) {
      this.flushEvents();
      return;
    }
    if (!this.eventTimer) {
      this.eventTimer = setTimeout(() => {
        this.eventTimer = null;
        this.flushEvents();
      }, EVENT_FLUSH_MS);
    }
  }

  async flushEvents({ beacon = false } = {}) {
    if (this.eventTimer) {
      clearTimeout(this.eventTimer);
      this.eventTimer = null;
    }
    if (!this.eventQueue.length) return;
    const events = this.eventQueue.slice(0, 50);
    this.eventQueue = this.eventQueue.slice(events.length);

    const payload = JSON.stringify({ events });
    if (beacon && typeof navigator !== "undefined" && typeof navigator.sendBeacon === "function") {
      try {
        const blob = new Blob([payload], { type: "application/json" });
        if (navigator.sendBeacon("/api/home/events", blob)) return;
      } catch (err) {
        /* fall through to fetch */
      }
    }
    try {
      await fetch("/api/home/events", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: payload,
        keepalive: Boolean(beacon),
      });
    } catch (err) {
      /* events are fire-and-forget */
    }
    if (this.eventQueue.length) {
      if (!this.eventTimer) {
        this.eventTimer = setTimeout(() => {
          this.eventTimer = null;
          this.flushEvents();
        }, EVENT_FLUSH_MS);
      }
    }
  }

  handleIntersections(entries) {
    for (const entry of entries) {
      const record = this.records.get(String(entry.target.dataset.cardId || ""));
      if (!record) continue;
      record.ratio = entry.intersectionRatio;
      const visible = entry.isIntersecting && entry.intersectionRatio >= 0.5;
      if (visible && !record.answered && !record.skipped) {
        if (!record.impressed) {
          record.impressed = true;
          this.queueEvent("impression", record.data.card_id);
        }
        this.startDwell(record);
      } else {
        this.stopDwell(record);
      }
    }
  }

  startDwell(record) {
    if (!record || record.visibleSince !== null || record.answered || record.skipped) return;
    if (typeof document !== "undefined" && document.hidden) return;
    record.visibleSince = typeof performance !== "undefined" ? performance.now() : Date.now();
  }

  stopDwell(record) {
    if (!record || record.visibleSince === null) return;
    const now = typeof performance !== "undefined" ? performance.now() : Date.now();
    const elapsed = Math.max(0, now - record.visibleSince);
    record.visibleSince = null;
    const remaining = MAX_DWELL_MS - record.dwellTotal;
    if (remaining <= 0) return;
    const delta = Math.min(elapsed, remaining);
    record.dwellTotal += delta;
    if (delta < 500) return;
    this.queueEvent("dwell", record.data.card_id, {
      ms: Math.round(delta),
      visible: Math.round(record.ratio * 100) / 100,
    });
  }

  stopAllDwell() {
    for (const record of this.records.values()) this.stopDwell(record);
  }

  // -- tiny helpers --------------------------------------------------------

  call(name) {
    const fn = this.api[name];
    if (typeof fn === "function") return fn();
    return undefined;
  }

  callAsync(name, argument) {
    const fn = this.api[name];
    if (typeof fn !== "function") return Promise.resolve(undefined);
    try {
      return Promise.resolve(fn(argument));
    } catch (err) {
      return Promise.reject(err);
    }
  }

  expireSession() {
    this.renderAuthState();
    this.call("onUnauthorized");
  }

  notifyUnauthorized() {
    this.call("onUnauthorized");
  }

  notifyDisabled() {
    if (this.disabledNotified) return;
    this.disabledNotified = true;
    this.call("onDisabled");
  }

  toast(message, kind) {
    const fn = this.api.showToast;
    if (typeof fn === "function") fn(message, kind || "info");
  }
}

// ---------------------------------------------------------------------------
// Module surface
// ---------------------------------------------------------------------------

let controller = null;

export function initHome(api = {}) {
  if (controller) {
    controller.api = Object.assign({}, controller.api, api);
    return controller;
  }
  const refs = {
    view: byId("home-view"),
    feed: byId("home-feed"),
    cards: byId("home-cards"),
    empty: byId("home-empty"),
    end: byId("home-end"),
    endLessons: byId("home-end-lessons"),
    endRefresh: byId("home-end-refresh"),
    skeleton: byId("home-skeleton"),
    status: byId("home-status"),
    loadMore: byId("home-load-more"),
    sentinel: byId("home-sentinel"),
    greeting: byId("home-greeting"),
    eyebrow: byId("home-eyebrow"),
    subline: byId("home-subline"),
    checkpoint: byId("home-checkpoint"),
    streak: byId("home-streak"),
    exam: byId("home-exam"),
    due: byId("home-due"),
    progressBar: byId("home-progress-bar"),
    progressFill: byId("home-progress-fill"),
    progressCount: byId("home-progress-count"),
  };
  if (!refs.view || !refs.cards) return null;
  controller = new HomeFeed(api, refs);
  if (typeof window !== "undefined") window.doomedHome = controller;
  return controller;
}

export function getHome() {
  return controller;
}
