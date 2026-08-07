/* Sortable table columns — click a header to sort ascending, click again for
 * descending (two-state cycling: asc <-> desc, no third "unsorted" click).
 * That matches the literal spec ("clicking sorts ascending; clicking again
 * sorts descending") and keeps the affordance predictable — a header never
 * silently reverts to server order underneath the admin.
 *
 * Read docs/OPS-LEARNINGS.md before touching this file, specifically the
 * catalogue entries about tests/verifiers that looked right and asserted
 * nothing (2.2), and the browser-measurement traps (2.4/§4) — a couple of
 * those apply directly here (see the timestamp and htmx notes below).
 *
 * WHY A SEPARATE MODULE (same shape as stepper.js)
 * -------------------------------------------------
 * Progressive enhancement over server-rendered <thead>/<th> markup: a column
 * is sortable IF AND ONLY IF its <th> carries `data-sort="<type>"` in the
 * template. No attribute -> not sortable, by design. An Actions column of
 * icon buttons, a decorative chevron/checkbox/expander column, a sparkline,
 * or a composite chip with no single scalar (reports.html's "Main driver",
 * a badge GROUP like "present on 4 servers") must never grow a sort arrow
 * it can't honestly act on. The per-column decision lives in the template,
 * next to the column it describes, where whoever adds a column next will
 * actually see it — not buried in this file as a guessed heuristic.
 *
 * CONTENT TYPES (data-sort="…") AND WHY EACH NEEDS ITS OWN PARSER
 * -----------------------------------------------------------------
 *   text       - the cell's own text, compared with localeCompare({numeric:
 *                true}) so "Server2" sorts before "Server10" instead of
 *                after "Server10" the way a plain string compare would.
 *   number     - a leading numeric token, comma-stripped. Also used for
 *   percent      percentages ("66.8%") and any other "<number><suffix>"
 *                cell (durations "12.3s", trend rates "-0.012%/d", byte-
 *                free counts) — the suffix is irrelevant once the number is
 *                pulled out, so both dataset values route to the same
 *                parser; the distinct name is documentation for whoever
 *                reads the template. A naive string sort puts "10%" before
 *                "9%"; this doesn't.
 *   bytes      - size strings ("12.3 KB", "512 MB", "1 GB"), normalised to
 *                a raw byte count (1024-based — matches operations.html's
 *                own `size_bytes / 1024` convention) so "900 KB" sorts
 *                before "1 MB" instead of after it (a string compare would
 *                put "1" before "9").
 *   timestamp  - an ABSOLUTE formatted date from the app's own formatTs()
 *                (base.html), which renders in the admin's configured
 *                date_format (DD.MM.YYYY by default) and time_format
 *                (24h/12h). That display string does not sort
 *                chronologically as text: "07.08.2026" < "31.07.2026"
 *                alphabetically, which is backwards. Parsed back to an
 *                epoch millisecond value using the SAME PRISM_TZ config
 *                formatTs() itself reads (declared earlier in base.html —
 *                a bare reference here resolves it via the shared global
 *                lexical scope, the same way every page script already
 *                calls the bare `formatTs(...)` function), so it round-
 *                trips exactly regardless of which format is configured.
 *   status     - a badge's label text, not the raw cell text. Several
 *                status cells append something else after the badge —
 *                servers.html's Status column is "<span>HEALTHY</span>
 *                <span>49s ago</span>" in one <td> — and sorting the whole
 *                concatenation would tie-break on the trailing text instead
 *                of grouping by status. Routes to the same extraction as
 *                `text` (see cellRawText below); kept as a separate dataset
 *                value purely so the template documents intent.
 *
 * relative-time ("49s ago", "2h ago") was REMOVED (audit defect #3 on this
 * branch): zero templates ever set data-sort="relative-time" — it existed
 * purely on spec for a hypothetical future "Last Check" column — and it
 * sorted backwards relative to every other time-flavoured type on this
 * page. parseRelativeTime() returned AGE (bigger number = older, "2h ago"
 * > "49s ago"); parseTimestamp() returns EPOCH (bigger number = newer).
 * The identical ascending-arrow click would therefore have put a
 * relative-time column in the opposite chronological order from every
 * timestamp column, and nothing would have said so — the only tests
 * exercising it were synthetic examples, not a real column, which is
 * exactly how an abstraction with no caller keeps its bugs (docs/OPS-
 * LEARNINGS.md #1). Deleted rather than fixed-to-agree-with-timestamp: a
 * *correct* parser nothing calls is still dead code, and dead code with a
 * non-obvious contract (which direction is "bigger") is a trap for
 * whoever wires up the next time column without re-deriving it from
 * scratch. If a real relative-time column is ever added, give it
 * parseTimestamp()'s epoch-forward convention directly, not this one.
 *
 * THE EXTRACTION RULE — cellRawText()
 * -------------------------------------
 * Prefer the cell's first ELEMENT child's own text; fall back to the
 * cell's whole text when it has no element child (the common case — most
 * cells here are plain interpolated text with no wrapper). This one rule,
 * checked against every real table in this codebase, is what:
 *   - skips servers.html's trailing "49s ago" span after the status badge,
 *   - skips the tag-pill / manage-tag button after a server's name,
 *   - skips an empty <i data-lucide> icon that precedes a label,
 *   - still returns the plain text of a cell that has no markup at all.
 *
 * WHAT COUNTS AS ONE SORTABLE ROW
 * --------------------------------
 * Two shapes exist here and both have to keep their auxiliary rows
 * attached to the row they belong to when the table is reordered:
 *
 *   1. servers.html wraps each server in its OWN <tbody class="server-
 *      group"> holding the visible row plus a hidden hover-detail row and
 *      a hidden test-result row. Sorting reorders whole <tbody> elements —
 *      table.tBodies.length > 1 is the signal.
 *   2. Every other table interleaves an expandable detail <tr> right after
 *      its owning row IN THE SAME <tbody> (reports.html's fleet detail
 *      rows, server_detail.html's event/log detail rows, server_
 *      comparison.html's accordion rows). A "continuation" row is detected
 *      structurally: exactly one <td> carrying a colspan attribute — which
 *      is also how every loading/empty-state placeholder row is shaped —
 *      and it's folded into the group headed by the nearest preceding real
 *      row, so sorting moves the pair together. A placeholder row with no
 *      preceding row (the table is simply empty) becomes its own one-row
 *      group, which the "fewer than 2 groups" guard in applySort() leaves
 *      alone — an empty or single-row table cannot break.
 *
 * PERSISTENCE — sessionStorage, per explicit product decision
 * -----------------------------------------------------------
 * Keyed by a hand-authored `data-sort-table="…"` slug on <thead> — stable
 * across releases, unlike a DOM index, which breaks the moment a table is
 * added above this one. Holds for the tab's session, forgotten when the
 * tab closes (sessionStorage, not localStorage). Restored on load and
 * after `htmx:afterSettle` — deliberately NOT `htmx:afterSwap`: idiomorph's
 * morph is still reconciling the incoming tree at that point, so reordering
 * then fights the swap instead of following it (the general shape of that
 * mistake — acting on an in-progress DOM update instead of the settled
 * one — is docs/OPS-LEARNINGS.md's transition/measurement traps, §2.4/§4,
 * applied to layout instead of style). A stored {col, dir} that no longer
 * matches a sortable column (the table's columns changed between releases)
 * is discarded silently and the table is left in whatever order it
 * rendered in — never throw over a stale sessionStorage key.
 *
 * IDEMPOTENT, SELF-HEALING, AND RE-RUNNABLE, same requirement as
 * stepper.js and for the same reason: several of these tables are rebuilt
 * from scratch outside any htmx swap — rbac.html's ACL table, servers.html's
 * dependency browser, dashboard.html's restart-results table (polled every
 * 30s) via plain fetch().then(html => el.innerHTML = …), and EVERY
 * server_detail.html table via its morphHTML() helper
 * (Idiomorph.morph(container, tmp, {morphStyle: 'innerHTML'})). Neither
 * path fires an htmx event, so a MutationObserver on <body> is what has to
 * notice the change and re-bind headers / re-apply the stored sort.
 *
 * DEFECT 1 (audit, this branch) — why that used to fail silently for some
 * tables and not others: a plain innerHTML replacement always ADDS fresh
 * nodes, which is easy to catch by looking at addedNodes. A morph does
 * not — idiomorph reconciles the incoming tree against whatever is already
 * there, and when an existing <thead data-sort-table="…"> resembles the
 * incoming one closely enough, idiomorph PATCHES it in place: this file's
 * injected <button>/<span class="sort-arrow"> gets swapped back out for
 * plain text, aria-sort gets cleared, and NONE of that is a node being
 * added or removed anywhere except a target already sitting inside a
 * <thead> that itself was never added or removed. The header looks bound
 * one moment and isn't the next, with no addedNodes anywhere to notice.
 * Whether idiomorph reuses or replaces a given container is an
 * implementation detail of its own diffing heuristic — not something this
 * file can special-case per table — so the fix below does not try to
 * recognise "the one bad path"; it removes the assumption that let any
 * unrecognised path go unnoticed.
 *
 * THE FIX is two independent changes, needed together:
 *   1. bindHeader() no longer trusts its own `sortBound` flag as proof a
 *      header is still wired. A dataset flag on a DOM node survives
 *      exactly as long as the node reference does, which an in-place patch
 *      does not disturb even while gutting the node's children/attributes.
 *      It now also checks the button it built, and the aria-sort attribute
 *      it set, are actually still there — cheap when nothing changed (a
 *      couple of extra reads), self-correcting when something external
 *      reset the header, regardless of why.
 *   2. The observer no longer excludes mutations by LOCATION (anything
 *      targeting inside a <thead>, on the theory that only this file's own
 *      code ever writes there). It excludes them by CAUSE instead:
 *      _headerMutating, set around every place this file writes to a
 *      header (bindHeader/setArrow/setAriaSort — mirrors _sorting below),
 *      is what the observer checks now. A <thead>-internal mutation this
 *      file did not just cause — from any source, present or future — now
 *      falls through to a re-bind of that table, the same as a fresh
 *      subtree being added elsewhere. The observed mutation types were
 *      widened from childList-only to also cover attributes and
 *      characterData, since idiomorph's in-place patch can touch either
 *      without any node ever being added or removed.
 * Neither change alone is sufficient: (1) without (2) still needs
 * something to call bindHeader() again after the external change happens;
 * (2) without (1) would re-run bindHeader()/restoreSortState() against a
 * flag that still (falsely) claims "already bound" and do nothing.
 *
 * An interval/idle poll ("is every thead[data-sort-table] th[data-sort]
 * actually bound, right now") was weighed and set aside: a
 * MutationObserver configured for {childList, attributes, characterData,
 * subtree: true} on <body> already sees every DOM write body-wide, by
 * construction — there is no way to change what the page renders without
 * one of those three mutation types firing somewhere in the tree a poll
 * would also have to walk. A poll can only ever lag behind that (if slow)
 * or spend cycles finding nothing (if fast); it cannot see anything the
 * observer's own filtering missed, because both read the same DOM through
 * the same API. What (2) buys that a poll cannot is reacting to *this*
 * filtering gap being closed, not papering over the next one with a timer.
 */
(function () {
  'use strict';

  // ── value extraction ──────────────────────────────────────────────────

  function cellRawText(cell) {
    if (!cell) return '';
    var el = cell.firstElementChild;
    var raw = (el ? el.textContent : cell.textContent) || '';
    return raw.replace(/\s+/g, ' ').trim();
  }

  function parseNumber(text) {
    var m = text.match(/-?[\d,]*\.?\d+/);
    if (!m) return NaN;
    return parseFloat(m[0].replace(/,/g, ''));
  }

  var BYTE_MULT = {
    B: 1,
    KB: 1024,
    MB: 1024 * 1024,
    GB: 1024 * 1024 * 1024,
    TB: Math.pow(1024, 4),
    PB: Math.pow(1024, 5)
  };
  function parseBytes(text) {
    var m = text.match(/(-?[\d,]*\.?\d+)\s*(PB|TB|GB|MB|KB|B)?/i);
    if (!m) return NaN;
    var n = parseFloat(m[1].replace(/,/g, ''));
    if (isNaN(n)) return NaN;
    var unit = (m[2] || 'B').toUpperCase();
    return n * (BYTE_MULT[unit] || 1);
  }

  // Reverses base.html's formatTs() using the same PRISM_TZ config it
  // reads, so a formatted display string sorts chronologically regardless
  // of which date_format/time_format the admin has configured.
  function parseTimestamp(text) {
    if (!text) return NaN;
    var tz = (typeof PRISM_TZ !== 'undefined' && PRISM_TZ) ? PRISM_TZ : null;
    var dateFmt = (tz && tz.date_format) || 'DD.MM.YYYY';
    var timeFmt = (tz && tz.time_format) || '24h';
    var dp, order;
    if (dateFmt === 'YYYY-MM-DD') { dp = /^(\d{4})-(\d{2})-(\d{2})/; order = 'ymd'; }
    else if (dateFmt === 'MM/DD/YYYY') { dp = /^(\d{2})\/(\d{2})\/(\d{4})/; order = 'mdy'; }
    else if (dateFmt === 'DD/MM/YYYY') { dp = /^(\d{2})\/(\d{2})\/(\d{4})/; order = 'dmy'; }
    else { dp = /^(\d{2})\.(\d{2})\.(\d{4})/; order = 'dmy'; }

    var dm = text.match(dp);
    if (!dm) return fallbackTimestamp(text);
    var y, mo, d;
    if (order === 'ymd') { y = dm[1]; mo = dm[2]; d = dm[3]; }
    else if (order === 'mdy') { mo = dm[1]; d = dm[2]; y = dm[3]; }
    else { d = dm[1]; mo = dm[2]; y = dm[3]; }

    var rest = text.slice(dm[0].length);
    var hh = 0, mm = 0, tm;
    if (timeFmt === '12h') {
      tm = rest.match(/(\d{1,2}):(\d{2})\s*(AM|PM)/i);
      if (tm) {
        hh = parseInt(tm[1], 10);
        mm = parseInt(tm[2], 10);
        var ap = tm[3].toUpperCase();
        if (ap === 'PM' && hh !== 12) hh += 12;
        if (ap === 'AM' && hh === 12) hh = 0;
      }
    } else {
      tm = rest.match(/(\d{1,2}):(\d{2})/);
      if (tm) { hh = parseInt(tm[1], 10); mm = parseInt(tm[2], 10); }
    }
    var t = Date.UTC(Number(y), Number(mo) - 1, Number(d), hh, mm);
    return isNaN(t) ? fallbackTimestamp(text) : t;
  }

  function fallbackTimestamp(text) {
    var t = Date.parse(text);
    return isNaN(t) ? NaN : t;
  }

  var TYPE_PARSERS = {
    text: function (cell) { return cellRawText(cell); },
    status: function (cell) { return cellRawText(cell); },
    number: function (cell) { return parseNumber(cellRawText(cell)); },
    percent: function (cell) { return parseNumber(cellRawText(cell)); },
    bytes: function (cell) { return parseBytes(cellRawText(cell)); },
    timestamp: function (cell) { return parseTimestamp(cellRawText(cell)); }
  };

  // ── comparison ─────────────────────────────────────────────────────────

  function isEmptyVal(v) {
    if (typeof v === 'number') return isNaN(v);
    return v === '' || v === null || v === undefined;
  }

  // Empties (unparseable / missing values — "—", "-", "never") always sort
  // last, in BOTH directions — flipping direction toggles real values, not
  // where the gaps go. Ties fall back to original row order (stable sort).
  function compareForSort(a, b, dir) {
    var aEmpty = isEmptyVal(a), bEmpty = isEmptyVal(b);
    if (aEmpty || bEmpty) {
      if (aEmpty && bEmpty) return 0;
      return aEmpty ? 1 : -1;
    }
    var c;
    if (typeof a === 'number' && typeof b === 'number') {
      c = a - b;
    } else {
      c = String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: 'base' });
    }
    return dir === 'asc' ? c : -c;
  }

  // ── row grouping (see module docstring: "WHAT COUNTS AS ONE ROW") ──────

  function isContinuationRow(row) {
    return !!(row.cells && row.cells.length === 1 && row.cells[0].hasAttribute('colspan'));
  }

  function sortUnits(table) {
    if (table.tBodies.length > 1) {
      var units = [];
      for (var i = 0; i < table.tBodies.length; i++) {
        (function (tb) {
          if (!tb.rows.length) return;
          units.push({
            headRow: tb.rows[0],
            place: function () { table.appendChild(tb); }
          });
        })(table.tBodies[i]);
      }
      return units;
    }

    var tbody = table.tBodies[0];
    if (!tbody) return [];
    var groups = [];
    var rows = Array.prototype.slice.call(tbody.rows);
    rows.forEach(function (row) {
      if (isContinuationRow(row) && groups.length) {
        groups[groups.length - 1].rows.push(row);
      } else {
        groups.push({ headRow: row, rows: [row] });
      }
    });
    return groups.map(function (g) {
      return {
        headRow: g.headRow,
        place: function () { g.rows.forEach(function (r) { tbody.appendChild(r); }); }
      };
    });
  }

  // Reordering rows via appendChild() is itself a childList mutation, and
  // the MutationObserver further down exists specifically to notice a
  // table's rows changing and re-apply the stored sort — so an unguarded
  // applySort() feeds its own output straight back into
  // restoreSortState(), which calls applySort() again, forever. Caught
  // live: clicking a header hung the tab instantly. _sorting suppresses
  // the observer's reaction for exactly the mutations THIS function
  // produces. It works across the async boundary because a
  // MutationObserver callback is queued as a microtask the moment a
  // mutation happens (i.e. during the synchronous appendChild loop below,
  // while _sorting is still true), and the Promise.resolve().then() reset
  // is queued strictly AFTER that — same-queue microtasks run in the order
  // they were queued, so the observer always sees _sorting still true for
  // mutations this function caused, and false again for anything after.
  // _headerMutating (declared with the header-binding code further down)
  // is the same flag shape for header-only writes — same guard, same
  // deferred-reset reasoning, just guarding a different set of mutations.
  var _sorting = false;

  function applySort(table, colIndex, type, dir) {
    var units = sortUnits(table);
    if (units.length < 2) return; // 0 or 1 data row(s) — nothing to reorder
    var parse = TYPE_PARSERS[type] || TYPE_PARSERS.text;
    var decorated = units.map(function (u, i) {
      var cell = u.headRow.children[colIndex];
      return { u: u, i: i, val: cell ? parse(cell) : '' };
    });
    decorated.sort(function (x, y) {
      var c = compareForSort(x.val, y.val, dir);
      return c !== 0 ? c : (x.i - y.i);
    });

    // DEFECT 2 (audit, this branch): the freshly-computed order can equal
    // the order the table is already in — most commonly because this call
    // was triggered by a mutation that changed cell CONTENT but not the
    // sort KEY (servers.html rewrites 29 status cells every 15s while the
    // table stays sorted by name/host/port, none of which that rewrite
    // touches). appendChild() on a node that is already the table's last
    // child is still a real detach+re-insert — a childList mutation, a
    // forced reflow, rows visibly shifting under the pointer. Measured
    // live before this check existed: 58 tbody moves every 15s across a
    // 29-row fleet table, with the resulting order identical to the order
    // before, every single time. Comparing the sorted position array
    // against the original index order is O(n) and touches no DOM at all;
    // skip the reorder entirely when it would be a no-op.
    var alreadyInOrder = decorated.every(function (d, pos) { return d.i === pos; });
    if (alreadyInOrder) return;

    _sorting = true;
    try {
      decorated.forEach(function (d) { d.u.place(); });
    } finally {
      // try/finally is insurance, not a reproduction of an observed bug —
      // every throwable step here runs before _sorting is even set, so
      // there is no known way to actually leave place() mid-loop with an
      // exception. But if one ever did, the un-guarded version left
      // _sorting stuck true forever (the reset line below it would simply
      // never run), silently disabling every future restoreSortState()
      // with no error anywhere. finally only guarantees the reset gets
      // SCHEDULED even on a throw — it stays a deferred microtask, not an
      // immediate reset, for exactly the ordering reason explained above.
      Promise.resolve().then(function () { _sorting = false; });
    }
  }

  // ── header UI: arrow affordance + aria-sort + keyboard ──────────────────
  //
  // A real <button> (not a click handler on the <th> itself) so Enter/Space
  // activate it for free — no separate keydown handling needed — while the
  // <th> keeps its native columnheader semantics and carries aria-sort.

  var ARROW_BOTH = '<svg viewBox="0 0 9 11" aria-hidden="true" focusable="false"><path d="M4.5 0 8 4H1z" fill="currentColor"/><path d="M4.5 11 1 7h7z" fill="currentColor"/></svg>';
  var ARROW_UP = '<svg viewBox="0 0 9 6" aria-hidden="true" focusable="false"><path d="M4.5 0 9 6H0z" fill="currentColor"/></svg>';
  var ARROW_DOWN = '<svg viewBox="0 0 9 6" aria-hidden="true" focusable="false"><path d="M4.5 6 0 0h9z" fill="currentColor"/></svg>';

  // Set around every DOM write this file makes to a <th> or its
  // descendants — bindHeader() building the button/arrow, setArrow()
  // swapping the icon, setAriaSort() below — mirroring _sorting above,
  // for the same reason and with the same deferred-reset mechanics. The
  // MutationObserver further down checks this (alongside _sorting) instead
  // of trying to recognise "a thead-internal mutation" by location — see
  // DEFECT 1 in the module docstring for why location stopped being a
  // reliable signal.
  var _headerMutating = false;

  // Visible before any click (ARROW_BOTH, text-faint) so the affordance is
  // there without interaction, then switches to a single brand-coloured
  // chevron once this column is the active sort.
  function setArrow(th, state) {
    var arrow = th.querySelector('.sort-arrow');
    if (!arrow) return;
    // Idempotent: restoreSortState() calls this unconditionally for every
    // sortable header on every invocation (only one of them actually
    // changed). Without this check every call replaces all their <svg>
    // children with byte-identical new ones — a real DOM mutation the
    // MutationObserver further down could not tell apart from new data.
    if (arrow.dataset.state === state) return;
    _headerMutating = true;
    try {
      arrow.dataset.state = state;
      if (state === 'asc') {
        arrow.innerHTML = ARROW_UP;
        arrow.className = 'sort-arrow ml-1 inline-flex shrink-0 w-[9px] text-brand';
      } else if (state === 'desc') {
        arrow.innerHTML = ARROW_DOWN;
        arrow.className = 'sort-arrow ml-1 inline-flex shrink-0 w-[9px] text-brand';
      } else {
        arrow.innerHTML = ARROW_BOTH;
        arrow.className = 'sort-arrow ml-1 inline-flex shrink-0 w-[9px] text-faint';
      }
    } finally {
      Promise.resolve().then(function () { _headerMutating = false; });
    }
  }

  // Guarded the same way as setArrow() — see _headerMutating above. Also
  // idempotent against the current value: restoreSortState()'s sibling
  // loop calls this unconditionally for every sortable column on every
  // invocation (most of them unchanged), and setAttribute() queues a
  // mutation record even when the value being set equals the value
  // already there — skipping the no-op write keeps a heal/restore pass
  // that changed nothing from generating any mutation at all.
  function setAriaSort(th, value) {
    if (th.getAttribute('aria-sort') === value) return;
    _headerMutating = true;
    try {
      th.setAttribute('aria-sort', value);
    } finally {
      Promise.resolve().then(function () { _headerMutating = false; });
    }
  }

  // Marker class on the injected <button>, used (not just styled) by the
  // re-entry guard below: th.dataset.sortBound is a flag on the <th> node
  // itself, which survives exactly as long as that node reference does —
  // and an idiomorph in-place patch (DEFECT 1) can gut a bound header's
  // children/attributes without the node itself ever being replaced, so
  // the flag alone can say "already bound" about a header that currently
  // has no button, no arrow and no aria-sort at all. Checking the button
  // is still actually there makes bindHeader() self-healing: cheap when
  // nothing changed, correct when something external reset the header.
  var SORT_TOGGLE_CLASS = 'sort-toggle';

  function isBound(th) {
    return th.dataset.sortBound === '1' &&
      th.hasAttribute('aria-sort') &&
      !!th.querySelector(':scope > button.' + SORT_TOGGLE_CLASS);
  }

  function bindHeader(th) {
    if (isBound(th)) return;
    _headerMutating = true;
    try {
      th.dataset.sortBound = '1';
      // setAriaSort()'s own idempotency check (compares against
      // getAttribute(), which is null when the attribute is simply
      // missing) covers "give it a default" and "leave an existing value
      // alone" in one call — no separate hasAttribute() check needed.
      setAriaSort(th, 'none');

      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = SORT_TOGGLE_CLASS + ' inline-flex items-center gap-1 bg-transparent border-0 p-0 m-0 cursor-pointer ' +
        'focus-visible:ring-2 focus-visible:ring-brand focus-visible:outline-none rounded-sm';
      // Move the existing header content — plain text, Jinja-rendered
      // text, or (on a re-bind after an external reset) whatever content
      // is currently sitting in the <th> — into the button rather than
      // re-typing it, so translations / Jinja output are preserved
      // exactly no matter which pass populated them.
      while (th.firstChild) btn.appendChild(th.firstChild);

      var arrow = document.createElement('span');
      arrow.className = 'sort-arrow ml-1 inline-flex shrink-0 w-[9px] text-faint';
      arrow.innerHTML = ARROW_BOTH;
      btn.appendChild(arrow);
      th.appendChild(btn);

      btn.addEventListener('click', function () {
        var table = th.closest('table');
        if (!table) return;
        var row = th.parentElement;
        var colIndex = Array.prototype.indexOf.call(row.children, th);
        var type = th.getAttribute('data-sort');
        var dir = th.getAttribute('aria-sort') === 'ascending' ? 'desc' : 'asc';

        Array.prototype.forEach.call(row.children, function (sib) {
          if (sib !== th && sib.hasAttribute('data-sort')) {
            setAriaSort(sib, 'none');
            setArrow(sib, 'none');
          }
        });

        applySort(table, colIndex, type, dir);
        setAriaSort(th, dir === 'asc' ? 'ascending' : 'descending');
        setArrow(th, dir);
        saveSortState(table, colIndex, dir);
      });
    } finally {
      Promise.resolve().then(function () { _headerMutating = false; });
    }
  }

  // ── sessionStorage persistence ───────────────────────────────────────

  function storageKey(tableId) { return 'prism-table-sort:' + tableId; }

  function saveSortState(table, colIndex, dir) {
    var thead = table.tHead;
    var tableId = thead && thead.getAttribute('data-sort-table');
    if (!tableId) return;
    try {
      sessionStorage.setItem(storageKey(tableId), JSON.stringify({ col: colIndex, dir: dir }));
    } catch (e) { /* storage disabled/full — sort still applies, just won't persist */ }
  }

  // Silently drops (and forgets) a stored key that no longer names a real
  // sortable column, rather than throwing — a table's columns can change
  // between releases and a stale key must not blank the page for whoever
  // still has last week's sessionStorage entry.
  function restoreSortState(table) {
    var thead = table.tHead;
    var tableId = thead && thead.getAttribute('data-sort-table');
    if (!tableId) return;
    var raw;
    try { raw = sessionStorage.getItem(storageKey(tableId)); } catch (e) { return; }
    if (!raw) return;

    var state;
    try { state = JSON.parse(raw); } catch (e) {
      try { sessionStorage.removeItem(storageKey(tableId)); } catch (e2) { /* ignore */ }
      return;
    }
    if (!state || typeof state.col !== 'number' || (state.dir !== 'asc' && state.dir !== 'desc')) {
      try { sessionStorage.removeItem(storageKey(tableId)); } catch (e3) { /* ignore */ }
      return;
    }

    var headerRow = thead.rows[thead.rows.length - 1];
    var th = headerRow && headerRow.children[state.col];
    if (!th || !th.hasAttribute('data-sort')) {
      try { sessionStorage.removeItem(storageKey(tableId)); } catch (e4) { /* ignore */ }
      return; // stale key — fall back to natural (server-rendered) order
    }

    bindHeader(th); // idempotent and self-healing — see module docstring, DEFECT 1
    applySort(table, state.col, th.getAttribute('data-sort'), state.dir);

    Array.prototype.forEach.call(headerRow.children, function (sib) {
      if (!sib.hasAttribute('data-sort')) return;
      bindHeader(sib);
      var active = sib === th;
      setAriaSort(sib, active ? (state.dir === 'asc' ? 'ascending' : 'descending') : 'none');
      setArrow(sib, active ? state.dir : 'none');
    });
  }

  // ── scanning / wiring ────────────────────────────────────────────────

  function scan(root) {
    (root || document).querySelectorAll('thead[data-sort-table] th[data-sort]').forEach(bindHeader);
  }

  function restoreAll(root) {
    var theads = (root || document).querySelectorAll('thead[data-sort-table]');
    Array.prototype.forEach.call(theads, function (thead) {
      var table = thead.closest('table');
      if (table) restoreSortState(table);
    });
  }

  function initial() {
    scan();
    restoreAll();
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initial);
  } else {
    initial();
  }

  // htmx morph-swapped panels (~5s refresh on some pages). Binding new
  // headers is safe at afterSwap; re-applying a stored sort is NOT — the
  // morph can still be reconciling the incoming tree, so that happens only
  // at afterSettle (see module docstring, "PERSISTENCE").
  document.body.addEventListener('htmx:afterSwap', function (e) {
    scan(e.target);
  });
  document.body.addEventListener('htmx:afterSettle', function (e) {
    var root = (e.target && e.target.nodeType === 1) ? e.target : document;
    scan(root);
    restoreAll(root);
  });

  // Tables rebuilt outside any htmx swap — via plain fetch().then(html =>
  // el.innerHTML = …) (rbac.html's ACL table, servers.html's dependency
  // browser, dashboard.html's 30s-polled restart-results table) or via
  // server_detail.html's morphHTML()/Idiomorph — never fire an htmx event
  // at all, so this observer is the only thing that notices those. See
  // the module docstring, "DEFECT 1", for why the filtering below reacts
  // to CAUSE (_sorting / _headerMutating) rather than to WHERE a mutation
  // landed, and why that replaced the previous thead-location exclusion.
  function queueTable(list, table) {
    if (table && table.tHead && table.tHead.hasAttribute('data-sort-table') && list.indexOf(table) === -1) {
      list.push(table);
    }
  }

  var _mo = new MutationObserver(function (mutations) {
    // A mutation this file just caused — writing to a <tbody> (_sorting)
    // or to a header (_headerMutating) — must not be read back as
    // "something else changed this table". That feedback loop is what
    // hung the tab solid before either flag existed (see both flags'
    // declarations for the microtask-ordering argument that makes this
    // check reliable across the async boundary).
    if (_sorting || _headerMutating) return;
    var freshRoots = [];
    var tablesToRestore = [];
    for (var i = 0; i < mutations.length; i++) {
      var m = mutations[i];
      var target = m.target;

      // Half of DEFECT 1's fix: a mutation whose TARGET already sits
      // inside a sortable table — an attribute changing, a text node's
      // data changing, a child being added or removed anywhere in the
      // table, INCLUDING inside its <thead> — means that table's header
      // or rows may need (re)binding / re-sorting. This no longer
      // excludes anything by location: the _headerMutating check above
      // already accounts for this file's own header writes, so a
      // <thead>-internal mutation that reaches this point came from
      // somewhere else (an idiomorph in-place patch today; anything else
      // tomorrow) and is exactly the case that must not be discarded.
      var ownerTable = target.nodeType === 1 ? target.closest('table')
        : (target.parentElement && target.parentElement.closest('table'));
      queueTable(tablesToRestore, ownerTable);

      // The other half: a brand new sortable table (or its <thead>)
      // appearing under a container that was not itself part of any table
      // a moment ago — the first-ever render of a JS-built section into
      // an empty <div>. The mutation's TARGET here is the container, not
      // a table, so the check above doesn't see it; only the newly added
      // node's own subtree does.
      var added = m.addedNodes;
      for (var j = 0; j < added.length; j++) {
        var node = added[j];
        if (node.nodeType !== 1) continue;
        if (node.matches('thead[data-sort-table]') || (node.querySelector && node.querySelector('thead[data-sort-table]'))) {
          freshRoots.push(node);
        }
        queueTable(tablesToRestore, node.closest ? node.closest('table') : null);
      }
    }
    freshRoots.forEach(function (root) { scan(root); restoreAll(root); });
    // scan() and restoreSortState() are both idempotent — bindHeader()
    // self-heals (see isBound()) and applySort() is a no-op when the
    // table is already in the target order (DEFECT 2) — so re-running
    // them on a table that, on inspection, did not actually need it costs
    // a handful of querySelectorAll/getAttribute calls and nothing else.
    // That is what makes reacting to every mutation touching a sortable
    // table (rather than trying to pre-filter to "the ones that matter")
    // affordable.
    tablesToRestore.forEach(function (table) {
      scan(table);
      restoreSortState(table);
    });
  });
  _mo.observe(document.body, { childList: true, subtree: true, attributes: true, characterData: true });

  window.__prismBindTableSort = scan;
  // Belt-and-suspenders hook for any direct caller that wants to force a
  // rebind + stored-sort reapply immediately after its own DOM update
  // (e.g. right after a morphHTML() call) rather than waiting for the
  // MutationObserver's microtask — scan() alone does not reapply a stored
  // sort. Both are idempotent, so calling this speculatively costs little.
  window.__prismHealTableSort = function (root) { scan(root); restoreAll(root); };
})();
