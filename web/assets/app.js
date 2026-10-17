// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * Main application controller for doom-ed.
 * Wires authentication, study coach triage, streaming Socratic chat,
 * interactive prompt cards, visual maker verification, and in-app file viewer.
 */

import { renderQuizCard, renderAskCard, renderWriteConfirmCard, renderVisualCard } from "./cards.js";
import { LessonLog } from "./log-export.js";
import { renderMarkdown } from "./renderer.js";
import { openViewer, closeViewer } from "./viewer.js";
import { initVizEngine, validateAndRasterize } from "./viz-engine.js";

// --- State -------------------------------------------------------------------
let currentSession = null;
let currentThreadId = null;
let currentThread = null;
let activeLog = new LessonLog();
let pendingAttachments = []; // files staged for next user send
let activeCitation = null; // { ref, filename, page }
let isStreaming = false;

// --- DOM References ----------------------------------------------------------
const $ = (id) => document.getElementById(id);

const profileCard = $("profile-card");
const profileAvatar = $("profile-avatar");
const profileName = $("profile-name");
const profileEmail = $("profile-email");
const profileGrade = $("profile-grade");
const signoutBtn = $("signout-btn");
const loginTrigger = $("login-trigger");

const countNow = $("count-now");
const countOverdue = $("count-overdue");
const countDueSoon = $("count-due-soon");
const countCalendar = $("count-calendar");
const countUnread = $("count-unread");
const listOverdue = $("list-overdue");
const listDueSoon = $("list-due-soon");
const listCalendarToday = $("list-calendar-today");
const listUnreadNotifs = $("list-unread-notifs");
const attendancePill = $("attendance-pill");
const refreshTriageBtn = $("refresh-triage-btn");

const countFix = $("count-fix");
const fixList = $("fix-list");

const breakWindow = $("break-window");
const breakQueue = $("break-queue");

const threadList = $("thread-list");
const newLessonBtn = $("new-lesson-btn");

const threadTitle = $("thread-title");
const phaseBadge = $("phase-badge");
const threadSubject = $("thread-subject");
const attachedTray = $("attached-tray");

const messageList = $("message-list");
const emptyState = $("empty-state");
const composerForm = $("composer-form");
const promptInput = $("prompt-input");
const attachBtn = $("attach-btn");
const fileInput = $("file-input");
const composerAttachments = $("composer-attachments");
const citationBadge = $("citation-badge");
const citationLabel = $("citation-label");
const sendBtn = $("send-btn");

const logDrawer = $("log-drawer");
const logToggle = $("log-toggle");
const logClose = $("log-close");
const logViewRendered = $("log-view-rendered");
const logViewRaw = $("log-view-raw");
const copyLogBtn = $("copy-log-btn");
const downloadLogBtn = $("download-log-btn");
const logEmpty = $("log-empty");
const logPreview = $("log-preview");
const logSource = $("log-source");

const loginModal = $("login-modal");
const loginForm = $("login-form");
const loginError = $("login-error");
const loginCancel = $("login-cancel");

const settingsModal = $("settings-modal");
const settingsBtn = $("settings-btn");
const settingsForm = $("settings-form");
const settingsStatus = $("settings-status");
const settingsCancel = $("settings-cancel");

const toasts = $("toasts");

// --- Toast Helper ------------------------------------------------------------
function showToast(message, type = "info") {
  const toast = document.createElement("div");
  toast.className = `toast toast-${type}`;
  toast.textContent = message;
  toasts.appendChild(toast);
  setTimeout(() => {
    toast.classList.add("fade-out");
    setTimeout(() => toast.remove(), 300);
  }, 4000);
}

// --- Auth Flow ---------------------------------------------------------------
async function checkAuth() {
  try {
    const res = await fetch("/api/session");
    if (res.ok) {
      const data = await res.json();
      currentSession = data.student;
      renderProfile();
      loadTriage();
      loadThreads();
    } else {
      currentSession = null;
      renderProfile();
    }
  } catch (e) {
    currentSession = null;
    renderProfile();
  }
}

function renderProfile() {
  if (currentSession) {
    profileCard.hidden = false;
    loginTrigger.hidden = true;
    profileName.textContent = currentSession.name || "Student";
    profileEmail.textContent = currentSession.email || "";
    profileGrade.textContent = currentSession.grade || "";
    profileAvatar.textContent = (currentSession.name || "S")[0].toUpperCase();
  } else {
    profileCard.hidden = true;
    loginTrigger.hidden = false;
    clearTriageUI();
  }
}

signoutBtn.addEventListener("click", async () => {
  await fetch("/api/logout", { method: "POST" });
  currentSession = null;
  renderProfile();
  showToast("Signed out", "info");
});

loginTrigger.addEventListener("click", () => {
  loginError.hidden = true;
  loginModal.showModal();
});

loginCancel.addEventListener("click", () => loginModal.close());

loginForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const email = $("login-email").value.trim();
  const password = $("login-password").value;
  loginError.hidden = true;

  try {
    const res = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || "Sign-in failed.");
    }
    const data = await res.json();
    currentSession = data.student;
    renderProfile();
    loginModal.close();
    loadTriage();
    loadThreads();
    showToast(`Signed in as ${currentSession.name}`, "success");
  } catch (err) {
    loginError.textContent = err.message;
    loginError.hidden = false;
  }
});

// --- Study Coach Triage Loader -----------------------------------------------
async function loadTriage(refresh = false) {
  if (!currentSession) return;
  try {
    const res = await fetch(`/api/study?refresh=${refresh}`);
    if (!res.ok) return;
    const json = await res.json();
    const data = json.data;
    if (!data) return;

    renderTriageNow(data.now);
    renderTriageWeak(data.weak_topics);
    renderTriageBreak(data.before_break);
  } catch (e) {
    console.warn("Triage load error:", e);
  }
}

function clearTriageUI() {
  listOverdue.innerHTML = "";
  listDueSoon.innerHTML = "";
  listCalendarToday.innerHTML = "";
  listUnreadNotifs.innerHTML = "";
  fixList.innerHTML = "";
  breakQueue.innerHTML = "";
  countOverdue.textContent = "0";
  countDueSoon.textContent = "0";
  countCalendar.textContent = "0";
  countUnread.textContent = "0";
  countFix.hidden = true;
  attendancePill.className = "attendance-pill unknown";
  attendancePill.textContent = "Not loaded";
}

function renderTriageNow(now) {
  if (!now) return;
  const overdue = now.overdue || [];
  const dueSoon = now.due_soon || [];
  const cal = now.calendar_today || [];
  const notifs = now.unread_notifications || [];

  countOverdue.textContent = overdue.length;
  countDueSoon.textContent = dueSoon.length;
  countCalendar.textContent = cal.length;
  countUnread.textContent = notifs.length;

  const totalNow = overdue.length + dueSoon.length + cal.length + notifs.length;
  if (totalNow > 0) {
    countNow.hidden = false;
    countNow.textContent = totalNow;
  } else {
    countNow.hidden = true;
  }

  listOverdue.innerHTML = overdue
    .map(
      (item) =>
        `<li><span class="item-title">${item.title || "Untitled"}</span><span class="item-sub">${item.subject || ""}</span></li>`
    )
    .join("");

  listDueSoon.innerHTML = dueSoon
    .map(
      (item) =>
        `<li><span class="item-title">${item.title || "Untitled"}</span><span class="item-sub">${item.subject || ""}</span></li>`
    )
    .join("");

  listCalendarToday.innerHTML = cal
    .map(
      (ev) =>
        `<li><span class="item-title">${ev.title || ev.label || "Event"}</span></li>`
    )
    .join("");

  listUnreadNotifs.innerHTML = notifs
    .map(
      (n) =>
        `<li><span class="item-title">${n.title || n.message || "Notice"}</span></li>`
    )
    .join("");

  if (now.attendance) {
    const att = now.attendance;
    const isPresent = att.present ?? att.status === "present";
    attendancePill.className = `attendance-pill ${isPresent ? "present" : "absent"}`;
    attendancePill.textContent = isPresent ? "Present Today" : "Absent / Pending";
  }
}

function renderTriageWeak(weakTopics) {
  if (!weakTopics || !weakTopics.length) {
    fixList.innerHTML = `<li class="empty-list-note">No active weak topics recorded. Great job!</li>`;
    countFix.hidden = true;
    return;
  }
  countFix.hidden = false;
  countFix.textContent = weakTopics.length;

  fixList.innerHTML = weakTopics
    .map((item) => {
      const wrongCount = item.wrong_questions ? item.wrong_questions.length : 0;
      return `
      <li class="fix-item">
        <div class="fix-head">
          <span class="fix-subject">${item.subject || "Subject"}</span>
          <span class="fix-score">${item.score_label || ""}</span>
        </div>
        <b class="fix-title">${item.topic || item.title}</b>
        <p class="fix-reason">${item.why_weak || ""}</p>
        <div class="fix-actions">
          <button class="btn sm" data-teach-topic="${item.topic}" data-teach-subj="${item.subject}">Teach from root</button>
          <button class="btn ghost sm" data-practise-topic="${item.topic}" data-practise-subj="${item.subject}">Practise IB</button>
        </div>
      </li>
    `;
    })
    .join("");

  // Attach click handlers
  fixList.querySelectorAll("[data-teach-topic]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const topic = btn.dataset.teachTopic;
      const subj = btn.dataset.teachSubj;
      startNewLessonWithPrompt(`Teach me ${topic} from foundations up. Probe my understanding first.`);
    });
  });

  fixList.querySelectorAll("[data-practise-topic]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const topic = btn.dataset.practiseTopic;
      startNewLessonWithPrompt(`Find IB past paper questions on ${topic} and give me a practice problem.`);
    });
  });
}

function renderTriageBreak(queue) {
  if (!queue || !queue.length) {
    breakQueue.innerHTML = `<li class="empty-list-note">Nothing due before break.</li>`;
    return;
  }
  breakQueue.innerHTML = queue
    .map(
      (item) => `
      <li>
        <span class="queue-title">${item.title}</span>
        <span class="queue-meta">${item.subject} · due ${item.due ? item.due.slice(0, 10) : "soon"}</span>
      </li>
    `
    )
    .join("");
}

refreshTriageBtn.addEventListener("click", () => loadTriage(true));

// --- Thread Management -------------------------------------------------------
async function loadThreads() {
  if (!currentSession) return;
  try {
    const res = await fetch("/api/threads");
    if (!res.ok) return;
    const json = await res.json();
    renderThreads(json.threads || []);
  } catch (e) {
    console.warn("Load threads error:", e);
  }
}

function renderThreads(threads) {
  if (!threads.length) {
    threadList.innerHTML = `<li class="empty-list-note">No previous lessons.</li>`;
    return;
  }
  threadList.innerHTML = threads
    .map(
      (t) => `
      <li class="thread-item ${t.id === currentThreadId ? "active" : ""}" data-thread-id="${t.id}">
        <span class="thread-item-title">${t.title || "Lesson"}</span>
        <span class="thread-item-phase">${t.phase || "probe"}</span>
      </li>
    `
    )
    .join("");

  threadList.querySelectorAll("[data-thread-id]").forEach((el) => {
    el.addEventListener("click", () => switchThread(el.dataset.threadId));
  });
}

async function switchThread(threadId) {
  if (isStreaming) return;
  currentThreadId = threadId;
  messageList.innerHTML = "";
  emptyState.hidden = true;
  activeLog = new LessonLog();

  try {
    const res = await fetch(`/api/threads/${threadId}`);
    if (!res.ok) return;
    const data = await res.json();
    currentThread = data.thread;

    threadTitle.textContent = currentThread.title || "Lesson";
    phaseBadge.dataset.phase = currentThread.phase || "probe";
    phaseBadge.textContent = (currentThread.phase || "probe").toUpperCase();
    activeLog.setMeta(currentThread.title, currentThread.topic);

    renderAttachedFiles(data.attachments || []);
    renderMessages(data.messages || []);
    loadThreads(); // update active highlight
  } catch (e) {
    showToast("Failed to load lesson thread", "error");
  }
}

function startNewLessonWithPrompt(initialText) {
  createNewThread().then(() => {
    if (initialText) {
      promptInput.value = initialText;
      sendMessage();
    }
  });
}

async function createNewThread(title = "New Lesson", topic = "General") {
  try {
    const res = await fetch("/api/threads", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title, topic }),
    });
    if (!res.ok) throw new Error("Failed to create lesson.");
    const data = await res.json();
    await switchThread(data.thread.id);
  } catch (e) {
    showToast(e.message, "error");
  }
}

newLessonBtn.addEventListener("click", () => createNewThread());

// --- Messages & Chat Streaming -----------------------------------------------
function renderAttachedFiles(attachments) {
  attachedTray.innerHTML = "";
  if (!attachments || !attachments.length) return;
  for (const att of attachments) {
    const chip = document.createElement("button");
    chip.className = "file-chip";
    chip.type = "button";
    chip.innerHTML = `<span class="chip-icon">📄</span><span class="chip-name">${att.filename}</span>`;
    chip.addEventListener("click", () => {
      openViewer({
        sha256: att.sha256,
        ref: att.ref,
        filename: att.filename,
        onAskPage: (p) => setCitation(att.ref, att.filename, p),
      });
    });
    attachedTray.appendChild(chip);
  }
}

function renderMessages(messages) {
  messageList.innerHTML = "";
  if (!messages || !messages.length) {
    emptyState.hidden = false;
    messageList.appendChild(emptyState);
    return;
  }
  emptyState.hidden = true;

  for (const msg of messages) {
    if (msg.role === "user") {
      appendUserMessageBubble(msg.content);
      activeLog.addMessage("user", msg.content);
    } else if (msg.role === "assistant") {
      appendAssistantMessageBubble(msg.content);
      activeLog.addMessage("assistant", msg.content);
    }
  }
  messageList.scrollTop = messageList.scrollHeight;
  updateLogDrawer();
}

function appendUserMessageBubble(text) {
  const div = document.createElement("div");
  div.className = "msg-row msg-user";
  div.innerHTML = `<div class="msg-bubble user-bubble">${renderMarkdown(text)}</div>`;
  messageList.appendChild(div);
  messageList.scrollTop = messageList.scrollHeight;
}

function appendAssistantMessageBubble(text = "") {
  const div = document.createElement("div");
  div.className = "msg-row msg-assistant";
  div.innerHTML = `<div class="msg-bubble assistant-bubble">${renderMarkdown(text)}</div>`;
  messageList.appendChild(div);
  messageList.scrollTop = messageList.scrollHeight;
  return div.querySelector(".assistant-bubble");
}

// --- Send Message Loop with SSE ----------------------------------------------
async function sendMessage() {
  const text = promptInput.value.trim();
  if ((!text && !pendingAttachments.length) || isStreaming) return;

  if (!currentThreadId) {
    await createNewThread(text.slice(0, 40) || "Lesson");
  }

  emptyState.hidden = true;
  promptInput.value = "";
  promptInput.style.height = "auto";
  isStreaming = true;
  sendBtn.disabled = true;

  let messageToSend = text;
  if (activeCitation) {
    messageToSend = `[Regarding ${activeCitation.filename}, p. ${activeCitation.page}]:\n${text}`;
    clearCitation();
  }

  appendUserMessageBubble(messageToSend);
  activeLog.addMessage("user", messageToSend);

  const assistantBubble = appendAssistantMessageBubble();
  let assistantText = "";

  const payload = {
    thread_id: currentThreadId,
    message: messageToSend,
    attached_refs: pendingAttachments.map((a) => a.ref),
  };
  clearPendingAttachments();

  try {
    const res = await fetch("/api/chat/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    if (!res.ok) {
      throw new Error(`Chat error: HTTP ${res.status}`);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      const lines = buffer.split("\n\n");
      buffer = lines.pop(); // keep remainder

      for (const block of lines) {
        if (!block.trim()) continue;
        const eventMatch = block.match(/event:\s*([^\n]+)/);
        const dataMatch = block.match(/data:\s*([\s\S]+)/);
        if (!eventMatch || !dataMatch) continue;

        const eventType = eventMatch[1].trim();
        let eventData = {};
        try {
          eventData = JSON.parse(dataMatch[1].trim());
        } catch (e) {
          continue;
        }

        handleSSEEvent(eventType, eventData, assistantBubble, () => {
          assistantBubble.innerHTML = renderMarkdown(assistantText);
          messageList.scrollTop = messageList.scrollHeight;
        });
      }
    }
  } catch (err) {
    showToast(err.message, "error");
    assistantBubble.innerHTML += `<div class="callout callout-failure"><div class="callout-title">Connection Error</div><div class="callout-body">${err.message}</div></div>`;
  } finally {
    isStreaming = false;
    sendBtn.disabled = false;
    if (assistantText) {
      activeLog.addMessage("assistant", assistantText);
      updateLogDrawer();
    }
  }
}

function handleSSEEvent(type, data, bubble, updateBubble) {
  if (type === "token") {
    bubble.innerHTML = renderMarkdown((bubble.dataset.rawText = (bubble.dataset.rawText || "") + data.content));
    messageList.scrollTop = messageList.scrollHeight;
  } else if (type === "card") {
    renderCardInChat(data);
  } else if (type === "file_attached") {
    renderAttachedFiles([data]);
    showToast(`Attached ${data.filename}`, "info");
  } else if (type === "viz_render_request") {
    handleVizRenderRequest(data);
  } else if (type === "tool_start") {
    showToolIndicator(data.tool_name);
  } else if (type === "tool_done") {
    removeToolIndicator();
  } else if (type === "error") {
    showToast(data.error || "Execution error", "error");
  }
}

function showToolIndicator(name) {
  removeToolIndicator();
  const ind = document.createElement("div");
  ind.id = "active-tool-ind";
  ind.className = "tool-indicator";
  ind.innerHTML = `<span class="spinner"></span> Running ${name}...`;
  messageList.appendChild(ind);
  messageList.scrollTop = messageList.scrollHeight;
}

function removeToolIndicator() {
  const el = $("active-tool-ind");
  if (el) el.remove();
}

function renderCardInChat(cardData) {
  removeToolIndicator();
  const cardContainer = document.createElement("div");
  cardContainer.className = "chat-card-mount";

  if (cardData.card_type === "quiz") {
    const el = renderQuizCard(cardData, async (reply) => {
      try {
        const res = await fetch("/api/chat/card-reply", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            thread_id: currentThreadId,
            card_id: cardData.call_id,
            reply: reply,
          }),
        });
        const resData = await res.json();
        const grade = resData.result ? resData.result.grade : null;
        if (grade) {
          activeLog.addQuiz(cardData.question, reply.selected, grade.correct, grade.dontKnow, grade.explanation);
          updateLogDrawer();
        }
        return grade;
      } catch (e) {
        showToast("Failed to submit check response", "error");
        return null;
      }
    });
    cardContainer.appendChild(el);
  } else if (cardData.card_type === "ask") {
    const el = renderAskCard(cardData, async (reply) => {
      try {
        await fetch("/api/chat/card-reply", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            thread_id: currentThreadId,
            card_id: cardData.call_id,
            reply: reply,
          }),
        });
        showToast("Answer recorded", "success");
      } catch (e) {
        showToast("Failed to record response", "error");
      }
    });
    cardContainer.appendChild(el);
  } else if (cardData.card_type === "write_confirm") {
    const el = renderWriteConfirmCard(cardData, async (confirmed) => {
      try {
        const res = await fetch("/api/chat/card-reply", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            thread_id: currentThreadId,
            card_id: cardData.token_id,
            reply: { confirm: confirmed },
          }),
        });
        if (confirmed) showToast("LMS action sent successfully!", "success");
      } catch (e) {
        showToast("LMS action failed", "error");
      }
    });
    cardContainer.appendChild(el);
  }

  messageList.appendChild(cardContainer);
  messageList.scrollTop = messageList.scrollHeight;
}

// --- Browser Visual Verification Loop ----------------------------------------
async function handleVizRenderRequest(data) {
  const { call_id, kind, source, slug } = data;
  showToolIndicator(`verifying ${kind} diagram`);

  try {
    const result = await validateAndRasterize(call_id, kind, source);
    // Post feedback to server
    const res = await fetch("/api/chat/viz-feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        call_id: call_id,
        feedback: {
          ok: result.ok,
          dom_warnings: result.warnings || [],
          png_data_url: result.pngDataUrl,
          error: result.error,
        },
      }),
    });
    const reply = await res.json();
    removeToolIndicator();

    if (reply.status === "published") {
      const cardMount = document.createElement("div");
      cardMount.className = "chat-card-mount";
      const vizCard = renderVisualCard(
        {
          visual_id: reply.visual_id,
          kind: reply.kind,
          slug: reply.slug,
          source_code: source,
          svg_data: source,
          png_path: reply.png_url,
        },
        () => {
          openViewer({
            sha256: reply.visual_id,
            filename: `${reply.slug}.png`,
            mime: "image/png",
          });
        }
      );
      cardMount.appendChild(vizCard);
      messageList.appendChild(cardMount);
      messageList.scrollTop = messageList.scrollHeight;

      activeLog.addVisual(reply.slug, reply.kind, reply.png_url);
      updateLogDrawer();
    } else if (reply.status === "retry_render") {
      // Maker revised diagram source; re-run validation on revised source
      handleVizRenderRequest(reply);
    }
  } catch (err) {
    removeToolIndicator();
    showToast(`Visual validation error: ${err.message}`, "error");
  }
}

// --- Citations & File Attachments --------------------------------------------
function setCitation(ref, filename, page) {
  activeCitation = { ref, filename, page };
  citationBadge.hidden = false;
  citationLabel.textContent = `${filename} · p. ${page}`;
  promptInput.focus();
}

function clearCitation() {
  activeCitation = null;
  citationBadge.hidden = true;
  citationLabel.textContent = "";
}

citationBadge.addEventListener("click", clearCitation);

attachBtn.addEventListener("click", () => fileInput.click());

fileInput.addEventListener("change", async () => {
  const files = Array.from(fileInput.files);
  for (const f of files) {
    const form = new FormData();
    form.append("file", f);
    try {
      showToast(`Uploading ${f.name}...`, "info");
      const res = await fetch("/api/upload", { method: "POST", body: form });
      if (res.ok) {
        const json = await res.json();
        pendingAttachments.push({
          ref: json.ref || `upload://${json.sha256}`,
          filename: f.name,
          sha256: json.sha256,
        });
        renderPendingAttachments();
      }
    } catch (e) {
      showToast(`Upload failed: ${f.name}`, "error");
    }
  }
  fileInput.value = "";
});

function renderPendingAttachments() {
  composerAttachments.innerHTML = "";
  for (const att of pendingAttachments) {
    const chip = document.createElement("span");
    chip.className = "file-chip pending";
    chip.innerHTML = `<span class="chip-name">${att.filename}</span><button class="chip-rm" type="button">×</button>`;
    chip.querySelector(".chip-rm").addEventListener("click", () => {
      pendingAttachments = pendingAttachments.filter((a) => a !== att);
      renderPendingAttachments();
    });
    composerAttachments.appendChild(chip);
  }
}

function clearPendingAttachments() {
  pendingAttachments = [];
  composerAttachments.innerHTML = "";
}

// --- Lesson Log Drawer -------------------------------------------------------
function toggleLogDrawer(open) {
  const isOpen = open ?? !logDrawer.classList.contains("open");
  if (isOpen) {
    logDrawer.classList.add("open");
    logDrawer.removeAttribute("inert");
    logDrawer.setAttribute("aria-hidden", "false");
    logToggle.setAttribute("aria-expanded", "true");
    updateLogDrawer();
  } else {
    logDrawer.classList.remove("open");
    logDrawer.setAttribute("inert", "");
    logDrawer.setAttribute("aria-hidden", "true");
    logToggle.setAttribute("aria-expanded", "false");
  }
}

function updateLogDrawer() {
  const md = activeLog.toMarkdown();
  if (!activeLog.entries.length) {
    logEmpty.hidden = false;
    logPreview.innerHTML = "";
    logSource.textContent = "";
  } else {
    logEmpty.hidden = true;
    logPreview.innerHTML = renderMarkdown(md);
    logSource.textContent = md;
  }
}

logToggle.addEventListener("click", () => toggleLogDrawer());
logClose.addEventListener("click", () => toggleLogDrawer(false));

logViewRendered.addEventListener("click", () => {
  logViewRendered.classList.add("on");
  logViewRaw.classList.remove("on");
  logPreview.hidden = false;
  logSource.hidden = true;
});

logViewRaw.addEventListener("click", () => {
  logViewRaw.classList.add("on");
  logViewRendered.classList.remove("on");
  logPreview.hidden = true;
  logSource.hidden = false;
});

copyLogBtn.addEventListener("click", async () => {
  await activeLog.copyToClipboard();
  showToast("Obsidian Markdown copied to clipboard!", "success");
});

downloadLogBtn.addEventListener("click", () => {
  activeLog.download();
  showToast("Downloading lesson log...", "info");
});

// --- Settings Modal ----------------------------------------------------------
settingsBtn.addEventListener("click", async () => {
  try {
    const res = await fetch("/api/settings");
    if (res.ok) {
      const data = await res.json();
      const cfg = data.config || {};
      $("setting-base-url").value = cfg.base_url || "";
      $("setting-teacher-model").value = cfg.teacher_model || "";
      $("setting-maker-model").value = cfg.maker_model || "";
      $("setting-researcher-model").value = cfg.researcher_model || "";
    }
  } catch (e) {}
  settingsStatus.hidden = true;
  settingsModal.showModal();
});

settingsCancel.addEventListener("click", () => settingsModal.close());

settingsForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {
    base_url: $("setting-base-url").value.trim(),
    api_key: $("setting-api-key").value.trim() || undefined,
    teacher_model: $("setting-teacher-model").value.trim(),
    maker_model: $("setting-maker-model").value.trim(),
    researcher_model: $("setting-researcher-model").value.trim(),
  };

  try {
    const res = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (res.ok) {
      settingsStatus.textContent = "Settings saved successfully.";
      settingsStatus.className = "settings-status success";
      settingsStatus.hidden = false;
      setTimeout(() => settingsModal.close(), 800);
      showToast("Model settings updated", "success");
    }
  } catch (err) {
    settingsStatus.textContent = "Failed to save settings.";
    settingsStatus.className = "settings-status error";
    settingsStatus.hidden = false;
  }
});

// --- Form & Keyboard Shortcuts -----------------------------------------------
composerForm.addEventListener("submit", (e) => {
  e.preventDefault();
  sendMessage();
});

promptInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});

// Auto-grow textarea
promptInput.addEventListener("input", () => {
  promptInput.style.height = "auto";
  promptInput.style.height = Math.min(promptInput.scrollHeight, 180) + "px";
});

// Global Citation Link click handler (delegated from messageList)
messageList.addEventListener("click", (e) => {
  const chip = e.target.closest("[data-viewer-ref]");
  if (chip) {
    const ref = chip.dataset.viewerRef;
    openViewer({
      ref: ref,
      filename: chip.querySelector(".chip-text")?.textContent || "Document",
      onAskPage: (p) => setCitation(ref, "Document", p),
    });
  }
});

// --- Startup Init ------------------------------------------------------------
window.addEventListener("DOMContentLoaded", async () => {
  await initVizEngine();
  await checkAuth();
});
