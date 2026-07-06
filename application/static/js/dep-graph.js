/* Workspace dependency graph renderer (tasks/graph.html).
 *
 * Self-contained SVG, no external library. Reads nodes + edges from the
 * embedded #dep-graph-data JSON, lays the DAG out in columns left→right by
 * longest prerequisite chain, and draws it. Edge {from, to} means `from`
 * depends on `to`, so `to` (the prerequisite) sits one column to the left and
 * the arrow points from `to` → `from` (the direction completion flows).
 *
 * No-ops when #dep-graph is absent, so it's safe to load globally.
 */
(function () {
  const SVG = "http://www.w3.org/2000/svg";
  const NODE_W = 210, NODE_H = 46, GAP_X = 70, GAP_Y = 18, PAD = 24;

  function statusColor(n) {
    if (n.status === "done" || n.status === "dropped")
      return { fill: "var(--bs-success-bg-subtle, #d1e7dd)", stroke: "var(--bs-success, #198754)" };
    if (n.blocked)
      return { fill: "var(--bs-warning-bg-subtle, #fff3cd)", stroke: "var(--bs-warning, #ffc107)" };
    if (n.status === "next")
      return { fill: "var(--bs-primary-bg-subtle, #cfe2ff)", stroke: "var(--bs-primary, #0d6efd)" };
    return { fill: "var(--bs-secondary-bg, #e9ecef)", stroke: "var(--bs-secondary, #6c757d)" };
  }

  function truncate(s, max) {
    s = s == null ? "" : String(s);
    return s.length > max ? s.slice(0, max - 1) + "…" : s;
  }

  function el(name, attrs) {
    const e = document.createElementNS(SVG, name);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  }

  function computeLevels(nodes, edges) {
    // prereqs[id] = list of tasks `id` depends on (its `to` targets).
    const prereqs = {};
    nodes.forEach((n) => (prereqs[n.id] = []));
    edges.forEach((e) => {
      if (prereqs[e.from]) prereqs[e.from].push(e.to);
    });
    const level = {}, visiting = {};
    function depth(id) {
      if (level[id] != null) return level[id];
      if (visiting[id]) return 0; // cycle guard (server keeps it a DAG anyway)
      visiting[id] = true;
      let d = 0;
      for (const p of prereqs[id] || []) {
        if (level[p] == null && !nodeById[p]) continue;
        d = Math.max(d, depth(p) + 1);
      }
      visiting[id] = false;
      return (level[id] = d);
    }
    var nodeById = {};
    nodes.forEach((n) => (nodeById[n.id] = n));
    nodes.forEach((n) => depth(n.id));
    return level;
  }

  function render(container, data) {
    const nodes = data.nodes || [], edges = data.edges || [];
    if (!nodes.length) return;
    const level = computeLevels(nodes, edges);

    // Bucket nodes per column, then assign a row within each column.
    const cols = {};
    nodes.forEach((n) => {
      const l = level[n.id] || 0;
      (cols[l] = cols[l] || []).push(n);
    });
    const pos = {};
    let maxRows = 0;
    Object.keys(cols).forEach((l) => {
      cols[l].forEach((n, row) => {
        pos[n.id] = {
          x: PAD + l * (NODE_W + GAP_X),
          y: PAD + row * (NODE_H + GAP_Y),
        };
      });
      maxRows = Math.max(maxRows, cols[l].length);
    });

    const maxLevel = Math.max(0, ...Object.keys(cols).map(Number));
    const width = PAD * 2 + (maxLevel + 1) * NODE_W + maxLevel * GAP_X;
    const height = PAD * 2 + maxRows * NODE_H + (maxRows - 1) * GAP_Y;

    const svg = el("svg", { width, height, viewBox: `0 0 ${width} ${height}` });

    // Arrowhead marker.
    const defs = el("defs", {});
    const marker = el("marker", {
      id: "dep-arrow", viewBox: "0 0 10 10", refX: "9", refY: "5",
      markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse",
    });
    marker.appendChild(el("path", { d: "M0,0 L10,5 L0,10 z", fill: "var(--bs-secondary, #6c757d)" }));
    defs.appendChild(marker);
    svg.appendChild(defs);

    // Edges first (under the nodes). from → depends on → to; arrow to→from.
    edges.forEach((e) => {
      const a = pos[e.to], b = pos[e.from]; // a = prerequisite (left)
      if (!a || !b) return;
      const x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2;
      const x2 = b.x, y2 = b.y + NODE_H / 2;
      const midX = (x1 + x2) / 2;
      svg.appendChild(el("path", {
        d: `M${x1},${y1} C${midX},${y1} ${midX},${y2} ${x2},${y2}`,
        fill: "none", stroke: "var(--bs-secondary, #6c757d)",
        "stroke-width": "1.5", "marker-end": "url(#dep-arrow)", opacity: "0.7",
      }));
    });

    // Nodes.
    nodes.forEach((n) => {
      const p = pos[n.id];
      const c = statusColor(n);
      const a = el("a", { href: n.url, class: "dep-node" });
      a.appendChild(el("rect", {
        x: p.x, y: p.y, width: NODE_W, height: NODE_H, rx: 8, ry: 8,
        fill: c.fill, stroke: c.stroke, "stroke-width": "1.5",
      }));
      const text = el("text", {
        x: p.x + 12, y: p.y + NODE_H / 2 + 4,
        "font-size": "13", fill: "var(--bs-body-color, #212529)",
      });
      if (n.status === "done" || n.status === "dropped")
        text.setAttribute("text-decoration", "line-through");
      text.textContent = truncate(n.title, 26);
      a.appendChild(text);
      const title = el("title", {});
      title.textContent = n.title;
      a.appendChild(title);
      svg.appendChild(a);
    });

    container.innerHTML = "";
    container.appendChild(svg);
  }

  document.addEventListener("DOMContentLoaded", () => {
    const container = document.getElementById("dep-graph");
    const dataEl = document.getElementById("dep-graph-data");
    if (!container || !dataEl) return;
    let data;
    try { data = JSON.parse(dataEl.textContent); } catch (_) { return; }
    render(container, data);
  });
})();
