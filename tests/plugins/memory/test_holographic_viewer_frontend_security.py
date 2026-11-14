"""Phase 7.2C — static frontend-security checks for the Holographic memory
dashboard's bundled HTML/CSS/JS (`plugins/memory/holographic/viewer_assets/`).

These are source-level checks (no browser dependency, per the Phase 7.2C
spec's own allowance), covering items 5-7, 10-11, 13 of the Phase 7.2C test
list: no inline script execution surface in the shipped HTML/JS, no CDN
references, no inline event-handler attributes, safe DOM construction, and
no raw FTS5 query construction on the client.

A real headless-Chrome render of `/dashboard` was also run manually during
development (CSP violation count: 0, console error count: 0, confirmed a
`<script>` payload in fact content rendered as escaped text, never
executed) — recorded here rather than wired into CI, since this suite
intentionally avoids a browser dependency per the spec.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from plugins.memory.holographic import viewer as _viewer

_ASSETS_DIR = Path(_viewer.__file__).parent / "viewer_assets"
_JS_DIR = _ASSETS_DIR / "js"
_JS_FILES = sorted(_JS_DIR.glob("*.js"))
_HTML_PATH = _ASSETS_DIR / "index.html"

_INLINE_HANDLER_ATTR_RE = re.compile(r'\son[a-z]+\s*=\s*["\']', re.IGNORECASE)
_CDN_HOST_RE = re.compile(r"(cdn\.jsdelivr\.net|unpkg\.com|cdnjs\.cloudflare\.com|cdn\.|https?://)", re.IGNORECASE)
_LINE_COMMENT_RE = re.compile(r"//.*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def _strip_comments(source: str) -> str:
    """Code-only view of a JS file — used by checks that must not be
    tripped up by this test suite's own explanatory comments (e.g. a
    comment explaining why `innerHTML` is deliberately NOT used contains
    the literal word `innerHTML`)."""
    return _LINE_COMMENT_RE.sub("", _BLOCK_COMMENT_RE.sub("", source))


class TestNoCdnReferences:
    def test_dashboard_html_has_no_cdn_or_external_url(self):
        html = _HTML_PATH.read_text(encoding="utf-8")
        assert not _CDN_HOST_RE.search(html), "dashboard HTML must load no external resource"

    @pytest.mark.parametrize("js_path", _JS_FILES, ids=lambda p: p.name)
    def test_js_module_has_no_cdn_or_external_url(self, js_path):
        source = js_path.read_text(encoding="utf-8")
        assert not _CDN_HOST_RE.search(source), f"{js_path.name} must not reference an external URL"

    def test_dashboard_only_loads_local_script_sources(self):
        html = _HTML_PATH.read_text(encoding="utf-8")
        for src in re.findall(r'<script[^>]*\bsrc=["\']([^"\']+)["\']', html):
            assert src.startswith("/static/"), f"script src must be local: {src!r}"


class TestNoInlineEventHandlers:
    def test_dashboard_html_has_no_inline_event_handler_attributes(self):
        html = _HTML_PATH.read_text(encoding="utf-8")
        assert not _INLINE_HANDLER_ATTR_RE.search(html), "no onclick=/onchange=/... attributes allowed"

    def test_dashboard_html_has_no_inline_script_block(self):
        html = _HTML_PATH.read_text(encoding="utf-8")
        inline_blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.IGNORECASE | re.DOTALL)
        non_empty = [b for b in inline_blocks if b.strip()]
        assert non_empty == [], "all JavaScript must live in external files"


class TestNoUnsafeJavaScriptPatterns:
    @pytest.mark.parametrize("js_path", _JS_FILES, ids=lambda p: p.name)
    def test_no_eval_or_function_constructor(self, js_path):
        source = js_path.read_text(encoding="utf-8")
        assert "eval(" not in source
        assert "new Function(" not in source

    @pytest.mark.parametrize("js_path", _JS_FILES, ids=lambda p: p.name)
    def test_no_innerhtml_assignment(self, js_path):
        """Every dynamic render path uses the `el()`/textContent helper
        from utils.js, never `.innerHTML =` — the exact pattern that caused
        the attribute/JS-string injection bugs found in the original
        index.html during the Phase 7.2 planning review."""
        source = _strip_comments(js_path.read_text(encoding="utf-8"))
        assert "innerHTML" not in source, f"{js_path.name} must not use innerHTML"

    @pytest.mark.parametrize("js_path", _JS_FILES, ids=lambda p: p.name)
    def test_no_inline_handler_assignment_via_dom_properties(self, js_path):
        """Catches the `el.onclick = "..."`/string-handler pattern, distinct
        from the HTML-attribute check above."""
        source = js_path.read_text(encoding="utf-8")
        assert not re.search(r"\.on[a-z]+\s*=\s*['\"]", source, re.IGNORECASE)

    def test_el_helper_rejects_inline_handler_props_defensively(self):
        """utils.js's own `el()` helper refuses an `onXxx` prop outright —
        confirmed by reading its source, since there is no JS runtime in
        this Python test suite to execute it directly."""
        source = (_JS_DIR / "utils.js").read_text(encoding="utf-8")
        assert 'startsWith("on")' in source
        assert "inline event handler props are not supported" in source


class TestNoRawFts5QueryConstruction:
    def test_search_js_never_constructs_a_match_expression(self):
        """The frontend only ever sends raw `q`/`mode` to the server — it
        must never build FTS5 operator syntax (quoting, MATCH, AND/OR/NOT)
        itself; that logic belongs solely to `_fts5_match_query` in
        viewer.py."""
        source = _strip_comments((_JS_DIR / "search.js").read_text(encoding="utf-8"))
        for forbidden in ("MATCH", "fts5_match", '" AND "', '" OR "'):
            assert forbidden not in source

    @pytest.mark.parametrize("js_path", _JS_FILES, ids=lambda p: p.name)
    def test_no_module_sends_a_non_get_request(self, js_path):
        source = js_path.read_text(encoding="utf-8")
        for method in ('"POST"', '"PUT"', '"DELETE"', '"PATCH"', "'POST'", "'PUT'", "'DELETE'", "'PATCH'"):
            assert method not in source, f"{js_path.name} must never issue a mutating HTTP request"


class TestLocalStorageUsageIsThemeOnly:
    def test_only_app_js_touches_localstorage_and_only_for_theme(self):
        for js_path in _JS_FILES:
            source = js_path.read_text(encoding="utf-8")
            if "localStorage" not in source:
                continue
            assert js_path.name == "app.js", f"unexpected localStorage usage outside app.js: {js_path.name}"
            assert "hermes-viewer-theme" in source

    @pytest.mark.parametrize("js_path", _JS_FILES, ids=lambda p: p.name)
    def test_no_fact_or_entity_content_is_persisted_to_browser_storage(self, js_path):
        source = js_path.read_text(encoding="utf-8")
        assert "sessionStorage" not in source
        assert re.search(r"localStorage\.setItem\([^)]*content", source) is None
