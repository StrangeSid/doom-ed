// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * Rich markdown, KaTeX math, Obsidian callouts, and citation links renderer.
 */

export function renderMarkdown(content) {
  if (!content) return "";

  // 1. Pre-process Obsidian Callouts: > [!type] Title
  let processed = content.replace(
    /(?:^|\n)> \[!([a-zA-Z0-9_-]+)\][ \t]*([^\n]*)\n((?:>.*\n?)*)/g,
    (match, type, title, bodyLines) => {
      const calloutType = type.toLowerCase();
      const calloutTitle = title.trim() || type.toUpperCase();
      const cleanBody = bodyLines
        .split("\n")
        .map((line) => line.replace(/^>[ \t]?/, ""))
        .join("\n");
      return `\n<div class="callout callout-${calloutType}">\n<div class="callout-title"><span class="callout-icon"></span>${calloutTitle}</div>\n<div class="callout-body">\n\n${cleanBody}\n\n</div>\n</div>\n`;
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
    (match, href, text) => {
      return `<button class="citation-chip" data-viewer-ref="${href}" type="button"><span class="chip-icon">📄</span><span class="chip-text">${text}</span></button>`;
    }
  );

  // 5. Sanitize HTML via DOMPurify
  if (window.DOMPurify && window.DOMPurify.sanitize) {
    html = window.DOMPurify.sanitize(html, {
      ADD_TAGS: ["button"],
      ADD_ATTR: ["data-viewer-ref", "type"],
    });
  }

  return html;
}
