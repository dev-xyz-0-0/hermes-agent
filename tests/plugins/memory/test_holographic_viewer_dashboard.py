"""Phase 7.2C — dashboard route and static-asset-allowlist tests for the
Holographic memory viewer (`plugins/memory/holographic/viewer.py`).

Covers item 1-4, 15-18 of the Phase 7.2C test list: the `/dashboard` route,
the extended Phase 7.2A allowlist, dashboard-specific security headers, and
legacy-route/Phase 7.2A/7.2B regression compatibility.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from plugins.memory.holographic import viewer as _viewer
from plugins.memory.holographic.store import MemoryStore

from .test_holographic_viewer import _RunningServer, _start_server  # noqa: F401

_ASSETS_DIR = Path(_viewer.__file__).parent / "viewer_assets"


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "memory_store.db"
    store = MemoryStore(db_path=path, hrr_dim=32)
    store.add_fact("A fact for dashboard route tests.", category="general", namespace="telegram:chat_DASHROUTE")
    store.close()
    return path


class TestDashboardRoute:
    def test_dashboard_returns_200(self, db_path):
        s = _start_server(db_path)
        try:
            resp = s.get("/dashboard")
            assert resp.status == 200
        finally:
            s.stop()

    def test_dashboard_is_served_from_the_canonical_asset_file_verbatim(self, db_path):
        on_disk = (_ASSETS_DIR / "index.html").read_bytes()
        s = _start_server(db_path)
        try:
            resp = s.get("/dashboard")
            assert resp.body == on_disk
        finally:
            s.stop()

    def test_dashboard_content_type_and_cache_control(self, db_path):
        s = _start_server(db_path)
        try:
            resp = s.get("/dashboard")
            assert resp.getheader("Content-Type") == "text/html; charset=utf-8"
            assert resp.getheader("Cache-Control") == "no-store"
        finally:
            s.stop()

    def test_dashboard_has_its_own_stricter_script_csp(self, db_path):
        s = _start_server(db_path)
        try:
            resp = s.get("/dashboard")
            csp = resp.getheader("Content-Security-Policy")
            assert "script-src 'self'" in csp
            assert "unsafe-eval" not in csp
            # script-src must carry no inline/eval allowance; style-src may
            # (Chart.js sets canvas.style directly — see viewer.py's
            # _DASHBOARD_SECURITY_HEADERS comment).
            script_src_clause = [c.strip() for c in csp.split(";") if c.strip().startswith("script-src")][0]
            assert "unsafe-inline" not in script_src_clause
        finally:
            s.stop()

    def test_dashboard_head_matches_get(self, db_path):
        s = _start_server(db_path)
        try:
            get_resp = s.get("/dashboard")
            head_resp = s.request("HEAD", "/dashboard")
            assert head_resp.status == 200
            assert head_resp.body == b""
            assert head_resp.getheader("Content-Length") == get_resp.getheader("Content-Length")
        finally:
            s.stop()

    @pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
    def test_dashboard_rejects_mutation_methods(self, db_path, method):
        s = _start_server(db_path)
        try:
            resp = s.request(method, "/dashboard")
            assert resp.status == 405
        finally:
            s.stop()


class TestExtendedStaticAllowlist:
    @pytest.mark.parametrize("name", sorted({
        "css/viewer.css", "js/app.js", "js/api.js", "js/charts.js", "js/dashboard.js",
        "js/facts.js", "js/entities.js", "js/search.js", "js/timeline.js", "js/utils.js",
        "vendor/chart.umd.4.4.7.min.js", "vendor/vis-network.9.1.9.min.js",
    }))
    def test_approved_asset_is_served(self, db_path, name):
        s = _start_server(db_path)
        try:
            resp = s.get(f"/static/{name}")
            assert resp.status == 200
            assert resp.body == (_ASSETS_DIR / name).read_bytes()
        finally:
            s.stop()

    @pytest.mark.parametrize("name,expected_prefix", [
        ("css/viewer.css", "text/css"),
        ("js/app.js", "application/javascript"),
    ])
    def test_approved_asset_content_type(self, db_path, name, expected_prefix):
        s = _start_server(db_path)
        try:
            resp = s.get(f"/static/{name}")
            assert resp.getheader("Content-Type").startswith(expected_prefix)
        finally:
            s.stop()

    @pytest.mark.parametrize("name", [
        "js/not_a_real_file.js", "css/not_real.css", "js/../viewer.py",
        "js/app.js.bak", "benchmark.js", "js/tiers.js", "js/activity.js",
    ])
    def test_unapproved_asset_is_404(self, db_path, name):
        s = _start_server(db_path)
        try:
            resp = s.get(f"/static/{name}")
            assert resp.status == 404
        finally:
            s.stop()

    def test_dashboard_css_and_js_are_not_cacheable_unlike_pinned_vendor_files(self, db_path):
        s = _start_server(db_path)
        try:
            css_resp = s.get("/static/css/viewer.css")
            vendor_resp = s.get("/static/vendor/chart.umd.4.4.7.min.js")
            assert css_resp.getheader("Cache-Control") == "no-store"
            assert vendor_resp.getheader("Cache-Control") != "no-store"
            assert "immutable" in vendor_resp.getheader("Cache-Control")
        finally:
            s.stop()


class TestLegacyAndPriorPhaseCompatibility:
    def test_legacy_routes_still_work_alongside_the_dashboard(self, db_path):
        s = _start_server(db_path)
        try:
            assert s.get("/").status == 200
            assert s.get("/dashboard").status == 200
        finally:
            s.stop()

    def test_index_page_csp_is_unaffected_by_the_dashboard_route(self, db_path):
        """The legacy pages' CSP must stay exactly as restrictive as before
        — _DASHBOARD_SECURITY_HEADERS is a separate dict, never a mutation
        of _SECURITY_HEADERS."""
        s = _start_server(db_path)
        try:
            resp = s.get("/")
            csp = resp.getheader("Content-Security-Policy")
            assert "script-src" not in csp  # default-src 'none' blocks scripts entirely
            assert "default-src 'none'" in csp
        finally:
            s.stop()

    def test_api_routes_still_work_alongside_the_dashboard(self, db_path):
        s = _start_server(db_path)
        try:
            resp = s.get("/api/namespaces")
            assert resp.status == 200
            assert resp.getheader("Content-Type") == "application/json; charset=utf-8"
        finally:
            s.stop()
