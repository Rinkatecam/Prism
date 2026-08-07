/* Violet stepper buttons for <input type="number">.
 *
 * WHY THIS EXISTS
 * ---------------
 * The native spin button cannot be recoloured. `accent-color` does not reach
 * it; a `filter` tints the whole spin-button box rather than the arrows in
 * it; and `-webkit-appearance: none` plus a drawn background does recolour
 * them but takes click-to-increment with it, because the widget IS the
 * behaviour. Every other affordance in the app is brand violet and this one
 * stayed browser grey.
 *
 * So the widget is hidden in app.css and the behaviour rebuilt here. That
 * also fixes three things the native one never did: the buttons are real
 * <button>s so they are reachable by keyboard, they fire `input` and
 * `change` so the existing listeners see the edit, and they are disabled at
 * the ends of the range instead of silently doing nothing.
 *
 * IDEMPOTENT AND RE-RUNNABLE. htmx swaps whole panels on every refresh
 * (~5s under collector v2), so this runs again after each swap and must not
 * wrap an input twice — hence the `data-stepper` marker.
 */
(function () {
  'use strict';

  var UP = '<svg viewBox="0 0 9 6" aria-hidden="true"><path d="M4.5 0 9 6H0z" fill="currentColor"/></svg>';
  var DOWN = '<svg viewBox="0 0 9 6" aria-hidden="true"><path d="M4.5 6 0 0h9z" fill="currentColor"/></svg>';

  function step(input, dir) {
    if (input.disabled || input.readOnly) return;
    // stepUp/stepDown throw on a non-numeric value; seed it from min or 0
    // first so the first click on an empty field does something sensible.
    if (input.value === '') input.value = input.min !== '' ? input.min : 0;
    try {
      dir > 0 ? input.stepUp() : input.stepDown();
    } catch (e) {
      return;
    }
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
  }

  function refresh(input, up, down) {
    var v = parseFloat(input.value);
    var max = parseFloat(input.max);
    var min = parseFloat(input.min);
    up.disabled = !isNaN(max) && !isNaN(v) && v >= max;
    down.disabled = !isNaN(min) && !isNaN(v) && v <= min;
    up.style.visibility = up.disabled ? 'hidden' : '';
    down.style.visibility = down.disabled ? 'hidden' : '';
  }

  function attach(input) {
    if (input.dataset.stepper === 'on') return;
    input.dataset.stepper = 'on';

    var wrap = document.createElement('span');
    wrap.className = 'stepper';
    // 7 of the 16 number fields are `w-full`. An inline-flex wrapper around
    // one collapses it to its content width, so the wrapper has to carry the
    // full-width intent too — otherwise a threshold input that filled its
    // grid cell suddenly measures 60px.
    if (/(^|\s)w-full(\s|$)/.test(input.className)) wrap.className += ' stepper--block';
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);

    var btns = document.createElement('span');
    btns.className = 'stepper-btns';

    var up = document.createElement('button');
    var down = document.createElement('button');
    [[up, UP, 1, 'Increase'], [down, DOWN, -1, 'Decrease']].forEach(function (spec) {
      var b = spec[0];
      b.type = 'button';           // never submits the surrounding form
      b.className = 'stepper-btn';
      b.innerHTML = spec[1];
      b.tabIndex = -1;             // the field itself is the tab stop; arrow
                                   // keys already step it
      b.setAttribute('aria-label', spec[3] + ' ' + (input.getAttribute('aria-label') || 'value'));
      b.addEventListener('click', function (ev) {
        ev.preventDefault();
        step(input, spec[2]);
        refresh(input, up, down);
      });
      btns.appendChild(b);
    });

    wrap.appendChild(btns);
    input.addEventListener('input', function () { refresh(input, up, down); });
    refresh(input, up, down);
  }

  function scan(root) {
    (root || document).querySelectorAll('input[type="number"]').forEach(attach);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { scan(); });
  } else {
    scan();
  }

  // htmx replaces whole panels; anything it inserts needs wiring too.
  document.body.addEventListener('htmx:afterSwap', function (e) {
    scan(e.target);
  });
  document.body.addEventListener('htmx:afterSettle', function (e) {
    scan(e.target);
  });

  window.__prismBindSteppers = scan;
})();
