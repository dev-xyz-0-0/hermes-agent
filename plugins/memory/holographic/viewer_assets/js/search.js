// Phase 7.2C — Search tab: literal or FTS5 search through GET
// /api/search?ns=... (Phase 7.2B/7.1E). The frontend never constructs an
// FTS5 MATCH expression itself — it only ever sends the raw query text and
// a `mode` hint; the server decides what actually ran and reports it back
// via `mode_used`/`fallback_reason`.

import { api } from "./api.js";
import { el, clearChildren, truncate, formatScore, openFactModal } from "./utils.js";

let currentAbort = null;

export async function runSearch(ns, { q, mode }) {
  const results = document.getElementById("searchResults");
  const notice = document.getElementById("searchModeNotice");
  clearChildren(results);
  if (notice) notice.textContent = "";
  if (!q) return;

  if (currentAbort) currentAbort.abort();
  currentAbort = new AbortController();
  try {
    const data = await api.search(ns, { q, mode, limit: 30 }, { signal: currentAbort.signal });
    renderResults(data);
  } catch (err) {
    if (err && err.name === "AbortError") return;
    clearChildren(results);
    results.appendChild(el("div", { className: "empty", text: "Search failed." }));
  }
}

function renderResults(data) {
  const results = document.getElementById("searchResults");
  const notice = document.getElementById("searchModeNotice");
  clearChildren(results);

  if (notice) {
    notice.textContent = data.fallback_reason
      ? data.fallback_reason
      : data.mode_used === "fts"
        ? "Full-text (FTS5) search results, ranked by relevance."
        : "";
  }

  if (!data.results.length) {
    results.appendChild(el("div", { className: "empty", text: `No results for "${data.query}".` }));
    return;
  }
  for (const fact of data.results) {
    const item = el("div", { className: "fact-item" }, [
      el("span", { className: "fact-id", text: `#${fact.fact_id}` }),
      el("div", { className: "fact-content", text: truncate(fact.content, 250) }),
      el("span", { className: "fact-meta", text: `${fact.category} · ⭐ ${formatScore(fact.trust_score)}` }),
    ]);
    item.addEventListener("click", () => openFactModal(fact));
    results.appendChild(item);
  }
}
