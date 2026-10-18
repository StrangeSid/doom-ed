// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * Rich markdown, KaTeX math, Obsidian callouts, and citation links renderer.
 */

// Inline SVG glyphs (stroke = currentColor so CSS decides the colour).
function svg(inner) {
  return (
    '<svg class="callout-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor"' +
    ' stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    inner +
    "</svg>"
  );
}

const CALLOUT_ICONS = {
  question: svg('<circle cx="12" cy="12" r="9"/><path d="M9.6 9.4a2.5 2.5 0 0 1 4.9.6c0 1.7-2.5 2.1-2.5 3.6"/><path d="M12 17.1h.01"/>'),
  note: svg('<circle cx="12" cy="12" r="9"/><path d="M12 11v5.4"/><path d="M12 7.7h.01"/>'),
  tip: svg(
    '<path d="M12 3.3l1.7 4.3a4 4 0 0 0 2.3 2.3l4.3 1.7-4.3 1.7a4 4 0 0 0-2.3 2.3L12 20l-1.7-4.4a4 4 0 0 0-2.3-2.3L3.7 11.6l4.3-1.7a4 4 0 0 0 2.3-2.3z"/>' +
      '<path d="M18.6 3.6v2.2M17.5 4.7h2.2"/>'
  ),
  success: svg('<circle cx="12" cy="12" r="9"/><path d="m8.3 12.4 2.6 2.6 4.8-5.4"/>'),
  failure: svg('<circle cx="12" cy="12" r="9"/><path d="m9.1 9.1 5.8 5.8M14.9 9.1l-5.8 5.8"/>'),
  warning: svg('<path d="M10.3 4.2 2.9 17.1A2 2 0 0 0 4.6 20h14.8a2 2 0 0 0 1.7-2.9L13.7 4.2a2 2 0 0 0-3.4 0Z"/><path d="M12 9.3v3.9"/><path d="M12 16.9h.01"/>'),
};

// Obsidian callout aliases → canonical icon/style key.
const CALLOUT_ALIASES = {
  question: "question",
  help: "question",
  faq: "question",
  note: "note",
  info: "note",
  todo: "note",
  abstract: "note",
  summary: "note",
  tldr: "note",
  tip: "tip",
  hint: "tip",
  important: "tip",
  success: "success",
  check: "success",
  done: "success",
  failure: "failure",
  fail: "failure",
  error: "failure",
  danger: "failure",
  bug: "failure",
  warning: "warning",
  warn: "warning",
  caution: "warning",
  attention: "warning",
};

function calloutKind(type) {
  const key = String(type || "").toLowerCase();
  return CALLOUT_ALIASES[key] || "note";
}

// Clean document glyph for citation chips (replaces the raw 📄 emoji).
const CHIP_ICON =
  '<svg class="chip-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
  '<path d="M14 3H7.6A2.6 2.6 0 0 0 5 5.6v12.8A2.6 2.6 0 0 0 7.6 21h8.8a2.6 2.6 0 0 0 2.6-2.6V8z"/>' +
  '<path d="M14 3v5h5"/></svg>';

function escapeAttr(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}

export function renderMarkdown(content) {
  if (!content) return "";

  // 1. Pre-process Obsidian Callouts: > [!type] Title
  let processed = content.replace(
    /(?:^|\n)> \[!([a-zA-Z0-9_-]+)\][ \t]*([^\n]*)\n((?:>.*\n?)*)/g,
    (match, type, title, bodyLines) => {
      const kind = calloutKind(type);
      const calloutTitle = title.trim() || kind.charAt(0).toUpperCase() + kind.slice(1);
      const cleanBody = bodyLines
        .split("\n")
        .map((line) => line.replace(/^>[ \t]?/, ""))
        .join("\n");
      return `\n<div class="callout callout-${kind}">\n<div class="callout-title"><span class="callout-icon">${CALLOUT_ICONS[kind]}</span><span class="callout-title-text">${calloutTitle}</span></div>\n<div class="callout-body">\n\n${cleanBody}\n\n</div>\n</div>\n`;
    }
  );

  // 2. Pre-process Math with KaTeX
  // Block math: $$ ... $$
  processed = processed.replace(/\$\$([\s\S]+?)\$\$/g, (match, expr) => {
    try {
      if (window.katex) {
        return `<div class="katex-block">${window.katex.renderToString(expr.trim(), { displayMode: true, throwOnError: false })}</div>`;
      }
    } catch (e) {
      console.warn("KaTeX block error:", e);
    }
    return match;
  });

  // Inline math: $ ... $ (avoiding currency $50)
  processed = processed.replace(/(?<!\\)\$([^\$\n]+?)(?<!\\)\$/g, (match, expr) => {
    // Basic heuristic: must not start or end with a space
    if (expr.startsWith(" ") || expr.endsWith(" ") || /^\d+(\.\d+)?$/.test(expr)) {
      return match;
    }
    try {
      if (window.katex) {
        return window.katex.renderToString(expr.trim(), { displayMode: false, throwOnError: false });
      }
    } catch (e) {
      console.warn("KaTeX inline error:", e);
    }
    return match;
  });

  // 3. Marked HTML conversion
  let html = "";
  if (window.marked && window.marked.parse) {
    html = window.marked.parse(processed, { breaks: true, gfm: true });
  } else {
    // Fallback if marked not ready
    html = processed.replace(/\n/g, "<br/>");
  }

  // 4. Citation and File badge conversion
  // Matches e.g. [Filename.pdf, p. 4](viewer:sha256?page=4) or [Filename.pdf](lms://...)
  html = html.replace(
    /<a href="(viewer:[^"]+|lms:[^"]+|ib:[^"]+)">([^<]+)<\/a>/g,
    (match, href, text) =>
      `<button class="citation-chip" data-viewer-ref="${escapeAttr(href)}" type="button">` +
      `<span class="chip-icon">${CHIP_ICON}</span>` +
      `<span class="chip-text">${text}</span>` +
      `</button>`
  );

  // 5. Sanitize HTML via DOMPurify (keeps inline SVG: DOMPurify allows svg by default)
  if (window.DOMPurify && window.DOMPurify.sanitize) {
    html = window.DOMPurify.sanitize(html, {
      ADD_TAGS: ["button"],
      ADD_ATTR: ["data-viewer-ref", "type"],
    });
  }

  return html;
}
