// Phase 7.2C — Facts explorer tab: server-side paginated listing with an
// optional category filter (both enforced by GET /api/facts?ns=... —
// nothing here loads more than one page's worth of rows).

import { api } from "./api.js";
import { el, clearChildren, truncate, formatScore, openFactModal } from "./utils.js";

const state = { ns: null, offset: 0, limit: 25, category: "" };
let currentAbort = null;

export function resetFactsState(ns) {
  state.ns = ns;
  state.offset = 0;
  state.category = "";
  const categorySelect = document.getElementById("factCategory");
  if (categorySelect) categorySelect.value = "";
}

// A pagination click calls loadFacts() with no external signal, so it
// makes its own AbortController (below) — independent of app.js's
// tab-level AbortController. If the namespace changes while that request
// is still in flight (e.g. the user clicked "next" then switched
// namespaces before it resolved), only this cancels it; app.js's own
// AbortController never sees it. Must be called on every namespace
// switch, regardless of which tab is active, or a stale page of facts
// can still land after the DOM was cleared.
export function cancelPendingFactsRequest() {
  if (currentAbort) {
    currentAbort.abort();
    currentAbort = null;
  }
}

export async function loadFacts({ signal } = {}) {
  if (currentAbort) currentAbort.abort();
  currentAbort = signal ? null : new AbortController();
  const useSignal = signal || currentAbort.signal;
  const data = await api.facts(
    state.ns,
    { limit: state.limit, offset: state.offset, category: state.category || undefined },
    { signal: useSignal },
  );
  renderFactsTable(data.facts || []);
  renderPagination(data.pagination);
  return data;
}

function renderFactsTable(facts) {
  const tbody = document.getElementById("factsBody");
  clearChildren(tbody);
  if (!facts.length) {
    tbody.appendChild(el("tr", {}, [el("td", { colspan: "6", className: "empty", text: "No facts match this filter." })]));
    return;
  }
  for (const fact of facts) {
    const row = el("tr", {}, [
      el("td", { text: String(fact.fact_id) }),
      el("td", { text: truncate(fact.content, 120) }),
      el("td", { text: fact.category || "" }),
      el("td", { text: fact.status || "" }),
      el("td", { text: formatScore(fact.trust_score) }),
      el("td", { text: (fact.created_at || "").slice(0, 10) }),
    ]);
    row.addEventListener("click", () => openFactModal(fact));
    tbody.appendChild(row);
  }
}

function renderPagination(pagination) {
  const container = document.getElementById("factsPagination");
  clearChildren(container);
  const { limit, offset, total, has_more } = pagination;

  const prev = el("button", { text: "« prev" });
  prev.disabled = offset === 0;
  prev.addEventListener("click", () => {
    state.offset = Math.max(0, offset - limit);
    loadFacts();
  });

  const info = el("span", {
    className: "page-info",
    text: total === 0 ? "0 of 0" : `${offset + 1}–${Math.min(offset + limit, total)} of ${total}`,
  });

  const next = el("button", { text: "next »" });
  next.disabled = !has_more;
  next.addEventListener("click", () => {
    state.offset = offset + limit;
    loadFacts();
  });

  container.appendChild(prev);
  container.appendChild(info);
  container.appendChild(next);
}

export function setFactCategory(category) {
  state.category = category;
  state.offset = 0;
  return loadFacts();
}
