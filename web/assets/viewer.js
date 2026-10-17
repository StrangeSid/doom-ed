// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * doom-ed in-app slide-over file viewer.
 *
 * Adapted from openLMS-indus `viewer.js` for the doom-ed sha256-based blob API:
 *   openViewer({ sha256, ref, filename, mime, page = 1 })
 *
 * Reads every file through `GET /api/files/{sha256}` (owner-gated, Range-capable):
 *   - PDF   → PDF.js single-page canvas with prev/next, zoom, fit width, rotate,
 *             and a jump to the requested initial page,
 *   - Image/SVG → pan-zoom stage (drag, pinch, ctrl/⌘ + wheel),
 *   - Text/Markdown → formatted text (renderer.js when available),
 *   - anything else → a download card.
 *
 * The toolbar carries "Page X of Y", Zoom In/Out, Rotate, Download and an
 * "Ask about Page X" button. The ask button communicates back to the chat input by
 * dispatching a cancelable `doomed:ask-about-page` CustomEvent with
 * `{ sha256, ref, filename, mime, page, text }`. A listener may call
 * preventDefault() to claim it; otherwise `window.doomedAskAboutPage(detail)` is
 * tried, and finally the known chat composers are pre-filled as a fallback.
 */

const VIEWER_CSS_ID = 'doomed-viewer-css';
const TEXT_FETCH_LIMIT = 400_000;
const HEX64 = /^[a-f0-9]{64}$/i;

const TEXT_EXTS = ['txt', 'md', 'markdown', 'csv', 'json', 'js', 'ts', 'py', 'java', 'c', 'cpp', 'h', 'css', 'xml', 'log', 'yaml', 'yml'];
const IMAGE_EXTS = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'avif', 'bmp', 'ico', 'tif', 'tiff', 'svg'];

const assetsBase = new URL('.', import.meta.url);

let activeViewer = null;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function escHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function escAttr(value) {
  return escHtml(value).replace(/'/g, '&#39;');
}

function extOf(filename) {
  return String(filename || '').split('.').pop().toLowerCase();
}

function fileNameFromUrl(url) {
  try {
    return decodeURIComponent(new URL(url, location.href).pathname.split('/').pop() || 'File');
  } catch (e) {
    return String(url || 'File').split('?')[0].split('#')[0].split('/').pop() || 'File';
  }
}

function kindOf(mime, filename) {
  const type = String(mime || '').split(';')[0].trim().toLowerCase();
  const ext = extOf(filename);
  if (type === 'application/pdf' || ext === 'pdf') return 'pdf';
  if (type.startsWith('image/') || IMAGE_EXTS.includes(ext)) return 'image';
  if (type === 'text/markdown' || ext === 'md' || ext === 'markdown') return 'markdown';
  if (type.startsWith('text/') || type === 'application/json' || TEXT_EXTS.includes(ext)) return 'text';
  return 'unknown';
}

function fileUrlFor(sha256) {
  return `/api/files/${encodeURIComponent(sha256)}`;
}

function downloadUrlFor(src, explicit) {
  if (explicit) return explicit;
  if (!src) return null;
  try {
    const url = new URL(src, location.href);
    if (url.origin === location.origin && /\/api\/files\//.test(url.pathname)) {
      url.searchParams.set('download', '1');
      url.hash = '';
      return url.pathname + url.search;
    }
  } catch (e) {
    /* not a local URL */
  }
  return src;
}

/**
 * Parse a citation ref into viewer options.
 * Accepts `viewer:<sha256>[?page=N]`, `ib://<sha256>`, a bare sha256, or an
 * `/api/files/<sha256>` URL. Returns null for refs that carry no sha256.
 */
export function parseViewerRef(raw) {
  if (!raw) return null;
  let ref = String(raw).trim();
  let page = 1;
  const schemeMatch = ref.match(/^(?:viewer|file|files):(.+)$/i);
  if (schemeMatch) ref = schemeMatch[1];
  const [pathPart, queryPart] = ref.split('#')[0].split('?');
  if (queryPart) {
    const pageParam = new URLSearchParams(queryPart).get('page');
    if (pageParam) page = Math.max(1, parseInt(pageParam, 10) || 1);
  }
  if (HEX64.test(pathPart)) return { sha256: pathPart.toLowerCase(), page };
  const ibMatch = pathPart.match(/^ib:\/\/([a-f0-9]{64})$/i);
  if (ibMatch) return { sha256: ibMatch[1].toLowerCase(), page };
  const apiMatch = pathPart.match(/\/api\/files\/([a-f0-9]{64})/i);
  if (apiMatch) return { sha256: apiMatch[1].toLowerCase(), page };
  return null;
}

function ensureCss() {
  if (document.getElementById(VIEWER_CSS_ID)) return;
  const style = document.createElement('style');
  style.id = VIEWER_CSS_ID;
  style.textContent = `
.dv-backdrop{position:fixed;inset:0;background:rgba(13,35,32,.5);z-index:80;display:flex;justify-content:flex-end}
.dv-panel{width:min(780px,97vw);height:100%;display:flex;flex-direction:column;background:var(--surface,#fff);box-shadow:var(--shadow-lift,0 12px 32px rgba(0,0,0,.2));animation:dv-in .18s ease-out}
@keyframes dv-in{from{transform:translateX(24px);opacity:.6}to{transform:none;opacity:1}}
.dv-head{display:flex;gap:12px;align-items:center;padding:14px 18px 10px;border-bottom:1px solid var(--line,#E1E8E7)}
.dv-head .meta{flex:1;min-width:0}
.dv-head b{display:block;font-size:15px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dv-head small{color:var(--muted,#7D8B93);font-weight:600;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:block}
.dv-close{flex:none;width:34px;height:34px;border-radius:50%;border:1px solid var(--line,#E1E8E7);background:var(--surface,#fff);font-size:18px;line-height:1;cursor:pointer;color:var(--ink-2,#44535C)}
.dv-toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:8px 18px;border-bottom:1px solid var(--line,#E1E8E7)}
.dv-grp{display:inline-flex;gap:4px;align-items:center}
.dv-toolbar button,.dv-toolbar a.dv-tb{border:1px solid var(--line-2,#D3DEDC);background:var(--surface,#fff);border-radius:8px;padding:6px 10px;font:inherit;font-weight:700;font-size:12px;color:var(--ink-2,#44535C);text-decoration:none;cursor:pointer}
.dv-toolbar button:hover,.dv-toolbar a.dv-tb:hover{border-color:var(--brand,#12807A);color:var(--brand-ink,#0B5F59)}
.dv-toolbar button:disabled{opacity:.5;cursor:default}
.dv-toolbar .dv-ask{border-color:var(--brand,#12807A);color:var(--brand-ink,#0B5F59);background:var(--brand-soft,#DDF0EC)}
.dv-counter,.dv-zoomlabel{min-width:52px;text-align:center;font-size:12px;font-weight:800;color:var(--ink-2,#44535C)}
.dv-sp{flex:1}
.dv-stage{flex:1;overflow:auto;padding:16px;background:var(--canvas,#EEF3F2)}
.dv-stage.dv-lock{overflow:hidden;padding:0}
.dv-pdfwrap{display:flex;justify-content:center;align-items:flex-start}
.dv-pdfpage{background:#fff;border:1px solid var(--line,#E1E8E7);box-shadow:var(--shadow,0 1px 3px rgba(0,0,0,.15));line-height:0}
.dv-pdfpage canvas{display:block;background:#fff}
.dv-imgwrap{position:relative;overflow:hidden;width:100%;height:100%;touch-action:none;cursor:grab;background:var(--canvas,#EEF3F2)}
.dv-imgwrap.dragging{cursor:grabbing}
.dv-imginner{position:absolute;left:0;top:0;transform-origin:0 0;will-change:transform}
.dv-imginner img{position:absolute;left:50%;top:50%;display:block;transform-origin:center;max-width:none}
.dv-textwrap{padding:2px}
.dv-text,.dv-prose{background:var(--surface,#fff);border:1px solid var(--line,#E1E8E7);border-radius:12px;padding:18px 20px;font-size:13.5px;line-height:1.6;color:var(--ink,#14232B);overflow-wrap:anywhere}
.dv-text{white-space:pre-wrap;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.dv-prose h1,.dv-prose h2,.dv-prose h3,.dv-prose h4{margin:1em 0 .45em;line-height:1.25}
.dv-prose h1{font-size:1.45em}.dv-prose h2{font-size:1.28em}.dv-prose h3{font-size:1.14em}
.dv-prose p{margin:.6em 0}
.dv-prose ul,.dv-prose ol{margin:.6em 0;padding-left:1.4em}
.dv-prose code{background:var(--canvas,#EEF3F2);border-radius:5px;padding:1px 5px;font-size:.92em}
.dv-prose pre{background:var(--canvas,#EEF3F2);border-radius:9px;padding:12px;overflow:auto}
.dv-prose pre code{background:none;padding:0}
.dv-prose blockquote{margin:.6em 0;padding:.15em 0 .15em 12px;border-left:3px solid var(--brand,#12807A);color:var(--ink-2,#44535C)}
.dv-prose table{border-collapse:collapse;margin:.8em 0}
.dv-prose th,.dv-prose td{border:1px solid var(--line,#E1E8E7);padding:6px 10px;text-align:left}
.dv-prose img{max-width:100%}
.dv-loading{padding:26px;color:var(--muted,#7D8B93);font-weight:650}
.dv-fallback{display:flex;flex-direction:column;gap:12px;align-items:center;text-align:center;padding:40px 24px}
.dv-fallback p{margin:0;color:var(--muted,#7D8B93);font-weight:600}
.dv-fallback .row{display:flex;gap:10px;flex-wrap:wrap;justify-content:center}
.dv-fallback a.dv-btn{display:inline-block;padding:9px 16px;border-radius:9px;background:var(--brand,#12807A);color:#fff;font-weight:750;text-decoration:none}
.dv-fallback a.dv-btn.ghost{background:var(--surface,#fff);color:var(--ink-2,#44535C);border:1px solid var(--line-2,#D3DEDC)}
.dv-note{margin:10px 0 0;font-size:12px;font-weight:650;color:var(--muted,#7D8B93)}
@media (max-width:760px){.dv-backdrop{justify-content:stretch}.dv-panel{width:100%}.dv-head{padding:12px 14px 8px}.dv-toolbar{padding:8px 14px}.dv-stage{padding:12px}}
@media (prefers-reduced-motion:reduce){.dv-panel{animation:none}}
`;
  document.head.appendChild(style);
}

// ---------------------------------------------------------------------------
// Shell: backdrop, panel, focus handling, cleanup
// ---------------------------------------------------------------------------

function shell(opts) {
  ensureCss();
  if (activeViewer) activeViewer.close();

  const lastFocus = document.activeElement;
  const previousOverflow = document.body.style.overflow;
  const controller = new AbortController();

  const backdrop = document.createElement('div');
  backdrop.className = 'dv-backdrop';
  const title = opts.filename || 'File';
  backdrop.innerHTML = `
    <div class="dv-panel" role="dialog" aria-modal="true" aria-label="${escAttr(title)}">
      <div class="dv-head">
        <div class="meta"><b>${escHtml(title)}</b><small>${escHtml(opts.subtitle || '')}</small></div>
        <button class="dv-close" aria-label="Close viewer">×</button>
      </div>
      <div class="dv-toolbar" role="toolbar"></div>
      <div class="dv-stage"></div>
    </div>`;
  document.body.appendChild(backdrop);
  document.body.style.overflow = 'hidden';

  const background = [...document.body.children].filter((element) => element !== backdrop && !element.inert);
  background.forEach((element) => {
    element.inert = true;
  });

  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    controller.abort();
    backdrop.remove();
    background.forEach((element) => {
      if (element && element.isConnected) {
        try {
          element.inert = false;
        } catch (e) {
          /* ignore */
        }
      }
    });
    document.body.style.overflow = previousOverflow;
    activeViewer = null;
    document.removeEventListener('keydown', onKey, true);
    if (lastFocus && lastFocus.isConnected && lastFocus.focus) lastFocus.focus();
    try {
      document.dispatchEvent(new CustomEvent('doomed:viewer-close', { detail: { sha256: opts.sha256 || null } }));
    } catch (e) {
      /* ignore */
    }
  };

  const onKey = (event) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopImmediatePropagation();
      close();
    }
  };
  document.addEventListener('keydown', onKey, true);
  backdrop.querySelector('.dv-close').addEventListener('click', close);
  let mouseDownTarget = null;
  backdrop.addEventListener('mousedown', (event) => {
    mouseDownTarget = event.target;
  });
  backdrop.addEventListener('click', (event) => {
    const inPanel = event.target.closest && event.target.closest('.dv-panel');
    const downInPanel = mouseDownTarget && mouseDownTarget.closest && mouseDownTarget.closest('.dv-panel');
    if (!inPanel && !downInPanel) close();
  });
  backdrop.addEventListener('keydown', (event) => {
    if (event.key !== 'Tab') return;
    const focusables = [...backdrop.querySelectorAll('button, a[href], input, select, [tabindex]')].filter((el) => !el.disabled);
    if (!focusables.length) return;
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });

  const viewer = {
    backdrop,
    panel: backdrop.querySelector('.dv-panel'),
    toolbar: backdrop.querySelector('.dv-toolbar'),
    stage: backdrop.querySelector('.dv-stage'),
    close,
    signal: controller.signal,
    opts,
  };
  activeViewer = viewer;
  backdrop.querySelector('.dv-close').focus();
  return viewer;
}

function toolbarBase(viewer, toolsHtml) {
  const download = viewer.opts.downloadUrl
    ? `<a class="dv-tb" href="${escAttr(viewer.opts.downloadUrl)}" download="${escAttr(viewer.opts.filename || 'file')}">Download</a>`
    : '';
  viewer.toolbar.innerHTML = `${toolsHtml || ''}<span class="dv-sp"></span>${download}`;
}

function pgmTools(page, total, zoom, rotation) {
  return `<span class="dv-grp"><button data-nav="prev" ${page <= 1 ? 'disabled' : ''} aria-label="Previous page">‹</button><span class="dv-counter" data-counter>Page ${page} of ${total}</span><button data-nav="next" ${page >= total ? 'disabled' : ''} aria-label="Next page">›</button></span>
    <span class="dv-grp"><button data-zoom="out" aria-label="Zoom out">−</button><span class="dv-zoomlabel" data-zoomlabel>${Math.round(zoom)}%</span><button data-zoom="in" aria-label="Zoom in">+</button><button data-zoom="width">Fit width</button><button data-rot="${rotation || 0}" aria-label="Rotate">Rotate</button></span>`;
}

function askButtonHtml() {
  return `<button class="dv-ask" data-ask>Ask about this file</button>`;
}

function setAskLabel(viewer, page, isPaged) {
  const button = viewer.toolbar.querySelector('[data-ask]');
  if (button) button.textContent = isPaged ? `Ask about Page ${page}` : 'Ask about this file';
}

// ---------------------------------------------------------------------------
// Chat round-trip for "Ask about Page X"
// ---------------------------------------------------------------------------

function askAboutPage(viewer, page, text) {
  const detail = {
    sha256: viewer.opts.sha256 || null,
    ref: viewer.opts.ref || null,
    filename: viewer.opts.filename || 'File',
    mime: viewer.opts.mime || null,
    page,
    text: text ? String(text).slice(0, 1500) : '',
  };
  let handled = false;
  try {
    handled = !document.dispatchEvent(new CustomEvent('doomed:ask-about-page', { detail, cancelable: true, bubbles: true }));
  } catch (e) {
    handled = false;
  }
  if (!handled && typeof window.doomedAskAboutPage === 'function') {
    try {
      handled = window.doomedAskAboutPage(detail) !== false;
    } catch (e) {
      handled = false;
    }
  }
  if (handled) return true;
  // Fallback: pre-fill whichever chat composer the app rendered.
  const selectors = ['#chat-input', '#chatInput', '#prompt-input', '#message-input', 'textarea[data-chat-input]', '.chat-input textarea', 'textarea.composer'];
  const input = selectors.map((selector) => document.querySelector(selector)).find(Boolean);
  if (input && (input.tagName === 'TEXTAREA' || input.tagName === 'INPUT')) {
    input.value = `About page ${page} of "${detail.filename}": `;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.focus();
    return true;
  }
  console.info('[viewer] Ask about page requested; no chat input handler found.', detail);
  return false;
}

// ---------------------------------------------------------------------------
// PDF viewer (PDF.js, single page at a time)
// ---------------------------------------------------------------------------

let pdfjsPromise = null;
function loadPdfJs() {
  if (!pdfjsPromise) {
    pdfjsPromise = (async () => {
      const candidates = ['vendor/pdfjs/build/pdf.min.mjs', 'vendor/pdf.min.mjs'];
      let lastError = null;
      for (const relative of candidates) {
        try {
          const moduleUrl = new URL(relative, assetsBase).href;
          const lib = await import(/* @vite-ignore */ moduleUrl);
          const workerPath = relative.replace(/pdf(\.min)?\.mjs$/i, (match, min) => `pdf.worker${min || ''}.mjs`);
          lib.GlobalWorkerOptions.workerSrc = new URL(workerPath, assetsBase).href;
          return lib;
        } catch (err) {
          lastError = err;
        }
      }
      throw lastError || new Error('PDF.js could not be loaded.');
    })().catch((err) => {
      pdfjsPromise = null;
      throw err;
    });
  }
  return pdfjsPromise;
}

async function openPdf(viewer) {
  const { opts, stage } = viewer;
  toolbarBase(viewer, pgmTools(opts.page, opts.page, 100, 0) + askButtonHtml());
  stage.innerHTML = '<div class="dv-loading">Loading document…</div>';

  let pdf;
  try {
    const lib = await loadPdfJs();
    if (viewer.signal.aborted) return;
    const url = opts.src;
    const loadingTask = lib.getDocument({
      url,
      isEvalSupported: false,
      cMapUrl: new URL('vendor/pdfjs/cmaps/', assetsBase).href,
      cMapPacked: true,
      standardFontDataUrl: new URL('vendor/pdfjs/standard_fonts/', assetsBase).href,
      wasmUrl: new URL('vendor/pdfjs/wasm/', assetsBase).href,
    });
    viewer.signal.addEventListener('abort', () => loadingTask.destroy().catch(() => {}), { once: true });
    pdf = await loadingTask.promise;
  } catch (err) {
    if (!viewer.signal.aborted) fallback(viewer, 'This PDF could not be rendered. Download it to open in your PDF app.');
    return;
  }
  if (viewer.signal.aborted) return;

  const state = {
    page: Math.min(Math.max(1, opts.page | 0 || 1), pdf.numPages),
    zoom: 100,
    fitWidth: true,
    rotation: 0,
  };
  const pageTextCache = new Map();
  let generation = 0;
  let renderTask = null;
  let resizeRaf = 0;

  stage.innerHTML = '<div class="dv-pdfwrap"><div class="dv-pdfpage"><canvas role="img"></canvas></div></div>';
  const wrap = stage.querySelector('.dv-pdfwrap');
  const pageElement = stage.querySelector('.dv-pdfpage');
  const canvas = stage.querySelector('canvas');

  function updateToolbar() {
    const counter = viewer.toolbar.querySelector('[data-counter]');
    if (counter) counter.textContent = `Page ${state.page} of ${pdf.numPages}`;
    viewer.toolbar.querySelector('[data-nav="prev"]').disabled = state.page <= 1;
    viewer.toolbar.querySelector('[data-nav="next"]').disabled = state.page >= pdf.numPages;
    const zoomLabel = viewer.toolbar.querySelector('[data-zoomlabel]');
    if (zoomLabel) zoomLabel.textContent = `${Math.round(state.zoom)}%`;
    setAskLabel(viewer, state.page, true);
  }

  async function pageText(pageNumber) {
    if (pageTextCache.has(pageNumber)) return pageTextCache.get(pageNumber);
    try {
      const page = await pdf.getPage(pageNumber);
      const content = await page.getTextContent();
      const text = content.items.map((item) => item.str + (item.hasEOL ? '\n' : ' ')).join('');
      pageTextCache.set(pageNumber, text);
      return text;
    } catch (e) {
      pageTextCache.set(pageNumber, '');
      return '';
    }
  }

  async function renderCurrent() {
    const current = ++generation;
    if (renderTask) {
      try {
        renderTask.cancel();
      } catch (e) {
        /* already finished */
      }
      renderTask = null;
    }
    const page = await pdf.getPage(state.page);
    if (viewer.signal.aborted || current !== generation) return;

    const rotation = ((page.rotate || 0) + state.rotation) % 360;
    const baseViewport = page.getViewport({ scale: 1, rotation });
    if (state.fitWidth) {
      const available = Math.max(120, wrap.clientWidth - 8);
      state.zoom = Math.max(10, Math.min(400, (available / baseViewport.width) * 100));
    }
    const viewport = page.getViewport({ scale: state.zoom / 100, rotation });
    const ratio = Math.min(window.devicePixelRatio || 1, 2, Math.sqrt(16e6 / Math.max(1, viewport.width * viewport.height)));
    canvas.width = Math.max(1, Math.round(viewport.width * ratio));
    canvas.height = Math.max(1, Math.round(viewport.height * ratio));
    canvas.style.width = `${Math.round(viewport.width)}px`;
    canvas.style.height = `${Math.round(viewport.height)}px`;
    canvas.setAttribute('aria-label', `${opts.filename || 'Document'}, page ${state.page} of ${pdf.numPages}`);
    pageElement.style.width = `${Math.round(viewport.width)}px`;
    pageElement.style.height = `${Math.round(viewport.height)}px`;

    renderTask = page.render({
      canvasContext: canvas.getContext('2d'),
      viewport,
      transform: [ratio, 0, 0, ratio, 0, 0],
    });
    try {
      await renderTask.promise;
    } catch (err) {
      if (err && err.name === 'RenderingCancelledException') return;
      if (!viewer.signal.aborted) fallback(viewer, 'This page could not be rendered. Download the file to open it elsewhere.');
      return;
    } finally {
      renderTask = null;
    }
    if (current === generation) updateToolbar();
    pageText(state.page).catch(() => {});
  }

  viewer.toolbar.addEventListener('click', (event) => {
    const nav = event.target.closest('[data-nav]');
    if (nav) {
      const next = state.page + (nav.dataset.nav === 'next' ? 1 : -1);
      state.page = Math.min(pdf.numPages, Math.max(1, next));
      renderCurrent().catch(() => {});
      return;
    }
    const zoom = event.target.closest('[data-zoom]');
    if (zoom) {
      if (zoom.dataset.zoom === 'in') {
        state.fitWidth = false;
        state.zoom = Math.min(400, state.zoom + 25);
      } else if (zoom.dataset.zoom === 'out') {
        state.fitWidth = false;
        state.zoom = Math.max(10, state.zoom - 25);
      } else if (zoom.dataset.zoom === 'width') {
        state.fitWidth = true;
      }
      renderCurrent().catch(() => {});
      return;
    }
    if (event.target.closest('[data-rot]')) {
      state.rotation = (state.rotation + 90) % 360;
      state.fitWidth = false;
      renderCurrent().catch(() => {});
      return;
    }
    if (event.target.closest('[data-ask]')) {
      pageText(state.page)
        .then((text) => askAboutPage(viewer, state.page, text))
        .catch(() => askAboutPage(viewer, state.page, ''));
      return;
    }
  });

  const resizeObserver = new ResizeObserver(() => {
    if (!state.fitWidth || viewer.signal.aborted) return;
    cancelAnimationFrame(resizeRaf);
    resizeRaf = requestAnimationFrame(() => renderCurrent().catch(() => {}));
  });
  resizeObserver.observe(wrap);
  viewer.signal.addEventListener('abort', () => resizeObserver.disconnect(), { once: true });

  updateToolbar();
  await renderCurrent();
}

// ---------------------------------------------------------------------------
// Image / SVG pan-zoom viewer
// ---------------------------------------------------------------------------

function openImage(viewer) {
  const { opts, stage } = viewer;
  toolbarBase(
    viewer,
    `<span class="dv-grp"><button data-zoom="out" aria-label="Zoom out">−</button><span class="dv-zoomlabel" data-zoomlabel>100%</span><button data-zoom="in" aria-label="Zoom in">+</button><button data-zoom="fit">Fit</button><button data-rot="0">Rotate</button></span>` + askButtonHtml()
  );
  stage.classList.add('dv-lock');
  stage.innerHTML = '<div class="dv-imgwrap"><div class="dv-imginner"><img alt=""></div></div>';
  const wrap = stage.querySelector('.dv-imgwrap');
  const inner = stage.querySelector('.dv-imginner');
  const img = stage.querySelector('img');
  img.alt = opts.filename || 'File preview';
  img.src = opts.src;

  const view = { z: 1, tx: 0, ty: 0, rotation: 0, width: 1, height: 1 };

  function apply() {
    inner.style.transform = `translate(${view.tx}px, ${view.ty}px) scale(${view.z})`;
    const label = viewer.toolbar.querySelector('[data-zoomlabel]');
    if (label) label.textContent = `${Math.round(view.z * 100)}%`;
  }

  function fit() {
    const rect = wrap.getBoundingClientRect();
    const sideways = view.rotation % 180 !== 0;
    const contentWidth = sideways ? view.height : view.width;
    const contentHeight = sideways ? view.width : view.height;
    const pad = 24;
    view.z = Math.max(0.05, Math.min(40, Math.min((rect.width - pad) / contentWidth, (rect.height - pad) / contentHeight, 8)));
    view.tx = (rect.width - contentWidth * view.z) / 2;
    view.ty = (rect.height - contentHeight * view.z) / 2;
    // The rotated image is centered inside the correctly sized inner box.
    inner.style.width = `${contentWidth}px`;
    inner.style.height = `${contentHeight}px`;
    apply();
  }

  function zoomBy(factor, cx, cy) {
    const rect = wrap.getBoundingClientRect();
    const px = cx ?? rect.width / 2;
    const py = cy ?? rect.height / 2;
    const next = Math.min(40, Math.max(0.05, view.z * factor));
    view.tx = px - ((px - view.tx) * next) / view.z;
    view.ty = py - ((py - view.ty) * next) / view.z;
    view.z = next;
    apply();
  }

  img.addEventListener('load', () => {
    view.width = img.naturalWidth || 600;
    view.height = img.naturalHeight || 400;
    img.style.width = `${view.width}px`;
    img.style.height = `${view.height}px`;
    fit();
  });
  img.addEventListener('error', () => fallback(viewer, 'This image could not be previewed. Download it to view it.'));

  viewer.toolbar.addEventListener('click', (event) => {
    const zoom = event.target.closest('[data-zoom]');
    if (zoom) {
      if (zoom.dataset.zoom === 'in') zoomBy(1.25);
      else if (zoom.dataset.zoom === 'out') zoomBy(0.8);
      else fit();
      return;
    }
    if (event.target.closest('[data-rot]')) {
      view.rotation = (view.rotation + 90) % 360;
      img.style.transform = `translate(-50%, -50%) rotate(${view.rotation}deg)`;
      fit();
      return;
    }
    if (event.target.closest('[data-ask]')) askAboutPage(viewer, 1, opts.filename || '');
  });

  const pointers = new Map();
  let pinch = null;
  wrap.addEventListener('pointerdown', (event) => {
    if (event.button != null && event.button !== 0) return;
    wrap.setPointerCapture(event.pointerId);
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    pinch = null;
    wrap.classList.add('dragging');
  });
  wrap.addEventListener('pointermove', (event) => {
    const previous = pointers.get(event.pointerId);
    if (!previous) return;
    const current = { x: event.clientX, y: event.clientY };
    pointers.set(event.pointerId, current);
    if (pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      const distance = Math.hypot(a.x - b.x, a.y - b.y);
      if (!pinch) pinch = { distance, z: view.z };
      if (pinch.distance > 0) {
        const rect = wrap.getBoundingClientRect();
        zoomBy((pinch.z * (distance / pinch.distance)) / view.z, (a.x + b.x) / 2 - rect.left, (a.y + b.y) / 2 - rect.top);
      }
      return;
    }
    view.tx += current.x - previous.x;
    view.ty += current.y - previous.y;
    apply();
  });
  ['pointerup', 'pointercancel', 'lostpointercapture'].forEach((type) =>
    wrap.addEventListener(type, (event) => {
      pointers.delete(event.pointerId);
      pinch = null;
      if (!pointers.size) wrap.classList.remove('dragging');
    })
  );
  wrap.addEventListener(
    'wheel',
    (event) => {
      if (!event.ctrlKey && !event.metaKey) return;
      event.preventDefault();
      const rect = wrap.getBoundingClientRect();
      zoomBy(event.deltaY < 0 ? 1.12 : 0.89, event.clientX - rect.left, event.clientY - rect.top);
    },
    { passive: false }
  );

  const resizeObserver = new ResizeObserver(() => {
    if (!viewer.signal.aborted && img.naturalWidth) fit();
  });
  resizeObserver.observe(wrap);
  viewer.signal.addEventListener('abort', () => resizeObserver.disconnect(), { once: true });
}

// ---------------------------------------------------------------------------
// Text / Markdown viewer
// ---------------------------------------------------------------------------

async function readCapped(response) {
  if (!response.body) {
    const text = await response.text();
    return { text: text.slice(0, TEXT_FETCH_LIMIT), truncated: text.length > TEXT_FETCH_LIMIT };
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let text = '';
  let bytes = 0;
  let truncated = false;
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) {
        text += decoder.decode();
        break;
      }
      bytes += value.length;
      if (bytes > TEXT_FETCH_LIMIT) {
        text += decoder.decode(value.subarray(0, value.length - (bytes - TEXT_FETCH_LIMIT)));
        truncated = true;
        await reader.cancel();
        break;
      }
      text += decoder.decode(value, { stream: true });
    }
  } finally {
    reader.releaseLock();
  }
  return { text, truncated };
}

async function openText(viewer, markdown) {
  const { opts, stage } = viewer;
  const state = { fontSize: 13.5 };
  toolbarBase(
    viewer,
    `<span class="dv-grp"><button data-font="down" aria-label="Smaller text">A−</button><button data-font="up" aria-label="Larger text">A+</button><button data-copy>Copy</button></span>` + askButtonHtml()
  );
  stage.innerHTML = '<div class="dv-loading">Loading file…</div>';

  let text = '';
  try {
    const response = await fetch(opts.src, { signal: viewer.signal });
    if (!response.ok) {
      if (response.status === 401) throw new Error('Your session has expired. Sign in again to read this file.');
      if (response.status === 403) throw new Error('This file is restricted and cannot be opened here.');
      if (response.status === 404) throw new Error('This file is no longer available.');
      throw new Error(`This file could not be loaded (HTTP ${response.status}).`);
    }
    ({ text } = await readCapped(response));
  } catch (err) {
    if (viewer.signal.aborted) return;
    fallback(viewer, (err && err.message) || 'This file could not be loaded.');
    return;
  }
  if (viewer.signal.aborted) return;

  const article = document.createElement('article');
  article.className = markdown ? 'dv-prose' : 'dv-text';
  if (markdown) {
    let rendered = null;
    try {
      const mod = await import('./renderer.js');
      if (mod && typeof mod.renderMarkdown === 'function') rendered = mod.renderMarkdown(text);
    } catch (e) {
      rendered = null;
    }
    if (rendered != null) article.innerHTML = rendered;
    else article.textContent = text;
  } else {
    article.textContent = text;
  }
  stage.innerHTML = '<div class="dv-textwrap"></div>';
  stage.querySelector('.dv-textwrap').appendChild(article);

  const applyFont = () => {
    article.style.fontSize = `${state.fontSize}px`;
  };
  applyFont();

  viewer.toolbar.addEventListener('click', (event) => {
    if (event.target.closest('[data-font="up"]')) {
      state.fontSize = Math.min(22, state.fontSize + 1.5);
      applyFont();
      return;
    }
    if (event.target.closest('[data-font="down"]')) {
      state.fontSize = Math.max(10, state.fontSize - 1.5);
      applyFont();
      return;
    }
    if (event.target.closest('[data-copy]')) {
      navigator.clipboard?.writeText(text).catch(() => {});
      return;
    }
    if (event.target.closest('[data-ask]')) askAboutPage(viewer, 1, text.slice(0, 1200));
  });
}

// ---------------------------------------------------------------------------
// Fallback card
// ---------------------------------------------------------------------------

function fallback(viewer, note) {
  const { opts, stage } = viewer;
  toolbarBase(viewer, askButtonHtml());
  const download = opts.downloadUrl
    ? `<a class="dv-btn" href="${escAttr(opts.downloadUrl)}" download="${escAttr(opts.filename || 'file')}">Download</a>`
    : '';
  stage.innerHTML = `<div class="dv-fallback">
    <div><b>${escHtml(opts.filename || 'File')}</b><p>${escHtml(note || 'This file type cannot be previewed here.')}</p></div>
    <div class="row">${download}</div>
  </div>`;
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

/**
 * Open the slide-over viewer. Accepts `{ sha256, ref, filename, mime, page }`;
 * for compatibility also accepts a raw `{ url }` (used for blobs / absolute URLs).
 * Returns the viewer handle (`{ close, panel, stage }`) or null when neither a
 * sha256 nor a usable URL is provided.
 */
export function openViewer(options) {
  const opts = { ...(options || {}) };
  if (!opts.sha256 && opts.url) {
    const parsed = parseViewerRef(opts.url);
    if (parsed) opts.sha256 = parsed.sha256;
    if (!opts.page && parsed && parsed.page) opts.page = parsed.page;
  }
  if (!opts.sha256 && !opts.url) return null;

  const src = opts.sha256 ? fileUrlFor(opts.sha256) : opts.url;
  const filename = opts.filename || fileNameFromUrl(src);
  const mime = opts.mime || '';
  const page = Math.max(1, parseInt(opts.page, 10) || 1);

  const viewer = shell({
    sha256: opts.sha256 || null,
    ref: opts.ref || null,
    filename,
    mime,
    page,
    src,
    downloadUrl: downloadUrlFor(src, opts.downloadUrl),
    subtitle: [opts.ref, mime || kindOf(mime, filename).toUpperCase()].filter(Boolean).join(' · '),
  });
  try {
    document.dispatchEvent(new CustomEvent('doomed:viewer-open', { detail: { sha256: opts.sha256 || null, filename, page } }));
  } catch (e) {
    /* ignore */
  }

  const kind = kindOf(mime, filename);
  if (kind === 'pdf') {
    openPdf(viewer).catch(() => fallback(viewer, 'This PDF could not be rendered. Download it to open in your PDF app.'));
  } else if (kind === 'image') {
    openImage(viewer);
  } else if (kind === 'markdown') {
    openText(viewer, true).catch(() => fallback(viewer, 'This file could not be opened.'));
  } else if (kind === 'text') {
    openText(viewer, false).catch(() => fallback(viewer, 'This file could not be opened.'));
  } else {
    fallback(viewer, 'Preview is not available for this file type — download the original.');
  }
  return viewer;
}

export function closeViewer() {
  if (activeViewer) activeViewer.close();
}

// ---------------------------------------------------------------------------
// Global click wiring for chat chips and viewer-tagged links
// ---------------------------------------------------------------------------

function onDocumentClick(event) {
  if (event.defaultPrevented || !event.target || !event.target.closest) return;

  // renderer.js emits citation chips: <button data-viewer-ref="viewer:<sha>?page=N">.
  const chip = event.target.closest('[data-viewer-ref]');
  if (chip) {
    const parsed = parseViewerRef(chip.getAttribute('data-viewer-ref'));
    if (!parsed) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    const label = (chip.textContent || '').replace(/^[^\w"'([]+/, '').trim().slice(0, 140);
    openViewer({
      ...parsed,
      filename: label || 'File',
    });
    return;
  }

  // File chips elsewhere in the app: <a data-viewer data-view-url="...">.
  const link = event.target.closest('[data-viewer]');
  if (link) {
    const url = link.dataset.viewUrl || link.getAttribute('href') || '';
    if (!url || url === '#') return;
    const parsed = parseViewerRef(url);
    const page = parseInt(link.dataset.viewPage, 10) || (parsed && parsed.page) || 1;
    event.preventDefault();
    event.stopImmediatePropagation();
    openViewer({
      sha256: parsed ? parsed.sha256 : undefined,
      url: parsed ? undefined : url,
      filename: link.dataset.viewName || link.textContent.trim().slice(0, 140) || undefined,
      mime: link.dataset.viewMime || undefined,
      page,
    });
  }
}

document.addEventListener('click', onDocumentClick, true);

// Non-module escape hatches for inline handlers and legacy wiring.
if (typeof window !== 'undefined') {
  window.openViewer = openViewer;
  window.closeViewer = closeViewer;
  window.doomedViewer = { openViewer, closeViewer, parseViewerRef };
}
