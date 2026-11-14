# Vendored third-party JavaScript

These files are pinned, locally-served copies of third-party libraries used
by the Holographic memory viewer's dashboard (Phase 7.2). They are fetched
once at development time and committed — the viewer never makes a runtime
request to any CDN. This lets the dashboard work entirely over an SSH
tunnel with no outbound internet access, and lets the viewer's CSP use
`script-src 'self'` with no external allowlist.

Do not bump these versions casually — the dashboard JS is written against
these exact APIs. If a version bump is needed, re-verify license and
known-CVE status for the new version, re-record its checksum below, and
re-test the dashboard.

| File | Version | Source | License | SHA-256 |
|---|---|---|---|---|
| `chart.umd.4.4.7.min.js` | 4.4.7 | https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js | MIT | `206b6e8bb00fc7bba2c7ee80ca41db3e9e05ba7be0aa35abeba9cfd5357f5d0e` |
| `vis-network.9.1.9.min.js` | 9.1.9 | https://cdn.jsdelivr.net/npm/vis-network@9.1.9/standalone/umd/vis-network.min.js | Apache-2.0 OR MIT (dual-licensed) | `f53f833ddb9bf97efe856bb0637d4fe88f39e39999c7e94a4b8afc8de8a1a2e5` |

Fetched 2026-11-14. Both are long-established, widely-used libraries with
no known advisories against these pinned versions at fetch time; re-check
before any future bump.

The version number is embedded directly in each filename (Phase 7.2D).
These are served with a 1-year immutable `Cache-Control`, so a future
version bump MUST introduce a new filename (e.g. `chart.umd.4.5.0.min.js`)
rather than overwriting the file in place under the old name — otherwise a
browser that already cached the old version under the old URL would never
see the update. Update `_STATIC_ASSET_ALLOWLIST` in `viewer.py` and the
`<script src>` in `viewer_assets/index.html` to the new filename at the
same time.

To verify a file hasn't drifted from this record:

```bash
sha256sum plugins/memory/holographic/viewer_assets/vendor/*.js
```
