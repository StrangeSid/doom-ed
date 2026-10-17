// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * Interactive chat cards for the doom-ed pedagogy loop.
 *
 * The orchestrator pauses its turn on three interactive cards (SSE `card` event):
 *   - quiz          → renderQuizCard(cardData, onReply)          → onReply({ selected: [...] })
 *   - ask           → renderAskCard(cardData, onReply)           → onReply({ answer: ... })
 *   - write_confirm → renderWriteConfirmCard(cardData, onConfirm) → onConfirm(true | false)
 *
 * Plus the published visual bubble:
 *   - renderVisualCard(vizData, onOpenViewer)
 *
 * Each function returns a detached card element for the chat stream to append.
 * `onReply` / `onConfirm` may return a promise (typically the
 * `POST /api/chat/card-reply` result); when it does, the card waits for it and
 * renders the graded outcome itself. Callers that fire-and-forget can instead use
 * the returned element's `applyGrade(grade)`, `markAnswered(answer)` and
 * `markError(message)` helpers.
 *
 * Server reply for a quiz resolves to roughly:
 *   { status: 'ok', result: { status: 'ok', card_type: 'quiz',
 *     grade: { correct, dontKnow, correct_options: [indices], explanation } } }
 */

import { renderMermaidToElement, svgToPngDataUrl } from './viz-engine.js';

const CARDS_CSS_ID = 'doomed-cards-css';
const DONT_KNOW_VALUE = '__dont_know__';

// ---------------------------------------------------------------------------
// Shared helpers
// ---------------------------------------------------------------------------

function h(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = String(text);
  return node;
}

function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function isThenable(value) {
  return value != null && typeof value.then === 'function';
}

function errorText(err) {
  return (err && (err.message || err.detail)) || String(err || 'Something went wrong.');
}

// Card questions/explanations can carry $LaTeX$ and Markdown. Paint escaped text
// immediately, then upgrade in place through renderer.js when it is available.
let rendererPromise = null;
function setRichText(node, text) {
  const source = String(text ?? '');
  node.innerHTML = escapeHtml(source).replace(/\n/g, '<br>');
  if (!source) return;
  if (rendererPromise === null) {
    rendererPromise = import('./renderer.js')
      .then((mod) => (mod && typeof mod.renderMarkdown === 'function' ? mod.renderMarkdown : null))
      .catch(() => null);
  }
  rendererPromise.then((renderMarkdown) => {
    if (!renderMarkdown || !node.isConnected) return;
    try {
      node.innerHTML = renderMarkdown(source);
    } catch (e) {
      /* keep the escaped text */
    }
  });
}

function ensureCardsCss() {
  if (document.getElementById(CARDS_CSS_ID)) return;
  const style = document.createElement('style');
  style.id = CARDS_CSS_ID;
  style.textContent = `
.dcard{display:flex;flex-direction:column;gap:10px;max-width:660px;padding:14px 16px;border:1px solid var(--line,#E1E8E7);border-radius:14px;background:var(--surface,#fff);color:var(--ink,#14232B);font-size:14px;line-height:1.45;box-shadow:var(--shadow,0 1px 3px rgba(13,58,53,.08))}
.dcard-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.dcard-chip{font-size:10.5px;font-weight:800;letter-spacing:.05em;text-transform:uppercase;color:var(--brand-ink,#0B5F59);background:var(--brand-soft,#DDF0EC);padding:3px 9px;border-radius:999px}
.dcard-chip.ask{color:var(--info,#3B6FD8);background:var(--info-soft,#E5EDFB)}
.dcard-chip.warn{color:var(--accent-ink,#94600F);background:var(--accent-soft,#FCEFD8)}
.dcard-chip.danger{color:var(--late-ink,#A6352F);background:var(--late-soft,#FBE5E3)}
.dcard-q{font-weight:650;font-size:15px}
.dcard-sub{font-size:12.5px;color:var(--muted,#7D8B93);font-weight:600}
.dcard-opts{display:flex;flex-direction:column;gap:8px}
.dcard-opt{display:flex;gap:10px;align-items:flex-start;width:100%;padding:9px 12px;border:1px solid var(--line-2,#D3DEDC);border-radius:10px;background:var(--surface,#fff);color:inherit;font:inherit;text-align:left;cursor:pointer}
.dcard-opt:hover{border-color:var(--brand,#12807A)}
.dcard-opt:focus-visible{outline:2px solid var(--brand,#12807A);outline-offset:2px}
.dcard-opt[disabled]{cursor:default}
.dcard-opt.is-chosen{border-color:var(--brand,#12807A);background:var(--brand-soft,#DDF0EC)}
.dcard-opt.is-correct{border-color:var(--done,#2F8A5B);background:var(--done-soft,#E0F2E8)}
.dcard-opt.is-wrong{border-color:var(--late,#D2524B);background:var(--late-soft,#FBE5E3)}
.dcard-opt-mark{flex:none;width:16px;height:16px;margin-top:2px;border:2px solid var(--line-2,#D3DEDC);border-radius:50%;background:#fff}
.dcard-opt.multi .dcard-opt-mark{border-radius:5px}
.dcard-opt.is-chosen .dcard-opt-mark{border-color:var(--brand,#12807A);background:var(--brand,#12807A)}
.dcard-opt.is-correct .dcard-opt-mark{border-color:var(--done,#2F8A5B);background:var(--done,#2F8A5B)}
.dcard-opt.is-wrong .dcard-opt-mark{border-color:var(--late,#D2524B);background:var(--late,#D2524B)}
.dcard-opt-label{font-weight:650}
.dcard-opt-desc{display:block;font-size:12.5px;color:var(--muted,#7D8B93);font-weight:600;margin-top:1px}
.dcard-grade{display:flex;flex-direction:column;gap:6px;padding:10px 12px;border:1px solid var(--line,#E1E8E7);border-radius:10px;background:var(--canvas,#EEF3F2)}
.dcard-grade.ok{border-color:var(--done,#2F8A5B);background:var(--done-soft,#E0F2E8)}
.dcard-grade.bad{border-color:var(--late,#D2524B);background:var(--late-soft,#FBE5E3)}
.dcard-grade.dk{border-color:var(--accent,#E8A33D);background:var(--accent-soft,#FCEFD8)}
.dcard-grade-title{font-weight:800}
.dcard-grade.ok .dcard-grade-title{color:var(--done-ink,#21704A)}
.dcard-grade.bad .dcard-grade-title{color:var(--late-ink,#A6352F)}
.dcard-grade.dk .dcard-grade-title{color:var(--accent-ink,#94600F)}
.dcard-expl{font-size:13.5px}
.dcard-expl :first-child{margin-top:0}
.dcard-expl :last-child{margin-bottom:0}
.dcard-status{margin:0;font-size:12.5px;font-weight:650;color:var(--muted,#7D8B93)}
.dcard-status.err{color:var(--late-ink,#A6352F)}
.dcard-status.ok{color:var(--done-ink,#21704A)}
.dcard-actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.dcard-btn{display:inline-flex;align-items:center;gap:6px;padding:8px 14px;border:1px solid transparent;border-radius:9px;background:var(--brand,#12807A);color:#fff;font:inherit;font-weight:750;cursor:pointer}
.dcard-btn:hover{background:var(--brand-ink,#0B5F59)}
.dcard-btn.ghost{background:var(--surface,#fff);border-color:var(--line-2,#D3DEDC);color:var(--ink-2,#44535C)}
.dcard-btn.ghost:hover{border-color:var(--brand,#12807A);color:var(--brand-ink,#0B5F59)}
.dcard-btn[disabled]{opacity:.55;cursor:default}
.dcard-other{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.dcard-input,.dcard-textarea{flex:1;min-width:180px;padding:8px 11px;border:1px solid var(--line-2,#D3DEDC);border-radius:9px;font:inherit;color:inherit;background:var(--surface,#fff)}
.dcard-textarea{min-height:74px;resize:vertical}
.dcard-input:focus,.dcard-textarea:focus{outline:2px solid var(--brand,#12807A);outline-offset:1px}
.dcard-payload{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;margin:0;padding:10px 12px;border:1px solid var(--line,#E1E8E7);border-radius:10px;background:var(--canvas,#EEF3F2);font-size:13px}
.dcard-payload dt{font-weight:750;color:var(--ink-2,#44535C);white-space:nowrap}
.dcard-payload dd{margin:0;overflow-wrap:anywhere}
.dcard-raw{margin:0;font-size:12px;color:var(--muted,#7D8B93)}
.dcard-raw pre{margin:6px 0 0;padding:8px 10px;border:1px solid var(--line,#E1E8E7);border-radius:8px;background:var(--surface,#fff);overflow:auto;max-height:180px}
.dcard-frame{border:1px solid var(--line,#E1E8E7);border-radius:12px;background:var(--canvas,#EEF3F2);overflow:hidden}
.dcard-frame-stage{position:relative;height:320px;overflow:hidden;cursor:grab;touch-action:none}
.dcard-frame-stage.dragging{cursor:grabbing}
.dcard-frame-inner{position:absolute;left:0;top:0;transform-origin:0 0;will-change:transform}
.dcard-frame-inner svg{display:block;max-width:none}
.dcard-frame-tools{display:flex;gap:6px;align-items:center;padding:7px 10px;border-top:1px solid var(--line,#E1E8E7);background:var(--surface,#fff)}
.dcard-frame-tools button{width:30px;height:30px;border:1px solid var(--line-2,#D3DEDC);border-radius:8px;background:var(--surface,#fff);color:var(--ink-2,#44535C);font:inherit;font-weight:800;cursor:pointer}
.dcard-frame-tools button.wide{width:auto;padding:0 10px;font-size:12.5px}
.dcard-frame-tools button:hover{border-color:var(--brand,#12807A);color:var(--brand-ink,#0B5F59)}
.dcard-zv{min-width:44px;text-align:center;font-size:12px;font-weight:800;color:var(--muted,#7D8B93)}
.dcard-note{margin:0;font-size:12.5px;font-weight:650;color:var(--accent-ink,#94600F)}
@media (prefers-reduced-motion:no-preference){.dcard-frame-inner{transition:transform .12s ease-out}}
`;
  document.head.appendChild(style);
}

function cardRoot(type, cardData, extraClass) {
  ensureCardsCss();
  const card = h('div', `dcard dcard-${type}${extraClass ? ` ${extraClass}` : ''}`);
  card.dataset.cardType = type;
  if (cardData && (cardData.call_id || cardData.token_id)) {
    card.dataset.cardId = String(cardData.call_id || cardData.token_id);
  }
  return card;
}

function headRow(chipText, chipClass, extra) {
  const head = h('div', 'dcard-head');
  const chip = h('span', `dcard-chip${chipClass ? ` ${chipClass}` : ''}`, chipText);
  head.appendChild(chip);
  if (extra) head.appendChild(extra);
  return head;
}

function dispatchCardEvent(card, name, detail) {
  try {
    card.dispatchEvent(new CustomEvent(name, { detail, bubbles: true }));
  } catch (e) {
    /* CustomEvent unsupported: logging only */
  }
}

function setStatus(card, text, kind) {
  let status = card.querySelector(':scope > .dcard-status');
  if (!status) {
    status = h('p', 'dcard-status');
    status.setAttribute('role', 'status');
    card.appendChild(status);
  }
  status.textContent = text || '';
  status.classList.toggle('err', kind === 'err');
  status.classList.toggle('ok', kind === 'ok');
  status.hidden = !text;
}

// ---------------------------------------------------------------------------
// Quiz card
// ---------------------------------------------------------------------------

function normalizeQuizOptions(cardData) {
  const raw = Array.isArray(cardData && cardData.options) ? cardData.options : [];
  return raw.map((option, i) => ({
    index: Number.isFinite(option.index) ? option.index : i + 1,
    label: String(option.label ?? option.text ?? option.value ?? `Option ${i + 1}`),
    value: option.value ?? option.label ?? `option-${i + 1}`,
    description: option.description || '',
  }));
}

function normalizeGrade(grade) {
  if (!grade || typeof grade !== 'object') return null;
  const dontKnow = Boolean(grade.dontKnow ?? grade.dont_know ?? grade.dontknow);
  const correct = Boolean(grade.correct ?? grade.isCorrect ?? grade.is_correct);
  const rawCorrect = grade.correct_options ?? grade.correct_indices ?? grade.correctOptions ?? [];
  const correctOptions = (Array.isArray(rawCorrect) ? rawCorrect : [rawCorrect])
    .map((n) => Number(n))
    .filter((n) => Number.isFinite(n));
  return { dontKnow, correct, correctOptions, explanation: grade.explanation || '' };
}

/** Dig a grade object out of `{grade}`, `{result:{grade}}`, or `{result:{result:{grade}}}`. */
function extractGrade(result) {
  if (!result || typeof result !== 'object') return null;
  const candidates = [
    result,
    result.grade,
    result.result,
    result.result && result.result.grade,
    result.result && result.result.result,
    result.result && result.result.result && result.result.result.grade,
  ];
  for (const candidate of candidates) {
    if (candidate && typeof candidate === 'object' && ('correct' in candidate || 'dontKnow' in candidate || 'dont_know' in candidate)) {
      return normalizeGrade(candidate);
    }
  }
  return null;
}

/**
 * Calibrated check card. The "I don't know" option is pinned first (display index 0,
 * value `__dont_know__`) and is never shuffled; server-shuffled options keep their
 * server-assigned `index` for every `onReply({ selected: [...] })` payload.
 */
export function renderQuizCard(cardData, onReply) {
  const data = cardData || {};
  const card = cardRoot('quiz', data);
  const multi = Boolean(data.multi_select);
  const options = normalizeQuizOptions(data);

  card.appendChild(headRow(multi ? 'Calibrated Check · Multi-select' : 'Calibrated Check', ''));

  const question = h('div', 'dcard-q');
  setRichText(question, data.question || 'Check your understanding.');
  card.appendChild(question);

  const list = h('div', 'dcard-opts');
  card.appendChild(list);

  const buttons = [];
  const selected = new Set();

  const buildOption = (option, isDontKnow) => {
    const button = h('button', `dcard-opt${multi && !isDontKnow ? ' multi' : ''}`);
    button.type = 'button';
    // Quiz replies are graded against the server-held shuffled indices, so the
    // selected value is the option index (or the pinned don't-know sentinel).
    button.dataset.value = String(isDontKnow ? DONT_KNOW_VALUE : option.index);
    button.dataset.index = String(isDontKnow ? 0 : option.index);
    button.appendChild(h('span', 'dcard-opt-mark'));

    const body = h('span', 'dcard-opt-body');
    const label = h('span', 'dcard-opt-label');
    setRichText(label, isDontKnow ? "I don't know" : option.label);
    body.appendChild(label);
    if (!isDontKnow && option.description) body.appendChild(h('span', 'dcard-opt-desc', option.description));
    button.appendChild(body);

    button.addEventListener('click', () => {
      if (card.dataset.state !== 'open') return;
      if (isDontKnow || !multi) {
        submitSelection([isDontKnow ? DONT_KNOW_VALUE : option.index], [button]);
        return;
      }
      const value = option.index;
      if (selected.has(value)) {
        selected.delete(value);
        button.classList.remove('is-chosen');
      } else {
        selected.add(value);
        button.classList.add('is-chosen');
      }
      submitBtn.disabled = selected.size === 0;
      submitBtn.textContent = selected.size > 1 ? `Submit ${selected.size} answers` : 'Submit answer';
    });

    buttons.push(button);
    return button;
  };

  buildOption({ index: 0, label: "I don't know", value: DONT_KNOW_VALUE }, true).classList.add('dcard-opt-dk');
  for (const option of options) buildOption(option, false);

  // Rebuild in pinned order: don't-know first, then the server-shuffled options.
  buttons.forEach((button) => list.appendChild(button));

  let submitBtn = null;
  if (multi) {
    submitBtn = h('button', 'dcard-btn', 'Submit answer');
    submitBtn.type = 'button';
    submitBtn.disabled = true;
    card.appendChild(submitBtn);
    submitBtn.addEventListener('click', () => {
      if (selected.size && card.dataset.state === 'open') {
        submitSelection(
          [...selected],
          buttons.filter((button) => selected.has(Number(button.dataset.value)))
        );
      }
    });
  }

  card.dataset.state = data.answered ? 'answered' : 'open';
  if (data.answered) lockControls('Answer recorded.');

  function lockControls(statusText) {
    card.dataset.state = card.dataset.state === 'open' ? 'locked' : card.dataset.state;
    for (const button of buttons) button.disabled = true;
    if (submitBtn) submitBtn.disabled = true;
    if (statusText) setStatus(card, statusText);
  }

  function fail(err) {
    card.dataset.state = 'open';
    for (const button of buttons) button.disabled = false;
    if (submitBtn) submitBtn.disabled = selected.size === 0;
    setStatus(card, `Could not record that answer: ${errorText(err)}`, 'err');
    dispatchCardEvent(card, 'doomed:card-error', { cardType: 'quiz', cardId: card.dataset.cardId, error: errorText(err) });
  }

  function submitSelection(values, clickedButtons) {
    if (!onReply) {
      console.warn('[cards] renderQuizCard called without an onReply handler.');
      return;
    }
    card.dataset.state = 'locked';
    for (const button of buttons) button.disabled = true;
    if (submitBtn) submitBtn.disabled = true;
    for (const button of clickedButtons || []) button.classList.add('is-chosen');
    setStatus(card, 'Checking…');
    dispatchCardEvent(card, 'doomed:card-reply', {
      cardType: 'quiz',
      cardId: card.dataset.cardId,
      reply: { selected: values },
    });

    let outcome;
    try {
      outcome = onReply({ selected: values });
    } catch (err) {
      fail(err);
      return;
    }
    if (!isThenable(outcome)) {
      setStatus(card, 'Answer recorded.');
      return;
    }
    outcome.then(
      (res) => {
        const grade = extractGrade(res);
        if (grade) applyGrade(grade);
        else setStatus(card, 'Answer recorded.');
      },
      (err) => fail(err)
    );
  }

  /** Render the server grade: banner + explanation + per-option highlighting. */
  function applyGrade(rawGrade) {
    const grade = normalizeGrade(rawGrade);
    if (!grade) return;
    card.dataset.state = 'graded';
    for (const button of buttons) button.disabled = true;
    if (submitBtn) submitBtn.disabled = true;
    setStatus(card, '');

    const correctSet = new Set(grade.correctOptions);
    for (const button of buttons) {
      const index = Number(button.dataset.index);
      const value = button.dataset.value;
      const isDontKnow = value === DONT_KNOW_VALUE;
      const chosen = button.classList.contains('is-chosen');
      if (!isDontKnow && correctSet.has(index)) button.classList.add('is-correct');
      else if (chosen && !isDontKnow) button.classList.add('is-wrong');
    }

    const banner = h('div', `dcard-grade ${grade.dontKnow ? 'dk' : grade.correct ? 'ok' : 'bad'}`);
    const title = grade.dontKnow ? '— I don’t know' : grade.correct ? '✓ Correct' : '✗ Incorrect';
    banner.appendChild(h('div', 'dcard-grade-title', title));
    if (grade.explanation) {
      const explanation = h('div', 'dcard-expl');
      setRichText(explanation, grade.explanation);
      banner.appendChild(explanation);
    }
    card.appendChild(banner);

    dispatchCardEvent(card, 'doomed:quiz-graded', {
      cardId: card.dataset.cardId,
      question: data.question || '',
      grade,
    });
  }

  card.applyGrade = applyGrade;
  card.markError = fail;
  return card;
}

// ---------------------------------------------------------------------------
// Ask card (ungraded)
// ---------------------------------------------------------------------------

function normalizeAskOptions(cardData) {
  const raw = Array.isArray(cardData && cardData.options) ? cardData.options : [];
  return raw.map((option, i) => {
    if (typeof option === 'string') return { label: option, value: option, description: '' };
    return {
      label: String(option.label ?? option.text ?? option.value ?? `Option ${i + 1}`),
      value: option.value ?? option.label ?? `option-${i + 1}`,
      description: option.description || '',
    };
  });
}

/**
 * Ungraded question card: goal-setting, plan approval, or a Socratic fork.
 * Modes: 'single-select' (click submits), 'multi-select' (toggle + submit), 'text'.
 * An "Other (type your own)" inline input is always appended to the select modes.
 */
export function renderAskCard(cardData, onReply) {
  const data = cardData || {};
  const card = cardRoot('ask', data);
  const mode = ['multi-select', 'text'].includes(data.mode) ? data.mode : 'single-select';
  const multi = mode === 'multi-select';
  const options = normalizeAskOptions(data);

  card.appendChild(headRow('Question', 'ask'));

  if (data.context) {
    const context = h('div', 'dcard-sub');
    setRichText(context, data.context);
    card.appendChild(context);
  }

  const question = h('div', 'dcard-q');
  setRichText(question, data.question || 'Your input, please.');
  card.appendChild(question);

  const answerBox = h('div', 'dcard-answer');
  card.appendChild(answerBox);

  const buttons = [];
  const selected = new Set();
  let submitted = false;

  function settle(answer, labelText) {
    submitted = true;
    card.dataset.state = 'answered';
    for (const button of buttons) button.disabled = true;
    if (submitBtn) submitBtn.disabled = true;
    if (otherToggle) otherToggle.disabled = true;
    if (otherInput) otherInput.disabled = true;
    if (otherSend) otherSend.disabled = true;
    if (textArea) textArea.disabled = true;
    if (textSend) textSend.disabled = true;
    let display = labelText ?? (Array.isArray(answer) ? answer.join(', ') : answer);
    display = String(display ?? '').trim();
    const chip = h('p', 'dcard-sub', display ? `Your answer: ${display}` : 'This question was already answered.');
    answerBox.replaceChildren(chip);
    setStatus(card, '');
  }

  function fail(err) {
    submitted = false;
    card.dataset.state = 'open';
    for (const button of buttons) button.disabled = false;
    if (submitBtn) submitBtn.disabled = selected.size === 0;
    if (otherToggle) otherToggle.disabled = false;
    if (otherInput) otherInput.disabled = false;
    if (otherSend) otherSend.disabled = false;
    if (textArea) textArea.disabled = false;
    if (textSend) textSend.disabled = false;
    setStatus(card, `Could not send that answer: ${errorText(err)}`, 'err');
    dispatchCardEvent(card, 'doomed:card-error', { cardType: 'ask', cardId: card.dataset.cardId, error: errorText(err) });
  }

  function submit(answer, labelText) {
    if (submitted) return;
    if (!onReply) {
      console.warn('[cards] renderAskCard called without an onReply handler.');
      return;
    }
    submitted = true;
    for (const button of buttons) button.disabled = true;
    if (submitBtn) submitBtn.disabled = true;
    setStatus(card, 'Sending…');
    dispatchCardEvent(card, 'doomed:card-reply', {
      cardType: 'ask',
      cardId: card.dataset.cardId,
      reply: { answer },
    });

    let outcome;
    try {
      outcome = onReply({ answer });
    } catch (err) {
      fail(err);
      return;
    }
    if (!isThenable(outcome)) {
      settle(answer, labelText);
      return;
    }
    outcome.then(
      () => settle(answer, labelText),
      (err) => fail(err)
    );
  }

  let submitBtn = null;
  let otherToggle = null;
  let otherRow = null;
  let otherInput = null;
  let otherSend = null;
  let textArea = null;
  let textSend = null;

  if (mode === 'text') {
    textArea = h('textarea', 'dcard-textarea');
    textArea.placeholder = 'Type your answer…';
    textArea.rows = 3;
    textSend = h('button', 'dcard-btn', 'Send answer');
    textSend.type = 'button';
    const wrap = h('div', 'dcard-other');
    wrap.appendChild(textArea);
    wrap.appendChild(textSend);
    card.appendChild(wrap);
    textSend.addEventListener('click', () => {
      const value = textArea.value.trim();
      if (value) submit(value, value);
    });
    textArea.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        textSend.click();
      }
    });
  } else {
    const list = h('div', 'dcard-opts');
    for (const option of options) {
      const button = h('button', `dcard-opt${multi ? ' multi' : ''}`);
      button.type = 'button';
      button.dataset.value = String(option.value);
      button.appendChild(h('span', 'dcard-opt-mark'));
      const body = h('span', 'dcard-opt-body');
      const label = h('span', 'dcard-opt-label');
      setRichText(label, option.label);
      body.appendChild(label);
      if (option.description) body.appendChild(h('span', 'dcard-opt-desc', option.description));
      button.appendChild(body);
      button.addEventListener('click', () => {
        if (submitted) return;
        if (!multi) {
          button.classList.add('is-chosen');
          submit(String(option.value), option.label);
          return;
        }
        const value = String(option.value);
        if (selected.has(value)) {
          selected.delete(value);
          button.classList.remove('is-chosen');
        } else {
          selected.add(value);
          button.classList.add('is-chosen');
        }
        submitBtn.disabled = selected.size === 0;
        submitBtn.textContent = selected.size > 1 ? `Submit ${selected.size} choices` : 'Submit choice';
      });
      buttons.push(button);
      list.appendChild(button);
    }
    card.appendChild(list);
    if (multi) {
      submitBtn = h('button', 'dcard-btn', 'Submit choice');
      submitBtn.type = 'button';
      submitBtn.disabled = true;
      card.appendChild(submitBtn);
      submitBtn.addEventListener('click', () => {
        if (selected.size && !submitted) submit([...selected], [...selected].join(', '));
      });
    }
  }

  if (mode !== 'text') {
    otherToggle = h('button', 'dcard-btn ghost', 'Other (type your own)');
    otherToggle.type = 'button';
    card.appendChild(otherToggle);

    otherRow = h('div', 'dcard-other');
    otherRow.hidden = true;
    otherInput = h('input', 'dcard-input');
    otherInput.type = 'text';
    otherInput.placeholder = 'Type your own answer…';
    otherSend = h('button', 'dcard-btn', 'Send');
    otherSend.type = 'button';
    otherRow.appendChild(otherInput);
    otherRow.appendChild(otherSend);
    card.appendChild(otherRow);

    otherToggle.addEventListener('click', () => {
      if (submitted) return;
      otherRow.hidden = false;
      otherToggle.hidden = true;
      otherInput.focus();
    });
    otherSend.addEventListener('click', () => {
      const value = otherInput.value.trim();
      if (value) submit(value, value);
    });
    otherInput.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        otherSend.click();
      }
    });
  }

  card.dataset.state = data.answered ? 'answered' : 'open';
  if (data.answered) settle('', '');

  card.markAnswered = settle;
  card.markError = fail;
  return card;
}

// ---------------------------------------------------------------------------
// Write confirmation card
// ---------------------------------------------------------------------------

const WRITE_ACTION_LABELS = {
  submit: 'Submit assignment',
  resubmit: 'Resubmit assignment',
  request_extension: 'Request deadline extension',
  send_message: 'Send message',
  mark_read: 'Mark notification as read',
};

function humanizeAction(action) {
  const key = String(action || '').trim();
  return WRITE_ACTION_LABELS[key] || key.replace(/[_-]+/g, ' ').replace(/^\w/, (c) => c.toUpperCase()) || 'LMS action';
}

function summarizeValue(value) {
  if (value == null) return '—';
  if (typeof value === 'string') return value.length > 220 ? `${value.slice(0, 217)}…` : value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  try {
    const json = JSON.stringify(value);
    return json.length > 220 ? `${json.slice(0, 217)}…` : json;
  } catch (e) {
    return String(value);
  }
}

/**
 * Ask-first write gate. `cardData` = { token_id, action, payload, reason, expires_at }.
 * `onConfirm(true)` confirms and sends; `onConfirm(false)` cancels. The one-time
 * token executes at most once, so buttons never re-enable after a decision.
 */
export function renderWriteConfirmCard(cardData, onConfirm) {
  const data = cardData || {};
  const card = cardRoot('write', data);
  card.appendChild(headRow('Confirm LMS Write', 'danger'));

  const action = h('div', 'dcard-q', humanizeAction(data.action));
  card.appendChild(action);

  if (data.reason) {
    const reason = h('div', 'dcard-sub');
    setRichText(reason, data.reason);
    card.appendChild(reason);
  }

  const payload = data.payload && typeof data.payload === 'object' ? data.payload : {};
  const entries = Object.entries(payload);
  if (entries.length) {
    const dl = h('dl', 'dcard-payload');
    for (const [key, value] of entries) {
      dl.appendChild(h('dt', null, key.replace(/[_-]+/g, ' ')));
      dl.appendChild(h('dd', null, summarizeValue(value)));
    }
    card.appendChild(dl);

    const raw = h('details', 'dcard-raw');
    raw.appendChild(h('summary', null, 'Raw payload'));
    const pre = h('pre');
    try {
      pre.textContent = JSON.stringify(payload, null, 2);
    } catch (e) {
      pre.textContent = String(payload);
    }
    raw.appendChild(pre);
    card.appendChild(raw);
  }

  const expiry = h('p', 'dcard-sub');
  let expiryTimer = null;
  const expiresAt = Number(data.expires_at);

  function expired() {
    return Number.isFinite(expiresAt) && expiresAt > 0 && Date.now() / 1000 > (expiresAt > 1e12 ? expiresAt / 1000 : expiresAt);
  }

  function tick() {
    if (expiryTimer && !card.isConnected) {
      clearInterval(expiryTimer);
      expiryTimer = null;
    }
    if (expired()) {
      expiry.textContent = 'This confirmation has expired. Ask the coach to re-issue it.';
      if (card.dataset.state === 'open') {
        confirmBtn.disabled = true;
        cancelBtn.disabled = true;
        setStatus(card, 'Expired — no write was sent.', 'err');
      }
      if (expiryTimer) clearInterval(expiryTimer);
      return;
    }
    if (!Number.isFinite(expiresAt) || expiresAt <= 0) return;
    const secondsLeft = Math.max(0, Math.round((expiresAt > 1e12 ? expiresAt / 1000 : expiresAt) - Date.now() / 1000));
    const mm = String(Math.floor(secondsLeft / 60)).padStart(2, '0');
    const ss = String(secondsLeft % 60).padStart(2, '0');
    expiry.textContent = `One-time confirmation · expires in ${mm}:${ss}`;
  }
  card.appendChild(expiry);

  const actions = h('div', 'dcard-actions');
  const confirmBtn = h('button', 'dcard-btn', 'Confirm & Send to Indus LMS');
  confirmBtn.type = 'button';
  confirmBtn.disabled = expired();
  const cancelBtn = h('button', 'dcard-btn ghost', 'Cancel');
  cancelBtn.type = 'button';
  actions.appendChild(confirmBtn);
  actions.appendChild(cancelBtn);
  card.appendChild(actions);
  card.dataset.state = 'open';

  // Start the countdown only after the buttons exist (tick() disables them on expiry).
  if (Number.isFinite(expiresAt) && expiresAt > 0) {
    expiryTimer = setInterval(tick, 1000);
    card.addEventListener('DOMNodeRemoved', () => expiryTimer && clearInterval(expiryTimer));
  }
  tick();

  function decide(confirm) {
    if (card.dataset.state !== 'open' || !onConfirm) {
      if (!onConfirm) console.warn('[cards] renderWriteConfirmCard called without an onConfirm handler.');
      return;
    }
    card.dataset.state = confirm ? 'confirming' : 'cancelled';
    confirmBtn.disabled = true;
    cancelBtn.disabled = true;
    if (expiryTimer) clearInterval(expiryTimer);
    dispatchCardEvent(card, 'doomed:card-reply', {
      cardType: 'write_confirm',
      cardId: card.dataset.cardId,
      reply: { confirm },
    });

    if (!confirm) {
      setStatus(card, 'Cancelled — nothing was sent to Indus LMS.');
      return;
    }

    setStatus(card, 'Sending to Indus LMS…');
    let outcome;
    try {
      outcome = onConfirm(true);
    } catch (err) {
      card.dataset.state = 'error';
      setStatus(card, `The write failed: ${errorText(err)}. Nothing was retried.`, 'err');
      return;
    }
    const done = () => {
      card.dataset.state = 'sent';
      setStatus(card, 'Sent to Indus LMS ✓');
      dispatchCardEvent(card, 'doomed:write-confirmed', { cardId: card.dataset.cardId });
    };
    const fail = (err) => {
      card.dataset.state = 'error';
      setStatus(card, `The write failed: ${errorText(err)}. Nothing was retried.`, 'err');
      dispatchCardEvent(card, 'doomed:card-error', { cardType: 'write_confirm', cardId: card.dataset.cardId, error: errorText(err) });
    };
    if (isThenable(outcome)) outcome.then(done, fail);
    else done();
  }

  confirmBtn.addEventListener('click', () => decide(true));
  cancelBtn.addEventListener('click', () => decide(false));

  card.confirm = () => decide(true);
  card.cancel = () => decide(false);
  return card;
}

// ---------------------------------------------------------------------------
// Published visual card
// ---------------------------------------------------------------------------

function svgViewBoxSize(svg) {
  const parts = (svg.getAttribute('viewBox') || '').trim().split(/[\s,]+/).map(Number);
  if (parts.length === 4 && parts[2] > 0 && parts[3] > 0) return { width: parts[2], height: parts[3] };
  // Percentage widths measure 0 in SVG geometry: fall through to getBoundingClientRect.
  const pixel = (name) => {
    const raw = svg.getAttribute(name) || '';
    return raw.includes('%') ? NaN : parseFloat(raw);
  };
  const width = pixel('width');
  const height = pixel('height');
  if (Number.isFinite(width) && Number.isFinite(height) && width > 0 && height > 0) return { width, height };
  const rect = svg.getBoundingClientRect();
  return { width: rect.width || 600, height: rect.height || 400 };
}

function sanitizeSvgElement(svg) {
  const drop = svg.querySelectorAll('script, iframe, object, embed, audio, video, foreignObject script');
  drop.forEach((node) => node.remove());
  const walk = (node) => {
    if (node.nodeType !== 1) return;
    for (const attr of [...node.attributes]) {
      const name = attr.name.toLowerCase();
      if (name.startsWith('on')) node.removeAttribute(attr.name);
      else if ((name === 'href' || name === 'xlink:href') && /^\s*(javascript:|data:text\/html)/i.test(attr.value)) {
        node.removeAttribute(attr.name);
      }
    }
    [...node.children].forEach(walk);
  };
  walk(svg);
  return svg;
}

function svgMarkupToElement(markup) {
  let clean = String(markup || '');
  if (window.DOMPurify && typeof window.DOMPurify.sanitize === 'function') {
    try {
      clean = window.DOMPurify.sanitize(clean, { USE_PROFILES: { svg: true, svgFilters: true }, ADD_TAGS: ['style'] });
    } catch (e) {
      /* fall back to the manual scrub below */
    }
  }
  const doc = new DOMParser().parseFromString(clean, 'image/svg+xml');
  if (doc.querySelector('parsererror')) throw new Error('SVG source could not be parsed.');
  const root = doc.documentElement;
  if (!root || root.localName.toLowerCase() !== 'svg') throw new Error('Visual source is not an SVG document.');
  return sanitizeSvgElement(document.importNode(root, true));
}

function triggerBlobDownload(blob, filename) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
}

async function downloadFromUrl(url, filename) {
  try {
    const res = await fetch(url, { credentials: 'same-origin' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    triggerBlobDownload(await res.blob(), filename);
  } catch (e) {
    window.open(url, '_blank', 'noopener');
  }
}

function safeSlug(slug, fallback = 'diagram') {
  return String(slug || fallback).replace(/[^a-z0-9_-]+/gi, '-').replace(/^-+|-+$/g, '') || fallback;
}

/**
 * Published visual bubble: renders the diagram inside a pan/zoom frame with
 * "Open in Viewer", "Download SVG" and "Download PNG" actions.
 * `vizData` accepts { kind, slug, source | source_code | svg_data, png_url | pngUrl,
 * download_url, visual_id, verified, dom_warnings }.
 */
export function renderVisualCard(vizData, onOpenViewer) {
  const data = vizData || {};
  const card = cardRoot('visual', data);
  const kind = String(data.kind || 'mermaid').toLowerCase() === 'svg' ? 'svg' : 'mermaid';
  const source = data.source || data.source_code || data.svg_data || data.svg || '';
  const slug = safeSlug(data.slug);
  const pngUrl = data.png_url || data.pngUrl || null;

  const head = headRow(`Visual · ${kind === 'svg' ? 'SVG' : 'Mermaid'}`, '', h('span', 'dcard-sub', slug));
  card.appendChild(head);

  const frame = h('div', 'dcard-frame');
  const stage = h('div', 'dcard-frame-stage');
  const inner = h('div', 'dcard-frame-inner');
  stage.appendChild(inner);
  frame.appendChild(stage);

  const tools = h('div', 'dcard-frame-tools');
  const zoomOut = h('button', null, '−');
  const zoomLabel = h('span', 'dcard-zv', '100%');
  const zoomIn = h('button', null, '+');
  const fitBtn = h('button', 'wide', 'Fit');
  const resetBtn = h('button', 'wide', 'Reset');
  zoomOut.type = zoomIn.type = fitBtn.type = resetBtn.type = 'button';
  zoomOut.title = 'Zoom out';
  zoomIn.title = 'Zoom in';
  tools.append(zoomOut, zoomLabel, zoomIn, fitBtn, resetBtn);
  frame.appendChild(tools);
  card.appendChild(frame);

  const actions = h('div', 'dcard-actions');
  const openBtn = h('button', 'dcard-btn', 'Open in Viewer');
  openBtn.type = 'button';
  const svgBtn = h('button', 'dcard-btn ghost', 'Download SVG');
  svgBtn.type = 'button';
  const pngBtn = h('button', 'dcard-btn ghost', 'Download PNG');
  pngBtn.type = 'button';
  actions.append(openBtn, svgBtn, pngBtn);
  card.appendChild(actions);

  const warnings = Array.isArray(data.dom_warnings) ? data.dom_warnings : [];
  if (data.verified === 0 || warnings.length) {
    card.appendChild(
      h('p', 'dcard-note', warnings[0] ? `Published with a layout note: ${warnings[0]}` : 'Published without full layout verification.')
    );
  }

  const view = { z: 1, tx: 0, ty: 0, width: 1, height: 1 };
  let svgElement = null;

  function applyTransform() {
    inner.style.transform = `translate(${view.tx}px, ${view.ty}px) scale(${view.z})`;
    zoomLabel.textContent = `${Math.round(view.z * 100)}%`;
  }

  function fit(allowUpscale = false) {
    const stageRect = stage.getBoundingClientRect();
    const pad = 20;
    const availableW = Math.max(60, stageRect.width - pad);
    const availableH = Math.max(60, stageRect.height - pad);
    let z = Math.min(availableW / view.width, availableH / view.height);
    z = allowUpscale ? Math.min(z, 3) : Math.min(z, 1);
    view.z = Math.max(0.05, z);
    view.tx = (stageRect.width - view.width * view.z) / 2;
    view.ty = (stageRect.height - view.height * view.z) / 2;
    applyTransform();
  }

  function zoomBy(factor, cx, cy, stageRect) {
    const rect = stageRect || stage.getBoundingClientRect();
    const px = cx ?? rect.width / 2;
    const py = cy ?? rect.height / 2;
    const next = Math.min(8, Math.max(0.05, view.z * factor));
    view.tx = px - ((px - view.tx) * next) / view.z;
    view.ty = py - ((py - view.ty) * next) / view.z;
    view.z = next;
    applyTransform();
  }

  zoomIn.addEventListener('click', () => zoomBy(1.25));
  zoomOut.addEventListener('click', () => zoomBy(0.8));
  fitBtn.addEventListener('click', () => fit(true));
  resetBtn.addEventListener('click', () => fit(false));

  // Pan with drag; pinch-zoom and ctrl/meta+wheel zoom inside the frame only, so
  // the surrounding chat keeps normal wheel scrolling.
  const pointers = new Map();
  let pinch = null;
  stage.addEventListener('pointerdown', (event) => {
    if (event.button != null && event.button !== 0) return;
    stage.setPointerCapture(event.pointerId);
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    pinch = null;
    stage.classList.add('dragging');
  });
  stage.addEventListener('pointermove', (event) => {
    const previous = pointers.get(event.pointerId);
    if (!previous) return;
    const current = { x: event.clientX, y: event.clientY };
    pointers.set(event.pointerId, current);
    if (pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      const distance = Math.hypot(a.x - b.x, a.y - b.y);
      if (!pinch) pinch = { distance, z: view.z };
      if (pinch.distance > 0) {
        const rect = stage.getBoundingClientRect();
        zoomBy((pinch.z * (distance / pinch.distance)) / view.z, (a.x + b.x) / 2 - rect.left, (a.y + b.y) / 2 - rect.top, rect);
      }
      return;
    }
    view.tx += current.x - previous.x;
    view.ty += current.y - previous.y;
    applyTransform();
  });
  const endPointer = (event) => {
    pointers.delete(event.pointerId);
    pinch = null;
    if (!pointers.size) stage.classList.remove('dragging');
  };
  ['pointerup', 'pointercancel', 'lostpointercapture'].forEach((type) => stage.addEventListener(type, endPointer));
  stage.addEventListener(
    'wheel',
    (event) => {
      if (!event.ctrlKey && !event.metaKey) return;
      event.preventDefault();
      const rect = stage.getBoundingClientRect();
      zoomBy(event.deltaY < 0 ? 1.12 : 0.89, event.clientX - rect.left, event.clientY - rect.top, rect);
    },
    { passive: false }
  );

  function serializeCurrentSvg() {
    if (!svgElement) return null;
    try {
      const clone = svgElement.cloneNode(true);
      if (!clone.getAttribute('xmlns')) clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
      return new XMLSerializer().serializeToString(clone);
    } catch (e) {
      return null;
    }
  }

  openBtn.addEventListener('click', () => {
    if (typeof onOpenViewer === 'function') {
      onOpenViewer(data);
      return;
    }
    if (pngUrl) {
      window.open(pngUrl, '_blank', 'noopener');
      return;
    }
    const markup = serializeCurrentSvg();
    if (markup) window.open(URL.createObjectURL(new Blob([markup], { type: 'image/svg+xml' })), '_blank', 'noopener');
  });

  svgBtn.addEventListener('click', () => {
    const markup = serializeCurrentSvg();
    if (!markup) return;
    svgBtn.disabled = true;
    triggerBlobDownload(new Blob([markup], { type: 'image/svg+xml;charset=utf-8' }), `${slug}.svg`);
    svgBtn.disabled = false;
  });

  pngBtn.addEventListener('click', async () => {
    if (pngUrl) {
      pngBtn.disabled = true;
      try {
        await downloadFromUrl(pngUrl, `${slug}.png`);
      } finally {
        pngBtn.disabled = false;
      }
      return;
    }
    if (!svgElement) return;
    pngBtn.disabled = true;
    try {
      const dataUrl = await svgToPngDataUrl(svgElement, 2);
      const blob = await (await fetch(dataUrl)).blob();
      triggerBlobDownload(blob, `${slug}.png`);
    } catch (e) {
      setStatus(card, `PNG export failed: ${errorText(e)}`, 'err');
    } finally {
      pngBtn.disabled = false;
    }
  });

  let observer = null;
  const teardown = () => {
    if (observer) observer.disconnect();
  };

  (async () => {
    try {
      if (kind === 'mermaid') {
        if (!source) throw new Error('No Mermaid source was provided.');
        svgElement = await renderMermaidToElement(inner, source);
      } else {
        if (!source && pngUrl) throw new Error('__png_only__');
        svgElement = svgMarkupToElement(source || '');
        inner.appendChild(svgElement);
      }
      const size = svgViewBoxSize(svgElement);
      view.width = size.width;
      view.height = size.height;
      svgElement.setAttribute('width', String(size.width));
      svgElement.setAttribute('height', String(size.height));
      svgElement.style.maxWidth = 'none';
      inner.style.width = `${size.width}px`;
      inner.style.height = `${size.height}px`;
      fit(false);
      if (typeof ResizeObserver === 'function') {
        observer = new ResizeObserver(() => {
          const rect = stage.getBoundingClientRect();
          if (view.width * view.z > rect.width || view.height * view.z > rect.height) fit(false);
        });
        observer.observe(stage);
      }
      card.addEventListener('DOMNodeRemoved', teardown);
    } catch (err) {
      inner.replaceChildren();
      if (pngUrl) {
        const img = h('img');
        img.src = pngUrl;
        img.alt = `${slug} diagram`;
        img.style.maxWidth = '100%';
        img.style.margin = 'auto';
        img.style.display = 'block';
        inner.style.position = 'static';
        inner.appendChild(img);
        frame.classList.add('dcard-frame-fallback');
        setStatus(card, 'Live render unavailable — showing the verified PNG raster.', 'err');
      } else {
        setStatus(card, `Visual could not be rendered: ${errorText(err)}`, 'err');
      }
    }
  })();

  card.markError = (message) => setStatus(card, message, 'err');
  return card;
}

// Non-module escape hatch for inline handlers and legacy wiring.
if (typeof window !== 'undefined') {
  window.doomedCards = { renderQuizCard, renderAskCard, renderWriteConfirmCard, renderVisualCard };
}
