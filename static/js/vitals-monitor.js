/* Prism — Estate Vitals Monitor
 * ─────────────────────────────────────────────────────────────────────
 * The ECG trace in the centre of the dashboard's quadrant. Answers ONE
 * question: is the estate healthy? Tempo encodes severity — a slow steady
 * beat when everything is fine, faster and harder as it degrades, and a
 * flat silent line when nothing is up at all.
 *
 * NOT the topbar pulse. `pulse-monitor.js` answers "is the collector
 * alive", polls its own endpoint, and stamps one beat per real check event.
 * This one is a status display driven by aggregate counts, and the two are
 * deliberately different signals: Prism can be perfectly healthy while the
 * estate is on fire, and vice versa.
 *
 * WHERE THE DATA COMES FROM, and why there is no second endpoint:
 *
 *   /partials/vitals  →  <div data-vitals-state data-vitals-severity=…>
 *                              ↑ swapped by htmx on every prismRefresh
 *   htmx:afterSettle  →  readState()  →  the numbers AND the tempo
 *
 * The counts and the tempo come from the same render, so the trace cannot
 * beat fast while the card reads 100%. A dedicated /api/vitals would have
 * been a second source of truth, a second request per refresh, and a new
 * way for the two to disagree. `afterSettle` rather than `afterSwap`
 * because idiomorph is still reconciling the incoming tree at swap time —
 * the same reason table-sort.js waits for settle.
 *
 * The CIRCLE ITSELF is static markup in dashboard.html that no swap
 * touches. It has to be: `morph:innerHTML` replaces the elements it swaps,
 * so a <canvas> inside the fragment would be discarded and rebuilt every
 * few seconds and the beat would never get past its first cycle.
 *
 * REDUCED MOTION — the exemption was considered and declined; the argument
 * is written out in full in the reduced-motion block of app.css. Short
 * version: the tempo is real information, but it is not the ONLY carrier
 * of that information (percentage, count and a state word all sit inside
 * the same circle), and a still status display asserts nothing false the
 * way a frozen spinner does. So: draw the waveform ONCE and never animate.
 * Note that no stylesheet rule can enforce this — a canvas painted from
 * rAF is invisible to `animation-duration` — which is why it is a branch
 * here and a test in tests/test_design_vitals.py.
 *
 * ENERGY:
 *   • rAF stops entirely when the tab is hidden (visibilitychange).
 *   • rAF never starts for a flat or idle estate — a straight line does
 *     not need animating, and "everything offline" is meant to read as
 *     silent.
 *   • ~30 FPS is the INTENT, and it is unverifiable in an automated browser
 *     pane: raw requestAnimationFrame there fires 0.7 times per second with
 *     gaps up to 8.9 seconds, even with document.visibilityState ===
 *     "visible". Measuring this loop's frame rate in that pane reported 3 FPS
 *     and meant nothing — the instrument was the throttle. HANDOFF §2 records
 *     that rAF does not fire while the tab is HIDDEN; this is the same trap
 *     with the tab visible, and it is worth knowing before someone reads a
 *     low frame rate as a bug in this file.
 *   • the frame is a full redraw of a ~250x40 CSS-pixel
 *     canvas: five gaussians per column, no getImageData. pulse-monitor.js
 *     scrolls its strip with getImageData/putImageData because it stamps
 *     unrepeatable real events and must not redraw them; this waveform is
 *     a pure function of (time, bpm), so recomputing it is both cheaper
 *     than a pixel readback and immune to the GPU sync stall that made
 *     `willReadFrequently` necessary over there.
 */
(function () {
  'use strict';

  // ── Tunables ────────────────────────────────────────────────────────
  const FRAME_MS = 33;          // ~30 FPS
  const SECONDS_VISIBLE = 2.5;  // canvas width = 2.5s of trace at any tempo,
                                // so a faster heart genuinely shows more
                                // beats — same as paper speed on a monitor.
  const LINE_W = 2;

  // Samples per CSS pixel when tracing the waveform. NOT one per column, and
  // this is the whole difference between an ECG and a lumpy sine.
  //
  // The QRS complex is genuinely narrow — real proportions, ~8% of the beat
  // for the whole complex and a fraction of that for the R spike itself. This
  // canvas is ~173 CSS px at 1280, which at 96 bpm over 2.5s is ~66px per
  // beat, so the R spike's sigma lands at well under one pixel. Sampled once
  // per column, the peak falls between samples on most frames and the tall
  // spike simply is not drawn — measured before this existed: 8 of 31 rows
  // inked, i.e. the trace was mostly baseline with the occasional bump.
  // Supersampling puts a vertex ON the peak, so the polyline draws it as the
  // 1-2px-wide vertical stroke an ECG spike actually looks like at this size.
  //
  // Cost: 6 x 173 x 5 gaussians per frame. Measured in the running app at
  // this canvas's real size, 300 iterations: 0.183 ms for the drawing work,
  // plus 0.0033 ms for the getBoundingClientRect() that paint() does each
  // frame via resize() — ~0.186 ms against a 33 ms budget.
  //
  // Be precise about what that number is: the 0.183 ms was timed over a
  // REIMPLEMENTATION of the loop below, at the same size and sample rate,
  // not over paint() itself, because paint() cannot be called in isolation
  // while the loop owns the canvas. An earlier version of this comment said
  // "measured over 300 paints", which described a measurement nobody took.
  // The layout read is timed separately and added, so the total is measured
  // rather than assumed — but it is a reconstruction, not an observation of
  // the real function.
  //
  // Raising this number is cheap; the reason not to is that beyond ~6 the
  // extra vertices land inside the same pixel and change nothing on screen.
  const SAMPLES_PER_PX = 6;

  // Amplitude by severity: "the worse it gets, the faster AND HARDER it
  // beats". Tempo alone reads as a nervous version of the same trace;
  // height is what makes it read as strain.
  const AMPLITUDE = { calm: 0.70, elevated: 0.85, urgent: 1.0 };

  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // ── State ───────────────────────────────────────────────────────────
  let core = null, canvas = null, ctx = null;
  let percentEl = null, countEl = null, stateEl = null;
  let labels = {};
  let severity = 'calm';
  let bpm = 60;
  let rafHandle = null;
  let lastDrawAt = 0;
  let cssW = 0, cssH = 0, dpr = 1;
  let drawComplained = false;
  let themeObserver = null;

  // ── Init ────────────────────────────────────────────────────────────
  function init() {
    core = document.getElementById('estate-vitals');
    if (!core) return;   // not the dashboard

    canvas = core.querySelector('canvas');
    percentEl = core.querySelector('[data-vitals-percent-out]');
    countEl = core.querySelector('[data-vitals-count-out]');
    stateEl = core.querySelector('[data-vitals-state-out]');

    try {
      labels = JSON.parse(core.getAttribute('data-vitals-labels') || '{}');
    } catch (e) { labels = {}; }

    // First paint's values, server-rendered. Read from the circle's own
    // attributes rather than waiting for the partial: the region below is
    // still a skeleton at this point, and blanking a readout the server
    // already filled in would be a step backwards.
    severity = core.getAttribute('data-severity') || 'calm';
    bpm = parseInt(core.getAttribute('data-bpm'), 10) || 0;

    if (canvas) {
      ctx = canvas.getContext('2d');
      resize();
    }

    // The partial's state, once it lands and after every refresh.
    document.body.addEventListener('htmx:afterSettle', readState);
    readState();

    // Theme toggle changes the stroke colour (it is `currentColor`, and the
    // token behind it has a per-theme value). A paused trace — flat, idle,
    // or reduced-motion — would otherwise keep the old theme's ink until
    // something else happened to repaint it.
    themeObserver = new MutationObserver(function () {
      if (!ctx) return;
      if (rafHandle == null) paint(now());
    });
    themeObserver.observe(document.documentElement,
      { attributes: true, attributeFilter: ['class'] });

    window.addEventListener('resize', onResize);

    document.addEventListener('visibilitychange', function () {
      if (document.hidden) stopRaf();
      else start();
    });

    start();
  }

  function now() { return performance.now(); }

  // ── Geometry ────────────────────────────────────────────────────────
  //
  // The canvas is sized by `clamp(…vw…)` in CSS, so its pixel size is a
  // function of the viewport and has to be re-read rather than assumed.
  function resize() {
    if (!canvas || !ctx) return false;
    const rect = canvas.getBoundingClientRect();
    const w = Math.max(1, Math.round(rect.width));
    const h = Math.max(1, Math.round(rect.height));
    const ratio = window.devicePixelRatio || 1;
    if (w === cssW && h === cssH && ratio === dpr) return false;
    cssW = w; cssH = h; dpr = ratio;
    canvas.width = Math.round(w * ratio);
    canvas.height = Math.round(h * ratio);
    // setTransform, not scale: scale() multiplies onto whatever transform is
    // already there, so re-running it on every resize compounds.
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    return true;
  }

  function onResize() {
    if (resize() && rafHandle == null) paint(now());
  }

  // ── State from the swapped partial ──────────────────────────────────
  function readState() {
    const src = document.querySelector('[data-vitals-state]');
    if (!src) return;   // still a skeleton, or the region is switched off

    const nextSeverity = src.getAttribute('data-vitals-severity') || severity;
    const nextBpm = parseInt(src.getAttribute('data-vitals-bpm'), 10);
    const percent = src.getAttribute('data-vitals-percent');
    const ok = src.getAttribute('data-vitals-ok');
    const monitored = src.getAttribute('data-vitals-monitored');

    const changed = nextSeverity !== severity;
    severity = nextSeverity;
    if (!isNaN(nextBpm)) bpm = nextBpm;

    // An empty percent means nothing is monitored — the honest readout is a
    // dash, not 0%.
    if (percentEl) percentEl.textContent = percent === '' || percent == null
      ? '—' : percent + '%';
    if (countEl && ok != null && monitored != null) {
      countEl.textContent = ok + '/' + monitored;
    }
    if (stateEl) {
      const label = labels[severity];
      if (label) stateEl.textContent = label;
    }

    // The attributes track state unconditionally; only the class swap is
    // guarded, because rewriting className every 5s for no change is a
    // pointless mutation. `data-bpm` used to be inside the guard, so it went
    // stale whenever the tempo moved without the severity moving — nothing
    // reads it after init today, which is exactly why it would have gone
    // unnoticed as a lie about live state.
    core.setAttribute('data-severity', severity);
    core.setAttribute('data-bpm', String(bpm));
    if (changed) {
      core.className = core.className.replace(/\bvitals-core--\S+/g, '').trim()
        + ' vitals-core--' + severity;
    }

    // Severity decides whether there is anything to animate at all, so a
    // transition into or out of flat/idle has to re-evaluate the loop.
    start();
  }

  // ── The loop ────────────────────────────────────────────────────────
  function beating() { return bpm > 0; }

  function start() {
    if (!ctx) return;
    // Every reason not to run, in one place. Each of them still PAINTS —
    // stopping the sweep must never leave an empty canvas, which reads as a
    // region that failed to load rather than as a monitor at rest.
    if (reduceMotion || !beating() || document.hidden) {
      stopRaf();
      paint(now());
      return;
    }
    startRaf();
  }

  function startRaf() {
    if (rafHandle != null) return;
    lastDrawAt = now() - FRAME_MS;
    const tick = function (t) {
      rafHandle = requestAnimationFrame(tick);
      if (t - lastDrawAt < FRAME_MS) return;
      lastDrawAt = t;
      try {
        paint(t);
      } catch (e) {
        if (!drawComplained) {
          console.warn('vitals draw failed', e);
          drawComplained = true;
        }
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

  // ── The waveform ────────────────────────────────────────────────────
  //
  // A gaussian per deflection, evaluated over the beat's phase u ∈ [0,1).
  // Centres and widths are shaped to read as PQRST at a glance rather than
  // to be clinically accurate: a low P bump, a narrow QRS with the R spike
  // dominating, and a broad T.
  const WAVE = [
    // centre, width, amplitude (negative = downward deflection)
    [0.140, 0.024,  0.16],   // P
    [0.226, 0.010, -0.11],   // Q
    [0.250, 0.008,  1.00],   // R
    [0.286, 0.013, -0.30],   // S
    [0.420, 0.038,  0.26]    // T
  ];

  function wave(u) {
    let y = 0;
    for (let i = 0; i < WAVE.length; i++) {
      const c = WAVE[i][0], w = WAVE[i][1], a = WAVE[i][2];
      let d = u - c;
      // Wrap so a deflection near u=0 is continuous across the beat
      // boundary. Without this the P wave of each beat is clipped flat on
      // its left-hand side and the trace looks like it is missing samples.
      if (d > 0.5) d -= 1;
      else if (d < -0.5) d += 1;
      y += a * Math.exp(-(d * d) / (2 * w * w));
    }
    return y;
  }

  function paint(t) {
    if (!ctx) return;
    resize();
    const w = cssW, h = cssH;
    const mid = h / 2;
    ctx.clearRect(0, 0, w, h);

    // currentColor, resolved. The severity -> token mapping lives in
    // app.css's .vitals-core--* modifiers and is read back here, so there
    // is one mapping rather than a copy in this file that can drift from it.
    const ink = window.getComputedStyle(canvas).color;
    ctx.strokeStyle = ink;
    ctx.lineWidth = LINE_W;
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';

    if (!beating()) {
      // Flatline: silent, and visibly dead. No sweep dot either — a moving
      // dot on a flat line still reads as "alive but quiet", which is the
      // opposite of what this state means.
      ctx.beginPath();
      ctx.moveTo(0, mid);
      ctx.lineTo(w, mid);
      ctx.stroke();
      return;
    }

    const amp = (AMPLITUDE[severity] || AMPLITUDE.calm) * (mid - LINE_W);
    const beatsPerSecond = bpm / 60;
    // x = w is "now"; x = 0 is SECONDS_VISIBLE ago. A static frame for a
    // reduced-motion user pins the phase to 0 so the trace is the same
    // every time it is painted rather than depending on when the page
    // happened to load.
    const nowBeats = reduceMotion ? 0 : (t / 1000) * beatsPerSecond;

    ctx.beginPath();
    const step = 1 / SAMPLES_PER_PX;
    for (let x = 0; x <= w; x += step) {
      const secondsAgo = (w - x) / w * SECONDS_VISIBLE;
      let u = (nowBeats - secondsAgo * beatsPerSecond) % 1;
      if (u < 0) u += 1;
      const y = mid - wave(u) * amp;
      if (x === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.stroke();

    // The leading edge, the way a monitor marks where the sweep is. Skipped
    // when nothing is moving, because a bright dot at a fixed point is not
    // a sweep position, it is a smudge.
    if (!reduceMotion) {
      let u = nowBeats % 1;
      if (u < 0) u += 1;
      ctx.fillStyle = ink;
      ctx.beginPath();
      ctx.arc(w, mid - wave(u) * amp, LINE_W * 1.4, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  // ── Bootstrap ───────────────────────────────────────────────────────
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
