"""Optional, read-only local diagnostic viewer for the Holographic memory
plugin.

Phase 7 of docs/plans/demo-reference-plan/implementation-plan.md. Adapted
from the demo's viewer concept, but rebuilt from scratch against the
production schema and the production safety rules — ``demo-reference/`` is
never imported at runtime.

Hard safety properties (see docs/plans/demo-reference-plan/checklist.md
section 11 and implementation-plan.md's "Phase 7" / "9.10"):

- **Optional, stdlib-only, never imported by normal startup.** This module
  uses nothing but ``http.server``/``sqlite3``/stdlib — no new third-party
  dependency was added for it (``fastapi``/``uvicorn`` are declared in
  pyproject.toml but are not imported by ANY existing runtime code path in
  this repository; pulling in an unused heavyweight ASGI stack for a single
  optional local tool would be a worse trade than stdlib, which is already
  present everywhere Python runs and trivially provably "optional" — see
  ``tests/plugins/memory/test_holographic_viewer_optional_import.py``).
  Nothing in ``run_agent.py``, ``cli.py``, ``model_tools.py``,
  ``hermes_cli/main.py``'s module-level imports, or the gateway imports this
  module. It is only ever imported from inside ``hermes_cli.memory_setup``'s
  ``cmd_viewer`` function body, which itself only runs when the operator
  types ``hermes memory viewer``.
- **Read-only, structurally.** Every database connection this module opens
  is opened with SQLite's ``mode=ro`` URI flag (the same technique
  ``diagnostics.run_diagnostics`` uses) — any accidental write attempt
  raises ``sqlite3.OperationalError`` rather than silently succeeding. No
  route ever calls a ``MemoryStore`` write method, and the HTTP handler
  below rejects every non-GET method explicitly (405), so "no restore, raw
  SQL execution, arbitrary file access, or hidden mutation endpoint" is true
  by construction, not by convention.
- **Disabled by default; loopback by default; non-loopback unsupported.**
  Nothing starts this server unless the operator explicitly runs
  ``hermes memory viewer``. ``create_server()`` only accepts
  ``127.0.0.1``/``localhost``/``::1`` as ``host`` — anything else raises
  ``NonLoopbackBindingNotSupported`` before a socket is ever opened. This is
  a deliberate decision, not a corner cut under time pressure: the plan
  explicitly says "CSRF protection and authentication are required before
  any future write operation or non-loopback binding," and this phase adds
  neither, so non-loopback binding is simply not offered at all rather than
  offered behind a flag that could be mistaken for a real safeguard.
- **Explicit profile, explicit namespace.** The CLI resolves the database
  path through the exact same ``_holographic_db_path()``/profile machinery
  every other ``hermes memory`` subcommand uses (see
  ``hermes_cli/memory_setup.py``) — there is no "all profiles" mode. Inside
  the server, the index/root route is a namespace *selector* (opaque
  digests + counts only); it never renders a single fact. Every fact/entity
  page requires an explicit ``ns=<digest>`` query parameter, and that digest
  is always re-derived from the ACTUAL namespaces present in the database
  and matched by equality against the requested digest — a request can
  never supply a raw namespace string, and a crafted ``ns``/``id`` pair that
  doesn't correspond to a real match always 404s rather than falling back to
  "no filter" (see ``_resolve_namespace``, ``_fact_in_namespace``,
  ``_entity_in_namespace``).
- **Opaque namespace labels by default.** Namespace labels reuse
  ``diagnostics._namespace_digest`` verbatim (the exact same one-way SHA-1
  digest function diagnostics/CLI output already uses) rather than a second
  hashing scheme. ``--show-raw-identity`` (``ViewerOptions.show_raw_identity``)
  is the plan's explicitly-allowed opt-in escape hatch ("opaque labels
  unless the operator explicitly requests identity metadata") — it is OFF
  by default, must be passed as a separate explicit CLI flag at server
  start time (never per-request/via query string), and even when on it only
  changes whether the raw namespace string is ADDITIONALLY shown next to
  the digest on the namespace selector page; it never changes which
  operations are available, never disables redaction, and never affects
  binding/read-only guarantees. It exists because the plan's own wording
  allows it as an explicit opt-in, but it is treated as a deliberately
  separate, narrow, documented switch rather than a default behavior.
- **Display redaction.** Fact content and tags are passed through
  ``redact_secret_shapes`` (``plugins/memory/holographic/__init__.py``) —
  the SAME regex-based secret-shape detector already used by the
  capture-time filter (``_fact_problem``) — before being rendered, as a
  defense-in-depth measure for data that predates that filter or was
  inserted directly against the store (e.g. by a test or import path).
- **Minimized provenance.** Fact pages show ``status``, ``category``,
  ``trust_score``, ``confidence``, ``source_type``, ``extraction_method``,
  ``created_at``/``updated_at`` — never ``fact_sources.chat_id`` /
  ``platform`` / ``author`` (raw identity) and never the on-disk database
  path or any environment value.
- **Active/superseded/forgotten are visually distinct.** Active and
  superseded facts get distinct status badges (color + text) in every
  listing; forgotten facts have no retrievable content at all (``remove_fact``
  hard-deletes the row — see store.py), so they are represented honestly as
  an opaque tombstone count on the namespace page, in a visually distinct
  "forgotten" section, rather than fabricated fact-like entries.
- **Revision chains and relationship provenance are inspectable.** A fact
  page links to its supersession ancestors/descendant within the same
  namespace (bounded depth). An entity page lists that entity's namespace-
  scoped relationships and a bounded BFS traversal, by calling
  ``relationships.list_relationships``/``relationships.traverse`` through
  ``_ReadOnlyStoreView`` — a minimal duck-typed adapter implementing only
  the two read methods those functions need
  (``list_relationships_for_entity``, ``relationship_neighbors``), backed
  directly by the same read-only connection used everywhere else in this
  module. A real ``store.MemoryStore`` is deliberately never instantiated
  here: its constructor opens a read-write connection and can run schema
  migrations, which would break this module's read-only guarantee.
- **Pagination and query limits everywhere.** Every list view (namespace
  selector, fact list, relationship list, traversal) is capped at
  ``MAX_PAGE_SIZE`` rows per page regardless of what a client requests, and
  every search/filter parameter (``q``) is passed as a bound SQL parameter
  — never interpolated into SQL text — so a SQL-injection-shaped query
  string is simply a literal (and almost certainly zero-hit) search term.
"""

from __future__ import annotations

import contextlib
import html
import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import relationships as relationships_module
from .diagnostics import _namespace_digest

ALGORITHM_VERSION = "1"

# Documented pagination cap (implementation-plan.md Phase 7: "Pagination and
# query limits prevent loading the full database"). No route may ever
# return more than this many rows in one response, regardless of what a
# client requests via ?per_page=.
MAX_PAGE_SIZE = 50
DEFAULT_PAGE_SIZE = 25

# Phase 7.1D — reject a `?page=` above this rather than silently turning it
# into an arbitrarily large (if cheap, for SQLite) OFFSET. Picked generously
# above any page count a real UI would ever produce via its own "next" link.
MAX_PAGE_NUMBER = 10000

# Phase 7.2B — the JSON API uses offset/limit (not page/per_page) query
# parameters, matching the envelope shape in the API contract; `offset` is
# clamped (never rejected — unlike `page` above, a large OFFSET doesn't grow
# the result set returned, only how much of the index SQLite walks past, and
# is bounded the same way a client's own "last page" link would produce) to
# this value, which mirrors MAX_PAGE_NUMBER's intent at JSON-API scale.
MAX_API_OFFSET = MAX_PAGE_NUMBER * MAX_PAGE_SIZE

# Phase 7.2B §B7 — /api/timeline buckets by day; bounded so a request can
# never force an unbounded number of GROUP BY buckets into one response.
TIMELINE_MAX_DAYS = 90

# Phase 7.1D — bounds how long any single request's database connection may
# spend executing SQL (checked via a SQLite progress handler — see
# `_connection`), independent of `relationships.traverse`'s own, separate
# `deadline_seconds` bookkeeping. Matches `_TRAVERSAL_LIMITS`'s own default
# below for consistency.
QUERY_DEADLINE_SECONDS = 2.0
# How many SQLite VM opcodes elapse between progress-handler callbacks —
# SQLite's own documented ballpark for a responsive-but-low-overhead check.
_PROGRESS_HANDLER_OPCODE_INTERVAL = 1000

# The only hosts `create_server` will bind to. Non-loopback binding is not
# supported at all in this phase — see the module docstring.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# ---------------------------------------------------------------------------
# Phase 7.1A — HTTP security hardening. These limits bound request cost and
# surface before any database connection is opened; the Host/Origin checks
# are defense-in-depth against DNS rebinding and cross-origin browser
# requests (never a substitute for authentication — see module docstring).
# ---------------------------------------------------------------------------

MAX_REQUEST_TARGET_LENGTH = 2048  # raw self.path, before parsing
MAX_QUERY_STRING_LENGTH = 2000  # query portion only
MAX_SEARCH_LENGTH = 256  # a single 'q' value — also the Phase 7.1D limit
MAX_QUERY_PARAM_COUNT = 20  # distinct query parameter keys
MAX_CONCURRENT_REQUESTS = 8  # bounded worker slots; beyond this -> 503
REQUEST_SOCKET_TIMEOUT = 10.0  # seconds

_LOOPBACK_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})

_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

# ---------------------------------------------------------------------------
# Phase 7.2A — static asset serving (vendored dashboard JS, pinned locally;
# see viewer_assets/vendor/VENDORED.md). Deliberately NOT generic file
# serving: `_STATIC_ASSET_ALLOWLIST` is a closed, hand-maintained map from a
# public URL name to a real on-disk file, so an unrecognized or malformed
# name (including any path-traversal or percent-encoded-traversal attempt —
# `self.path` is never percent-decoded or normalized before this exact
# string lookup) simply isn't a key and 404s. `_resolve_static_asset` adds a
# second, independent check (the resolved real path must stay inside
# `_VIEWER_ASSETS_DIR`) purely as defense against a future mistake in the
# allowlist itself, not because the lookup above is believed to be
# bypassable. No route here ever constructs a filesystem path out of
# request data.
# ---------------------------------------------------------------------------

_VIEWER_ASSETS_DIR = (Path(__file__).parent / "viewer_assets").resolve()
_DASHBOARD_HTML_PATH = _VIEWER_ASSETS_DIR / "index.html"

# name (as it appears after "/static/" in the URL) -> (relative path under
# _VIEWER_ASSETS_DIR, Content-Type, cacheable). Pinned vendor assets carry
# their version in the filename itself (Phase 7.2D) — a future version
# bump gets a new filename/allowlist entry, never an in-place content swap
# under the same URL, so the 1-year immutable Cache-Control below can
# never serve stale bytes to a client that cached the old version.
_STATIC_ASSET_ALLOWLIST: "dict[str, tuple[str, str, bool]]" = {
    "vendor/chart.umd.4.4.7.min.js": (
        "vendor/chart.umd.4.4.7.min.js", "application/javascript; charset=utf-8", True,
    ),
    "vendor/vis-network.9.1.9.min.js": (
        "vendor/vis-network.9.1.9.min.js", "application/javascript; charset=utf-8", True,
    ),
    # Phase 7.2C — the dashboard's own CSS/JS. Not cacheable: unlike the
    # pinned vendor files above, these change as the dashboard is developed,
    # with no version segment in their URL to invalidate a stale cache.
    "css/viewer.css": ("css/viewer.css", "text/css; charset=utf-8", False),
    "js/app.js": ("js/app.js", "application/javascript; charset=utf-8", False),
    "js/api.js": ("js/api.js", "application/javascript; charset=utf-8", False),
    "js/charts.js": ("js/charts.js", "application/javascript; charset=utf-8", False),
    "js/dashboard.js": ("js/dashboard.js", "application/javascript; charset=utf-8", False),
    "js/facts.js": ("js/facts.js", "application/javascript; charset=utf-8", False),
    "js/entities.js": ("js/entities.js", "application/javascript; charset=utf-8", False),
    "js/search.js": ("js/search.js", "application/javascript; charset=utf-8", False),
    "js/timeline.js": ("js/timeline.js", "application/javascript; charset=utf-8", False),
    "js/utils.js": ("js/utils.js", "application/javascript; charset=utf-8", False),
}

# Phase 7.2C — the dashboard HTML needs a looser CSP than every other page
# in this module (it loads local scripts), so it gets its own header set
# rather than loosening `_SECURITY_HEADERS` globally. `script-src 'self'`
# with no `unsafe-inline`/`unsafe-eval`; `connect-src 'self'` so the
# dashboard's own `fetch()` calls to `/api/...` are allowed; everything else
# stays as restrictive as `_SECURITY_HEADERS`.
_DASHBOARD_SECURITY_HEADERS = {
    **_SECURITY_HEADERS,
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; "
        # style-src keeps 'unsafe-inline' (same as _SECURITY_HEADERS for
        # every other page) because Chart.js sets the <canvas> element's
        # `style` attribute directly for responsive sizing — confirmed via
        # a real headless-Chrome run against this exact page, which
        # reported "Refused to apply inline style" repeatedly until this
        # was added. This is a style-only relaxation; script-src stays
        # strict with no unsafe-inline/unsafe-eval, which is the actual
        # requirement (CSS cannot execute script).
        "style-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
}


def _resolve_static_asset(name: str) -> "tuple[Path, str, bool] | None":
    entry = _STATIC_ASSET_ALLOWLIST.get(name)
    if entry is None:
        return None
    relative_path, content_type, cacheable = entry
    resolved = (_VIEWER_ASSETS_DIR / relative_path).resolve()
    try:
        resolved.relative_to(_VIEWER_ASSETS_DIR)
    except ValueError:
        return None
    if not resolved.is_file():
        return None
    return resolved, content_type, cacheable


def _parse_netloc(value: str) -> "tuple[str | None, int | None]":
    """Parse a bare `host[:port]` value (no scheme) via urlsplit, which
    handles IPv6 bracket syntax for free. Raises ValueError for a
    syntactically invalid port or unexpected userinfo (e.g. `user@host`),
    which callers treat as a malformed header."""
    split = urlsplit("//" + value)
    if split.username is not None or split.password is not None:
        raise ValueError("unexpected userinfo in netloc")
    return split.hostname, split.port


def _is_valid_host_header(value: "str | None", bound_port: int) -> bool:
    """A Host header is valid only if it names one of the loopback hostnames
    AND matches the port this server is actually bound to. Requiring an
    exact port match (not just "any loopback host") is what makes this
    effective against DNS rebinding: the only accepted value is the exact
    address+port the OS bound, which is also what an SSH `-L` local forward
    presents to the browser."""
    if not value:
        return False
    try:
        hostname, port = _parse_netloc(value)
    except ValueError:
        return False
    if hostname is None or hostname.lower() not in _LOOPBACK_HOSTNAMES:
        return False
    return port == bound_port


def _is_valid_origin_header(value: "str | None", bound_port: int) -> bool:
    """Origin validation is defense in depth, not authentication (see module
    docstring) — it narrows which *browser* requests are honored, but does
    not and cannot distinguish one local process from another. Absent
    Origin (typical for direct navigation) is accepted; `null` (opaque
    origins, e.g. sandboxed iframes or `file://`) is rejected like any other
    non-local origin."""
    if value is None:
        return True
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    if parts.username is not None or parts.password is not None:
        return False
    if parts.scheme != "http" or parts.hostname is None:
        return False
    if parts.hostname.lower() not in _LOOPBACK_HOSTNAMES:
        return False
    return parts.port == bound_port

_REL_MAX_LIMIT = MAX_PAGE_SIZE
_TRAVERSAL_LIMITS = relationships_module.TraversalLimits(max_depth=2, max_nodes=MAX_PAGE_SIZE, max_edges=100, deadline_seconds=2.0)
# Phase 7.1D §D4 — see relationship_neighbors' docstring for why this must
# stay above _TRAVERSAL_LIMITS.max_edges.
_REL_NEIGHBORS_MAX_ROWS = _TRAVERSAL_LIMITS.max_edges * 2
_SUPERSESSION_CHAIN_MAX_HOPS = 50


class NonLoopbackBindingNotSupported(ValueError):
    """Raised by `create_server`/`_validate_host` when asked to bind to
    anything other than a loopback address. See the module docstring's
    "Disabled by default; loopback by default; non-loopback unsupported"
    section for why this is a hard refusal, not a warning."""


class UnsupportedSchemaError(ValueError):
    """Raised by `create_server` (Phase 7.1C) when the database at
    `db_path` isn't a valid SQLite file, or is missing a table/column the
    viewer's own queries depend on. Always raised at server-construction
    time, before any socket is opened — never automatically repaired or
    migrated; the message names exactly what's missing so the operator can
    run the normal `hermes memory` maintenance/migration path instead."""


class QueryDeadlineExceeded(TimeoutError):
    """Raised by `_connection` (Phase 7.1D §D3) when a single request's
    database connection exceeds `QUERY_DEADLINE_SECONDS` of SQL execution,
    detected via a SQLite progress handler. Caught by `_Handler._handle`
    and turned into a `503`, never a generic `500`/stack trace. Independent
    of `relationships.traverse`'s own, separate `deadline_seconds` bound —
    this one limits raw SQL execution time, not BFS traversal steps."""


@dataclass
class ViewerOptions:
    host: str = "127.0.0.1"
    port: int = 0
    show_raw_identity: bool = False


def _validate_host(host: str) -> str:
    if host not in LOOPBACK_HOSTS:
        raise NonLoopbackBindingNotSupported(
            f"Refusing to bind the Holographic memory viewer to {host!r}: non-loopback "
            "binding is not supported in this release because no authentication or CSRF "
            "protection exists yet (implementation-plan.md Phase 7 explicitly requires "
            "both before any non-loopback binding). Use 127.0.0.1, localhost, or ::1."
        )
    return host


def _open_conn(db_path: "str | Path") -> sqlite3.Connection:
    """Open `db_path` strictly read-only (mode=ro URI) — identical base
    technique to diagnostics.run_diagnostics, hardened further for Phase
    7.1C. Any accidental write still raises sqlite3.OperationalError instead
    of silently succeeding (the mode=ro URI flag alone already guarantees
    that); `PRAGMA query_only=ON` makes the same guarantee structural at
    the connection level too, as defense in depth.

    `resolve(strict=True)` raises FileNotFoundError up front if the file is
    missing — the viewer must never create a database — and normalizes
    symlinks before handing the path to SQLite. `timeout=2.0` (connection-
    level) plus `PRAGMA busy_timeout=2000` bound how long a read ever waits
    on a lock held by a concurrent writer (store.py runs in
    journal_mode=WAL, so the viewer must coexist with one), rather than
    blocking indefinitely. If pragma setup itself fails partway through,
    the connection is closed before re-raising so no file descriptor leaks
    on that error path.
    """
    resolved = Path(db_path).resolve(strict=True)
    uri = f"file:{resolved}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA busy_timeout = 2000")
        return conn
    except Exception:
        conn.close()
        raise


@contextlib.contextmanager
def _connection(db_path: "str | Path"):
    """Context manager guaranteeing `conn.close()` regardless of how the
    body exits (Phase 7.1C §C2) — `with sqlite3.connect(...)` only manages
    the transaction (commit/rollback), not the connection's lifetime, so
    this is deliberately a separate wrapper rather than relying on
    `sqlite3.Connection`'s own context-manager protocol.

    Also installs (Phase 7.1D §D3) a SQLite progress handler that aborts
    any SQL still running past `QUERY_DEADLINE_SECONDS` from the moment
    this connection was opened — bounding one request's total SQL execution
    time, not just one statement. A `sqlite3.OperationalError` caused by
    that abort is re-raised as `QueryDeadlineExceeded`, which `_handle`
    turns into a controlled `503`; any OTHER `OperationalError` (e.g. a
    genuine lock-busy error from a concurrent writer) passes through
    unchanged. The handler is always cleared before the connection closes.
    """
    conn = _open_conn(db_path)
    deadline = time.monotonic() + QUERY_DEADLINE_SECONDS
    timed_out = False

    def _on_progress() -> int:
        nonlocal timed_out
        if time.monotonic() >= deadline:
            timed_out = True
            return 1  # non-zero -> SQLite aborts the running statement
        return 0

    conn.set_progress_handler(_on_progress, _PROGRESS_HANDLER_OPCODE_INTERVAL)
    try:
        yield conn
    except sqlite3.OperationalError:
        if timed_out:
            raise QueryDeadlineExceeded(
                f"a database query exceeded its {QUERY_DEADLINE_SECONDS}s execution deadline"
            ) from None
        raise
    finally:
        conn.set_progress_handler(None, 0)
        conn.close()


# Required tables/columns the viewer's own queries depend on — checked at
# `create_server` time (never per-request) via PRAGMA table_info, the same
# introspection technique store.py's own migrations already use. Optional
# tables are column-checked only if present; if genuinely absent,
# `_table_exists` already degrades those features gracefully elsewhere in
# this module (unchanged by this phase).
_REQUIRED_TABLE_COLUMNS = {
    "facts": {
        "fact_id", "content", "category", "tags", "trust_score", "status",
        "superseded_by", "source_type", "extraction_method", "confidence",
        "created_at", "updated_at", "namespace",
    },
    "entities": {"entity_id", "name", "entity_type"},
    "fact_entities": {"fact_id", "entity_id"},
}
_OPTIONAL_TABLE_COLUMNS = {
    "entity_relationships": {
        "relationship_id", "source_entity_id", "target_entity_id",
        "relationship_type", "namespace", "strength", "confidence", "status",
    },
    "forgotten_facts": {"namespace"},
}


def _table_columns(conn: sqlite3.Connection, table: str) -> "set[str]":
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _validate_schema(conn: sqlite3.Connection) -> None:
    """Raise `UnsupportedSchemaError` with an actionable message if `conn`
    is missing a table/column the viewer's queries require. Never attempts
    repair or migration — see `UnsupportedSchemaError`'s docstring."""
    problems: list[str] = []
    for table, required_cols in _REQUIRED_TABLE_COLUMNS.items():
        if not _table_exists(conn, table):
            problems.append(f"required table {table!r} is missing")
            continue
        missing = required_cols - _table_columns(conn, table)
        if missing:
            problems.append(f"table {table!r} is missing column(s): {', '.join(sorted(missing))}")
    for table, required_cols in _OPTIONAL_TABLE_COLUMNS.items():
        if not _table_exists(conn, table):
            continue
        missing = required_cols - _table_columns(conn, table)
        if missing:
            problems.append(f"optional table {table!r} is present but missing column(s): {', '.join(sorted(missing))}")
    if problems:
        raise UnsupportedSchemaError(
            "Holographic memory viewer: unsupported database schema — " + "; ".join(problems) +
            ". The viewer never attempts automatic schema repair or migration; run the normal "
            "hermes memory maintenance/migration path against this database instead."
        )


def _redact(text: "str | None") -> str:
    from . import redact_secret_shapes

    return redact_secret_shapes(text or "")


def _esc(text: Any) -> str:
    return html.escape(str(text if text is not None else ""), quote=True)


def _parse_int(params: dict, key: str, default: int) -> int:
    try:
        return int(params.get(key, [str(default)])[0])
    except (ValueError, TypeError, IndexError):
        return default


def _parse_page(params: dict) -> int:
    return max(1, _parse_int(params, "page", 1))


def _parse_page_size(params: dict) -> int:
    return max(1, min(MAX_PAGE_SIZE, _parse_int(params, "per_page", DEFAULT_PAGE_SIZE)))


def _parse_str(params: dict, key: str) -> "str | None":
    """Return the first value for `key`, or None if the key is absent.

    Unlike a naive "falsy means missing" check, this distinguishes an
    ABSENT query parameter (None — reject as a missing required field) from
    a PRESENT-BUT-EMPTY one (""), which matters because `ns=""` is the
    legitimate digest-equivalent for the unscoped/CLI namespace (see
    `bank_name`/`_namespace_digest`: namespace == "" is always reported as
    "", never hashed). `self._dispatch` calls `parse_qs(..., keep_blank_values=True)`
    so an explicit `?ns=` survives as `[""]` rather than being dropped.
    """
    values = params.get(key)
    if values is None:
        return None
    return values[0]


def _parse_path_int(segment: str) -> "int | None":
    """Parse a URL path segment (e.g. the `{id}` in `/api/facts/{id}`) as a
    non-negative integer, or None if it isn't one — callers turn None into a
    400, never a 500 from a stray `int()` exception or a negative row id."""
    try:
        value = int(segment)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


# ---------------------------------------------------------------------------
# Phase 7.2B — JSON API pagination/error helpers. `limit`/`offset` (not
# `page`/`per_page`) to match the API's own envelope shape; `offset` is
# clamped rather than rejected — see MAX_API_OFFSET's comment.
# ---------------------------------------------------------------------------

def _parse_api_limit(params: dict) -> int:
    return max(1, min(MAX_PAGE_SIZE, _parse_int(params, "limit", DEFAULT_PAGE_SIZE)))


def _parse_api_offset(params: dict) -> int:
    return max(0, min(MAX_API_OFFSET, _parse_int(params, "offset", 0)))


def _pagination_meta(limit: int, offset: int, total: int) -> dict:
    return {"limit": limit, "offset": offset, "total": total, "has_more": offset + limit < total}


def _api_error(code: str, message: str) -> dict:
    """The one error envelope shape every /api/* route uses. `message` must
    always be a fixed, hand-written string — never an exception's own
    `str(exc)` or anything else that could leak a database path or a raw
    namespace/identity value into a response body."""
    return {"error": {"code": code, "message": message}}


# ---------------------------------------------------------------------------
# Namespace resolution — never trust a client-supplied raw namespace. The
# digest is always re-derived from the real namespaces present in the
# database and matched by equality, so a request can only ever "unlock" a
# namespace that both (a) actually has data and (b) hashes to the digest the
# caller supplied — never namespace B's facts via namespace A's digest, and
# never an arbitrary raw namespace string injected into the query string.
# ---------------------------------------------------------------------------

def _resolve_namespace(conn: sqlite3.Connection, digest: str) -> "str | None":
    """Re-derive the raw namespace a client-supplied digest refers to.

    A namespace digest is an identifier for selecting which EXISTING
    namespace to view — re-derived from the real data every time — never an
    authentication credential (same framing as Origin validation in Phase
    7.1A); it proves nothing about who is asking, only which namespace they
    asked to see.

    Collects every DISTINCT raw namespace (from `facts` and
    `entity_relationships`) whose digest matches into a set and returns the
    single element only if exactly one matched. Zero matches is "not found"
    (`None`); more than one distinct namespace hashing to the same digest —
    a SHA-1 collision, practically impossible but handled defensively
    rather than assumed away — is treated the same way (`None`, so the
    caller 404s) rather than silently resolving to whichever was found
    first.
    """
    if digest == "":
        return ""
    candidates: "set[str]" = set()
    for (ns,) in conn.execute("SELECT DISTINCT namespace FROM facts").fetchall():
        if _namespace_digest(ns or "") == digest:
            candidates.add(ns or "")
    if _table_exists(conn, "entity_relationships"):
        for (ns,) in conn.execute("SELECT DISTINCT namespace FROM entity_relationships").fetchall():
            if _namespace_digest(ns or "") == digest:
                candidates.add(ns or "")
    if len(candidates) != 1:
        return None
    return next(iter(candidates))


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?", (name,),
    ).fetchone() is not None


def _fact_in_namespace(conn: sqlite3.Connection, fact_id: int, namespace: str) -> "sqlite3.Row | None":
    return conn.execute(
        """
        SELECT fact_id, content, category, tags, trust_score, status, superseded_by,
               source_type, extraction_method, confidence, created_at, updated_at, namespace
        FROM facts WHERE fact_id = ? AND namespace = ?
        """,
        (fact_id, namespace),
    ).fetchone()


def _entity_in_namespace(conn: sqlite3.Connection, entity_id: int, namespace: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM fact_entities fe JOIN facts f ON f.fact_id = fe.fact_id
        WHERE fe.entity_id = ? AND f.namespace = ? LIMIT 1
        """,
        (entity_id, namespace),
    ).fetchone()
    if row is not None:
        return True
    if not _table_exists(conn, "entity_relationships"):
        return False
    row = conn.execute(
        """
        SELECT 1 FROM entity_relationships
        WHERE namespace = ? AND (source_entity_id = ? OR target_entity_id = ?) LIMIT 1
        """,
        (namespace, entity_id, entity_id),
    ).fetchone()
    return row is not None


class _ReadOnlyStoreView:
    """Minimal duck-typed read-only adapter for `relationships.py`.

    `relationships.list_relationships`/`relationships.traverse` only ever
    call two methods on the `store` object they're given:
    `list_relationships_for_entity` and `relationship_neighbors`. This class
    implements exactly those two, as plain read-only SQL against the same
    `mode=ro` connection every other query in this module uses, mirroring
    `MemoryStore`'s own SQL byte-for-byte. A real `MemoryStore` is
    deliberately never constructed here — see the module docstring.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def list_relationships_for_entity(
        self, entity_id: int, *, namespace: str = "", direction: str = "both",
        relationship_type: "str | None" = None, status: "str | None" = "active",
        limit: int = 200,
    ) -> list[dict]:
        limit = max(1, min(_REL_MAX_LIMIT, int(limit)))
        clauses = ["namespace = ?"]
        params: list = [namespace or ""]
        if direction == "outgoing":
            clauses.append("source_entity_id = ?")
            params.append(entity_id)
        elif direction == "incoming":
            clauses.append("target_entity_id = ?")
            params.append(entity_id)
        else:
            clauses.append("(source_entity_id = ? OR target_entity_id = ?)")
            params.extend([entity_id, entity_id])
        if relationship_type is not None:
            clauses.append("relationship_type = ?")
            params.append(relationship_type)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        params.append(limit)
        sql = f"""
            SELECT relationship_id, source_entity_id, target_entity_id, relationship_type,
                   namespace, strength, confidence, extraction_method, source_fact_id,
                   target_fact_id, status, created_at, updated_at
            FROM entity_relationships
            WHERE {' AND '.join(clauses)}
            ORDER BY relationship_id
            LIMIT ?
        """
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def relationship_neighbors(
        self, entity_ids: list[int], *, namespace: str = "", status: "str | None" = "active",
    ) -> list[dict]:
        """Phase 7.1D §D4: bounded by `_REL_NEIGHBORS_MAX_ROWS` (deliberately
        well above `_TRAVERSAL_LIMITS.max_edges`) so one pathologically
        over-connected entity can't force a single query to materialize an
        unbounded number of rows. `relationships.traverse`'s own BFS loop
        already stops and marks `truncated=True` as soon as it has
        collected `max_edges` edges across the whole traversal; because
        `_REL_NEIGHBORS_MAX_ROWS > max_edges`, this LIMIT can only ever
        discard rows in cases where `traverse()` was already going to
        truncate on its own — it never changes the user-visible
        `truncated`/`truncated_reason` outcome, only the worst-case query
        cost that feeds it."""
        if not entity_ids:
            return []
        placeholders = ",".join("?" * len(entity_ids))
        params: list = list(entity_ids) + list(entity_ids) + [namespace or ""]
        sql = f"""
            SELECT relationship_id, source_entity_id, target_entity_id, relationship_type,
                   namespace, strength, confidence, status
            FROM entity_relationships
            WHERE (source_entity_id IN ({placeholders}) OR target_entity_id IN ({placeholders}))
              AND namespace = ?
        """
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY relationship_id LIMIT ?"
        params.append(_REL_NEIGHBORS_MAX_ROWS)
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]


# ---------------------------------------------------------------------------
# Rendering (minimal inline HTML — no template engine dependency)
# ---------------------------------------------------------------------------

_PAGE_HEAD = """<!doctype html>
<html><head><meta charset="utf-8"><title>Holographic Memory Viewer</title>
<style>
body { font-family: -apple-system, system-ui, sans-serif; margin: 2rem; background: #0f1115; color: #e6e6e6; }
a { color: #7ab8ff; }
.badge { display: inline-block; padding: 0.1rem 0.5rem; border-radius: 0.3rem; font-size: 0.8rem; font-weight: 600; }
.status-active { background: #16401e; color: #7ee787; }
.status-superseded { background: #40360f; color: #e6c766; text-decoration: line-through; }
.status-forgotten { background: #3a3a3a; color: #aaaaaa; font-style: italic; }
.status-unknown { background: #552020; color: #ffb3b3; font-style: italic; }
table { border-collapse: collapse; width: 100%; margin: 1rem 0; }
td, th { border: 1px solid #333; padding: 0.4rem 0.6rem; text-align: left; vertical-align: top; }
.warn { color: #ffb86c; font-weight: 600; }
.muted { color: #8a8f98; }
code { background: #1c1f26; padding: 0.1rem 0.3rem; border-radius: 0.2rem; }
</style></head><body>
<p><a href="/">&larr; namespaces</a></p>
"""
_PAGE_TAIL = "</body></html>"


def _status_badge(status: str) -> str:
    """Phase 7.1F §F2: an explicit mapping, not a default-to-active
    fallback — `facts.status`/`entity_relationships.status` are plain TEXT
    columns with no CHECK constraint, so a status value other than
    'active'/'superseded' is possible (a future code path, a hand-edited
    row, a migration bug) and must render as visibly unusual, never as a
    normal, trustworthy "active" badge."""
    cls = {"active": "status-active", "superseded": "status-superseded"}.get(status, "status-unknown")
    return f'<span class="badge {cls}">{_esc(status)}</span>'


def _render_index(conn: sqlite3.Connection, *, show_raw_identity: bool, params: dict) -> str:
    """Phase 7.1D §D1: paginated at the SQL level (LIMIT/OFFSET), not by
    fetching every distinct namespace and slicing in Python — the query
    cost no longer scales with total namespace count, only with
    `page_size`.

    Phase 7.1F §F2: active/superseded use explicit `= 'active'`/
    `= 'superseded'` comparisons, not `!= 'superseded'` for "active" — the
    latter would silently fold any OTHER status value (there's no CHECK
    constraint on `facts.status`) into "active". A third `unknown` bucket
    catches anything that's neither, so `active + superseded + unknown ==
    total` always holds and nothing is misclassified as trustworthy by
    default (see `_status_badge`'s docstring for the matching per-row
    fix)."""
    page = _parse_page(params)
    page_size = _parse_page_size(params)
    offset = (page - 1) * page_size
    page_rows = conn.execute(
        """
        SELECT namespace,
               COUNT(*) AS total,
               SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active,
               SUM(CASE WHEN status = 'superseded' THEN 1 ELSE 0 END) AS superseded,
               SUM(CASE WHEN status NOT IN ('active', 'superseded') THEN 1 ELSE 0 END) AS unknown
        FROM facts GROUP BY namespace ORDER BY namespace
        LIMIT ? OFFSET ?
        """,
        (page_size, offset),
    ).fetchall()
    namespace_count = conn.execute(
        "SELECT COUNT(DISTINCT namespace) FROM facts"
    ).fetchone()[0]
    tombstones_by_ns: dict[str, int] = {}
    if _table_exists(conn, "forgotten_facts"):
        for ns, count in conn.execute("SELECT namespace, COUNT(*) FROM forgotten_facts GROUP BY namespace").fetchall():
            tombstones_by_ns[ns or ""] = count

    total_pages = max(1, (namespace_count + page_size - 1) // page_size)

    parts = [_PAGE_HEAD, "<h1>Holographic memory — namespaces</h1>",
             "<p class='muted'>Select a namespace below to view its facts. "
             "This page intentionally never lists individual facts.</p>"]
    if show_raw_identity:
        parts.append("<p class='warn'>--show-raw-identity is enabled: raw namespace identities are shown below.</p>")
    parts.append(
        "<table><tr><th>Namespace</th><th>Total</th><th>Active</th><th>Superseded</th>"
        "<th>Unknown status</th><th>Forgotten (tombstones)</th></tr>"
    )
    for ns, total, active, superseded, unknown in page_rows:
        ns = ns or ""
        digest = _namespace_digest(ns) if ns else "(unscoped)"
        label = digest if not ns else (f"{digest}&nbsp;<span class='muted'>({_esc(ns)})</span>" if show_raw_identity else digest)
        href = f"/namespace?ns={digest if ns else ''}"
        tombstone_count = tombstones_by_ns.get(ns, 0)
        unknown_cell = (
            f"<span class='badge status-unknown'>{unknown}</span>" if unknown else "0"
        )
        parts.append(
            f"<tr><td><a href='{href}'>{label}</a></td><td>{total}</td><td>{active or 0}</td>"
            f"<td>{superseded or 0}</td><td>{unknown_cell}</td>"
            f"<td><span class='badge status-forgotten'>{tombstone_count}</span></td></tr>"
        )
    parts.append("</table>")
    parts.append(f"<p class='muted'>Page {page} of {total_pages} (max {page_size} per page).</p>")
    if page > 1:
        parts.append(f"<a href='/?page={page - 1}&per_page={page_size}'>&larr; prev</a> ")
    if page < total_pages:
        parts.append(f"<a href='/?page={page + 1}&per_page={page_size}'>next &rarr;</a>")
    parts.append(_PAGE_TAIL)
    return "".join(parts)


def _render_namespace(conn: sqlite3.Connection, namespace: str, digest: str, *, params: dict) -> str:
    page = _parse_page(params)
    page_size = _parse_page_size(params)
    query = _parse_str(params, "q")
    requested_mode = _parse_str(params, "search_mode") or "literal"
    offset = (page - 1) * page_size

    rows, total, mode_used, fallback_reason = _search_namespace_facts(
        conn, namespace, query, requested_mode, page_size, offset,
    )

    tombstone_count = 0
    if _table_exists(conn, "forgotten_facts"):
        tombstone_count = conn.execute(
            "SELECT COUNT(*) FROM forgotten_facts WHERE namespace = ?", (namespace,),
        ).fetchone()[0]

    total_pages = max(1, (total + page_size - 1) // page_size)
    label = digest if namespace else "(unscoped)"
    parts = [_PAGE_HEAD, f"<h1>Namespace {_esc(label)}</h1>"]
    parts.append(
        "<form method='get' action='/namespace'>"
        f"<input type='hidden' name='ns' value='{_esc(digest)}'>"
        f"<input type='text' name='q' value='{_esc(query or '')}' placeholder='search content'>"
        "<select name='search_mode'>"
        f"<option value='literal'{' selected' if requested_mode != 'fts' else ''}>literal (substring, not executed)</option>"
        f"<option value='fts'{' selected' if requested_mode == 'fts' else ''}>full-text (FTS5, every word required)</option>"
        "</select>"
        "<button type='submit'>Search</button></form>"
    )
    if fallback_reason:
        parts.append(f"<p class='warn'>{_esc(fallback_reason)}</p>")
    elif query and mode_used == "fts":
        parts.append("<p class='muted'>Full-text search (FTS5) results, ranked by relevance.</p>")
    parts.append("<table><tr><th>ID</th><th>Status</th><th>Category</th><th>Trust</th><th>Created</th><th>Content</th></tr>")
    for row in rows:
        content = _esc(_redact(row["content"]))
        parts.append(
            f"<tr><td><a href='/fact?ns={_esc(digest)}&id={row['fact_id']}'>{row['fact_id']}</a></td>"
            f"<td>{_status_badge(row['status'])}</td><td>{_esc(row['category'])}</td>"
            f"<td>{row['trust_score']:.2f}</td><td>{_esc(row['created_at'])}</td><td>{content}</td></tr>"
        )
    parts.append("</table>")
    parts.append(
        f"<p><span class='badge status-forgotten'>forgotten (tombstones): {tombstone_count}</span> "
        "<span class='muted'>— forgotten facts have no retrievable content; only a count is shown.</span></p>"
    )
    parts.append(f"<p class='muted'>Page {page} of {total_pages} (max {page_size} per page, {total} total match(es)).</p>")
    if page > 1:
        parts.append(
            f"<a href='/namespace?ns={_esc(digest)}&page={page - 1}&per_page={page_size}"
            f"&q={_esc(query or '')}&search_mode={_esc(requested_mode)}'>&larr; prev</a> "
        )
    if page < total_pages:
        parts.append(
            f"<a href='/namespace?ns={_esc(digest)}&page={page + 1}&per_page={page_size}"
            f"&q={_esc(query or '')}&search_mode={_esc(requested_mode)}'>next &rarr;</a>"
        )
    parts.append(_PAGE_TAIL)
    return "".join(parts)


# ---------------------------------------------------------------------------
# Phase 7.1E — FTS5 search integration.
#
# `facts_fts` (store.py) is an EXTERNAL-CONTENT FTS5 virtual table over
# `facts(content, tags)` with `content_rowid=fact_id` (so `fts.rowid ==
# f.fact_id` directly, no separate mapping table), kept in sync by triggers
# on every facts insert/update/delete (store.py's `facts_ai`/`facts_au`/
# `facts_ad`), using FTS5's default `unicode61` tokenizer (no custom
# tokenizer configured). The viewer never rebuilds, migrates, or otherwise
# writes to this index — read-only `mode=ro` connections make that
# structurally impossible, not just a convention.
# ---------------------------------------------------------------------------

_LIKE_ESCAPE_CHAR = "\\"


def _escape_like(term: str) -> str:
    """Escape SQL LIKE wildcards (`%`, `_`) and the escape character itself
    in a literal search term, so e.g. searching for a literal `50%` doesn't
    also match `50` followed by anything. Paired with `LIKE ? ESCAPE '\\'`
    at the call site."""
    return (
        term.replace(_LIKE_ESCAPE_CHAR, _LIKE_ESCAPE_CHAR * 2)
        .replace("%", _LIKE_ESCAPE_CHAR + "%")
        .replace("_", _LIKE_ESCAPE_CHAR + "_")
    )


def _fts5_available(conn: sqlite3.Connection) -> bool:
    return _table_exists(conn, "facts_fts")


def _fts5_match_query(raw_query: str) -> str:
    """Build a safe FTS5 MATCH expression from free-form user input.

    Every whitespace-separated token is individually double-quoted (FTS5
    phrase-query syntax, with embedded `"` doubled per FTS5's own escaping
    rule) and ANDed together (FTS5's implicit default between bareword
    tokens). Quoting each token neutralizes every bit of FTS5 query syntax
    the raw string might otherwise contain — boolean operators
    (AND/OR/NOT), column filters (`col:term`), prefix wildcards (`term*`),
    NEAR, and parentheses all become literal characters inside a phrase
    rather than being interpreted — so a user's search text can never
    itself become part of the query's *structure*, only its content.
    """
    tokens = raw_query.split()
    return " ".join('"' + t.replace('"', '""') + '"' for t in tokens)


def _search_namespace_facts(
    conn: sqlite3.Connection, namespace: str, query: "str | None", requested_mode: str,
    page_size: int, offset: int,
) -> "tuple[list[sqlite3.Row], int, str, str | None]":
    """Return `(rows, total_count, mode_used, fallback_reason)` for
    `/namespace`'s fact listing, with or without a search term.

    `mode_used` is always `"literal"` or `"fts"` — never a lie about what
    actually ran. `fallback_reason` is set (and `mode_used` forced to
    `"literal"`) whenever `requested_mode == "fts"` but FTS5 search
    couldn't actually be used — the index is absent (older/synthetic
    schema) or, defensively, a MATCH query still failed for some other
    reason — so the page can say so explicitly rather than silently
    returning literal-search results while claiming they're full-text.
    """
    base_columns = "fact_id, content, category, status, trust_score, created_at"

    if not query:
        total = conn.execute(
            "SELECT COUNT(*) FROM facts WHERE namespace = ?", (namespace,),
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT {base_columns} FROM facts WHERE namespace = ? "
            "ORDER BY fact_id DESC LIMIT ? OFFSET ?",
            (namespace, page_size, offset),
        ).fetchall()
        return rows, total, "literal", None

    if requested_mode == "fts":
        if not _fts5_available(conn):
            return (*_literal_search(conn, namespace, query, page_size, offset), "literal",
                    "FTS5 search index not present in this database — showing literal search results instead.")
        try:
            match_query = _fts5_match_query(query)
            total = conn.execute(
                "SELECT COUNT(*) FROM facts f JOIN facts_fts fts ON fts.rowid = f.fact_id "
                "WHERE facts_fts MATCH ? AND f.namespace = ?",
                (match_query, namespace),
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT f.fact_id, f.content, f.category, f.status, f.trust_score, f.created_at "
                "FROM facts f JOIN facts_fts fts ON fts.rowid = f.fact_id "
                "WHERE facts_fts MATCH ? AND f.namespace = ? "
                "ORDER BY fts.rank, f.trust_score DESC LIMIT ? OFFSET ?",
                (match_query, namespace, page_size, offset),
            ).fetchall()
            return rows, total, "fts", None
        except sqlite3.OperationalError:
            return (*_literal_search(conn, namespace, query, page_size, offset), "literal",
                    "FTS5 query could not be executed — showing literal search results instead.")

    rows, total = _literal_search(conn, namespace, query, page_size, offset)
    return rows, total, "literal", None


def _literal_search(
    conn: sqlite3.Connection, namespace: str, query: str, page_size: int, offset: int,
) -> "tuple[list[sqlite3.Row], int]":
    escaped = f"%{_escape_like(query)}%"
    total = conn.execute(
        "SELECT COUNT(*) FROM facts WHERE namespace = ? AND content LIKE ? ESCAPE '\\'",
        (namespace, escaped),
    ).fetchone()[0]
    rows = conn.execute(
        "SELECT fact_id, content, category, status, trust_score, created_at FROM facts "
        "WHERE namespace = ? AND content LIKE ? ESCAPE '\\' "
        "ORDER BY fact_id DESC LIMIT ? OFFSET ?",
        (namespace, escaped, page_size, offset),
    ).fetchall()
    return rows, total


def _supersession_chain(conn: sqlite3.Connection, fact_id: int, namespace: str) -> tuple[list[dict], "dict | None"]:
    """Ancestors (older facts this one superseded, if any point here) and
    the direct successor (if this fact itself was superseded), bounded to
    `_SUPERSESSION_CHAIN_MAX_HOPS` — never the whole table.

    Phase 7.1F §F1: deliberately IMMEDIATE relationships only (the fact(s)
    this one directly superseded, and the fact that directly superseded
    this one) — not a multi-hop walk of the full revision history. A fact
    page can still be navigated transitively by following `successor`'s own
    link to ITS fact page, one hop at a time; this function itself never
    recurses. Multi-hop history with cycle detection was considered and
    rejected for this phase as unneeded complexity — `superseded_by` forms
    a simple forward chain by construction (each fact points to at most one
    direct successor), so no cycle is possible in the first place, and no
    operator need identified for seeing the whole chain in one page."""
    ancestors = conn.execute(
        """
        SELECT fact_id, content, status, created_at FROM facts
        WHERE superseded_by = ? AND namespace = ? LIMIT ?
        """,
        (fact_id, namespace, _SUPERSESSION_CHAIN_MAX_HOPS),
    ).fetchall()
    ancestors = [dict(r) for r in ancestors]
    for a in ancestors:
        a["content"] = _redact(a["content"])

    successor = None
    row = conn.execute("SELECT superseded_by FROM facts WHERE fact_id = ? AND namespace = ?", (fact_id, namespace)).fetchone()
    if row and row["superseded_by"] is not None:
        succ = conn.execute(
            "SELECT fact_id, content, status, created_at FROM facts WHERE fact_id = ? AND namespace = ?",
            (row["superseded_by"], namespace),
        ).fetchone()
        if succ:
            successor = dict(succ)
            successor["content"] = _redact(successor["content"])
    return ancestors, successor


def _render_fact(conn: sqlite3.Connection, row: sqlite3.Row, digest: str, namespace: str) -> str:
    entities = conn.execute(
        """
        SELECT e.entity_id, e.name FROM entities e
        JOIN fact_entities fe ON fe.entity_id = e.entity_id
        WHERE fe.fact_id = ? LIMIT ?
        """,
        (row["fact_id"], MAX_PAGE_SIZE),
    ).fetchall()

    ancestors, successor = _supersession_chain(conn, row["fact_id"], namespace)

    parts = [_PAGE_HEAD, f"<p><a href='/namespace?ns={_esc(digest)}'>&larr; namespace</a></p>"]
    parts.append(f"<h1>Fact #{row['fact_id']} {_status_badge(row['status'])}</h1>")
    parts.append("<table>")
    parts.append(f"<tr><th>Content</th><td>{_esc(_redact(row['content']))}</td></tr>")
    parts.append(f"<tr><th>Category</th><td>{_esc(row['category'])}</td></tr>")
    parts.append(f"<tr><th>Tags</th><td>{_esc(_redact(row['tags']))}</td></tr>")
    parts.append(f"<tr><th>Trust score</th><td>{row['trust_score']:.2f}</td></tr>")
    parts.append(f"<tr><th>Confidence</th><td>{row['confidence']:.2f}</td></tr>")
    parts.append(f"<tr><th>Source type</th><td>{_esc(row['source_type'])}</td></tr>")
    parts.append(f"<tr><th>Extraction method</th><td>{_esc(row['extraction_method'])}</td></tr>")
    parts.append(f"<tr><th>Created</th><td>{_esc(row['created_at'])}</td></tr>")
    parts.append(f"<tr><th>Updated</th><td>{_esc(row['updated_at'])}</td></tr>")
    parts.append("</table>")

    if entities:
        parts.append("<h2>Entities</h2><ul>")
        for e in entities:
            parts.append(f"<li><a href='/entity?ns={_esc(digest)}&id={e['entity_id']}'>{_esc(_redact(e['name']))}</a></li>")
        parts.append("</ul>")

    if ancestors or successor:
        parts.append("<h2>Revision chain</h2><ul>")
        for a in ancestors:
            parts.append(
                f"<li>superseded by this fact: #{a['fact_id']} {_status_badge(a['status'])} "
                f"— <code>{_esc(a['content'])}</code> ({_esc(a['created_at'])})</li>"
            )
        if successor:
            parts.append(
                f"<li>superseded by: <a href='/fact?ns={_esc(digest)}&id={successor['fact_id']}'>#{successor['fact_id']}</a> "
                f"{_status_badge(successor['status'])} — <code>{_esc(successor['content'])}</code></li>"
            )
        parts.append("</ul>")

    parts.append(_PAGE_TAIL)
    return "".join(parts)


def _render_entity(conn: sqlite3.Connection, entity_id: int, digest: str, namespace: str) -> str:
    row = conn.execute("SELECT entity_id, name, entity_type FROM entities WHERE entity_id = ?", (entity_id,)).fetchone()
    name = _redact(row["name"]) if row else f"entity {entity_id}"

    view = _ReadOnlyStoreView(conn)
    edges = relationships_module.list_relationships(view, entity_id, namespace=namespace, direction="both", limit=MAX_PAGE_SIZE)
    traversal = relationships_module.traverse(view, entity_id, namespace=namespace, limits=_TRAVERSAL_LIMITS)

    entity_names: dict[int, str] = {}
    if traversal.nodes:
        placeholders = ",".join("?" * len(traversal.nodes))
        for r in conn.execute(f"SELECT entity_id, name FROM entities WHERE entity_id IN ({placeholders})", list(traversal.nodes)):
            entity_names[r["entity_id"]] = _redact(r["name"])

    parts = [_PAGE_HEAD, f"<p><a href='/namespace?ns={_esc(digest)}'>&larr; namespace</a></p>"]
    parts.append(f"<h1>Entity: {_esc(name)}</h1>")
    parts.append(f"<p class='muted'>entity_id={entity_id} type={_esc(row['entity_type']) if row else 'unknown'}</p>")

    parts.append(f"<h2>Relationships ({len(edges)})</h2><table><tr><th>Source</th><th>Type</th><th>Target</th><th>Strength</th><th>Confidence</th></tr>")
    for e in edges:
        parts.append(
            f"<tr><td>{e.source_entity_id}</td><td>{_esc(_redact(e.relationship_type))}</td><td>{e.target_entity_id}</td>"
            f"<td>{e.strength:.2f}</td><td>{e.confidence:.2f}</td></tr>"
        )
    parts.append("</table>")

    parts.append(
        f"<h2>Bounded traversal</h2><p class='muted'>nodes={len(traversal.nodes)} edges={len(traversal.edges)} "
        f"truncated={traversal.truncated} ({_esc(traversal.truncated_reason or 'complete')})</p><ul>"
    )
    for node_id in sorted(traversal.nodes):
        if node_id == entity_id:
            continue
        label = entity_names.get(node_id, f"entity {node_id}")
        parts.append(f"<li><a href='/entity?ns={_esc(digest)}&id={node_id}'>{_esc(label)}</a></li>")
    parts.append("</ul>")
    parts.append(_PAGE_TAIL)
    return "".join(parts)


# ---------------------------------------------------------------------------
# Phase 7.2B — namespace-scoped JSON API. Every function below is a plain
# read-only query builder returning a JSON-serializable dict; none of them
# touches the HTTP layer directly (see `_Handler._dispatch_api`). They
# deliberately reuse the exact same namespace-resolution, redaction,
# pagination, FTS5, and relationship-traversal helpers the HTML routes above
# use — no second database-connection manager, FTS5 integration, or
# namespace implementation exists for the API.
# ---------------------------------------------------------------------------

def _api_namespaces(conn: sqlite3.Connection, params: dict, *, show_raw_identity: bool) -> dict:
    """GET /api/namespaces — the opaque namespace selector. Never returns an
    individual fact, a relationship label, or (unless `--show-raw-identity`
    was passed at server start) a raw namespace identity — mirrors
    `_render_index`'s own query and the same explicit active/superseded/
    unknown three-way split (Phase 7.1F §F2)."""
    limit = _parse_api_limit(params)
    offset = _parse_api_offset(params)
    page_rows = conn.execute(
        """
        SELECT namespace,
               COUNT(*) AS total,
               SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active,
               SUM(CASE WHEN status = 'superseded' THEN 1 ELSE 0 END) AS superseded,
               SUM(CASE WHEN status NOT IN ('active', 'superseded') THEN 1 ELSE 0 END) AS unknown
        FROM facts GROUP BY namespace ORDER BY namespace
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()
    namespace_count = conn.execute("SELECT COUNT(DISTINCT namespace) FROM facts").fetchone()[0]
    tombstones_by_ns: dict[str, int] = {}
    if _table_exists(conn, "forgotten_facts"):
        for ns, count in conn.execute(
            "SELECT namespace, COUNT(*) FROM forgotten_facts GROUP BY namespace"
        ).fetchall():
            tombstones_by_ns[ns or ""] = count

    namespaces = []
    for ns, total, active, superseded, unknown in page_rows:
        ns = ns or ""
        digest = _namespace_digest(ns) if ns else ""
        entry = {
            "namespace": digest,
            "total": total,
            "active": active or 0,
            "superseded": superseded or 0,
            "unknown": unknown or 0,
            "forgotten": tombstones_by_ns.get(ns, 0),
        }
        if show_raw_identity and ns:
            entry["raw_identity"] = ns
        namespaces.append(entry)
    return {"namespaces": namespaces, "pagination": _pagination_meta(limit, offset, namespace_count)}


def _api_overview(conn: sqlite3.Connection, namespace: str, digest: str) -> dict:
    """GET /api/overview?ns=... — scoped counts and summary metrics, using
    only verified production columns (Phase 7.2B §B7). Every query below
    filters on `namespace = ?` as the authoritative scope — no global
    aggregate leaks into a per-namespace response."""
    status_row = conn.execute(
        """
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active,
          SUM(CASE WHEN status = 'superseded' THEN 1 ELSE 0 END) AS superseded,
          SUM(CASE WHEN status NOT IN ('active', 'superseded') THEN 1 ELSE 0 END) AS unknown
        FROM facts WHERE namespace = ?
        """,
        (namespace,),
    ).fetchone()
    entity_count = conn.execute(
        """
        SELECT COUNT(DISTINCT fe.entity_id) FROM fact_entities fe
        JOIN facts f ON f.fact_id = fe.fact_id WHERE f.namespace = ?
        """,
        (namespace,),
    ).fetchone()[0]
    relationship_count = 0
    if _table_exists(conn, "entity_relationships"):
        relationship_count = conn.execute(
            "SELECT COUNT(*) FROM entity_relationships WHERE namespace = ?", (namespace,),
        ).fetchone()[0]
    categories = conn.execute(
        "SELECT category, COUNT(*) FROM facts WHERE namespace = ? GROUP BY category ORDER BY category",
        (namespace,),
    ).fetchall()
    trust_buckets = conn.execute(
        """
        SELECT CAST(trust_score * 10 AS INTEGER) AS bucket, COUNT(*) AS count
        FROM facts WHERE namespace = ? GROUP BY bucket ORDER BY bucket
        """,
        (namespace,),
    ).fetchall()
    tombstone_count = 0
    if _table_exists(conn, "forgotten_facts"):
        tombstone_count = conn.execute(
            "SELECT COUNT(*) FROM forgotten_facts WHERE namespace = ?", (namespace,),
        ).fetchone()[0]
    recent_rows = conn.execute(
        "SELECT fact_id, content, category, status, trust_score, created_at FROM facts "
        "WHERE namespace = ? ORDER BY fact_id DESC LIMIT ?",
        (namespace, DEFAULT_PAGE_SIZE),
    ).fetchall()
    recent_facts = [
        {
            "fact_id": r["fact_id"],
            "content": _redact(r["content"]),
            "category": r["category"],
            "status": r["status"],
            "trust_score": r["trust_score"],
            "created_at": r["created_at"],
        }
        for r in recent_rows
    ]
    return {
        "namespace": digest,
        "facts": {
            "total": status_row["total"],
            "active": status_row["active"] or 0,
            "superseded": status_row["superseded"] or 0,
            "unknown": status_row["unknown"] or 0,
            "forgotten": tombstone_count,
        },
        "entities_total": entity_count,
        "relationships_total": relationship_count,
        "categories": {row[0]: row[1] for row in categories},
        "trust_distribution": [{"bucket": b * 10, "count": c} for b, c in trust_buckets],
        "recent_facts": recent_facts,
    }


def _api_facts(conn: sqlite3.Connection, namespace: str, digest: str, params: dict) -> dict:
    """GET /api/facts?ns=... — paginated facts, optionally filtered by
    `category` (a small fixed enum the app itself assigns, not freeform user
    content — same as the HTML route's unredacted rendering of it)."""
    limit = _parse_api_limit(params)
    offset = _parse_api_offset(params)
    category = _parse_str(params, "category")
    clauses = ["namespace = ?"]
    sql_params: list = [namespace]
    if category:
        clauses.append("category = ?")
        sql_params.append(category)
    where = " AND ".join(clauses)
    total = conn.execute(f"SELECT COUNT(*) FROM facts WHERE {where}", sql_params).fetchone()[0]
    rows = conn.execute(
        f"SELECT fact_id, content, category, status, trust_score, created_at, updated_at FROM facts "
        f"WHERE {where} ORDER BY fact_id DESC LIMIT ? OFFSET ?",
        [*sql_params, limit, offset],
    ).fetchall()
    facts = [
        {
            "fact_id": r["fact_id"],
            "content": _redact(r["content"]),
            "category": r["category"],
            "status": r["status"],
            "trust_score": r["trust_score"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]
    return {"namespace": digest, "facts": facts, "pagination": _pagination_meta(limit, offset, total)}


def _api_fact_detail(conn: sqlite3.Connection, namespace: str, digest: str, fact_id: int) -> "dict | None":
    """GET /api/facts/{id}?ns=... — None means "not found in this
    namespace", which the caller turns into a 404 (never a fallback to an
    unscoped lookup)."""
    row = _fact_in_namespace(conn, fact_id, namespace)
    if row is None:
        return None
    ancestors, successor = _supersession_chain(conn, fact_id, namespace)
    return {
        "namespace": digest,
        "fact": {
            "fact_id": row["fact_id"],
            "content": _redact(row["content"]),
            "category": row["category"],
            "tags": _redact(row["tags"]),
            "status": row["status"],
            "trust_score": row["trust_score"],
            "confidence": row["confidence"],
            "source_type": row["source_type"],
            "extraction_method": row["extraction_method"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        },
        "revision": {
            # _supersession_chain already redacted `content` on both of
            # these — never re-redact-and-double-escape here.
            "ancestors": [
                {"fact_id": a["fact_id"], "status": a["status"], "content": a["content"], "created_at": a["created_at"]}
                for a in ancestors
            ],
            "successor": (
                {"fact_id": successor["fact_id"], "status": successor["status"], "content": successor["content"]}
                if successor else None
            ),
        },
    }


def _api_search(conn: sqlite3.Connection, namespace: str, digest: str, params: dict) -> dict:
    """GET /api/search?ns=... — reuses `_search_namespace_facts` byte-for-
    byte (no separate FTS5 integration or query builder for the API)."""
    limit = _parse_api_limit(params)
    offset = _parse_api_offset(params)
    query = _parse_str(params, "q") or ""
    requested_mode = _parse_str(params, "mode") or "literal"
    rows, total, mode_used, fallback_reason = _search_namespace_facts(
        conn, namespace, query, requested_mode, limit, offset,
    )
    results = [
        {
            "fact_id": r["fact_id"],
            "content": _redact(r["content"]),
            "category": r["category"],
            "status": r["status"],
            "trust_score": r["trust_score"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]
    return {
        "namespace": digest,
        "query": query,
        "mode_requested": requested_mode,
        "mode_used": mode_used,
        "fallback_reason": fallback_reason,
        "results": results,
        "pagination": _pagination_meta(limit, offset, total),
    }


def _api_entities(conn: sqlite3.Connection, namespace: str, digest: str, params: dict) -> dict:
    """GET /api/entities?ns=... — entities scoped to this namespace via
    their fact links (an entity with zero facts in this namespace never
    appears, even if it's shared by name with another namespace via
    `entity_relationships` — consistent with how entities are globally
    resolved by name but namespace-scoped only at the association level;
    see the module docstring)."""
    limit = _parse_api_limit(params)
    offset = _parse_api_offset(params)
    total = conn.execute(
        """
        SELECT COUNT(*) FROM (
          SELECT DISTINCT fe.entity_id FROM fact_entities fe
          JOIN facts f ON f.fact_id = fe.fact_id WHERE f.namespace = ?
        )
        """,
        (namespace,),
    ).fetchone()[0]
    rows = conn.execute(
        """
        SELECT e.entity_id, e.name, e.entity_type, COUNT(DISTINCT fe.fact_id) AS fact_count
        FROM entities e
        JOIN fact_entities fe ON fe.entity_id = e.entity_id
        JOIN facts f ON f.fact_id = fe.fact_id
        WHERE f.namespace = ?
        GROUP BY e.entity_id ORDER BY e.entity_id
        LIMIT ? OFFSET ?
        """,
        (namespace, limit, offset),
    ).fetchall()
    entities = [
        {
            "entity_id": r["entity_id"],
            "name": _redact(r["name"]),
            "entity_type": r["entity_type"],
            "fact_count": r["fact_count"],
        }
        for r in rows
    ]
    return {"namespace": digest, "entities": entities, "pagination": _pagination_meta(limit, offset, total)}


def _api_entity_graph(conn: sqlite3.Connection, namespace: str, digest: str, entity_id: int) -> "dict | None":
    """GET /api/entities/{id}/graph?ns=... — None means "not found in this
    namespace" (checked via the same `_entity_in_namespace` the HTML `/entity`
    route uses). Reuses `relationships.list_relationships`/`traverse`
    through the same `_ReadOnlyStoreView` and `_TRAVERSAL_LIMITS` the HTML
    route uses — no separate traversal implementation for the API."""
    if not _entity_in_namespace(conn, entity_id, namespace):
        return None
    row = conn.execute(
        "SELECT entity_id, name, entity_type FROM entities WHERE entity_id = ?", (entity_id,),
    ).fetchone()
    name = _redact(row["name"]) if row else f"entity {entity_id}"

    view = _ReadOnlyStoreView(conn)
    edges = relationships_module.list_relationships(
        view, entity_id, namespace=namespace, direction="both", limit=MAX_PAGE_SIZE,
    )
    traversal = relationships_module.traverse(view, entity_id, namespace=namespace, limits=_TRAVERSAL_LIMITS)

    entity_names: dict[int, str] = {}
    if traversal.nodes:
        placeholders = ",".join("?" * len(traversal.nodes))
        for r in conn.execute(
            f"SELECT entity_id, name FROM entities WHERE entity_id IN ({placeholders})", list(traversal.nodes),
        ):
            entity_names[r["entity_id"]] = _redact(r["name"])

    return {
        "namespace": digest,
        "entity": {
            "entity_id": entity_id,
            "name": name,
            "entity_type": row["entity_type"] if row else None,
        },
        "relationships": [
            {
                "source_entity_id": e.source_entity_id,
                "target_entity_id": e.target_entity_id,
                "relationship_type": _redact(e.relationship_type),
                "strength": e.strength,
                "confidence": e.confidence,
            }
            for e in edges
        ],
        "graph": {
            "nodes": [
                {"entity_id": nid, "name": entity_names.get(nid, f"entity {nid}")}
                for nid in sorted(traversal.nodes)
            ],
            "edges": [
                {
                    "source_entity_id": e.source_entity_id,
                    "target_entity_id": e.target_entity_id,
                    "relationship_type": _redact(e.relationship_type),
                }
                for e in traversal.edges
            ],
            "truncated": traversal.truncated,
            "truncated_reason": traversal.truncated_reason,
        },
    }


def _api_timeline(conn: sqlite3.Connection, namespace: str, digest: str, params: dict) -> dict:
    """GET /api/timeline?ns=... — bucketed by `DATE(created_at)` only
    (Phase 7.2B §B7: a timeline entry means "a fact was created that day",
    never "something happened" from `updated_at`, which a trust-score bump
    or recall-count increment also changes). Bounded to `TIMELINE_MAX_DAYS`
    buckets regardless of what a client requests."""
    days_requested = max(1, min(TIMELINE_MAX_DAYS, _parse_int(params, "days", 30)))
    rows = conn.execute(
        """
        SELECT DATE(created_at) AS day, COUNT(*) AS count, AVG(trust_score) AS avg_trust
        FROM facts WHERE namespace = ?
        GROUP BY day ORDER BY day DESC LIMIT ?
        """,
        (namespace, days_requested),
    ).fetchall()
    days = [{"day": r["day"], "count": r["count"], "avg_trust": r["avg_trust"]} for r in rows]
    days.reverse()
    return {"namespace": digest, "timestamp_basis": "created_at", "days": days}


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    server_version = "HermesMemoryViewer/1"

    # Bounds how long the stdlib request-parsing loop will block waiting on
    # a slow/idle client (honored automatically by
    # socketserver.StreamRequestHandler.setup(), a BaseHTTPRequestHandler
    # base class) — part of Phase 7.1A's request limits (A4).
    timeout = REQUEST_SOCKET_TIMEOUT

    def _send_html(self, status: int, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        for name, value in _SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        if not getattr(self, "_suppress_body", False):
            self.wfile.write(encoded)

    def _send_plain(self, status: int, body: bytes, *, extra_headers: "dict[str, str] | None" = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for name, value in _SECURITY_HEADERS.items():
            self.send_header(name, value)
        if extra_headers:
            for name, value in extra_headers.items():
                self.send_header(name, value)
        self.end_headers()
        if not getattr(self, "_suppress_body", False):
            self.wfile.write(body)

    def _send_json(self, status: int, data: dict, *, extra_headers: "dict[str, str] | None" = None) -> None:
        encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        for name, value in _SECURITY_HEADERS.items():
            self.send_header(name, value)
        if extra_headers:
            for name, value in extra_headers.items():
                self.send_header(name, value)
        self.end_headers()
        if not getattr(self, "_suppress_body", False):
            self.wfile.write(encoded)

    def _is_api_path(self) -> bool:
        return urlsplit(self.path).path.startswith("/api/")

    def _send_static_asset(self, body: bytes, content_type: str, *, cacheable: bool) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        headers = dict(_SECURITY_HEADERS)
        if cacheable:
            # Pinned, versioned vendor assets only — see
            # _STATIC_ASSET_ALLOWLIST. Every other response in this module
            # keeps the default `no-store`.
            headers["Cache-Control"] = "public, max-age=31536000, immutable"
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        if not getattr(self, "_suppress_body", False):
            self.wfile.write(body)

    def _send_dashboard_html(self, body: bytes) -> None:
        """Phase 7.2C — the interactive dashboard shell gets its own,
        looser-but-still-strict CSP (`_DASHBOARD_SECURITY_HEADERS`) instead
        of `_SECURITY_HEADERS`, since it's the only page that loads local
        JavaScript. `Cache-Control: no-store` either way — it's HTML, not a
        pinned asset."""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for name, value in _DASHBOARD_SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        if not getattr(self, "_suppress_body", False):
            self.wfile.write(body)

    def _reject_method(self) -> None:
        if self._is_api_path():
            self._send_json(
                405, _api_error("METHOD_NOT_ALLOWED", "The Holographic memory viewer is read-only."),
                extra_headers={"Allow": "GET, HEAD"},
            )
            return
        self._send_plain(
            405,
            b"405 Method Not Allowed: the Holographic memory viewer is read-only.\n",
            extra_headers={"Allow": "GET, HEAD"},
        )

    def _check_request_target_length(self) -> bool:
        if len(self.path) > MAX_REQUEST_TARGET_LENGTH:
            self._send_plain(414, b"414 Request-URI Too Long\n")
            return False
        if len(urlsplit(self.path).query) > MAX_QUERY_STRING_LENGTH:
            self._send_plain(414, b"414 Request-URI Too Long: query string too long\n")
            return False
        return True

    def _check_host_header(self) -> bool:
        bound_port = self.server.server_address[1]  # type: ignore[attr-defined]
        if not _is_valid_host_header(self.headers.get("Host"), bound_port):
            self._send_plain(400, b"400 Bad Request: invalid or missing Host header\n")
            return False
        return True

    def _check_origin_header(self) -> bool:
        bound_port = self.server.server_address[1]  # type: ignore[attr-defined]
        if not _is_valid_origin_header(self.headers.get("Origin"), bound_port):
            self._send_plain(403, b"403 Forbidden: cross-origin request rejected\n")
            return False
        return True

    def _acquire_slot(self) -> bool:
        if not self.server.request_semaphore.acquire(blocking=False):  # type: ignore[attr-defined]
            self._send_plain(503, b"503 Service Unavailable: too many concurrent requests.\n")
            return False
        return True

    # Every HTTP method funnels through `_handle`, which applies the
    # Host/Origin/size checks uniformly before any method- or
    # route-specific behavior — including the mutation methods, so even a
    # rejected POST/OPTIONS/TRACE/CONNECT first gets the same DNS-rebinding
    # and request-size defenses as a legitimate GET.
    def do_GET(self) -> None:
        self._handle("GET")

    def do_HEAD(self) -> None:
        self._handle("HEAD")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def do_PATCH(self) -> None:
        self._handle("PATCH")

    def do_OPTIONS(self) -> None:
        self._handle("OPTIONS")

    def do_TRACE(self) -> None:
        self._handle("TRACE")

    def do_CONNECT(self) -> None:
        self._handle("CONNECT")

    def _handle(self, method: str) -> None:
        self._suppress_body = method == "HEAD"
        try:
            if not self._check_request_target_length():
                return
            if not self._check_host_header():
                return
            if not self._check_origin_header():
                return
            if method not in ("GET", "HEAD"):
                self._reject_method()
                return
            if not self._acquire_slot():
                return
            try:
                self._dispatch()
            finally:
                self.server.request_semaphore.release()  # type: ignore[attr-defined]
        except QueryDeadlineExceeded:
            if self._is_api_path():
                self._send_json(503, _api_error("QUERY_DEADLINE_EXCEEDED", "A database query exceeded its execution deadline."))
            else:
                self._send_plain(503, b"503 Service Unavailable: a database query exceeded its execution deadline.\n")
        except Exception:
            # Never the exception's own str(exc) or any database path —
            # see _api_error's docstring for why the API side is a fixed
            # string too.
            if self._is_api_path():
                self._send_json(500, _api_error("INTERNAL_ERROR", "An internal error occurred."))
            else:
                self._send_html(500, _PAGE_HEAD + "<h1>Internal error</h1>" + _PAGE_TAIL)

    def _dispatch_static(self, name: str) -> None:
        resolved = _resolve_static_asset(name)
        if resolved is None:
            self._send_plain(404, b"404 Not Found\n")
            return
        file_path, content_type, cacheable = resolved
        body = file_path.read_bytes()
        self._send_static_asset(body, content_type, cacheable=cacheable)

    def _resolve_required_namespace(self, conn: sqlite3.Connection, params: dict) -> "tuple[str | None, str | None, bool]":
        """Every /api/ route except /api/namespaces requires `ns` and
        resolves it server-side via `_resolve_namespace` — identical
        resolution to the HTML `/namespace`/`/fact`/`/entity` routes, never
        a raw namespace string accepted from the client. Sends the error
        response itself and returns `(None, None, False)` on failure, so
        callers can `if not ok: return` without duplicating the error
        envelope."""
        digest = _parse_str(params, "ns")
        if digest is None:
            self._send_json(400, _api_error("MISSING_NAMESPACE", "The ns query parameter is required."))
            return None, None, False
        namespace = _resolve_namespace(conn, digest)
        if namespace is None:
            self._send_json(404, _api_error("INVALID_NAMESPACE", "Namespace not found"))
            return None, None, False
        return namespace, digest, True

    def _dispatch_api(self, conn: sqlite3.Connection, path: str, params: dict, show_raw_identity: bool) -> None:
        if path == "/api/namespaces":
            self._send_json(200, _api_namespaces(conn, params, show_raw_identity=show_raw_identity))
            return

        if path == "/api/overview":
            namespace, digest, ok = self._resolve_required_namespace(conn, params)
            if not ok:
                return
            self._send_json(200, _api_overview(conn, namespace, digest))
            return

        if path == "/api/facts":
            namespace, digest, ok = self._resolve_required_namespace(conn, params)
            if not ok:
                return
            self._send_json(200, _api_facts(conn, namespace, digest, params))
            return

        if path.startswith("/api/facts/"):
            fact_id = _parse_path_int(path[len("/api/facts/"):])
            if fact_id is None:
                self._send_json(400, _api_error("INVALID_FACT_ID", "The fact id must be a non-negative integer."))
                return
            namespace, digest, ok = self._resolve_required_namespace(conn, params)
            if not ok:
                return
            result = _api_fact_detail(conn, namespace, digest, fact_id)
            if result is None:
                self._send_json(404, _api_error("NOT_FOUND", "Fact not found in this namespace"))
                return
            self._send_json(200, result)
            return

        if path == "/api/search":
            namespace, digest, ok = self._resolve_required_namespace(conn, params)
            if not ok:
                return
            self._send_json(200, _api_search(conn, namespace, digest, params))
            return

        if path == "/api/entities":
            namespace, digest, ok = self._resolve_required_namespace(conn, params)
            if not ok:
                return
            self._send_json(200, _api_entities(conn, namespace, digest, params))
            return

        if path.startswith("/api/entities/") and path.endswith("/graph"):
            entity_id = _parse_path_int(path[len("/api/entities/"):-len("/graph")])
            if entity_id is None:
                self._send_json(400, _api_error("INVALID_ENTITY_ID", "The entity id must be a non-negative integer."))
                return
            namespace, digest, ok = self._resolve_required_namespace(conn, params)
            if not ok:
                return
            result = _api_entity_graph(conn, namespace, digest, entity_id)
            if result is None:
                self._send_json(404, _api_error("NOT_FOUND", "Entity not found in this namespace"))
                return
            self._send_json(200, result)
            return

        if path == "/api/timeline":
            namespace, digest, ok = self._resolve_required_namespace(conn, params)
            if not ok:
                return
            self._send_json(200, _api_timeline(conn, namespace, digest, params))
            return

        self._send_json(404, _api_error("NOT_FOUND", "Unknown API route"))

    def _dispatch(self) -> None:
        parsed = urlsplit(self.path)
        params = parse_qs(parsed.query, keep_blank_values=True)
        path = parsed.path

        # Phase 7.2A — static assets never touch the database, so this is
        # handled before any connection is opened and before the
        # query-parameter checks below (a static asset request carries no
        # meaningful query string). `path` is the raw, never percent-decoded
        # request path (see _STATIC_ASSET_ALLOWLIST's docstring above).
        if path.startswith("/static/"):
            self._dispatch_static(path[len("/static/"):])
            return

        # Phase 7.2C — the dashboard shell is a fixed, bundled file (never
        # built from request data), so it's served the same way a static
        # asset would be, just with the dashboard-specific CSP and its own
        # Content-Type. It never touches the database either.
        if path == "/dashboard":
            try:
                body = _DASHBOARD_HTML_PATH.read_bytes()
            except OSError:
                self._send_html(500, _PAGE_HEAD + "<h1>Internal error</h1>" + _PAGE_TAIL)
                return
            self._send_dashboard_html(body)
            return

        db_path = self.server.db_path  # type: ignore[attr-defined]
        show_raw_identity = self.server.show_raw_identity  # type: ignore[attr-defined]

        is_api = path.startswith("/api/")

        if len(params) > MAX_QUERY_PARAM_COUNT:
            if is_api:
                self._send_json(400, _api_error("TOO_MANY_PARAMETERS", "Too many query parameters."))
            else:
                self._send_html(400, _PAGE_HEAD + "<h1>Too many query parameters</h1>" + _PAGE_TAIL)
            return
        if any(len(v) > MAX_SEARCH_LENGTH for v in params.get("q", [])):
            if is_api:
                self._send_json(400, _api_error("SEARCH_QUERY_TOO_LONG", "Search query too long."))
            else:
                self._send_html(400, _PAGE_HEAD + "<h1>Search query too long</h1>" + _PAGE_TAIL)
            return
        if _parse_int(params, "page", 1) > MAX_PAGE_NUMBER:
            if is_api:
                self._send_json(400, _api_error("PAGE_NUMBER_TOO_LARGE", "Page number too large."))
            else:
                self._send_html(400, _PAGE_HEAD + "<h1>Page number too large</h1>" + _PAGE_TAIL)
            return

        with _connection(db_path) as conn:
            if is_api:
                self._dispatch_api(conn, path, params, show_raw_identity)
                return

            if path in ("/", "/index", "/index.html"):
                self._send_html(200, _render_index(conn, show_raw_identity=show_raw_identity, params=params))
                return

            if path == "/namespace":
                digest = _parse_str(params, "ns")
                if digest is None:
                    self._send_html(400, _PAGE_HEAD + "<h1>Missing required ns parameter</h1>"
                                     "<p>The namespace must be selected explicitly from the index page.</p>" + _PAGE_TAIL)
                    return
                namespace = _resolve_namespace(conn, digest)
                if namespace is None:
                    self._send_html(404, _PAGE_HEAD + "<h1>Namespace not found</h1>" + _PAGE_TAIL)
                    return
                self._send_html(200, _render_namespace(conn, namespace, digest, params=params))
                return

            if path == "/fact":
                digest = _parse_str(params, "ns")
                fact_id = _parse_int(params, "id", -1)
                if digest is None or fact_id < 0:
                    self._send_html(400, _PAGE_HEAD + "<h1>Missing required ns/id parameters</h1>" + _PAGE_TAIL)
                    return
                namespace = _resolve_namespace(conn, digest)
                if namespace is None:
                    self._send_html(404, _PAGE_HEAD + "<h1>Namespace not found</h1>" + _PAGE_TAIL)
                    return
                row = _fact_in_namespace(conn, fact_id, namespace)
                if row is None:
                    self._send_html(404, _PAGE_HEAD + "<h1>Fact not found in this namespace</h1>" + _PAGE_TAIL)
                    return
                self._send_html(200, _render_fact(conn, row, digest, namespace))
                return

            if path == "/entity":
                digest = _parse_str(params, "ns")
                entity_id = _parse_int(params, "id", -1)
                if digest is None or entity_id < 0:
                    self._send_html(400, _PAGE_HEAD + "<h1>Missing required ns/id parameters</h1>" + _PAGE_TAIL)
                    return
                namespace = _resolve_namespace(conn, digest)
                if namespace is None:
                    self._send_html(404, _PAGE_HEAD + "<h1>Namespace not found</h1>" + _PAGE_TAIL)
                    return
                if not _entity_in_namespace(conn, entity_id, namespace):
                    self._send_html(404, _PAGE_HEAD + "<h1>Entity not found in this namespace</h1>" + _PAGE_TAIL)
                    return
                self._send_html(200, _render_entity(conn, entity_id, digest, namespace))
                return

            self._send_html(404, _PAGE_HEAD + "<h1>Not found</h1>" + _PAGE_TAIL)

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A002
        # Avoid default stderr request logging (noisy under tests and no
        # operational need for it beyond what the CLI already prints).
        pass


class ViewerServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address: tuple, db_path: "str | Path", show_raw_identity: bool = False) -> None:
        super().__init__(server_address, _Handler)
        self.db_path = Path(db_path)
        self.show_raw_identity = bool(show_raw_identity)
        self.request_semaphore = threading.Semaphore(MAX_CONCURRENT_REQUESTS)


def create_server(
    db_path: "str | Path", *, host: str = "127.0.0.1", port: int = 0, show_raw_identity: bool = False,
) -> ViewerServer:
    """Construct (but do not start) the viewer's HTTP server.

    Raises `NonLoopbackBindingNotSupported` before any socket is opened if
    `host` is not a loopback address — see the module docstring. Raises
    `UnsupportedSchemaError` (Phase 7.1C) if `db_path` isn't a valid SQLite
    database, or is missing a table/column the viewer's queries require —
    checked once here, at construction time, rather than only surfacing
    later as a generic request-time 500. `port=0` lets the OS assign an
    ephemeral free port (what tests use); the CLI
    (`hermes_cli/memory_setup.cmd_viewer`) picks a fixed default port for
    interactive use.
    """
    host = _validate_host(host)
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"Holographic memory database not found: {db_path}")
    try:
        with _connection(db_path) as conn:
            _validate_schema(conn)
    except sqlite3.DatabaseError as exc:
        raise UnsupportedSchemaError(
            f"Holographic memory viewer: {db_path} does not look like a valid SQLite database: {exc}"
        ) from exc
    return ViewerServer((host, port), db_path, show_raw_identity=show_raw_identity)
