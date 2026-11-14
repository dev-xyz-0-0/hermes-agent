// Phase 7.2C — small DOM/formatting helpers shared by every tab module.
//
// `el()` only ever assigns to `.textContent` for a "text" prop (never
// `.innerHTML`), so nothing built with it can inject markup or break out of
// an attribute — fact content, tags, and entity names are untrusted data
// (they come from captured conversation text) and are rendered through this
// helper everywhere, never via string-concatenated HTML.

export function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "className") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "onclick" || key.startsWith("on")) {
      throw new Error("el(): inline event handler props are not supported — use addEventListener");
    } else {
      node.setAttribute(key, value);
    }
  }
  for (const child of children) node.appendChild(child);
  return node;
}

export function clearChildren(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

export function formatScore(value) {
  return (typeof value === "number" ? value : 0).toFixed(2);
}

export function truncate(text, maxLength) {
  text = text || "";
  return text.length > maxLength ? text.slice(0, maxLength) + "…" : text;
}

// ---------------------------------------------------------------------------
// Fact detail modal — shared by the Facts and Search tabs. Content is set
// via textContent only.
// ---------------------------------------------------------------------------

export function openFactModal(fact) {
  document.getElementById("modalTitle").textContent = `Fact #${fact.fact_id}`;
  const body = document.getElementById("modalBody");
  clearChildren(body);
  body.appendChild(el("p", { text: fact.content || "(empty)" }));
  const metaLines = [
    `Category: ${fact.category || "—"}`,
    `Status: ${fact.status || "—"}`,
    `Trust score: ${formatScore(fact.trust_score)}`,
  ];
  if (fact.tags) metaLines.push(`Tags: ${fact.tags}`);
  if (fact.created_at) metaLines.push(`Created: ${fact.created_at}`);
  for (const line of metaLines) {
    body.appendChild(el("p", { className: "muted", text: line }));
  }
  document.getElementById("modalOverlay").classList.remove("hidden");
}

export function closeFactModal() {
  document.getElementById("modalOverlay").classList.add("hidden");
}
