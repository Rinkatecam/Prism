/*
 * Prism — Interactive Infrastructure Topology
 *
 * Renders the dependency graph returned by /api/topology/data into an SVG
 * canvas with:
 *   - Top-down layered layout (layer 0 at the top, dependents flowing down)
 *   - Workflow-block style nodes (icon on the left, name + type stacked)
 *   - Curved Bezier edges with arrowheads
 *   - Hand-rolled pan (mousedrag) + zoom (wheel, zoom-to-cursor)
 *   - Rich HTML tooltip on hover with status / CPU / RAM / dependency list
 *   - Search + status/type filter dimming
 *   - Click to open server detail
 *   - Blast radius highlight via /api/topology/blast-radius/<name>
 *   - prismRefresh auto-reload that preserves the user's pan/zoom
 *
 * No external JS libraries. Just vanilla DOM + SVG.
 */

(function () {
  'use strict';

  // ── Lucide icon path library (inlined so we don't depend on lucide.createIcons
  //    inside the SVG transform) ────────────────────────────────────────────
  // Paths are scaled to a 24×24 viewBox. Pulled from lucide-static 0.344 and
  // trimmed to the types Prism uses. Fallback = "server".
  const ICON_PATHS = {
    'shield':         '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
    'folder':         '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
    'database':       '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5V19A9 3 0 0 0 21 19V5"/><path d="M3 12A9 3 0 0 0 21 12"/>',
    'server-cog':     '<path d="M5 10H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v4a2 2 0 0 1-2 2h-1.172"/><path d="M5 14H4a2 2 0 0 0-2 2v4a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-4a2 2 0 0 0-2-2h-1.172"/><circle cx="12" cy="17" r="3"/><path d="M12 11v1"/><path d="M12 22v1"/><path d="m6 6 0 0"/><path d="m6 18 0 0"/>',
    'globe':          '<circle cx="12" cy="12" r="10"/><path d="M12 2a14.5 14.5 0 0 0 0 20a14.5 14.5 0 0 0 0-20"/><path d="M2 12h20"/>',
    'mail':           '<rect width="20" height="16" x="2" y="4" rx="2"/><path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/>',
    'hard-drive':     '<line x1="22" x2="2" y1="12" y2="12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/><line x1="6" x2="6.01" y1="16" y2="16"/><line x1="10" x2="10.01" y1="16" y2="16"/>',
    'printer':        '<polyline points="6 9 6 2 18 2 18 9"/><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><rect width="12" height="8" x="6" y="14"/>',
    'download-cloud': '<path d="M4 14.899A7 7 0 1 1 15.71 8h1.79a4.5 4.5 0 0 1 2.5 8.242"/><path d="M12 12v9"/><path d="m8 17 4 4 4-4"/>',
    'server':         '<rect width="20" height="8" x="2" y="2" rx="2" ry="2"/><rect width="20" height="8" x="2" y="14" rx="2" ry="2"/><line x1="6" x2="6.01" y1="6" y2="6"/><line x1="6" x2="6.01" y1="18" y2="18"/>',
    // Utility icons (used by the tooltip + controls)
    'activity':       '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>',
    'cpu':            '<rect width="16" height="16" x="4" y="4" rx="2"/><rect width="6" height="6" x="9" y="9"/><path d="M15 2v2"/><path d="M15 20v2"/><path d="M2 15h2"/><path d="M2 9h2"/><path d="M20 15h2"/><path d="M20 9h2"/><path d="M9 2v2"/><path d="M9 20v2"/>',
    'memory':         '<path d="M6 19v-3"/><path d="M10 19v-3"/><path d="M14 19v-3"/><path d="M18 19v-3"/><path d="M8 11V9"/><path d="M16 11V9"/><path d="M12 11V9"/><path d="M2 15h20"/><path d="M2 7a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v1.1a2 2 0 0 0 0 3.837V17a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2v-5.1a2 2 0 0 0 0-3.837Z"/>',
  };

  // ── Status colors ────────────────────────────────────────────────────────
  const STATUS_COLORS = {
    healthy:  '#10B981',
    warning:  '#F59E0B',
    critical: '#DC2626',
    offline:  '#6B7280',
    unknown:  '#9CA3AF',
  };
  const STATUS_LABELS = {
    healthy: 'Healthy', warning: 'Warning', critical: 'Critical',
    offline: 'Offline', unknown: 'Unknown',
  };

  // ── Node dimensions (must match topology.py CARD_W / CARD_H) ────────────
  const CARD_W = 200;
  const CARD_H = 72;

  // ── State ───────────────────────────────────────────────────────────────
  const state = {
    data: null,            // { nodes, edges, bounds, empty }
    tx: 0, ty: 0, s: 1,    // pan/zoom transform
    drag: null,            // { startX, startY, origTx, origTy }
    blastSet: null,        // set of server names highlighted
    highlightServer: null, // the server the blast radius was computed from
    search: '',
    statusFilter: 'all',
    typeFilter: 'all',
    hoverNode: null,
  };

  // Namespaces / helpers
  const NS = 'http://www.w3.org/2000/svg';
  function el(tag, attrs) {
    const e = document.createElementNS(NS, tag);
    if (attrs) for (const k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  }
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  // ── Fetch + render ──────────────────────────────────────────────────────
  function fetchAndRender() {
    const firstLoad = !state.data;
    const hadStoredView = state._restoredFromStorage;
    fetch('/api/topology/data')
      .then(r => r.json())
      .then(data => {
        if (!data.ok) throw new Error(data.error || 'Failed');
        state.data = data;
        render();
        // On first load, ALWAYS recompute fit. Stored views from the old
        // (buggy) coordinate system would land off-screen because tx/ty
        // were in mixed units. After two animation frames the SVG has its
        // final layout-driven size, so getBoundingClientRect() is reliable.
        if (firstLoad) {
          requestAnimationFrame(() => requestAnimationFrame(() => {
            // Re-render once we know the real container size, then fit.
            render();
            if (!hadStoredView || !isStoredViewVisible()) {
              fitToScreen();
            } else {
              applyTransform();
            }
          }));
        }
      })
      .catch(err => {
        const svg = document.getElementById('topology-canvas');
        if (svg) {
          svg.innerHTML = '<text x="20" y="40" fill="#DC2626" font-size="14">Failed to load topology: ' + esc(err.message) + '</text>';
        }
      });
  }

  // Stored views from the old coordinate system can leave the graph parked
  // outside the viewport. Sanity-check before trusting persisted tx/ty/s.
  function isStoredViewVisible() {
    if (!state.data) return false;
    const svg = document.getElementById('topology-canvas');
    if (!svg) return false;
    const r = svg.getBoundingClientRect();
    const { w, h } = state.data.bounds;
    if (state.s <= 0.02 || state.s > 5) return false;
    const left = state.tx;
    const top = state.ty;
    const right = state.tx + w * state.s;
    const bottom = state.ty + h * state.s;
    // At least 80px of the graph must overlap the viewport
    return right > 80 && bottom > 80 && left < r.width - 80 && top < r.height - 80;
  }

  function render() {
    const svg = document.getElementById('topology-canvas');
    if (!svg) return;
    svg.innerHTML = ''; // clear

    const { bounds, nodes, edges, empty } = state.data;

    // CRITICAL: viewBox is the SVG's pixel size (1:1), NOT the graph bounds.
    // Older versions used viewBox=graph-bounds + preserveAspectRatio=meet,
    // which auto-letterboxed the graph. That broke pan/zoom because the
    // wheel handler measured cursor position in pixels but applied them as
    // viewBox user-units — so zooming made the graph jump off-screen and
    // fitToScreen() computed the wrong scale entirely. By matching the
    // viewBox to the on-screen pixel dimensions, every coordinate (cursor,
    // pan offset, scale) lives in the same unit system.
    const rect = svg.getBoundingClientRect();
    const vw = Math.max(100, rect.width);
    const vh = Math.max(100, rect.height);
    svg.setAttribute('viewBox', `0 0 ${vw} ${vh}`);
    svg.setAttribute('preserveAspectRatio', 'xMinYMin slice');
    state._viewportPx = { w: vw, h: vh };

    // <defs> for arrowheads + drop shadow
    const defs = el('defs');
    defs.innerHTML = `
      <marker id="topo-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
        <path d="M 0 0 L 10 5 L 0 10 z" fill="#94A3B8"/>
      </marker>
      <marker id="topo-arrow-blast" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
        <path d="M 0 0 L 10 5 L 0 10 z" fill="#F59E0B"/>
      </marker>
      <filter id="topo-node-glow" x="-50%" y="-50%" width="200%" height="200%">
        <feGaussianBlur stdDeviation="3" result="blur"/>
        <feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge>
      </filter>
      <filter id="topo-shadow" x="-20%" y="-20%" width="140%" height="140%">
        <feDropShadow dx="0" dy="2" stdDeviation="3" flood-color="#000" flood-opacity="0.12"/>
      </filter>
    `;
    svg.appendChild(defs);

    // Background covers the SVG viewport (not the graph bounds — the graph
    // pans/scales independently inside it).
    const bg = el('rect', {
      x: 0, y: 0, width: vw, height: vh,
      fill: document.documentElement.classList.contains('dark') ? '#0F172A' : '#F9FAFB',
    });
    svg.appendChild(bg);

    if (empty || !nodes.length) {
      const msg = el('text', {
        x: vw / 2, y: vh / 2,
        'text-anchor': 'middle', 'dominant-baseline': 'central',
        fill: '#6B7280', 'font-size': 14, 'font-family': 'system-ui, sans-serif',
      });
      msg.textContent = 'No dependencies configured. Add some in Settings → Servers.';
      svg.appendChild(msg);
      applyTransform();
      return;
    }

    // Viewport group — this is what pan/zoom transforms
    const viewport = el('g', { id: 'topo-viewport' });
    svg.appendChild(viewport);

    // Build a name → node index for fast edge lookups
    const nodeById = {};
    nodes.forEach(n => (nodeById[n.id] = n));

    // Render edges first so nodes sit on top
    const edgesG = el('g', { id: 'topo-edges' });
    viewport.appendChild(edgesG);
    edges.forEach(e => {
      const from = nodeById[e.from];
      const to = nodeById[e.to];
      if (!from || !to) return;
      const path = buildEdgePath(from, to);
      const isBlast = state.blastSet && (state.blastSet.has(e.to) || e.to === state.highlightServer);
      const stroke = isBlast ? '#F59E0B' : '#94A3B8';
      const width = isBlast ? 2.5 : 1.5;
      const edgeEl = el('path', {
        d: path, fill: 'none', stroke, 'stroke-width': width,
        'marker-end': isBlast ? 'url(#topo-arrow-blast)' : 'url(#topo-arrow)',
        'stroke-linecap': 'round',
        class: 'topo-edge',
        'data-from': e.from, 'data-to': e.to,
      });
      edgesG.appendChild(edgeEl);

      // Optional small label on the edge if there's a type
      if (e.type) {
        const midX = (from.x + to.x) / 2 + CARD_W / 2;
        const midY = (from.y + to.y) / 2 + CARD_H / 2;
        const label = el('text', {
          x: midX, y: midY - 4, 'text-anchor': 'middle',
          'font-size': 9, fill: '#6B7280', 'font-family': 'system-ui, sans-serif',
          class: 'topo-edge-label',
        });
        label.textContent = e.type;
        edgesG.appendChild(label);
      }
    });

    // Render nodes
    const nodesG = el('g', { id: 'topo-nodes' });
    viewport.appendChild(nodesG);
    nodes.forEach(n => {
      const g = buildNode(n);
      nodesG.appendChild(g);
    });

    applyTransform();
    applyFilters();
  }

  // ── Node rendering ──────────────────────────────────────────────────────
  function buildNode(n) {
    const statusColor = STATUS_COLORS[n.status] || STATUS_COLORS.unknown;
    const isDark = document.documentElement.classList.contains('dark');
    const fill = isDark ? '#1E293B' : '#FFFFFF';
    const textColor = isDark ? '#F8FAFC' : '#1F2937';
    const subColor = isDark ? '#94A3B8' : '#6B7280';

    const g = el('g', {
      class: 'topo-node',
      'data-id': n.id,
      'data-name-lower': (n.name || '').toLowerCase(),
      'data-type': n.type || 'other',
      'data-status': n.status || 'unknown',
      transform: `translate(${n.x} ${n.y})`,
      style: 'cursor: pointer;',
    });

    // Main card
    g.appendChild(el('rect', {
      x: 0, y: 0, width: CARD_W, height: CARD_H,
      rx: 12, ry: 12,
      fill, stroke: statusColor, 'stroke-width': 2,
      filter: 'url(#topo-shadow)',
    }));

    // Left icon bubble
    const iconBg = n.type_color || '#6B7280';
    g.appendChild(el('rect', {
      x: 10, y: 10, width: 52, height: 52, rx: 10, ry: 10,
      fill: iconBg, opacity: 0.12,
    }));

    // Icon (inlined SVG path at 24×24, scaled to 32×32 inside the bubble)
    const iconPath = ICON_PATHS[n.icon] || ICON_PATHS.server;
    const iconG = el('g', {
      transform: `translate(${10 + (52 - 32) / 2} ${10 + (52 - 32) / 2}) scale(1.333)`,
      fill: 'none', stroke: iconBg, 'stroke-width': 2,
      'stroke-linecap': 'round', 'stroke-linejoin': 'round',
    });
    iconG.innerHTML = iconPath;
    g.appendChild(iconG);

    // Server name
    const name = el('text', {
      x: 74, y: 28,
      fill: textColor,
      'font-size': 14, 'font-weight': 700,
      'font-family': 'system-ui, sans-serif',
    });
    name.textContent = n.name;
    g.appendChild(name);

    // Type label
    const type = el('text', {
      x: 74, y: 46,
      fill: subColor,
      'font-size': 11, 'font-family': 'system-ui, sans-serif',
    });
    type.textContent = n.type_label || n.type || '';
    g.appendChild(type);

    // Dependency counts (tiny badges on the right)
    if (n.dep_out_count || n.dep_in_count) {
      const badgeText = (n.dep_out_count ? '↑' + n.dep_out_count + ' ' : '') +
                        (n.dep_in_count  ? '↓' + n.dep_in_count : '');
      const badge = el('text', {
        x: CARD_W - 12, y: 62,
        'text-anchor': 'end',
        fill: subColor,
        'font-size': 10, 'font-family': 'system-ui, sans-serif',
      });
      badge.textContent = badgeText.trim();
      g.appendChild(badge);
    }

    // Status dot (top-right)
    g.appendChild(el('circle', {
      cx: CARD_W - 14, cy: 14, r: 5,
      fill: statusColor,
    }));

    // Hover + click
    g.addEventListener('mouseenter', () => showTooltip(n, g));
    g.addEventListener('mousemove', moveTooltip);
    g.addEventListener('mouseleave', hideTooltip);
    g.addEventListener('click', e => {
      // Don't navigate if the user was panning
      if (state.drag && state.drag.moved) return;
      e.stopPropagation();
      window.location = '/server/' + encodeURIComponent(n.id);
    });

    return g;
  }

  // ── Edge path (cubic Bezier, top-down) ─────────────────────────────────
  function buildEdgePath(from, to) {
    // Start at bottom-center of the source card, end at top-center of target
    const x1 = from.x + CARD_W / 2;
    const y1 = from.y + CARD_H;
    const x2 = to.x + CARD_W / 2;
    const y2 = to.y;
    // Control points offset vertically to produce a smooth downward curve
    const dy = y2 - y1;
    const c1x = x1;
    const c1y = y1 + Math.max(30, dy * 0.4);
    const c2x = x2;
    const c2y = y2 - Math.max(30, dy * 0.4);
    return `M ${x1} ${y1} C ${c1x} ${c1y} ${c2x} ${c2y} ${x2} ${y2}`;
  }

  // ── Pan + zoom ──────────────────────────────────────────────────────────
  function applyTransform() {
    const vp = document.getElementById('topo-viewport');
    if (vp) vp.setAttribute('transform', `translate(${state.tx} ${state.ty}) scale(${state.s})`);
  }

  function initPanZoom() {
    const svg = document.getElementById('topology-canvas');
    if (!svg) return;

    svg.addEventListener('mousedown', e => {
      // Ignore clicks that land on a node — those are handled by the node's
      // own click handler.
      if (e.target.closest('.topo-node')) return;
      state.drag = { startX: e.clientX, startY: e.clientY, origTx: state.tx, origTy: state.ty, moved: false };
      svg.style.cursor = 'grabbing';
      e.preventDefault();
    });
    window.addEventListener('mousemove', e => {
      if (!state.drag) return;
      const dx = e.clientX - state.drag.startX;
      const dy = e.clientY - state.drag.startY;
      if (Math.abs(dx) + Math.abs(dy) > 3) state.drag.moved = true;
      state.tx = state.drag.origTx + dx;
      state.ty = state.drag.origTy + dy;
      applyTransform();
    });
    window.addEventListener('mouseup', () => {
      if (!state.drag) return;
      state.drag = null;
      svg.style.cursor = 'grab';
      persistView();
    });
    // Set grab cursor as idle state
    svg.style.cursor = 'grab';

    // Wheel = zoom to cursor. Cursor is in viewBox user-units (which we've
    // pinned to pixel size in render(), so SVG user-units == container px).
    svg.addEventListener('wheel', e => {
      e.preventDefault();
      const r = svg.getBoundingClientRect();
      const px = (e.clientX - r.left);
      const py = (e.clientY - r.top);
      const factor = Math.exp(-e.deltaY * 0.0015);
      const newS = Math.min(4, Math.max(0.05, state.s * factor));
      state.tx = px - (px - state.tx) * (newS / state.s);
      state.ty = py - (py - state.ty) * (newS / state.s);
      state.s = newS;
      applyTransform();
      persistView();
    }, { passive: false });
  }

  // Fit the graph bounds inside the SVG viewport with a small margin.
  // Works because viewBox is now 1:1 with container pixels — graph bounds
  // are in viewBox user-units and the transform maps them straight onto
  // the SVG. No more pixel↔user-unit confusion.
  function fitToScreen() {
    const svg = document.getElementById('topology-canvas');
    if (!svg || !state.data) return;
    const r = svg.getBoundingClientRect();
    const vw = Math.max(100, r.width);
    const vh = Math.max(100, r.height);
    const { w, h } = state.data.bounds;
    if (!w || !h) return;
    const margin = 40;  // px on each side
    const scaleX = (vw - margin * 2) / w;
    const scaleY = (vh - margin * 2) / h;
    const scale = Math.min(scaleX, scaleY, 1.5);  // never auto-zoom > 1.5x
    const safeScale = Math.max(0.05, scale);
    state.s = safeScale;
    state.tx = (vw - w * safeScale) / 2;
    state.ty = (vh - h * safeScale) / 2;
    applyTransform();
    persistView();
  }

  // Reset = call fit. "scale=1, pan=0" used to be useless on big graphs
  // because the graph was 7000px wide and the user couldn't see anything.
  function resetView() { fitToScreen(); }

  function persistView() {
    try {
      sessionStorage.setItem('prism-topo-view', JSON.stringify({ tx: state.tx, ty: state.ty, s: state.s }));
    } catch (e) { /* ignore */ }
  }
  function restoreView() {
    try {
      const raw = sessionStorage.getItem('prism-topo-view');
      if (raw) {
        const v = JSON.parse(raw);
        if (typeof v.tx === 'number') state.tx = v.tx;
        if (typeof v.ty === 'number') state.ty = v.ty;
        if (typeof v.s === 'number') state.s = v.s;
        state._restoredFromStorage = true;
      }
    } catch (e) { /* ignore */ }
  }

  // ── Tooltip ─────────────────────────────────────────────────────────────
  function showTooltip(n, nodeG) {
    state.hoverNode = n.id;
    highlightConnectedEdges(n.id);
    const tip = document.getElementById('topo-tooltip');
    if (!tip) return;

    const statusColor = STATUS_COLORS[n.status] || STATUS_COLORS.unknown;
    const statusLabel = STATUS_LABELS[n.status] || 'Unknown';

    function bar(label, value) {
      if (value == null || value < 0) return '';
      const pct = Math.min(100, Math.max(0, value));
      let color = '#10B981';
      if (pct >= 85) color = '#DC2626';
      else if (pct >= 70) color = '#F59E0B';
      return `
        <div class="flex items-center gap-2 mt-1">
          <span class="text-[10px] text-[#94A3B8] w-8">${label}</span>
          <div class="flex-1 h-1.5 rounded bg-[#334155] overflow-hidden">
            <div style="width:${pct}%; background:${color}" class="h-full"></div>
          </div>
          <span class="text-[10px] font-mono text-[#CBD5E1] w-10 text-right">${pct.toFixed(1)}%</span>
        </div>`;
    }

    function depList(list, arrow) {
      if (!list || !list.length) return '';
      const items = list.slice(0, 8).map(d => {
        const parts = [];
        parts.push(esc(d.from || d.to));
        if (d.port) parts.push('port ' + d.port);
        if (d.service_name) parts.push('svc ' + esc(d.service_name));
        if (d.process_name) parts.push('proc ' + esc(d.process_name));
        const label = d.type ? `<span class="text-[#F59E0B]">${esc(d.type)}</span>` : '';
        return `<div class="text-[10px] text-[#CBD5E1]">${arrow} <b>${parts[0]}</b>${parts.length > 1 ? ' · ' + parts.slice(1).join(' · ') : ''}${label ? ' · ' + label : ''}</div>`;
      }).join('');
      const more = list.length > 8 ? `<div class="text-[10px] text-[#64748B] italic">+ ${list.length - 8} more</div>` : '';
      return items + more;
    }

    tip.innerHTML = `
      <div class="flex items-start justify-between gap-3">
        <div>
          <div class="text-sm font-bold text-[#F9FAFB]">${esc(n.name)}</div>
          <div class="text-[10px] text-[#94A3B8] mt-0.5">${esc(n.type_label || n.type || '')}${n.host ? ' · ' + esc(n.host) : ''}</div>
        </div>
        <span style="background:${statusColor}20;color:${statusColor};border:1px solid ${statusColor}40" class="px-1.5 py-0.5 rounded text-[10px] font-bold uppercase">${statusLabel}</span>
      </div>
      <div class="mt-2 pt-2 border-t border-[#334155]">
        ${bar('CPU', n.cpu)}
        ${bar('RAM', n.ram)}
        ${bar('C:',  n.disk_c)}
        ${bar('D:',  n.disk_d)}
      </div>
      ${(n.deps_out && n.deps_out.length) ? `
        <div class="mt-2 pt-2 border-t border-[#334155]">
          <div class="text-[10px] font-bold text-[#94A3B8] uppercase mb-1">Depends on (${n.deps_out.length})</div>
          ${depList(n.deps_out, '↓')}
        </div>` : ''}
      ${(n.deps_in && n.deps_in.length) ? `
        <div class="mt-2 pt-2 border-t border-[#334155]">
          <div class="text-[10px] font-bold text-[#94A3B8] uppercase mb-1">Depended on by (${n.deps_in.length})</div>
          ${depList(n.deps_in, '↑')}
        </div>` : ''}
      <div class="mt-2 pt-2 border-t border-[#334155] text-[10px] text-[#64748B] italic">Click to open server detail</div>
    `;
    tip.classList.add('visible');
  }
  function moveTooltip(e) {
    const tip = document.getElementById('topo-tooltip');
    if (!tip) return;
    const pad = 14;
    const r = tip.getBoundingClientRect();
    let x = e.clientX + pad;
    let y = e.clientY + pad;
    if (x + r.width + pad > window.innerWidth) x = e.clientX - r.width - pad;
    if (y + r.height + pad > window.innerHeight) y = e.clientY - r.height - pad;
    tip.style.left = Math.max(pad, x) + 'px';
    tip.style.top  = Math.max(pad, y) + 'px';
  }
  function hideTooltip() {
    state.hoverNode = null;
    highlightConnectedEdges(null);
    const tip = document.getElementById('topo-tooltip');
    if (tip) tip.classList.remove('visible');
  }

  function highlightConnectedEdges(id) {
    document.querySelectorAll('.topo-edge').forEach(edge => {
      if (id === null) { edge.style.opacity = ''; return; }
      const from = edge.getAttribute('data-from');
      const to = edge.getAttribute('data-to');
      edge.style.opacity = (from === id || to === id) ? '1' : '0.15';
    });
  }

  // ── Search + filters ────────────────────────────────────────────────────
  function applyFilters() {
    const q = state.search.toLowerCase();
    document.querySelectorAll('.topo-node').forEach(nodeEl => {
      const name = nodeEl.getAttribute('data-name-lower') || '';
      const type = nodeEl.getAttribute('data-type') || '';
      const status = nodeEl.getAttribute('data-status') || '';
      const matchesQ = !q || name.includes(q);
      const matchesStatus = state.statusFilter === 'all' || status === state.statusFilter;
      const matchesType = state.typeFilter === 'all' || type === state.typeFilter;
      const visible = matchesQ && matchesStatus && matchesType;
      nodeEl.style.opacity = visible ? '1' : '0.15';
      nodeEl.style.pointerEvents = visible ? '' : 'none';
    });
  }

  function bindControls() {
    const searchInput = document.getElementById('topo-search');
    if (searchInput) {
      searchInput.addEventListener('input', e => {
        state.search = e.target.value;
        applyFilters();
      });
    }
    document.querySelectorAll('[data-status-filter]').forEach(btn => {
      btn.addEventListener('click', () => {
        state.statusFilter = btn.getAttribute('data-status-filter');
        document.querySelectorAll('[data-status-filter]').forEach(b => b.classList.toggle('active', b === btn));
        applyFilters();
      });
    });
    document.querySelectorAll('[data-type-filter]').forEach(btn => {
      btn.addEventListener('click', () => {
        state.typeFilter = btn.getAttribute('data-type-filter');
        document.querySelectorAll('[data-type-filter]').forEach(b => b.classList.toggle('active', b === btn));
        applyFilters();
      });
    });
    const fitBtn = document.getElementById('topo-fit');
    if (fitBtn) fitBtn.addEventListener('click', fitToScreen);
    const resetBtn = document.getElementById('topo-reset');
    if (resetBtn) resetBtn.addEventListener('click', resetView);
    const refreshBtn = document.getElementById('topo-refresh');
    if (refreshBtn) refreshBtn.addEventListener('click', fetchAndRender);
    const highlightSel = document.getElementById('topo-highlight');
    if (highlightSel) highlightSel.addEventListener('change', () => loadBlastRadius(highlightSel.value));
  }

  // ── Blast radius ────────────────────────────────────────────────────────
  function loadBlastRadius(serverName) {
    if (!serverName) {
      state.blastSet = null;
      state.highlightServer = null;
      const panel = document.getElementById('blast-radius-panel');
      if (panel) panel.classList.add('hidden');
      render();
      return;
    }
    fetch('/api/topology/blast-radius/' + encodeURIComponent(serverName))
      .then(r => r.json())
      .then(data => {
        if (!data.ok) return;
        state.blastSet = new Set(data.affected);
        state.highlightServer = serverName;
        render();

        const panel = document.getElementById('blast-radius-panel');
        const list = document.getElementById('blast-radius-list');
        if (panel && list) {
          if (data.affected && data.affected.length) {
            panel.classList.remove('hidden');
            list.innerHTML = data.affected.map(s =>
              `<div class="flex items-center gap-1.5 text-[#6B7280] dark:text-[#CBD5E1]">
                 <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4"/><path d="m12 17 .01 0"/></svg>
                 <a href="/server/${encodeURIComponent(s)}" class="hover:text-[#2563EB] dark:hover:text-[#3B82F6]">${esc(s)}</a>
               </div>`
            ).join('');
          } else {
            panel.classList.add('hidden');
          }
        }
      });
  }

  // ── Init ────────────────────────────────────────────────────────────────
  function init() {
    restoreView();
    initPanZoom();
    bindControls();
    fetchAndRender();

    // Re-render on new data (prismRefresh signal fires ~5-10s)
    document.body.addEventListener('prismRefresh', fetchAndRender);

    // Window resize: re-render so viewBox tracks the new container size and
    // — when the user hadn't manually panned — refit so the graph stays
    // centred and inside the visible area.
    let resizeTimer = null;
    window.addEventListener('resize', () => {
      if (resizeTimer) clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        if (!state.data) return;
        render();
        // Refit unless the user has clearly chosen a custom view
        if (!isStoredViewVisible()) fitToScreen();
        else applyTransform();
      }, 120);
    });

    // Re-theme nodes when user toggles dark mode
    const origToggle = window.toggleTheme;
    if (origToggle) {
      window.toggleTheme = function () {
        origToggle();
        setTimeout(render, 100);
      };
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
