// Phase 7.2C — thin Chart.js wrapper. Reuses one Chart instance per
// canvas id, destroying the previous one first, so switching namespaces or
// tabs never leaks chart instances or double-renders.

const registry = new Map();

export function renderChart(canvasId, config) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return null;
  const existing = registry.get(canvasId);
  if (existing) existing.destroy();
  const chart = new Chart(canvas, config);
  registry.set(canvasId, chart);
  return chart;
}

export function destroyChart(canvasId) {
  const existing = registry.get(canvasId);
  if (existing) {
    existing.destroy();
    registry.delete(canvasId);
  }
}

export function destroyAllCharts() {
  for (const chart of registry.values()) chart.destroy();
  registry.clear();
}
