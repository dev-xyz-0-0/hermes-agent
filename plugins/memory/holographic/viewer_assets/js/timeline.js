// Phase 7.2C — Timeline tab: fact-creation trend from GET
// /api/timeline?ns=... (Phase 7.2B). The API's `timestamp_basis` field is
// always "created_at" — surfaced verbatim here so the UI never implies an
// update (a trust-score bump, a recall) is a new event.

import { api } from "./api.js";
import { renderChart } from "./charts.js";

export async function loadTimeline(ns, { signal } = {}) {
  const data = await api.timeline(ns, { days: 30 }, { signal });
  const days = data.days || [];

  renderChart("chartTimeline", {
    type: "bar",
    data: {
      labels: days.map((d) => d.day),
      datasets: [{ label: "Facts created", data: days.map((d) => d.count) }],
    },
    options: { responsive: true, scales: { y: { beginAtZero: true } } },
  });

  renderChart("chartTimelineTrust", {
    type: "line",
    data: {
      labels: days.map((d) => d.day),
      datasets: [{ label: "Average trust score", data: days.map((d) => d.avg_trust) }],
    },
    options: { responsive: true, scales: { y: { beginAtZero: true, max: 1 } } },
  });

  const basisNotice = document.getElementById("timelineBasisNotice");
  if (basisNotice) {
    basisNotice.textContent = days.length
      ? `Based on fact creation time (${data.timestamp_basis}), not last update.`
      : "No facts yet in this namespace.";
  }
  return data;
}
