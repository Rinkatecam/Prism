"""Infrastructure dependency map generator for Prism.
Generates SVG dependency graphs using pure Python."""

import logging
import math
from collections import deque

logger = logging.getLogger("prism.topology")

# SVG template constants
SVG_HEADER = '<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
SVG_FOOTER = '</svg>'

STATUS_COLORS = {
    "healthy": "#10B981", "warning": "#F59E0B", "critical": "#DC2626",
    "offline": "#6B7280", "unknown": "#9CA3AF",
}

NODE_W = 140
NODE_H = 44
H_GAP = 40
V_GAP = 70
PAD = 40

# Interactive-canvas layout constants (Phase 1 — used by build_topology_data).
# Top-down tree: roots at the top, dependents flowing downward. Bigger nodes
# than the static SVG's, because the interactive canvas renders workflow-block
# style cards (icon + name + type stacked).
CARD_W = 200
CARD_H = 72
CARD_H_GAP = 56        # horizontal gap between cards in a layer
CARD_V_GAP = 110       # vertical gap between layers
CARD_PAD = 60          # canvas padding around the whole graph
TYPE_GROUP_GAP = 20    # extra gap between type groups within a layer


# Server-type → icon + color map used by the interactive topology renderer.
# Keys match DEFAULT_THRESHOLDS in models.py plus a few synonyms for
# robustness (custom user types fall through to "other").
TYPE_META = {
    "domain_controller": {"icon": "shield",         "color": "#8B5CF6", "label": "Domain Controller"},
    "file_server":       {"icon": "folder",         "color": "#F59E0B", "label": "File Server"},
    "database_server":   {"icon": "database",       "color": "#10B981", "label": "Database Server"},
    "sql_server":        {"icon": "database",       "color": "#10B981", "label": "SQL Server"},
    "app_server":        {"icon": "server-cog",     "color": "#2563EB", "label": "Application Server"},
    "web_server":        {"icon": "globe",          "color": "#06B6D4", "label": "Web Server"},
    "mail_server":       {"icon": "mail",           "color": "#EC4899", "label": "Mail Server"},
    "backup_server":     {"icon": "hard-drive",     "color": "#64748B", "label": "Backup Server"},
    "print_server":      {"icon": "printer",        "color": "#F97316", "label": "Print Server"},
    "update_server":     {"icon": "download-cloud", "color": "#0EA5E9", "label": "Update Server"},
    "wsus":              {"icon": "download-cloud", "color": "#0EA5E9", "label": "WSUS"},
    "other":             {"icon": "server",         "color": "#6B7280", "label": "Server"},
}

# Group ordering within a layer — within each layer we sort nodes so that
# type groups sit next to each other, with DCs on the left, then data stores,
# then application tier, then infra. This makes the graph read naturally.
TYPE_GROUP_ORDER = [
    "domain_controller",
    "database_server", "sql_server",
    "file_server", "backup_server",
    "mail_server", "web_server",
    "app_server",
    "print_server",
    "update_server", "wsus",
    "other",
]


def _escape(text):
    """Escape XML special characters."""
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))


def _build_layers(dependencies):
    """Assign servers to layers using topological ordering.
    Layer 0 = roots (no dependencies). Layer N = depends on something in layer N-1."""
    # Collect all servers
    all_servers = set()
    depends_on_map = {}  # server -> set of what it depends on
    for dep in dependencies:
        s = dep["server_name"]
        t = dep["depends_on"]
        all_servers.add(s)
        all_servers.add(t)
        depends_on_map.setdefault(s, set()).add(t)

    # Servers with no dependencies go to layer 0
    layers = {}
    remaining = set(all_servers)

    # Assign layers iteratively
    current_layer = 0
    changed = True
    while remaining and changed:
        changed = False
        layer_nodes = set()
        for srv in list(remaining):
            deps = depends_on_map.get(srv, set())
            # All dependencies must already be assigned a layer
            if deps <= (all_servers - remaining):
                layer_nodes.add(srv)
        if layer_nodes:
            for n in layer_nodes:
                layers[n] = current_layer
                remaining.discard(n)
            current_layer += 1
            changed = True

    # Handle cycles: put remaining in the next layer
    for srv in remaining:
        layers[srv] = current_layer

    return layers, all_servers


def get_blast_radius(dependencies, server_name):
    """BFS through dependency graph to find all transitive dependents."""
    # Build adjacency: for each server, what depends ON it
    dependents = {}
    for dep in dependencies:
        dependents.setdefault(dep["depends_on"], []).append(dep["server_name"])

    visited = set()
    queue = deque([server_name])
    while queue:
        current = queue.popleft()
        if current in visited:
            continue
        visited.add(current)
        for child in dependents.get(current, []):
            queue.append(child)

    visited.discard(server_name)  # Don't include the server itself
    return list(visited)


def generate_dependency_svg(dependencies, server_statuses, highlight_server=None, dark_mode=False):
    """Generate SVG string showing server dependency graph.

    Args:
        dependencies: list of {server_name, depends_on, dependency_type}
        server_statuses: dict of {server_name: status_string}
        highlight_server: if set, highlight blast radius
        dark_mode: adjust colors for dark background
    Returns:
        SVG string
    """
    if not dependencies:
        # Empty state
        w, h = 400, 120
        bg = "#1E293B" if dark_mode else "#F9FAFB"
        fg = "#94A3B8" if dark_mode else "#6B7280"
        parts = [SVG_HEADER.format(w=w, h=h)]
        parts.append(f'<rect width="{w}" height="{h}" fill="{bg}" rx="8"/>')
        parts.append(f'<text x="{w//2}" y="{h//2}" text-anchor="middle" '
                      f'dominant-baseline="central" fill="{fg}" font-size="14" '
                      f'font-family="system-ui, sans-serif">No dependencies configured</text>')
        parts.append(SVG_FOOTER)
        return "\n".join(parts)

    layers, all_servers = _build_layers(dependencies)

    # Compute blast radius if needed
    blast_set = set()
    if highlight_server:
        blast_set = set(get_blast_radius(dependencies, highlight_server))

    # Group servers by layer
    layer_groups = {}
    for srv, layer in layers.items():
        layer_groups.setdefault(layer, []).append(srv)
    for layer in layer_groups:
        layer_groups[layer].sort()

    max_layer = max(layer_groups.keys()) if layer_groups else 0
    max_per_layer = max(len(v) for v in layer_groups.values()) if layer_groups else 1

    # Compute canvas size
    canvas_w = max(max_per_layer * (NODE_W + H_GAP) - H_GAP + PAD * 2, 400)
    canvas_h = (max_layer + 1) * (NODE_H + V_GAP) - V_GAP + PAD * 2

    # Compute node positions
    positions = {}
    for layer_idx, nodes in layer_groups.items():
        n = len(nodes)
        total_w = n * NODE_W + (n - 1) * H_GAP
        start_x = (canvas_w - total_w) / 2
        y = PAD + layer_idx * (NODE_H + V_GAP)
        for i, srv in enumerate(nodes):
            x = start_x + i * (NODE_W + H_GAP)
            positions[srv] = (x, y)

    bg_color = "#1E293B" if dark_mode else "#F9FAFB"
    text_color = "#F8FAFC" if dark_mode else "#1F2937"
    edge_color = "#64748B" if dark_mode else "#94A3B8"
    label_color = "#94A3B8" if dark_mode else "#6B7280"

    parts = [SVG_HEADER.format(w=int(canvas_w), h=int(canvas_h))]
    parts.append(f'<rect width="{int(canvas_w)}" height="{int(canvas_h)}" fill="{bg_color}" rx="8"/>')

    # Defs for arrow marker and glow filter
    parts.append('<defs>')
    parts.append(f'<marker id="arrowhead" markerWidth="10" markerHeight="7" refX="10" refY="3.5" orient="auto">'
                 f'<polygon points="0 0, 10 3.5, 0 7" fill="{edge_color}"/></marker>')
    parts.append('<filter id="glow" x="-30%" y="-30%" width="160%" height="160%">'
                 '<feGaussianBlur stdDeviation="3" result="blur"/>'
                 '<feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge>'
                 '</filter>')
    parts.append('</defs>')

    # Draw edges (arrows from depends_on -> server_name, i.e. top to bottom)
    for dep in dependencies:
        src = dep["depends_on"]
        tgt = dep["server_name"]
        if src not in positions or tgt not in positions:
            continue
        sx, sy = positions[src]
        tx, ty = positions[tgt]
        # Arrow from bottom-center of source to top-center of target
        x1 = sx + NODE_W / 2
        y1 = sy + NODE_H
        x2 = tx + NODE_W / 2
        y2 = ty
        # Shorten by marker size
        dy = y2 - y1
        dx = x2 - x1
        dist = math.sqrt(dx * dx + dy * dy)
        if dist > 0:
            x2 = x2 - dx / dist * 6
            y2 = y2 - dy / dist * 6
        is_blast = highlight_server and (tgt in blast_set or tgt == highlight_server)
        stroke = "#F59E0B" if is_blast else edge_color
        sw = "2" if is_blast else "1.5"
        dep_type = dep.get("dependency_type", "")
        parts.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                     f'stroke="{stroke}" stroke-width="{sw}" marker-end="url(#arrowhead)"/>')
        # Label on edge
        if dep_type:
            mx = (sx + NODE_W / 2 + tx + NODE_W / 2) / 2
            my = (sy + NODE_H + ty) / 2
            parts.append(f'<text x="{mx:.1f}" y="{my:.1f}" text-anchor="middle" '
                         f'font-size="9" fill="{label_color}" '
                         f'font-family="system-ui, sans-serif">{_escape(dep_type)}</text>')

    # Draw nodes
    for srv, (x, y) in positions.items():
        status = server_statuses.get(srv, "unknown")
        color = STATUS_COLORS.get(status, STATUS_COLORS["unknown"])
        is_highlight = highlight_server and srv == highlight_server
        is_blast = highlight_server and srv in blast_set

        # Node rectangle
        extra = ' filter="url(#glow)"' if (is_highlight or is_blast) else ''
        stroke_color = "#F59E0B" if is_highlight else ("#EF4444" if is_blast else color)
        stroke_w = "2.5" if (is_highlight or is_blast) else "1.5"
        fill = "#0F172A" if dark_mode else "#FFFFFF"
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{NODE_W}" height="{NODE_H}" '
                     f'rx="8" fill="{fill}" stroke="{stroke_color}" stroke-width="{stroke_w}"{extra}/>')

        # Status dot
        dot_x = x + 12
        dot_y = y + NODE_H / 2
        parts.append(f'<circle cx="{dot_x:.1f}" cy="{dot_y:.1f}" r="4" fill="{color}"/>')

        # Server name (truncate if long)
        name_display = srv if len(srv) <= 14 else srv[:12] + ".."
        tx = x + 24
        ty_text = y + NODE_H / 2 + 1
        parts.append(f'<text x="{tx:.1f}" y="{ty_text:.1f}" dominant-baseline="central" '
                     f'font-size="11" font-weight="500" fill="{text_color}" '
                     f'font-family="system-ui, sans-serif">{_escape(name_display)}</text>')

    parts.append(SVG_FOOTER)
    return "\n".join(parts)


def _type_meta(t: str) -> dict:
    """Look up icon/color/label for a server type, falling back to 'other'."""
    if not t:
        return TYPE_META["other"]
    key = str(t).strip().lower()
    if key in TYPE_META:
        return TYPE_META[key]
    return TYPE_META["other"]


def _type_sort_key(t: str) -> int:
    """Order value used to group servers of similar types next to each other
    within the same layer. Lower = further left / earlier in the row."""
    key = str(t or "other").strip().lower()
    try:
        return TYPE_GROUP_ORDER.index(key)
    except ValueError:
        return len(TYPE_GROUP_ORDER)  # unknown → rightmost


def _layout_topdown(layer_groups: dict, server_types: dict) -> tuple[dict, int, int]:
    """Compute x,y positions for each server in a top-down tree.

    Layer 0 sits at the top (roots). Subsequent layers flow downward.
    Within each layer, nodes are sorted by server type group so DCs cluster
    together, SQL next, apps next, etc. An extra gap is inserted between
    type-group boundaries so the clusters are visually distinct.

    Returns (positions, canvas_w, canvas_h).
    """
    positions: dict[str, tuple[float, float]] = {}
    if not layer_groups:
        return positions, CARD_PAD * 2, CARD_PAD * 2

    # Sort nodes within each layer by (type-group, name) for stable layout
    for layer_idx in layer_groups:
        layer_groups[layer_idx] = sorted(
            layer_groups[layer_idx],
            key=lambda s: (_type_sort_key(server_types.get(s, "other")), s),
        )

    # Compute row widths with inter-type-group padding
    def row_width(nodes):
        if not nodes:
            return 0.0
        w = len(nodes) * CARD_W + max(0, len(nodes) - 1) * CARD_H_GAP
        # Add extra gap between distinct type groups
        prev_type = None
        extras = 0
        for s in nodes:
            t = _type_sort_key(server_types.get(s, "other"))
            if prev_type is not None and t != prev_type:
                extras += 1
            prev_type = t
        return w + extras * TYPE_GROUP_GAP

    max_row_w = max(row_width(nodes) for nodes in layer_groups.values()) if layer_groups else 0.0
    canvas_w = max(400.0, max_row_w + CARD_PAD * 2)

    max_layer = max(layer_groups.keys())
    canvas_h = (max_layer + 1) * (CARD_H + CARD_V_GAP) - CARD_V_GAP + CARD_PAD * 2

    # Assign x,y
    for layer_idx, nodes in layer_groups.items():
        row_w = row_width(nodes)
        start_x = (canvas_w - row_w) / 2
        y = CARD_PAD + layer_idx * (CARD_H + CARD_V_GAP)
        cx = start_x
        prev_type = None
        for s in nodes:
            t = _type_sort_key(server_types.get(s, "other"))
            if prev_type is not None and t != prev_type:
                cx += TYPE_GROUP_GAP
            positions[s] = (cx, y)
            cx += CARD_W + CARD_H_GAP
            prev_type = t

    return positions, canvas_w, canvas_h


def build_topology_data(dependencies: list, servers_config: dict, latest_cache: dict) -> dict:
    """Build the JSON payload consumed by the interactive topology canvas.

    Args:
        dependencies: list of dep dicts from db.get_all_dependencies()
        servers_config: dict of {name: ServerConfig}
        latest_cache: dict of {name: metric_row} from collector.latest_by_server
                      (may be partially empty if a cycle hasn't finished)

    Shape of returned dict:
        {
            "nodes": [...],
            "edges": [...],
            "bounds": {"w": int, "h": int},
            "generated_at": "ISO",
            "empty": bool,
        }
    """
    import time as _time

    now_iso = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())

    # If there are no deps AND no servers we return an empty payload so the
    # frontend can render a placeholder instead of a one-line "empty" SVG.
    all_server_names: set[str] = set()
    for dep in dependencies:
        all_server_names.add(dep["server_name"])
        all_server_names.add(dep["depends_on"])
    for name in servers_config:
        all_server_names.add(name)

    if not all_server_names:
        return {
            "nodes": [], "edges": [],
            "bounds": {"w": 400, "h": 120},
            "generated_at": now_iso,
            "empty": True,
        }

    # Layer assignment (reuse the existing topological sort)
    if dependencies:
        layers, _ = _build_layers(dependencies)
    else:
        layers = {}

    # Anything not in the dependency graph goes into a "free" layer 0 so
    # standalone servers still appear on the canvas.
    for s in all_server_names:
        if s not in layers:
            layers[s] = 0

    # server_name -> type (string, lowercased)
    server_types: dict[str, str] = {}
    for name, cfg in servers_config.items():
        try:
            server_types[name] = (cfg.type or "other").lower()
        except Exception:
            server_types[name] = "other"
    for s in all_server_names:
        server_types.setdefault(s, "other")

    # Group by layer
    layer_groups: dict[int, list[str]] = {}
    for srv, layer in layers.items():
        layer_groups.setdefault(layer, []).append(srv)

    positions, canvas_w, canvas_h = _layout_topdown(layer_groups, server_types)

    # Build per-node dependency counts + inline lists
    deps_in: dict[str, list] = {}   # server -> [{from, type, port, service, description}]
    deps_out: dict[str, list] = {}  # server -> [{to, type, port, service, description}]
    for dep in dependencies:
        src = dep["depends_on"]     # the thing being depended on
        tgt = dep["server_name"]    # the thing that has the dependency
        edge = {
            "type": dep.get("dependency_type", ""),
            "target_mode": dep.get("target_mode", ""),
            "port": dep.get("port"),
            "service_name": dep.get("service_name"),
            "process_name": dep.get("process_name"),
            "description": dep.get("description", ""),
        }
        deps_out.setdefault(tgt, []).append({**edge, "to": src})
        deps_in.setdefault(src, []).append({**edge, "from": tgt})

    # Build the node list
    nodes = []
    for name in sorted(all_server_names):
        pos = positions.get(name, (CARD_PAD, CARD_PAD))
        cfg = servers_config.get(name)
        stype = server_types.get(name, "other")
        meta = _type_meta(stype)
        latest = latest_cache.get(name) or {}

        nodes.append({
            "id": name,
            "name": name,
            "host": getattr(cfg, "host", "") if cfg else (latest.get("host") or ""),
            "type": stype,
            "type_label": meta["label"],
            "icon": meta["icon"],
            "type_color": meta["color"],
            "layer": layers.get(name, 0),
            "x": round(pos[0], 1),
            "y": round(pos[1], 1),
            "status": latest.get("status") or "unknown",
            "cpu": latest.get("cpu_percent"),
            "ram": latest.get("ram_percent"),
            "disk_c": latest.get("disk_c_percent"),
            "disk_d": latest.get("disk_d_percent"),
            "last_seen": latest.get("timestamp"),
            "dep_in_count": len(deps_in.get(name, [])),
            "dep_out_count": len(deps_out.get(name, [])),
            "deps_in": deps_in.get(name, []),
            "deps_out": deps_out.get(name, []),
        })

    # Build the edge list (src → tgt means "src is depended on by tgt", which
    # visually flows from the parent layer DOWN to the child layer)
    edges = []
    for dep in dependencies:
        src = dep["depends_on"]
        tgt = dep["server_name"]
        if src not in positions or tgt not in positions:
            continue
        edges.append({
            "from": src,
            "to": tgt,
            "type": dep.get("dependency_type", ""),
            "target_mode": dep.get("target_mode", ""),
            "port": dep.get("port"),
            "service_name": dep.get("service_name"),
            "process_name": dep.get("process_name"),
            "description": dep.get("description", ""),
        })

    return {
        "nodes": nodes,
        "edges": edges,
        "bounds": {"w": int(canvas_w), "h": int(canvas_h)},
        "generated_at": now_iso,
        "empty": False,
    }
