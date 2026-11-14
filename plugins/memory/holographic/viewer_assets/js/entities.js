// Phase 7.2C — Entities tab: a namespace-scoped entity list plus a bounded
// relationship graph rendered with vis-network. Node/edge labels are plain
// text drawn on <canvas> (vis-network's default rendering), not HTML — and
// deliberately no `title` (tooltip) option is set anywhere here, since
// vis-network renders tooltips as DOM/HTML by default, which would be an
// injection vector for an entity name pulled from captured content.

import { api } from "./api.js";
import { el, clearChildren } from "./utils.js";

let currentGraph = null;
let currentAbort = null;

export async function loadEntities(ns, { signal } = {}) {
  const data = await api.entities(ns, { limit: 50 }, { signal });
  const entities = data.entities || [];
  renderEntityGrid(entities, ns);
  if (entities.length) {
    await selectEntity(ns, entities[0].entity_id, entities[0].name);
  } else {
    clearGraph();
  }
  return data;
}

function renderEntityGrid(entities, ns) {
  const grid = document.getElementById("entityGrid");
  clearChildren(grid);
  if (!entities.length) {
    grid.appendChild(el("div", { className: "empty", text: "No entities in this namespace." }));
    return;
  }
  for (const entity of entities) {
    const chip = el("div", { className: "entity-chip" }, [
      el("span", { className: "name", text: entity.name }),
      el("span", { className: "count", text: String(entity.fact_count) }),
    ]);
    chip.addEventListener("click", () => selectEntity(ns, entity.entity_id, entity.name));
    grid.appendChild(chip);
  }
}

export async function selectEntity(ns, entityId, name) {
  if (currentAbort) currentAbort.abort();
  currentAbort = new AbortController();
  const title = document.getElementById("graphTitle");
  if (title) title.textContent = `Relationships for ${name}`;
  try {
    const data = await api.entityGraph(ns, entityId, { signal: currentAbort.signal });
    renderGraph(data, entityId);
  } catch (err) {
    if (err && err.name === "AbortError") return;
    showGraphError();
  }
}

function renderGraph(data, entityId) {
  destroyGraphInstance();
  const container = document.getElementById("graph");
  clearChildren(container);

  const nodes = new vis.DataSet(
    (data.graph.nodes || []).map((node) => ({
      id: node.entity_id,
      label: node.name,
      shape: "dot",
      color: node.entity_id === entityId ? "#58a6ff" : "#8b949e",
    })),
  );
  const edges = new vis.DataSet(
    (data.graph.edges || []).map((edge, index) => ({
      id: index,
      from: edge.source_entity_id,
      to: edge.target_entity_id,
      label: edge.relationship_type,
      arrows: "to",
    })),
  );

  currentGraph = new vis.Network(container, { nodes, edges }, {
    physics: { stabilization: { iterations: 100 } },
    // navigationButtons is deliberately left off: vis-network renders them
    // from base64 data: URI icons, which the strict dashboard CSP (no
    // img-src, so default-src 'none' applies) blocks outright — the
    // buttons would be invisible and non-functional. #graphFitButton
    // (fitGraph()) already covers the one navigation action users need.
    interaction: { hover: true, navigationButtons: false },
  });

  const notice = document.getElementById("graphTruncationNotice");
  if (notice) {
    notice.textContent = data.graph.truncated
      ? `Graph truncated (${data.graph.truncated_reason || "limit reached"}) — showing a bounded subset.`
      : "";
  }
}

function showGraphError() {
  const container = document.getElementById("graph");
  clearChildren(container);
  container.appendChild(el("div", { className: "empty", text: "Failed to load relationships." }));
}

function destroyGraphInstance() {
  if (currentGraph) {
    currentGraph.destroy();
    currentGraph = null;
  }
}

export function clearGraph() {
  // selectEntity() makes its own AbortController (currentAbort, above),
  // independent of app.js's tab-level one — the initial entities-list
  // fetch is cancelled by that outer signal, but the auto-selected graph
  // fetch that follows is not. If the namespace changes while that graph
  // request is still in flight, aborting it here (unconditionally, even
  // when the Entities tab isn't active) is the only thing stopping a
  // stale-namespace graph from rendering after this clear.
  if (currentAbort) {
    currentAbort.abort();
    currentAbort = null;
  }
  destroyGraphInstance();
  const container = document.getElementById("graph");
  if (container) clearChildren(container);
  const title = document.getElementById("graphTitle");
  if (title) title.textContent = "Select an entity to see its relationships";
  const notice = document.getElementById("graphTruncationNotice");
  if (notice) notice.textContent = "";
}

export function clearEntityGrid() {
  const grid = document.getElementById("entityGrid");
  if (grid) clearChildren(grid);
}

export function fitGraph() {
  if (currentGraph) currentGraph.fit();
}
