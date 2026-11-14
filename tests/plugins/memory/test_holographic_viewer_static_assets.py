"""Phase 7.2A — static asset serving for the Holographic memory viewer
(`plugins/memory/holographic/viewer.py`).

Covers the `/static/<name>` allowlisted file route added to vendor pinned
dashboard JavaScript (Chart.js, vis-network) locally, with no runtime CDN
request. This phase is deliberately scoped to asset serving only — no JSON
API, no dashboard HTML, no database-backed feature. See viewer_assets/
vendor/VENDORED.md for the pinned versions/licenses/checksums this test
file verifies against.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from plugins.memory.holographic import viewer as _viewer
from plugins.memory.holographic.store import MemoryStore

from .test_holographic_viewer import _RunningServer, _start_server  # noqa: F401

_ASSETS_DIR = Path(_viewer.__file__).parent / "viewer_assets"

# Recorded independently of VENDORED.md so a corrupted/edited vendor file
# fails this test even if VENDORED.md was edited to match it.
_EXPECTED_CHECKSUMS = {
    "vendor/chart.umd.min.js": "206b6e8bb00fc7bba2c7ee80ca41db3e9e05ba7be0aa35abeba9cfd5357f5d0e",
    "vendor/vis-network.min.js": "f53f833ddb9bf97efe856bb0637d4fe88f39e39999c7e94a4b8afc8de8a1a2e5",
}


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "memory_store.db"
    MemoryStore(db_path=path, hrr_dim=32).close()
    return path


class TestVendoredFileIntegrity:
    @pytest.mark.parametrize("name", sorted(_EXPECTED_CHECKSUMS))
    def test_on_disk_checksum_matches_recorded_value(self, name):
        file_path = _ASSETS_DIR / name
        assert file_path.is_file(), f"missing vendored asset: {name}"
        digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
        assert digest == _EXPECTED_CHECKSUMS[name]

    def test_vendored_md_documents_both_files(self):
        doc = (_ASSETS_DIR / "vendor" / "VENDORED.md").read_text(encoding="utf-8")
        for name in _EXPECTED_CHECKSUMS:
            assert name.split("/")[-1] in doc
        for digest in _EXPECTED_CHECKSUMS.values():
            assert digest in doc


class TestNoRuntimeCdnReferences:
    def test_viewer_module_source_contains_no_cdn_or_external_url(self):
        source = Path(_viewer.__file__).read_text(encoding="utf-8")
        for needle in ("cdn.jsdelivr.net", "unpkg.com", "cdnjs.cloudflare.com", "http://", "https://"):
            assert needle not in source, f"unexpected external reference in viewer.py: {needle!r}"


class TestAllowlistedAssetAccess:
    @pytest.mark.parametrize("name,expected_file", sorted({
        "vendor/chart.umd.min.js": "chart.umd.min.js",
        "vendor/vis-network.min.js": "vis-network.min.js",
    }.items()))
    def test_allowlisted_asset_served_with_correct_body(self, db_path, name, expected_file):
        s = _start_server(db_path)
        try:
            resp = s.get(f"/static/{name}")
            assert resp.status == 200
            on_disk = (_ASSETS_DIR / "vendor" / expected_file).read_bytes()
            assert resp.body == on_disk
        finally:
            s.stop()

    def test_content_type_is_javascript(self, db_path):
        s = _start_server(db_path)
        try:
            resp = s.get("/static/vendor/chart.umd.min.js")
            assert resp.getheader("Content-Type") == "application/javascript; charset=utf-8"
        finally:
            s.stop()

    def test_nosniff_header_present(self, db_path):
        s = _start_server(db_path)
        try:
            resp = s.get("/static/vendor/chart.umd.min.js")
            assert resp.getheader("X-Content-Type-Options") == "nosniff"
        finally:
            s.stop()

    def test_vendor_assets_are_cacheable_unlike_every_other_response(self, db_path):
        s = _start_server(db_path)
        try:
            resp = s.get("/static/vendor/chart.umd.min.js")
            cache_control = resp.getheader("Cache-Control")
            assert cache_control != "no-store"
            assert "immutable" in cache_control
        finally:
            s.stop()

    def test_data_pages_still_default_to_no_store(self, db_path):
        """Regression: adding a cacheable asset class must not loosen
        caching on the existing sensitive server-rendered pages."""
        s = _start_server(db_path)
        try:
            resp = s.get("/")
            assert resp.getheader("Cache-Control") == "no-store"
        finally:
            s.stop()

    def test_head_matches_get_headers_with_no_body(self, db_path):
        s = _start_server(db_path)
        try:
            get_resp = s.get("/static/vendor/vis-network.min.js")
            head_resp = s.request("HEAD", "/static/vendor/vis-network.min.js")
            assert head_resp.status == 200
            assert head_resp.body == b""
            assert head_resp.getheader("Content-Length") == get_resp.getheader("Content-Length")
            assert head_resp.getheader("Content-Type") == get_resp.getheader("Content-Type")
        finally:
            s.stop()


class TestUnknownAndTraversalRejection:
    @pytest.mark.parametrize("path", [
        "/static/vendor/unknown.js",
        "/static/vendor/chart.umd.min.js.bak",
        "/static/does-not-exist.js",
        "/static/",
        "/static/vendor/",
        "/static/vendor/../../../../etc/passwd",
        "/static/..%2f..%2f..%2fetc%2fpasswd",
        "/static/vendor%2f..%2f..%2fviewer.py",
        "/static/vendor\\..\\..\\viewer.py",
        "/static/vendor/chart.umd.min.js/../../viewer.py",
        "/static/vendor/%2e%2e/%2e%2e/viewer.py",
        "/static/vendor/chart.umd.min%2ejs",
    ])
    def test_rejected_as_404_not_as_a_file(self, db_path, path):
        s = _start_server(db_path)
        try:
            resp = s.get(path)
            assert resp.status == 404
        finally:
            s.stop()

    def test_resolve_static_asset_rejects_a_hostile_allowlist_entry(self, monkeypatch):
        """Even if a future edit to the allowlist pointed outside
        _VIEWER_ASSETS_DIR, the containment check in _resolve_static_asset
        must still refuse to serve it — defense in depth, not reliance on
        the allowlist alone."""
        hostile = {"escape.js": ("../../../../etc/passwd", "application/javascript", False)}
        monkeypatch.setattr(_viewer, "_STATIC_ASSET_ALLOWLIST", hostile)
        assert _viewer._resolve_static_asset("escape.js") is None

    def test_unknown_asset_still_carries_security_headers(self, db_path):
        s = _start_server(db_path)
        try:
            resp = s.get("/static/vendor/unknown.js")
            assert resp.getheader("X-Content-Type-Options") == "nosniff"
            assert resp.getheader("X-Frame-Options") == "DENY"
        finally:
            s.stop()


class TestCspUnchangedForExistingPages:
    """Phase 7.2A does not loosen the CSP for any existing server-rendered
    page — script-src 'self' is reserved for the dashboard page a later
    phase introduces, scoped to that page's own response only."""

    def test_index_csp_header_is_unchanged(self, db_path):
        s = _start_server(db_path)
        try:
            resp = s.get("/")
            csp = resp.getheader("Content-Security-Policy")
            assert "unsafe-inline" not in csp.replace("style-src 'unsafe-inline'", "")
            assert "unsafe-eval" not in csp
            assert "default-src 'none'" in csp
        finally:
            s.stop()


class TestExistingRoutesStillFunctional:
    def test_index_namespace_fact_entity_routes_unaffected_by_static_route(self, db_path):
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        store.add_fact("A fact for the static-asset regression check.", category="general")
        store.close()

        s = _start_server(db_path)
        try:
            resp = s.get("/")
            assert resp.status == 200
            assert b"Holographic Memory Viewer" in resp.body
        finally:
            s.stop()
