"""Phase 7.2D — real-browser regression coverage for the Holographic memory
dashboard, using a minimal stdlib-only Chrome DevTools Protocol client
(`_browser_cdp.py`) instead of a new playwright/selenium dependency.

These tests drive an actual headless Chrome instance to verify behavior the
static source checks in `test_holographic_viewer_frontend_security.py`
can't reach: whether a namespace switch actually clears a *hidden* tab's
rendered DOM, and whether an in-flight per-module request (facts
pagination, the entities tab's auto-selected graph fetch, an on-demand
search) can still repopulate stale content after that clear. Both gaps
were found and fixed in Phase 7.2D (see `viewer_assets/js/app.js`'s
`clearAllTabPanels()` and the `cancelPendingFactsRequest`/
`cancelPendingSearch`/`clearGraph` exports it calls).

If no headless-Chrome-compatible binary is available, every test here
skips (never fails) — this module intentionally degrades to "not
exercised in this environment" rather than breaking the suite, consistent
with the Phase 7.2C/D specs' own allowance to document manual verification
when automated browser interaction isn't available.
"""

from __future__ import annotations

import tempfile
import threading

import pytest

from plugins.memory.holographic import viewer as _viewer
from plugins.memory.holographic.store import MemoryStore

from ._browser_cdp import ChromeProcess, CDPTab, find_chrome

pytestmark = pytest.mark.skipif(find_chrome() is None, reason="no headless-Chrome-compatible binary on PATH")


class _RunningServer:
    def __init__(self, server, thread):
        self.server = server
        self.thread = thread

    @property
    def port(self):
        return self.server.server_address[1]

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _start_server(db_path):
    server = _viewer.create_server(db_path, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return _RunningServer(server, thread)


@pytest.fixture(scope="module")
def chrome():
    tmp_dir = tempfile.mkdtemp(prefix="hermes_cdp_profile_")
    try:
        proc = ChromeProcess(tmp_dir)
    except RuntimeError as exc:
        pytest.skip(f"headless Chrome unavailable: {exc}")
    yield proc
    proc.close()


@pytest.fixture
def two_namespace_server(tmp_path):
    """Namespace A: many facts plus a directly-seeded entity relationship
    (so a graph auto-selects). Namespace B: few facts, one entity — small
    enough that the two are trivially distinguishable by fact count, which
    the dashboard's own namespace-select labels expose (`{digest} — {n}
    fact(s)`)."""
    db_path = tmp_path / "memory_store.db"
    store = MemoryStore(db_path=db_path, hrr_dim=32)
    ns_a = "telegram:chat_CDPA"
    ns_b = "telegram:chat_CDPB"
    for i in range(10):
        store.add_fact(f"Routine fact {i} in namespace A.", category="general", namespace=ns_a)

    # /api/entities scopes an entity to a namespace via fact_entities -> a
    # fact in that namespace (not entity_relationships.namespace, which
    # only scopes the separate /graph endpoint) — so both a fact_entities
    # link AND a relationship row are needed for the entity to both list
    # in the grid and auto-select a non-empty graph.
    entity_a_fact = store.add_fact("A fact linking to EntityA.", category="general", namespace=ns_a)
    store._conn.execute("INSERT INTO entities (name) VALUES ('EntityA')")
    entity_a = store._conn.execute("SELECT entity_id FROM entities WHERE name = 'EntityA'").fetchone()["entity_id"]
    store._conn.execute("INSERT INTO fact_entities (fact_id, entity_id) VALUES (?, ?)", (entity_a_fact, entity_a))
    other = store._conn.execute("INSERT INTO entities (name) VALUES ('OtherA') RETURNING entity_id").fetchone()["entity_id"]
    store._conn.execute(
        "INSERT INTO entity_relationships "
        "(source_entity_id, target_entity_id, relationship_type, namespace, strength, confidence, status) "
        "VALUES (?, ?, 'related_to', ?, 0.5, 1.0, 'active')",
        (entity_a, other, ns_a),
    )
    store._conn.commit()

    b_fact = store.add_fact("A single fact in namespace B.", category="general", namespace=ns_b)
    store._conn.execute("INSERT INTO entities (name) VALUES ('EntityB')")
    entity_b = store._conn.execute("SELECT entity_id FROM entities WHERE name = 'EntityB'").fetchone()["entity_id"]
    store._conn.execute("INSERT INTO fact_entities (fact_id, entity_id) VALUES (?, ?)", (b_fact, entity_b))
    store._conn.commit()
    store.close()

    server = _start_server(db_path)
    try:
        yield server
    finally:
        server.stop()


def _digests_by_fact_count(tab: CDPTab):
    """Returns (digest_with_more_facts, digest_with_fewer_facts)."""
    opts = tab.eval(
        "Array.from(document.getElementById('namespaceSelect').options)"
        ".map(o => ({value: o.value, count: parseInt(o.textContent.match(/(\\d+) fact/)[1])}))"
    )
    opts.sort(key=lambda o: -o["count"])
    return opts[0]["value"], opts[-1]["value"]


def _select_namespace(tab: CDPTab, digest: str):
    tab.eval(
        "(function(){"
        "const sel = document.getElementById('namespaceSelect');"
        f"sel.value = {digest!r};"
        "sel.dispatchEvent(new Event('change'));"
        "})();"
    )


class TestHiddenTabClearedOnNamespaceSwitch:
    """The exact regression Task 1 asks for: namespace A's content must be
    absent from both visible AND hidden DOM after switching to B."""

    def test_entities_tab_dom_is_cleared_even_when_hidden_at_switch_time(self, chrome, two_namespace_server):
        tab = CDPTab(chrome)
        try:
            tab.navigate(f"http://127.0.0.1:{two_namespace_server.port}/dashboard", wait_s=1.5)
            digest_a, digest_b = _digests_by_fact_count(tab)

            _select_namespace(tab, digest_a)
            tab.drain(1.0)
            tab.eval('document.querySelector(\'[data-tab="entities"]\').click()')
            tab.drain(1.2)
            assert tab.eval("document.querySelectorAll('#entityGrid .entity-chip').length") >= 1
            assert "EntityA" in tab.eval("document.getElementById('entityGrid').textContent")

            # Move away from Entities (now hidden) before switching namespace.
            tab.eval('document.querySelector(\'[data-tab="facts"]\').click()')
            tab.drain(0.8)
            _select_namespace(tab, digest_b)
            tab.drain(1.2)

            # Inspect the hidden Entities tab's DOM directly, without revisiting it.
            hidden_grid_text = tab.eval("document.getElementById('entityGrid').textContent")
            hidden_graph_title = tab.eval("document.getElementById('graphTitle').textContent")
            hidden_graph_html = tab.eval("document.getElementById('graph').innerHTML")
            assert "EntityA" not in hidden_grid_text
            assert hidden_graph_title == "Select an entity to see its relationships"
            assert hidden_graph_html.strip() == ""
            assert "EntityA" not in tab.eval("document.body.textContent")
        finally:
            tab.close()


class TestOrphanedPerModuleRequestsCannotRepopulateAfterSwitch:
    """Each of facts.js/entities.js/search.js makes its own AbortController
    for a request `app.js`'s own tab-level AbortController doesn't cover
    (pagination clicks, the auto-selected graph fetch, an on-demand
    search). Delaying each one artificially and switching namespace mid-
    flight proves `clearAllTabPanels()` actually cancels them, not just
    the DOM."""

    def test_delayed_entity_graph_fetch_is_discarded_after_namespace_switch(self, chrome, two_namespace_server):
        tab = CDPTab(chrome)
        try:
            tab.navigate(f"http://127.0.0.1:{two_namespace_server.port}/dashboard", wait_s=1.5)
            digest_a, digest_b = _digests_by_fact_count(tab)

            tab.eval(
                "(function(){"
                "const real = window.fetch.bind(window);"
                "window.fetch = function(url, opts) {"
                "  const u = String(url);"
                "  if (u.includes('/graph?')) {"
                "    return new Promise((resolve, reject) => setTimeout(() => real(url, opts).then(resolve, reject), 1500));"
                "  }"
                "  return real(url, opts);"
                "};"
                "})();"
            )
            _select_namespace(tab, digest_a)
            tab.drain(0.3)
            tab.eval('document.querySelector(\'[data-tab="entities"]\').click()')
            tab.drain(0.3)  # entities list resolves fast; the graph fetch is now delayed in flight
            _select_namespace(tab, digest_b)
            tab.drain(2.2)  # long enough for the delayed fetch to land, if unprotected

            graph_title = tab.eval("document.getElementById('graphTitle').textContent")
            assert "EntityA" not in graph_title
            assert tab.eval("document.getElementById('entityGrid').textContent").count("EntityB") >= 1
        finally:
            tab.close()

    def test_delayed_facts_pagination_fetch_is_discarded_after_namespace_switch(self, chrome, two_namespace_server):
        tab = CDPTab(chrome)
        try:
            tab.navigate(f"http://127.0.0.1:{two_namespace_server.port}/dashboard", wait_s=1.5)
            digest_a, digest_b = _digests_by_fact_count(tab)

            tab.eval(
                "(function(){"
                "const real = window.fetch.bind(window);"
                "window.fetch = function(url, opts) {"
                "  const u = String(url);"
                "  if (u.includes('/api/facts?') && u.includes('offset=5')) {"
                "    return new Promise((resolve, reject) => setTimeout(() => real(url, opts).then(resolve, reject), 1500));"
                "  }"
                "  return real(url, opts);"
                "};"
                "})();"
            )
            _select_namespace(tab, digest_a)
            tab.drain(0.3)
            tab.eval('document.querySelector(\'[data-tab="facts"]\').click()')
            tab.drain(0.5)
            tab.eval(
                "(function(){"
                "const btns = Array.from(document.querySelectorAll('#factsPagination button'));"
                "const next = btns.find(b => b.textContent.includes('next'));"
                "if (next) next.click();"
                "})();"
            )
            tab.drain(0.3)  # delayed offset=5 request now in flight
            _select_namespace(tab, digest_b)
            tab.drain(2.2)

            pagination_text = tab.eval("document.getElementById('factsPagination').textContent")
            # Namespace B has exactly one fact — any multi-row pagination
            # state here would mean A's stale page landed.
            assert "of 1" in pagination_text
        finally:
            tab.close()

    def test_delayed_search_fetch_is_discarded_after_namespace_switch(self, chrome, two_namespace_server):
        tab = CDPTab(chrome)
        try:
            tab.navigate(f"http://127.0.0.1:{two_namespace_server.port}/dashboard", wait_s=1.5)
            digest_a, digest_b = _digests_by_fact_count(tab)

            tab.eval(
                "(function(){"
                "const real = window.fetch.bind(window);"
                "window.fetch = function(url, opts) {"
                "  const u = String(url);"
                "  if (u.includes('/api/search?')) {"
                "    return new Promise((resolve, reject) => setTimeout(() => real(url, opts).then(resolve, reject), 1500));"
                "  }"
                "  return real(url, opts);"
                "};"
                "})();"
            )
            _select_namespace(tab, digest_a)
            tab.drain(0.3)
            tab.eval('document.querySelector(\'[data-tab="search"]\').click()')
            tab.drain(0.3)
            tab.eval(
                "(function(){"
                "document.getElementById('searchInput').value = 'Routine';"
                "document.getElementById('searchForm').dispatchEvent(new Event('submit', {cancelable:true}));"
                "})();"
            )
            tab.drain(0.3)  # delayed search now in flight, submitted under namespace A
            _select_namespace(tab, digest_b)
            tab.drain(2.2)

            results_text = tab.eval("document.getElementById('searchResults').textContent")
            assert results_text.strip() == ""
        finally:
            tab.close()


class TestDashboardRendersCleanly:
    def test_no_console_errors_or_csp_violations_across_every_tab(self, chrome, two_namespace_server):
        tab = CDPTab(chrome)
        try:
            tab.navigate(f"http://127.0.0.1:{two_namespace_server.port}/dashboard", wait_s=1.5)
            digest_a, _ = _digests_by_fact_count(tab)
            _select_namespace(tab, digest_a)
            tab.drain(1.0)
            for name in ("facts", "search", "entities", "timeline", "dashboard"):
                tab.eval(f'document.querySelector(\'[data-tab="{name}"]\').click()')
                tab.drain(0.8)
            assert tab.csp_violations == []
            assert [m for m in tab.console_messages if m["level"] == "error"] == []
        finally:
            tab.close()
