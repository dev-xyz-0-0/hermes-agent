// Phase 7.2C — dashboard bootstrap: namespace selection, tab routing, theme,
// and event wiring. Owns the only piece of state shared across tabs; each
// tab module keeps its own pagination/filter state internally.
//
// No inline event handler attributes exist anywhere in index.html — every
// listener is wired here (or inside a tab module) via addEventListener.

import { api } from "./api.js";
import { el, clearChildren, closeFactModal } from "./utils.js";
import { loadDashboard } from "./dashboard.js";
import { loadFacts, resetFactsState, setFactCategory } from "./facts.js";
import { loadEntities, clearGraph, fitGraph } from "./entities.js";
import { runSearch } from "./search.js";
import { loadTimeline } from "./timeline.js";

const state = {
  namespace: null,
  activeTab: "dashboard",
  theme: safeLocalStorageGet("hermes-viewer-theme") || "dark",
};

let tabAbort = null;

function safeLocalStorageGet(key) {
  try {
    return window.localStorage.getItem(key);
  } catch (_err) {
    return null;
  }
}

function safeLocalStorageSet(key, value) {
  try {
    window.localStorage.setItem(key, value);
  } catch (_err) {
    // Private browsing / blocked storage — theme just won't persist.
  }
}

function setTheme(theme) {
  state.theme = theme;
  document.documentElement.setAttribute("data-theme", theme);
  const toggle = document.getElementById("themeToggle");
  if (toggle) toggle.textContent = theme === "dark" ? "🌙" : "☀️";
  safeLocalStorageSet("hermes-viewer-theme", theme);
}

async function loadNamespaces() {
  const select = document.getElementById("namespaceSelect");
  const data = await api.namespaces();
  clearChildren(select);
  if (!data.namespaces.length) {
    select.appendChild(el("option", { value: "", text: "(no namespaces yet)" }));
    setStatus("No namespaces found in this database.", "err");
    return null;
  }
  for (const entry of data.namespaces) {
    const label = entry.namespace === "" ? "(unscoped)" : entry.namespace;
    select.appendChild(el("option", { value: entry.namespace, text: `${label} — ${entry.total} fact(s)` }));
  }
  const first = data.namespaces[0].namespace;
  select.value = first;
  setStatus("ok", "ok");
  return first;
}

function setStatus(text, kind) {
  const dot = document.getElementById("statusDot");
  const label = document.getElementById("statusText");
  if (dot) dot.className = `status-dot ${kind}`;
  if (label) label.textContent = text;
}

function switchTab(tab) {
  state.activeTab = tab;
  document.querySelectorAll(".tab").forEach((tabEl) => tabEl.classList.toggle("active", tabEl.dataset.tab === tab));
  document.querySelectorAll(".tab-content").forEach((panel) => panel.classList.toggle("active", panel.id === `tab-${tab}`));
  loadActiveTab();
}

async function loadActiveTab() {
  if (state.namespace === null) return;
  if (tabAbort) tabAbort.abort();
  tabAbort = new AbortController();
  const { signal } = tabAbort;
  const ns = state.namespace;
  try {
    if (state.activeTab === "dashboard") {
      await loadDashboard(ns, { signal });
    } else if (state.activeTab === "facts") {
      resetFactsState(ns);
      await loadFacts({ signal });
    } else if (state.activeTab === "entities") {
      clearGraph();
      await loadEntities(ns, { signal });
    } else if (state.activeTab === "timeline") {
      await loadTimeline(ns, { signal });
    }
    // The "search" tab loads on demand when the user submits a query —
    // nothing to prefetch when switching to it.
  } catch (err) {
    if (err && err.name === "AbortError") return;
    console.error(`Failed to load the "${state.activeTab}" tab`, err);
  }
}

async function onNamespaceChange(event) {
  if (tabAbort) tabAbort.abort();
  state.namespace = event.target.value;
  // Switching namespaces must drop every previously rendered view, not
  // just the active tab — a stale graph or search result from the old
  // namespace must never linger on screen.
  clearGraph();
  const searchResults = document.getElementById("searchResults");
  if (searchResults) clearChildren(searchResults);
  await loadActiveTab();
}

function wireEvents() {
  document.getElementById("namespaceSelect").addEventListener("change", onNamespaceChange);
  document.getElementById("themeToggle").addEventListener("click", () => setTheme(state.theme === "dark" ? "light" : "dark"));
  document.querySelectorAll(".tab").forEach((tabEl) => tabEl.addEventListener("click", () => switchTab(tabEl.dataset.tab)));
  document.getElementById("modalClose").addEventListener("click", closeFactModal);
  document.getElementById("modalOverlay").addEventListener("click", (event) => {
    if (event.target.id === "modalOverlay") closeFactModal();
  });

  const categorySelect = document.getElementById("factCategory");
  if (categorySelect) categorySelect.addEventListener("change", (event) => setFactCategory(event.target.value));

  document.getElementById("searchForm").addEventListener("submit", (event) => {
    event.preventDefault();
    const q = document.getElementById("searchInput").value.trim();
    const mode = document.getElementById("searchMode").value;
    runSearch(state.namespace, { q, mode });
  });

  const fitButton = document.getElementById("graphFitButton");
  if (fitButton) fitButton.addEventListener("click", fitGraph);
}

async function init() {
  setTheme(state.theme);
  wireEvents();
  try {
    const first = await loadNamespaces();
    if (first !== null) {
      state.namespace = first;
      await loadActiveTab();
    }
  } catch (err) {
    console.error("Failed to initialize the dashboard", err);
    setStatus(`Error: ${err && err.message ? err.message : "failed to load"}`, "err");
  }
}

init();
