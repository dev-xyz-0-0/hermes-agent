// Phase 7.2C — Overview tab: scoped counts, category/trust-score charts,
// and a short list of recent facts. Everything comes from one call to
// GET /api/overview?ns=... (Phase 7.2B) — no client-side aggregation.

import { api } from "./api.js";
import { renderChart } from "./charts.js";
import { el, clearChildren, formatScore, truncate } from "./utils.js";

export async function loadDashboard(ns, { signal } = {}) {
  const data = await api.overview(ns, { signal });
  renderStatsCards(data);
  renderCategoryChart(data);
  renderTrustChart(data);
  renderRecentFacts(data.recent_facts || []);
  return data;
}

function renderStatsCards(data) {
  const row = document.getElementById("statsRow");
  clearChildren(row);
  const cards = [
    ["Active facts", data.facts.active],
    ["Superseded", data.facts.superseded],
    ["Unknown status", data.facts.unknown],
    ["Forgotten", data.facts.forgotten],
    ["Entities", data.entities_total],
    ["Relationships", data.relationships_total],
  ];
  for (const [label, value] of cards) {
    row.appendChild(
      el("div", { className: "card" }, [
        el("div", { className: "value", text: String(value ?? 0) }),
        el("div", { className: "label", text: label }),
      ]),
    );
  }
}

function renderCategoryChart(data) {
  const categories = data.categories || {};
  renderChart("chartCategories", {
    type: "pie",
    data: {
      labels: Object.keys(categories),
      datasets: [{ data: Object.values(categories) }],
    },
    options: { responsive: true },
  });
}

function renderTrustChart(data) {
  const buckets = data.trust_distribution || [];
  renderChart("chartTrust", {
    type: "bar",
    data: {
      labels: buckets.map((b) => `${b.bucket}%`),
      datasets: [{ label: "Facts", data: buckets.map((b) => b.count) }],
    },
    options: { responsive: true, scales: { y: { beginAtZero: true } } },
  });
}

function renderRecentFacts(facts) {
  const container = document.getElementById("recentFacts");
  clearChildren(container);
  if (!facts.length) {
    container.appendChild(el("div", { className: "empty", text: "No facts yet in this namespace." }));
    return;
  }
  for (const fact of facts) {
    container.appendChild(
      el("div", { className: "fact-item" }, [
        el("span", { className: "fact-id", text: `#${fact.fact_id}` }),
        el("div", { className: "fact-content", text: truncate(fact.content, 200) }),
        el("span", { className: "fact-meta", text: `${fact.category} · ⭐ ${formatScore(fact.trust_score)}` }),
      ]),
    );
  }
}
