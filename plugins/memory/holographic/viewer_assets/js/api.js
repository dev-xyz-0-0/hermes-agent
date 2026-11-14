// Phase 7.2C — centralized, read-only API client for the Hermes Holographic
// Memory dashboard. Every call here is a GET against the Phase 7.2B
// namespace-scoped JSON API; nothing in this module ever mutates data, and
// nothing stores credentials or memory content in browser storage.

export class ApiError extends Error {
  constructor(status, body) {
    super((body && body.error && body.error.message) || `Request failed (${status})`);
    this.status = status;
    this.code = body && body.error ? body.error.code : null;
    this.body = body;
  }
}

export async function fetchJson(path, params = {}, { signal } = {}) {
  const url = new URL(path, window.location.origin);
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") {
      search.set(key, String(value));
    } else if (key === "ns" && value === "") {
      // `ns=` (empty) is the valid unscoped namespace, not a missing
      // parameter — see viewer.py's _parse_str docstring. Must survive as
      // an explicit empty value, never be dropped by the `value !== ""`
      // check above.
      search.set(key, "");
    }
  }
  url.search = search.toString();

  const response = await fetch(url.toString(), {
    method: "GET",
    credentials: "same-origin",
    cache: "no-store",
    signal,
  });

  let body = null;
  try {
    body = await response.json();
  } catch (_err) {
    body = null;
  }
  if (!response.ok) {
    throw new ApiError(response.status, body);
  }
  return body;
}

export const api = {
  namespaces(opts) {
    return fetchJson("/api/namespaces", {}, opts);
  },
  overview(ns, opts) {
    return fetchJson("/api/overview", { ns }, opts);
  },
  facts(ns, { limit, offset, category } = {}, opts) {
    return fetchJson("/api/facts", { ns, limit, offset, category }, opts);
  },
  fact(ns, factId, opts) {
    return fetchJson(`/api/facts/${encodeURIComponent(factId)}`, { ns }, opts);
  },
  search(ns, { q, mode, limit, offset } = {}, opts) {
    return fetchJson("/api/search", { ns, q, mode, limit, offset }, opts);
  },
  entities(ns, { limit, offset } = {}, opts) {
    return fetchJson("/api/entities", { ns, limit, offset }, opts);
  },
  entityGraph(ns, entityId, opts) {
    return fetchJson(`/api/entities/${encodeURIComponent(entityId)}/graph`, { ns }, opts);
  },
  timeline(ns, { days } = {}, opts) {
    return fetchJson("/api/timeline", { ns, days }, opts);
  },
};
