/* Prism — Collector Pulse Monitor
 * ─────────────────────────────────────────────────────────────────────
 * ECG-style live heartbeat in the topbar. Replaces the old wifi/wifi-off
 * widget that inferred freshness from HTMX swaps (which was lying on
 * pages without auto-refreshing partials).
 *
 * Data flow:
 *   server  →  /api/collector/pulse  →  PulseMonitor.poll()
 *                                          │
 *                                          ├─ enqueue events into beat ring
 *                                          ├─ update fleet counter + dot color
 *                                          └─ refresh hover panel (if open)
 *
 *   rAF loop  →  PulseMonitor.draw()
 *                  └─ scroll canvas 1px every ~100ms, paint pending beats
 *
 * Energy-aware:
 *   • rAF pauses after 5s with no events (just shows static flatline)
 *   • Pauses entirely on visibilitychange (tab hidden)
 *   • prefers-reduced-motion → canvas hidden, dot+counter only
 *
 * Resilient:
 *   • 3 consecutive endpoint errors → back off to 5s poll
 *   • Anything that throws inside draw() is logged once + suppressed
 *
 * Globals expected from base.html before this script runs:
 *   • PULSE_I18N   — translation map ({pulse_title_healthy, ...})
 *   • prismFetch() — JSON-safe fetch helper
 *   • formatTs()   — global timestamp formatter (optional, only for panel)
 */
(function () {
  'use strict';

  // ── Tunables ────────────────────────────────────────────────────────
  const POLL_OK_MS = 1500;     // steady-state cadence
  const POLL_BACKOFF_MS = 5000; // after 3 consecutive errors
  const ERRORS_BEFORE_BACKOFF = 3;
  const STRIP_SCROLL_MS = 100;  // 1px every Nms = 10 FPS
  const IDLE_PAUSE_MS = 5000;   // pause rAF after N ms with no events
  const STRIP_W = 140;
  const STRIP_H = 28;
  const SECONDS_VISIBLE = 12;   // canvas width = SECONDS_VISIBLE seconds

  // Beat color palette (must match Prism's existing palette).
  const COLOR_OK = '#10B981';
  const COLOR_SLOW = '#F59E0B';
  const COLOR_FAIL = '#DC2626';
  const COLOR_WARMING = '#3B82F6';
  const COLOR_BASELINE_LIGHT = '#CBD5E1';
  const COLOR_BASELINE_DARK = '#475569';

  // Reduced-motion users get no canvas at all — they keep the dot + counter.
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // ── State ───────────────────────────────────────────────────────────
  let canvas = null, ctx = null;
  let dotEl = null, counterEl = null, panelEl = null, errorEl = null;
  // The panel's SHELL and the element inside it that scrolls. Two references
  // rather than one because they have different lifetimes: the shell persists
  // and carries the hover handlers, the scroller persists and holds the
  // scroll position, and only the scroller's CHILDREN are rewritten.
  let panelScrollEl = null;
  let lastEventTs = 0;       // server-side timestamp watermark
  let consecutiveErrors = 0;
  let pollTimer = null;
  let rafHandle = null;
  let lastDrawAt = 0;
  let lastEventAtClient = Date.now();
  // Beats carry a ``drawn`` flag so we stamp each one to the canvas
  // exactly once; afterwards the imageData scroll carries it left. Used
  // to be redrawn-every-frame which was ~12s × 10fps = 120× per beat
  // (audit M4).
  let beats = [];            // [{ts, server, check, ok, ms, drawn}]
  let state = 'warming';     // warming|healthy|degraded|critical|dead
  let lastSnapshot = null;   // last full server response (for panel render)
  let panelOpen = false;
  let panelHideTimer = null;
  let drawComplained = false;
  let themeObserver = null;

  // ── Init ────────────────────────────────────────────────────────────
  function init() {
    const root = document.getElementById('collector-pulse');
    if (!root) return;

    canvas = root.querySelector('canvas');
    if (canvas) {
      // Hi-DPI: render at devicePixelRatio for crisp lines.
      const dpr = window.devicePixelRatio || 1;
      canvas.width = STRIP_W * dpr;
      canvas.height = STRIP_H * dpr;
      canvas.style.width = STRIP_W + 'px';
      canvas.style.height = STRIP_H + 'px';
      // willReadFrequently: draw() below calls getImageData()/putImageData()
      // every ~100ms for as long as events keep flowing (measured live: ~26
      // read/write pairs in 4s of normal polling — this runs continuously,
      // not just while a beat is in flight). Without the hint the browser
      // keeps this canvas GPU-backed and every getImageData() forces a
      // GPU->CPU sync stall; Chrome's own console warns about exactly this.
      //
      // The obvious-looking alternative — `ctx.drawImage(canvas, -px, 0)`,
      // a canvas drawing itself, which never reads pixels back at all — was
      // measured and rejected: drawImage() alpha-composites (source-over)
      // by default, so it does NOT replace pixels the way putImageData()
      // does, and even with globalCompositeOperation='copy' to force a
      // replace, a 60-frame side-by-side render (matching this canvas's
      // real size/DPR and beat-stamp pattern) came out byte-identical to
      // the current output only up to a point: at full scale, with the
      // anti-aliased diagonal beat strokes this file actually draws,
      // drawImage's normal compositing pipeline diverged from
      // putImageData's raw buffer replace on ~4.8% of pixels after
      // sustained scrolling (3,003 of 62,720 bytes; putImageData does a
      // non-premultiplied raw copy, drawImage does not). That is a real,
      // if subtle, rendering bug for a strip whose entire job is to show
      // health data accurately, so it was not shipped. willReadFrequently
      // changes zero drawing calls — same getImageData()/putImageData(),
      // same output, byte-for-byte, forever — it only relocates the
      // backing store to CPU memory so those calls stop paying the GPU
      // sync cost. Synthetic benchmark at this canvas's real size (300
      // scroll-frames): willReadFrequently added no measurable per-call
      // regression over the current bare getContext('2d') (~0.065ms vs
      // ~0.069ms per frame; noise-level), because the canvas is tiny
      // (140x28 CSS px) and the per-frame drawing is a handful of strokes.
      ctx = canvas.getContext('2d', { willReadFrequently: true });
      ctx.scale(dpr, dpr);
      paintBaselineFlat();
    }

    dotEl = root.querySelector('[data-pulse-dot]');
    counterEl = root.querySelector('[data-pulse-counter]');
    panelEl = document.getElementById('pulse-panel');
    errorEl = document.getElementById('pulse-error');

    // Theme switch — when the user toggles light/dark we must repaint the
    // strip's baseline color and re-stamp visible beats, else the canvas
    // shows a 12-second-long gradient of the old theme scrolling out
    // (audit M2).
    themeObserver = new MutationObserver(() => {
      if (!ctx) return;
      paintBaselineFlat();
      // Mark all current beats as "needs stamp again" so they're re-drawn
      // in their freshly-themed colors. Position is recomputed from ts.
      for (const ev of beats) ev.drawn = false;
      // If rAF was paused (idle window), wake it up to do the repaint.
      if (!reduceMotion && rafHandle == null) startRaf();
    });
    themeObserver.observe(document.documentElement,
      { attributes: true, attributeFilter: ['class'] });

    // The panel is a clipping shell around a scrolling child, and the child
    // is created HERE, once, for the length of the page's life.
    //
    // Why it cannot be part of the markup or part of the render: the shell
    // needs `overflow: hidden` so its 8px radius clips the scrollbar
    // (Chromium paints the bar in the padding box and does not round it), so
    // something inside it has to scroll — and `renderPanel()` replaces the
    // panel's content on every poll, 1.5s apart while it is open. A scroller
    // recreated by each render would reset `scrollTop` every time, which is
    // worse than the artefact being fixed: the reader is thrown back to the
    // top of a list they were halfway down. So the scroller is built once and
    // only its children are rewritten.
    //
    // `if absent` rather than unconditionally, because init() is reachable
    // more than once in principle (DOMContentLoaded plus a manual call) and a
    // second scroller would silently orphan the first along with its
    // position.
    if (panelEl) {
      panelScrollEl = panelEl.querySelector('.pulse-panel-scroll');
      if (!panelScrollEl) {
        panelScrollEl = document.createElement('div');
        panelScrollEl.className = 'pulse-panel-scroll';
        panelEl.appendChild(panelScrollEl);
      }
    }

    // Hover panel wiring. Show on enter, hide on leave with 200ms grace so
    // the user can move the cursor INTO the panel to click the CTA without
    // it snapping shut. Bound to the SHELL, not the scroller: the grace
    // period exists so the cursor can travel into the panel, and a listener
    // on the inner element would miss the padding it has to cross first.
    root.addEventListener('mouseenter', showPanel);
    root.addEventListener('mouseleave', queuePanelHide);
    if (panelEl) {
      panelEl.addEventListener('mouseenter', () => {
        if (panelHideTimer) { clearTimeout(panelHideTimer); panelHideTimer = null; }
      });
      panelEl.addEventListener('mouseleave', queuePanelHide);
    }

    // Click the strip → take the operator to Operations → System Health.
    root.addEventListener('click', (e) => {
      // Don't intercept clicks INSIDE the panel (links, etc.)
      if (panelEl && panelEl.contains(e.target)) return;
      window.location.href = '/operations#health';
    });

    // Tab visibility — fresh poll on refocus, suspend rAF when hidden.
    // Clear the queued poll first so we don't double-fetch on focus
    // (audit L1).
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) {
        stopRaf();
      } else {
        if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
        // Skip animating the missed period; jump to "now".
        beats = [];
        lastEventAtClient = Date.now();
        if (ctx) paintBaselineFlat();
        poll();
      }
    });

    // Kick off
    poll();
    if (!reduceMotion) startRaf();
  }

  // ── Polling ─────────────────────────────────────────────────────────
  function poll() {
    const qs = lastEventTs ? ('?since=' + encodeURIComponent(lastEventTs)) : '';
    const url = '/api/collector/pulse' + qs;

    // Use prismFetch when available (handles CSRF errors etc.), fall back
    // to plain fetch.
    const f = window.prismFetch || function (u) { return fetch(u).then(r => r.json()); };

    f(url).then(onPollSuccess).catch(onPollError);
  }

  function onPollSuccess(data) {
    consecutiveErrors = 0;
    lastSnapshot = data;
    // Ingest new events into the beat queue.
    if (Array.isArray(data.events) && data.events.length) {
      for (const ev of data.events) {
        // Annotate with our per-beat "have we stamped this yet?" flag.
        // We pin one freshly-themed stamp per beat and let imageData
        // scroll carry it left (audit M4); a theme change resets all
        // drawn flags so the beats re-render in the new palette.
        ev.drawn = false;
        beats.push(ev);
        if (ev.ts > lastEventTs) lastEventTs = ev.ts;
      }
      lastEventAtClient = Date.now();
      if (!reduceMotion) startRaf();
    }
    // Recompute overall state.
    state = deriveState(data);
    updateChrome(data);
    if (panelOpen) renderPanel();
    schedulePoll(POLL_OK_MS);
  }

  function onPollError(err) {
    consecutiveErrors += 1;
    if (consecutiveErrors >= ERRORS_BEFORE_BACKOFF) {
      // Endpoint is down OR auth lapsed OR network gone. Use our own
      // indicator (#pulse-error) instead of the shared htmx error banner
      // — the htmx banner is hidden by every successful HTMX swap so it
      // would flicker against unrelated dashboard partials (audit H2).
      state = 'dead';
      updateChrome(null);
      if (errorEl) errorEl.style.display = 'inline-flex';
      schedulePoll(POLL_BACKOFF_MS);
    } else {
      schedulePoll(POLL_OK_MS);
    }
  }

  function schedulePoll(ms) {
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = setTimeout(poll, ms);
  }

  // ── State derivation ────────────────────────────────────────────────
  function deriveState(data) {
    if (!data) return 'dead';
    // "Warming" — we trust the dot to be blue/spinning until at least
    // one event has actually been received from the aggregator. Without
    // this guard the dot turns green from millisecond 0 whenever Prism
    // has servers configured, even before any data has flowed (audit H1).
    if (lastEventTs === 0) return 'warming';
    const sub = data.subsystems || {};
    // Critical: a core subsystem heartbeat is stale.
    if (sub.supervisor && sub.supervisor.ok === false) return 'critical';
    if (sub.aggregator && sub.aggregator.ok === false) return 'critical';
    const silent = (data.fleet && data.fleet.silent) || [];
    if (silent.length >= 2) return 'critical';
    if (silent.length === 1) return 'degraded';
    if (sub.workers && sub.workers.ok === false) return 'degraded';
    if (sub.periodics && sub.periodics.ok === false) return 'degraded';
    return 'healthy';
  }

  function updateChrome(data) {
    if (dotEl) {
      dotEl.className = 'pulse-dot pulse-dot--' + state;
    }
    if (counterEl) {
      const f = data && data.fleet;
      counterEl.textContent = f && f.total
        ? (f.up + '/' + f.total)
        : '--';
    }
    // Hide our own error indicator on recovery.
    if (state !== 'dead' && errorEl) {
      errorEl.style.display = 'none';
    }
  }

  // ── Canvas rendering ────────────────────────────────────────────────
  function startRaf() {
    if (reduceMotion || rafHandle != null) return;
    lastDrawAt = performance.now();
    const tick = (now) => {
      rafHandle = requestAnimationFrame(tick);
      // Throttle to ~10 FPS (1px per 100ms scroll → 12s visible at 120px).
      if (now - lastDrawAt < STRIP_SCROLL_MS) return;
      const dtMs = now - lastDrawAt;
      lastDrawAt = now;
      try {
        draw(dtMs);
      } catch (e) {
        // One-time complaint then suppress so a render bug doesn't fill
        // the console.
        if (!drawComplained) {
          console.warn('pulse draw failed', e);
          drawComplained = true;
        }
      }
      // Idle pause: if no events in IDLE_PAUSE_MS, freeze the strip on a
      // flatline frame. Resumes when the next event arrives via poll().
      if (Date.now() - lastEventAtClient > IDLE_PAUSE_MS && state !== 'dead') {
        stopRaf();
      }
    };
    rafHandle = requestAnimationFrame(tick);
  }

  function stopRaf() {
    if (rafHandle != null) {
      cancelAnimationFrame(rafHandle);
      rafHandle = null;
    }
  }

  function paintBaselineFlat() {
    if (!ctx) return;
    ctx.clearRect(0, 0, STRIP_W, STRIP_H);
    const baseline = STRIP_H / 2;
    const darkMode = document.documentElement.classList.contains('dark');
    ctx.strokeStyle = darkMode ? COLOR_BASELINE_DARK : COLOR_BASELINE_LIGHT;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, baseline);
    ctx.lineTo(STRIP_W, baseline);
    ctx.stroke();
  }

  // Build the next frame: scroll existing content left by N px (function
  // of dtMs), draw the baseline in the freshly-revealed strip on the
  // right, then stamp any beats whose virtual x has crossed into view.
  function draw(dtMs) {
    if (!ctx) return;
    const pxScroll = Math.max(1, Math.round(dtMs / (1000 * SECONDS_VISIBLE / STRIP_W)));
    const baseline = STRIP_H / 2;
    const darkMode = document.documentElement.classList.contains('dark');
    const baselineColor = darkMode ? COLOR_BASELINE_DARK : COLOR_BASELINE_LIGHT;

    // Scroll: copy a slice of the canvas left.
    const dpr = window.devicePixelRatio || 1;
    const imgData = ctx.getImageData(pxScroll * dpr, 0,
                                     (STRIP_W - pxScroll) * dpr, STRIP_H * dpr);
    ctx.putImageData(imgData, 0, 0);

    // Clear the just-revealed right edge + draw baseline there.
    ctx.clearRect(STRIP_W - pxScroll, 0, pxScroll, STRIP_H);
    ctx.strokeStyle = baselineColor;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(STRIP_W - pxScroll, baseline);
    ctx.lineTo(STRIP_W, baseline);
    ctx.stroke();

    // Stamp un-drawn beats exactly once. The imageData copy above carries
    // already-stamped beats leftward each frame, so we don't redraw them.
    // (audit M4 — the old code redrew every beat every frame.)
    if (beats.length) {
      const nowS = Date.now() / 1000;
      const remaining = [];
      for (const ev of beats) {
        const ageS = Math.max(0, nowS - ev.ts);
        if (ageS > SECONDS_VISIBLE) continue;  // off the left edge, drop
        if (!ev.drawn) {
          // Map age → x position on canvas. Newest beat = right edge.
          // Clip x to canvas's drawable region [0, STRIP_W - 1] so the
          // rightmost beat isn't partially clipped (audit H3).
          let x = STRIP_W - 1 - Math.round(ageS * (STRIP_W / SECONDS_VISIBLE));
          if (x < 0) x = 0;
          if (x > STRIP_W - 1) x = STRIP_W - 1;
          drawBeat(x, baseline, ev);
          ev.drawn = true;
        }
        remaining.push(ev);
      }
      beats = remaining;
    }
  }

  function drawBeat(x, baseline, ev) {
    if (!ctx) return;
    // Color
    let color = COLOR_OK;
    if (!ev.ok) color = COLOR_FAIL;
    else if (ev.ms > 2000) color = COLOR_SLOW;

    // Spike height — fast checks make tall sharp spikes, slow ones are
    // shorter wider humps. Cap to STRIP_H/2 - 2 so we don't clip.
    const maxH = (STRIP_H / 2) - 2;
    const fast = Math.max(0.2, 1 - Math.min(1, ev.ms / 2000));
    const h = Math.round(fast * maxH);

    // Width — METRICS is the steady-state pulse (narrow); LOGS/UPDATES/HW
    // are the wider "S-waves". Server sends lowercase (CheckType.value);
    // case-fold defensively in case a future caller doesn't.
    const isMetrics = (String(ev.check || '').toLowerCase() === 'metrics');
    const w = isMetrics ? 2 : 4;

    ctx.strokeStyle = color;
    ctx.lineWidth = w;
    ctx.beginPath();
    // P-Q-R-S-T-ish: tiny dip, sharp up, sharp down, tiny dip.
    ctx.moveTo(x - w, baseline);
    ctx.lineTo(x, baseline - h);
    ctx.lineTo(x + w, baseline + (h * 0.3));
    ctx.lineTo(x + w + 1, baseline);
    ctx.stroke();
  }

  // ── Hover panel ─────────────────────────────────────────────────────
  function showPanel() {
    if (!panelEl) return;
    if (panelHideTimer) { clearTimeout(panelHideTimer); panelHideTimer = null; }
    panelOpen = true;
    // CLEARING the inline display rather than setting `block`. The panel is a
    // flex column now — the shell has to be a flex container for its scrolling
    // child to be constrained by its max-height — and `display: block` from
    // here would silently override that, leaving the child unconstrained and
    // the panel growing past the bottom of the viewport. Clearing it lets
    // app.css decide, so the layout mode has one owner.
    panelEl.style.display = '';
    renderPanel();
  }

  function queuePanelHide() {
    if (panelHideTimer) clearTimeout(panelHideTimer);
    panelHideTimer = setTimeout(() => {
      if (!panelEl) return;
      panelEl.style.display = 'none';
      panelOpen = false;
    }, 200);
  }

  function fmtAgo(seconds) {
    if (seconds == null) return '—';
    if (seconds < 60) return Math.round(seconds) + 's';
    if (seconds < 3600) return Math.floor(seconds / 60) + 'm ' + Math.floor(seconds % 60) + 's';
    return Math.floor(seconds / 3600) + 'h ' + Math.floor((seconds % 3600) / 60) + 'm';
  }

  function fmtMs(ms) {
    if (ms == null) return '—';
    if (ms < 1000) return ms + 'ms';
    return (ms / 1000).toFixed(1) + 's';
  }

  function renderPanel() {
    // Guards on the SCROLLER, not the shell: it is what gets written to, and
    // an absent scroller with a present shell would throw on the assignment
    // at the end of this function.
    if (!panelScrollEl || !lastSnapshot) return;
    const data = lastSnapshot;
    const i18n = window.PULSE_I18N || {};
    const titles = {
      healthy:  i18n.title_healthy   || 'All systems normal',
      degraded: i18n.title_degraded  || 'Degraded',
      critical: i18n.title_critical  || 'Collector under stress',
      dead:     i18n.title_dead      || 'Collector unreachable',
      warming:  i18n.title_warming   || 'Warming up',
    };

    // IN FLIGHT
    const inflight = (data.active || []).slice(0, 8);
    const inflightHtml = inflight.length
      ? inflight.map(a =>
          `<div class="pulse-row"><span class="pulse-srv">${esc(a.name)}</span>` +
          `<span class="pulse-meta">${esc((a.check || '').toUpperCase())}</span></div>`
        ).join('')
      : `<div class="pulse-row pulse-row--empty">—</div>`;

    // RECENT — last ~30s of pulses from the response (will be empty on
    // since-filtered polls; in steady state events arrive on every poll).
    const evs = (data.events || []).slice(-12).reverse();
    const nowS = data.now || (Date.now() / 1000);
    const recentHtml = evs.length
      ? evs.map(ev => {
          const ago = Math.max(0, nowS - ev.ts);
          const cls = ev.ok ? 'ok' : 'fail';
          const mark = ev.ok ? '✓' : '⚠';
          return `<div class="pulse-row pulse-row--${cls}">` +
                 `<span class="pulse-srv">${esc(ev.server)}</span>` +
                 `<span class="pulse-meta">${mark} ${fmtAgo(ago)} ago — ` +
                 `${esc((ev.check || '').toLowerCase())} ${fmtMs(ev.ms)}</span>` +
                 `</div>`;
        }).join('')
      : `<div class="pulse-row pulse-row--empty">${esc(i18n.no_recent || 'No events in window')}</div>`;

    // SILENT
    const silent = (data.fleet && data.fleet.silent) || [];
    const silentHtml = silent.length
      ? silent.map(s => {
          const meta = s.silent_s == null
            ? (i18n.no_samples_yet || 'no samples yet')
            : ((i18n.silent_for || 'silent for') + ' ' + fmtAgo(s.silent_s));
          return `<div class="pulse-row pulse-row--fail">` +
                 `<span class="pulse-srv">${esc(s.name)}</span>` +
                 `<span class="pulse-meta">${meta}</span></div>`;
        }).join('')
      : `<div class="pulse-row pulse-row--empty">—</div>`;

    // SUBSYSTEMS
    const sub = data.subsystems || {};
    const subRow = (label, key) => {
      const s = sub[key] || {};
      const mark = s.ok ? '✓' : '✕';
      const cls = s.ok ? 'ok' : 'fail';
      const age = s.age_s == null ? '—' : fmtAgo(s.age_s) + ' ago';
      return `<div class="pulse-sub pulse-sub--${cls}">` +
             `<span>${mark} ${esc(label)}</span><span>${age}</span></div>`;
    };

    // Into the persistent scroller, NOT into the panel. Writing to the panel
    // would destroy the scroller — and with it the reader's scroll position —
    // on every poll.
    panelScrollEl.innerHTML = `
      <div class="pulse-panel-head">
        <div class="pulse-panel-title pulse-panel-title--${state}">
          ${esc(titles[state] || titles.warming)}
        </div>
        <div class="pulse-panel-bpm">${Number(data.bpm) || 0} ${esc(i18n.bpm || 'BPM')}</div>
      </div>

      <div class="pulse-section">
        <div class="pulse-section-label">${esc(i18n.in_flight || 'IN FLIGHT')}
          <span class="pulse-section-count">${inflight.length}</span>
        </div>
        ${inflightHtml}
      </div>

      <div class="pulse-section">
        <div class="pulse-section-label">${esc(i18n.recent || 'RECENT')}</div>
        ${recentHtml}
      </div>

      <div class="pulse-section">
        <div class="pulse-section-label">${esc(i18n.silent || 'SILENT')}
          <span class="pulse-section-count">${silent.length}</span>
        </div>
        ${silentHtml}
      </div>

      <div class="pulse-section pulse-section--subs">
        ${subRow(i18n.sub_supervisor || 'Supervisor', 'supervisor')}
        ${subRow(i18n.sub_aggregator || 'Aggregator', 'aggregator')}
        ${subRow(i18n.sub_workers    || 'Workers',    'workers')}
        ${subRow(i18n.sub_periodics  || 'Periodics',  'periodics')}
      </div>

      <a href="/operations#health" class="pulse-cta">
        ${esc(i18n.open_operations || 'Open Operations → System Health')} →
      </a>
    `;
  }

  function esc(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  // ── Bootstrap ───────────────────────────────────────────────────────
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
