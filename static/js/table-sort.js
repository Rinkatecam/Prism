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
 * relative-time ("49s ago", "2h ago") has a parser below too, exercised in
 * tests/test_table_sort.py against exactly those examples, but no column
 * in the current markup carries relative time on its own — the only place
 * it appears is fused into servers.html's Status cell above, and status
 * text (not staleness) is what "Status" should sort by. Shipped anyway so
 * a future standalone "Last Check" column has a correct type ready.
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
 * IDEMPOTENT AND RE-RUNNABLE, same requirement as stepper.js and for the
 * same reason: several of these tables are rebuilt from scratch by plain
 * fetch().then(html => el.innerHTML = …) with NO htmx involved at all —
 * rbac.html's ACL table, every server_detail.html table, servers.html's
 * dependency browser, dashboard.html's restart-results table (polled every
 * 30s). There is no htmx event to hook for those, so a MutationObserver on
 * <body> is what notices the replacement and re-binds headers / re-applies
 * the stored sort. htmx pages get both: the observer AND the explicit
 * afterSettle hook, belt and suspenders — applying the same sort twice is
 * a no-op, so there is no double-apply hazard.
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

  var RELTIME_MULT = { s: 1, m: 60, h: 3600, d: 86400 };
  function parseRelativeTime(text) {
    var m = text.match(/(-?\d+(?:\.\d+)?)\s*(s|m|h|d)\b/i);
    if (!m) return NaN;
    var mult = RELTIME_MULT[m[2].toLowerCase()];
    return parseFloat(m[1]) * mult;
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
    'relative-time': function (cell) { return parseRelativeTime(cellRawText(cell)); },
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
    _sorting = true;
    decorated.forEach(function (d) { d.u.place(); });
    Promise.resolve().then(function () { _sorting = false; });
  }

  // ── header UI: arrow affordance + aria-sort + keyboard ──────────────────
  //
  // A real <button> (not a click handler on the <th> itself) so Enter/Space
  // activate it for free — no separate keydown handling needed — while the
  // <th> keeps its native columnheader semantics and carries aria-sort.

  var ARROW_BOTH = '<svg viewBox="0 0 9 11" aria-hidden="true" focusable="false"><path d="M4.5 0 8 4H1z" fill="currentColor"/><path d="M4.5 11 1 7h7z" fill="currentColor"/></svg>';
  var ARROW_UP = '<svg viewBox="0 0 9 6" aria-hidden="true" focusable="false"><path d="M4.5 0 9 6H0z" fill="currentColor"/></svg>';
  var ARROW_DOWN = '<svg viewBox="0 0 9 6" aria-hidden="true" focusable="false"><path d="M4.5 6 0 0h9z" fill="currentColor"/></svg>';

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
  }

  function bindHeader(th) {
    if (th.dataset.sortBound === '1') return;
    th.dataset.sortBound = '1';
    if (!th.hasAttribute('aria-sort')) th.setAttribute('aria-sort', 'none');

    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'inline-flex items-center gap-1 bg-transparent border-0 p-0 m-0 cursor-pointer ' +
      'focus-visible:ring-2 focus-visible:ring-brand focus-visible:outline-none rounded-sm';
    // Move the existing header content (plain text, or Jinja-rendered text)
    // into the button rather than re-typing it, so translations / Jinja
    // output are preserved exactly.
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
          sib.setAttribute('aria-sort', 'none');
          setArrow(sib, 'none');
        }
      });

      applySort(table, colIndex, type, dir);
      th.setAttribute('aria-sort', dir === 'asc' ? 'ascending' : 'descending');
      setArrow(th, dir);
      saveSortState(table, colIndex, dir);
    });
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

    bindHeader(th); // idempotent — ensures the arrow span exists to update
    applySort(table, state.col, th.getAttribute('data-sort'), state.dir);

    Array.prototype.forEach.call(headerRow.children, function (sib) {
      if (!sib.hasAttribute('data-sort')) return;
      bindHeader(sib);
      var active = sib === th;
      sib.setAttribute('aria-sort', active ? (state.dir === 'asc' ? 'ascending' : 'descending') : 'none');
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

  // Tables rebuilt by plain fetch().then(html => el.innerHTML = …) never
  // fire an htmx event at all (rbac.html's ACL table, every
  // server_detail.html table, servers.html's dependency browser,
  // dashboard.html's 30s-polled restart-results table) — this is the only
  // thing that notices those. Mutations are batched per synchronous task by
  // the browser already; the affected-table Set below just avoids re-
  // sorting the same table once per row when a caller appends rows one at
  // a time in a loop (reports.html's fleet table does exactly that).
  var _mo = new MutationObserver(function (mutations) {
    if (_sorting) return; // this batch is applySort()'s own row-reorder — see its docstring
    var freshRoots = [];
    var tablesToRestore = [];
    for (var i = 0; i < mutations.length; i++) {
      // A mutation whose target lives INSIDE a <thead> is header-decoration
      // churn this file caused itself (bindHeader building the button/arrow,
      // setArrow swapping the icon) — never new data. Found live: without
      // this, restoreSortState()'s unconditional setArrow() calls (one per
      // sortable column, every invocation) looked exactly like "this
      // table's rows changed" to the loop below, which called
      // restoreSortState() again, which called setArrow() again — a second
      // self-feedback cycle _sorting alone doesn't cover, because it only
      // guards applySort()'s tbody mutations, not thead ones. Reloading any
      // page with a stored sort locked the tab solid before this check
      // existed.
      var target = mutations[i].target;
      if (target.closest && target.closest('thead')) continue;
      var added = mutations[i].addedNodes;
      for (var j = 0; j < added.length; j++) {
        var node = added[j];
        if (node.nodeType !== 1) continue;
        if (node.querySelector && (node.matches('thead[data-sort-table]') || node.querySelector('thead[data-sort-table]'))) {
          freshRoots.push(node);
        }
        var owner = node.closest ? node.closest('table') : null;
        if (owner && owner.tHead && owner.tHead.hasAttribute('data-sort-table')) {
          if (tablesToRestore.indexOf(owner) === -1) tablesToRestore.push(owner);
        }
      }
    }
    freshRoots.forEach(function (root) { scan(root); restoreAll(root); });
    tablesToRestore.forEach(restoreSortState);
  });
  _mo.observe(document.body, { childList: true, subtree: true });

  window.__prismBindTableSort = scan;
})();
