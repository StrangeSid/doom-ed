// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * doom-ed visual maker verification engine (browser side).
 *
 * Flow: the teacher agent dispatches a `mermaid-maker` / `svg-maker` subagent and
 * emits a `viz_render_request` SSE event `{ call_id, kind, slug, source }`. The
 * app calls `validateAndRasterize(call_id, kind, source)`, which:
 *
 *   1. renders the source in an offscreen container,
 *   2. checks parse/syntax errors (mermaid.parse / XML parsererror),
 *   3. checks layout geometry (content bbox vs viewBox clipping, overlapping labels),
 *   4. rasterizes the SVG to a 2x-DPI PNG data URL,
 *   5. POSTs `{ call_id, ok, dom_warnings, png_data_url, error }` to
 *      `/api/chat/viz-feedback` so the maker model can iterate or publish.
 *
 * No build step: Mermaid is read from the page global when index.html provides it,
 * otherwise a local vendor script is loaded on demand. Everything degrades to a
 * clear `error` string when the renderer is unavailable — never to a thrown page.
 */

const MERMAID_VENDOR_PATHS = ['vendor/mermaid.min.js', 'vendor/mermaid/mermaid.min.js'];

const VIZ_CSS_ID = 'doomed-viz-css';
const MAX_DOM_WARNINGS = 8;
const RASTER_SCALE = 2; // spec: 2x DPI PNG for the vision model
const MAX_RASTER_EDGE = 8192;
const MAX_RASTER_PIXELS = 16e6;
const CLIP_RATIO_TOLERANCE = 0.01; // 1% of the viewBox before it counts as clipped
const MAX_LABELS_SCANNED = 40;

let mermaidPromise = null;
let mermaidInitialized = false;
let mermaidSeq = 0;

function assetsBase() {
  try {
    return new URL('.', import.meta.url);
  } catch (e) {
    return new URL('.', location.href);
  }
}

function ensureVizCss() {
  if (document.getElementById(VIZ_CSS_ID)) return;
  const style = document.createElement('style');
  style.id = VIZ_CSS_ID;
  // Offscreen but fully rendered (visibility/geometry preserved) so getBBox()
  // and getBoundingClientRect() return real numbers.
  style.textContent = `.doomed-viz-offscreen{position:fixed;left:-100000px;top:0;width:1400px;height:1000px;pointer-events:none;z-index:-1;opacity:0.01}`;
  document.head.appendChild(style);
}

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = src;
    script.onload = () => resolve();
    script.onerror = () => {
      script.remove();
      reject(new Error(`Could not load renderer from ${src}`));
    };
    document.head.appendChild(script);
  });
}

function mermaidError(err) {
  const raw = (err && (err.str || err.message)) || String(err || 'Unknown Mermaid error');
  const firstLine = String(raw).split('\n')[0].trim().slice(0, 300);
  return new Error(`Mermaid error: ${firstLine || 'invalid diagram source'}`);
}

/**
 * Resolve the Mermaid global, loading the pinned vendor bundle on demand when the
 * page did not include it. Failures clear the cache so a later retry can succeed.
 */
export function ensureMermaid() {
  if (window.mermaid) return Promise.resolve(window.mermaid);
  if (!mermaidPromise) {
    mermaidPromise = (async () => {
      const base = assetsBase();
      let lastError = null;
      for (const path of MERMAID_VENDOR_PATHS) {
        try {
          await loadScript(new URL(path, base).href);
        } catch (err) {
          lastError = err;
          continue;
        }
        if (window.mermaid) return window.mermaid;
        lastError = new Error('Mermaid script loaded but window.mermaid is missing.');
      }
      throw lastError || new Error('Mermaid renderer is not available.');
    })().catch((err) => {
      mermaidPromise = null;
      throw err;
    });
  }
  return mermaidPromise;
}

/**
 * Initialize Mermaid once with the doom-ed defaults.
 * Resolves `true` when Mermaid is ready, `false` when it cannot be loaded.
 */
export async function initVizEngine() {
  try {
    const mermaid = await ensureMermaid();
    if (!mermaidInitialized) {
      mermaid.initialize({
        startOnLoad: false,
        theme: 'neutral',
        securityLevel: 'strict',
        fontFamily: 'system-ui, "Segoe UI", "Helvetica Neue", sans-serif',
        // Real <text> labels (not <foreignObject> HTML): the geometry check can
        // then detect overlapping labels, and canvas rasterization keeps them.
        flowchart: { htmlLabels: false },
      });
      mermaidInitialized = true;
    }
    return true;
  } catch (err) {
    console.warn('[viz-engine]', err);
    return false;
  }
}

function cleanupMermaidTemp(mermaidId) {
  try {
    document.getElementById(`d${mermaidId}`)?.remove();
    document.getElementById(mermaidId)?.remove();
  } catch (e) {
    /* best effort */
  }
}

/**
 * Render Mermaid source into `container` (replacing its content) and return the
 * resulting `<svg>` element. Throws a compact Error on syntax/render failure.
 */
export async function renderMermaidToElement(container, source) {
  if (!container) throw new Error('renderMermaidToElement requires a container element.');
  const mermaid = await ensureMermaid();
  if (!mermaidInitialized) await initVizEngine();

  const text = String(source ?? '').trim();
  if (!text) throw new Error('Empty Mermaid source.');

  try {
    await Promise.resolve(mermaid.parse(text));
  } catch (err) {
    throw mermaidError(err);
  }

  const mermaidId = `doomed-mermaid-${Date.now().toString(36)}-${++mermaidSeq}`;
  let result;
  try {
    result = await Promise.resolve(mermaid.render(mermaidId, text));
  } catch (err) {
    cleanupMermaidTemp(mermaidId);
    throw mermaidError(err);
  }
  cleanupMermaidTemp(mermaidId);

  const svgText = typeof result === 'string' ? result : result && result.svg;
  if (!svgText) throw new Error('Mermaid returned no SVG output.');

  container.innerHTML = svgText;
  const svg = container.querySelector('svg');
  if (!svg) throw new Error('Mermaid output contained no <svg> element.');
  return svg;
}

/**
 * Parse raw SVG source (the svg-maker output). Throws on XML/syntax errors and
 * returns a DOM-imported <svg> element ready to be attached.
 */
export function parseSvgSource(source) {
  const text = String(source ?? '').trim();
  if (!text) throw new Error('Empty SVG source.');
  const doc = new DOMParser().parseFromString(text, 'image/svg+xml');
  const parserError = doc.querySelector('parsererror');
  if (parserError) {
    const detail = (parserError.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 240);
    throw new Error(`SVG parse error: ${detail || 'malformed XML'}`);
  }
  const root = doc.documentElement;
  if (!root || root.localName.toLowerCase() !== 'svg') {
    throw new Error('SVG source root element must be <svg>.');
  }
  return document.importNode(root, true);
}

function pixelDimension(svg, name) {
  const raw = svg.getAttribute(name) || '';
  if (raw.includes('%')) return NaN; // SVG percentages do not resolve for intrinsic sizing
  const value = parseFloat(raw);
  return Number.isFinite(value) ? value : NaN;
}

function readViewBox(svg) {
  const parts = (svg.getAttribute('viewBox') || '').trim().split(/[\s,]+/).map(Number);
  if (parts.length === 4 && parts.every((n) => Number.isFinite(n)) && parts[2] > 0 && parts[3] > 0) {
    return { x: parts[0], y: parts[1], width: parts[2], height: parts[3] };
  }
  const width = pixelDimension(svg, 'width');
  const height = pixelDimension(svg, 'height');
  if (Number.isFinite(width) && Number.isFinite(height) && width > 0 && height > 0) {
    return { x: 0, y: 0, width, height };
  }
  return null;
}

function formatBox(box) {
  const round = (n) => Math.round(n * 10) / 10;
  return `x=${round(box.x)} y=${round(box.y)} w=${round(box.width)} h=${round(box.height)}`;
}

/**
 * Check 2 — layout geometry.
 * `dom_warnings` covers (a) content drawn outside the declared viewBox (clipping)
 * and (b) visually overlapping label elements (<text> / <foreignObject>).
 * Always returns an array; never throws.
 */
export function analyzeSvgGeometry(svg) {
  const warnings = [];
  const viewBox = readViewBox(svg);

  let bbox = null;
  try {
    bbox = svg.getBBox();
  } catch (e) {
    bbox = null;
  }
  if (bbox && viewBox && bbox.width > 0 && bbox.height > 0) {
    const tolX = Math.max(1, viewBox.width * CLIP_RATIO_TOLERANCE);
    const tolY = Math.max(1, viewBox.height * CLIP_RATIO_TOLERANCE);
    const clipped =
      bbox.x < viewBox.x - tolX ||
      bbox.y < viewBox.y - tolY ||
      bbox.x + bbox.width > viewBox.x + viewBox.width + tolX ||
      bbox.y + bbox.height > viewBox.y + viewBox.height + tolY;
    if (clipped) {
      warnings.push(`Content is clipped: drawn bounds (${formatBox(bbox)}) exceed viewBox (${formatBox(viewBox)}).`);
    }
  }

  const labels = [];
  for (const node of svg.querySelectorAll('text, foreignObject')) {
    let rect;
    try {
      rect = node.getBoundingClientRect();
    } catch (e) {
      continue;
    }
    if (!rect || rect.width < 1 || rect.height < 1) continue;
    const text = (node.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 28);
    labels.push({ node, rect, label: text || (node.localName === 'text' ? '(empty text)' : '(label)') });
    if (labels.length >= MAX_LABELS_SCANNED) break;
  }

  outer: for (let i = 0; i < labels.length; i++) {
    for (let j = i + 1; j < labels.length; j++) {
      const a = labels[i];
      const b = labels[j];
      if (a.node.contains(b.node) || b.node.contains(a.node)) continue;
      const overlapW = Math.min(a.rect.right, b.rect.right) - Math.max(a.rect.left, b.rect.left);
      const overlapH = Math.min(a.rect.bottom, b.rect.bottom) - Math.max(a.rect.top, b.rect.top);
      if (overlapW > 1.5 && overlapH > 1.5 && overlapW * overlapH > 8) {
        warnings.push(
          `Labels overlap: "${a.label}" and "${b.label}" share about ${Math.round(overlapW)}×${Math.round(overlapH)} px.`
        );
      }
      if (warnings.length >= MAX_DOM_WARNINGS) break outer;
    }
  }

  return warnings.slice(0, MAX_DOM_WARNINGS);
}

/**
 * Check 3 — rasterize an SVG element to a PNG data URL at `scale`x (default 2x),
 * clamped so the canvas stays inside browser limits. Throws when the SVG cannot
 * be drawn (broken markup or blocked external asset).
 */
export async function svgToPngDataUrl(svg, scale = RASTER_SCALE) {
  const viewBox = readViewBox(svg);
  let width = viewBox ? viewBox.width : pixelDimension(svg, 'width');
  let height = viewBox ? viewBox.height : pixelDimension(svg, 'height');

  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
    try {
      const bbox = svg.getBBox();
      width = bbox.width;
      height = bbox.height;
    } catch (e) {
      /* fall through */
    }
  }
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
    width = svg.clientWidth || 600;
    height = svg.clientHeight || 400;
  }

  const effectiveScale = Math.max(
    0.1,
    Math.min(scale, MAX_RASTER_EDGE / width, MAX_RASTER_EDGE / height, Math.sqrt(MAX_RASTER_PIXELS / (width * height)))
  );

  const clone = svg.cloneNode(true);
  clone.setAttribute('width', String(width));
  clone.setAttribute('height', String(height));
  if (!clone.getAttribute('xmlns')) clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
  if (!clone.getAttribute('xmlns:xlink') && clone.querySelector('[xlink\\:href]')) {
    clone.setAttribute('xmlns:xlink', 'http://www.w3.org/1999/xlink');
  }

  // XMLSerializer + data URL: no blob lifetime management, and named HTML entities
  // (e.g. &nbsp; from stray HTML) would break the XML parser — normalize them.
  const xml = new XMLSerializer().serializeToString(clone).replace(/&nbsp;/g, '&#160;');
  const url = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(xml)}`;

  const image = new Image();
  image.decoding = 'async';
  await new Promise((resolve, reject) => {
    image.onload = () => resolve();
    image.onerror = () => reject(new Error('SVG rasterization failed (unsupported markup or blocked external asset).'));
    image.src = url;
  });

  const canvas = document.createElement('canvas');
  canvas.width = Math.max(1, Math.round(width * effectiveScale));
  canvas.height = Math.max(1, Math.round(height * effectiveScale));
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(image, 0, 0, canvas.width, canvas.height);
  return canvas.toDataURL('image/png');
}

/**
 * Full maker-loop check for one `viz_render_request`.
 *
 * Returns the server result: `{ status: 'published', png_url, visual_id, ... }`
 * or `{ status: 'retry_render', source, ... }` (the retry has already been
 * re-rendered and re-posted, so callers normally see `published`), plus
 * `source` carrying the diagram source that was actually published.
 * Endpoint/network failures return `{ status: 'error', error }` instead of throwing.
 */
export async function validateAndRasterize(callId, kind, source, attempt = 1) {
  if (!callId) return { status: 'error', error: 'Missing call_id for visual feedback.' };
  const normalizedKind = kind === 'svg' ? 'svg' : 'mermaid';

  let error = null;
  let svgElement = null;
  const warnings = [];

  ensureVizCss();
  const holder = document.createElement('div');
  holder.className = 'doomed-viz-offscreen';
  holder.setAttribute('aria-hidden', 'true');
  document.body.appendChild(holder);

  try {
    try {
      if (document.fonts && document.fonts.ready) await document.fonts.ready;
    } catch (e) {
      /* fonts may never settle; measure with whatever is loaded */
    }
    if (normalizedKind === 'mermaid') {
      await initVizEngine();
      svgElement = await renderMermaidToElement(holder, source);
    } else {
      svgElement = parseSvgSource(source);
      holder.appendChild(svgElement);
    }
    warnings.push(...analyzeSvgGeometry(svgElement));
  } catch (err) {
    error = (err && err.message) || String(err);
  }

  let pngDataUrl = null;
  if (!error && svgElement) {
    try {
      pngDataUrl = await svgToPngDataUrl(svgElement, RASTER_SCALE);
    } catch (err) {
      warnings.push(`Rasterization failed: ${(err && err.message) || err}`);
    }
  }
  holder.remove();

  const ok = !error && warnings.length === 0;
  const feedback = { ok, dom_warnings: warnings, png_data_url: pngDataUrl, error };

  let response = null;
  try {
    const res = await fetch('/api/chat/viz-feedback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      // Send the nested `feedback` object the FastAPI model expects and the flat
      // fields as well, so either endpoint contract validates cleanly.
      body: JSON.stringify({ call_id: callId, ...feedback, feedback }),
    });
    response = await res.json().catch(() => null);
    if (!res.ok) {
      const detail = response && (response.detail || response.error);
      return { status: 'error', error: detail || `Visual feedback failed (HTTP ${res.status}).`, dom_warnings: warnings };
    }
  } catch (err) {
    return {
      status: 'error',
      error: `Could not reach the visual feedback endpoint: ${(err && err.message) || err}`,
      dom_warnings: warnings,
    };
  }

  if (!response || typeof response !== 'object') {
    return { status: 'error', error: 'Empty response from the visual feedback endpoint.', dom_warnings: warnings };
  }
  if (response.status === 'retry_render' && response.source && attempt < 4) {
    return validateAndRasterize(callId, normalizedKind, response.source, attempt + 1);
  }
  return { ...response, source: response.source || source };
}

// Non-module escape hatch for inline handlers and legacy wiring.
if (typeof window !== 'undefined') {
  window.doomedViz = {
    initVizEngine,
    ensureMermaid,
    renderMermaidToElement,
    validateAndRasterize,
    analyzeSvgGeometry,
    svgToPngDataUrl,
  };
}
