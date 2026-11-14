"""Phase 7.2C — frontend/backend compatibility checks for the Holographic
memory dashboard.

Confirms the dashboard's `api.js` only ever calls endpoints that actually
exist in `viewer.py` (Phase 7.2B), and that every deferred/unsupported
feature from the original `index.html` (benchmark, memory tiers, an
activity/audit feed) has no surviving reference anywhere in the shipped
dashboard — covering item 15 and the "do not fabricate" requirements from
the Phase 7.2C spec.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from plugins.memory.holographic import viewer as _viewer
from plugins.memory.holographic.store import MemoryStore

from .test_holographic_viewer import _RunningServer, _start_server  # noqa: F401

_ASSETS_DIR = Path(_viewer.__file__).parent / "viewer_assets"
_JS_DIR = _ASSETS_DIR / "js"
_API_JS = (_JS_DIR / "api.js").read_text(encoding="utf-8")
_ALL_JS_SOURCE = "\n".join(p.read_text(encoding="utf-8") for p in _JS_DIR.glob("*.js"))
_HTML_SOURCE = (_ASSETS_DIR / "index.html").read_text(encoding="utf-8")

def _extract_js_api_path_prefixes() -> "set[str]":
    prefixes = set()
    for match in re.finditer(r'[`"](/api/[^"`]*)[`"]', _API_JS):
        literal = match.group(1)
        prefixes.add(re.split(r"\$\{", literal)[0].rstrip("/") or "/api")
    return prefixes


class TestApiPathsMatchTheRealBackend:
    def test_every_api_js_path_prefix_is_a_real_registered_route(self):
        prefixes = _extract_js_api_path_prefixes()
        assert prefixes, "expected api.js to reference at least one /api/ path"
        known_prefixes = (
            "/api/namespaces", "/api/overview", "/api/facts", "/api/search",
            "/api/entities", "/api/timeline",
        )
        for prefix in prefixes:
            assert any(prefix == k or prefix.startswith(k) for k in known_prefixes), (
                f"api.js references {prefix!r}, which is not a Phase 7.2B endpoint"
            )

    def test_api_js_never_references_benchmark_tiers_or_activity_endpoints(self):
        for forbidden in ("/api/benchmark", "/api/tiers", "/api/activity", "/api/health", "/api/categories"):
            assert forbidden not in _ALL_JS_SOURCE, f"unsupported/deferred endpoint referenced: {forbidden}"

    def test_live_server_confirms_every_api_js_path_prefix_resolves(self, tmp_path):
        """Round-trips every prefix found in api.js against a real running
        server — a 404 here would mean api.js drifted from the actual
        backend, not just a typo in this test's own known-prefix list."""
        db_path = tmp_path / "memory_store.db"
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        store.add_fact("A fact for compatibility checks.", category="general", namespace="telegram:chat_COMPAT")
        store.close()

        s = _start_server(db_path)
        try:
            for prefix in _extract_js_api_path_prefixes():
                # Drive each prefix with the minimum query string it needs
                # to reach real endpoint logic (not just the pre-connection
                # 400 for a missing ns) — a bare GET with no ns is enough to
                # prove the ROUTE exists (never a 404 "unknown route").
                resp = s.get(prefix if prefix == "/api/namespaces" else f"{prefix}?ns=")
                assert resp.status != 404, f"{prefix} is not a real route on the live server"
        finally:
            s.stop()


class TestDeferredFeaturesHaveNoSurvivingReference:
    @pytest.mark.parametrize("term", [
        "Benchmark", "benchmark", "R@5", "r_at_5",
    ])
    def test_benchmark_feature_is_fully_absent(self, term):
        assert term not in _HTML_SOURCE
        assert term not in _ALL_JS_SOURCE

    @pytest.mark.parametrize("term", ["tier:", "working/episodic", "Crystal", "tierGrid", "tier_"])
    def test_memory_tier_feature_is_fully_absent(self, term):
        assert term not in _HTML_SOURCE
        assert term not in _ALL_JS_SOURCE

    @pytest.mark.parametrize("term", ["activityList", "op_type", "Activity Feed", "switchActivityFilter"])
    def test_activity_audit_feature_is_fully_absent(self, term):
        assert term not in _HTML_SOURCE
        assert term not in _ALL_JS_SOURCE

    def test_only_five_tabs_exist_matching_implemented_endpoints(self):
        tabs = re.findall(r'data-tab="([a-z]+)"', _HTML_SOURCE)
        assert set(tabs) == {"dashboard", "facts", "search", "entities", "timeline"}


class TestNoDuplicateDashboardImplementation:
    def test_the_old_top_level_index_html_no_longer_exists(self):
        stray_path = Path(_viewer.__file__).parent / "index.html"
        assert not stray_path.exists(), (
            "the pre-Phase-7.2C stray index.html must be migrated, not kept "
            "alongside the canonical viewer_assets/index.html"
        )

    def test_viewer_py_references_exactly_one_dashboard_html_path(self):
        source = Path(_viewer.__file__).read_text(encoding="utf-8")
        assert source.count("_DASHBOARD_HTML_PATH = ") == 1
