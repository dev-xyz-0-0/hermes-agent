# Holographic memory viewer

`hermes memory viewer` starts an **optional, local, read-only** web page for
inspecting the active profile's Holographic memory database — namespaces,
facts, entities, relationships, and supersession history. It is a
diagnostic tool, not a management console: there is no edit, delete,
restore, or raw-SQL capability anywhere in it.

It is implemented entirely in `plugins/memory/holographic/viewer.py`, using
only the Python standard library (`http.server`, `sqlite3`) — no new
dependency, no third-party web framework. It is never imported by normal
Hermes startup (CLI, gateway, or agent loop); the only way it runs is
`hermes memory viewer`.

## Starting it

```bash
hermes memory viewer
```

Flags:

| Flag | Default | Notes |
|---|---|---|
| `--port N` | `8787` | TCP port to bind. |
| `--host HOST` | `127.0.0.1` | Must be `127.0.0.1`, `localhost`, or `::1` — anything else is refused before any socket is opened (see [Non-loopback binding](#non-loopback-binding-is-not-supported)). |
| `--show-raw-identity` | off | Explicit opt-in to show raw namespace identities (e.g. a Telegram chat ID) next to their opaque digest on the namespace list. Off by default; cannot be enabled via the URL. |

Like every other `hermes memory` subcommand, it resolves the database path
through the active profile (`-p <profile>` / `HERMES_HOME`) — there's no way
to point it at "all profiles" or an arbitrary file.

Press `Ctrl+C` to stop it. Startup prints the database path, confirms it's
read-only, and prints the URL it's serving on; a bind failure (e.g. the port
is already in use) or an incompatible database prints one clear line and
exits non-zero — never a raw traceback.

## What it shows

- **Namespace list** (`/`) — one row per namespace, as an opaque SHA-1
  digest (not the raw chat/session identifier) unless `--show-raw-identity`
  was passed, with active/superseded/unknown-status/forgotten counts.
- **Namespace detail** (`/namespace?ns=<digest>`) — that namespace's facts,
  paginated, with literal or full-text (FTS5) search over content.
- **Fact detail** (`/fact?ns=<digest>&id=<id>`) — one fact's content (secret
  shapes redacted), tags, trust/confidence, and its immediate supersession
  ancestors/successor.
- **Entity detail** (`/entity?ns=<digest>&id=<id>`) — that entity's
  namespace-scoped relationships and a bounded graph traversal.

Every page requires an explicit namespace digest; the index page never
renders an individual fact, and a fact/entity that doesn't belong to the
requested namespace 404s rather than falling back to "no filter".

## Security model (summary)

Full detail lives in `plugins/memory/holographic/viewer.py`'s module
docstring; the short version:

- **Loopback-only.** `--host` only accepts `127.0.0.1`/`localhost`/`::1`.
  Non-loopback binding isn't offered behind a flag that could be mistaken
  for a real safeguard — it's refused outright, because no authentication
  or CSRF protection exists yet.
- **Read-only, structurally.** Connections are opened `mode=ro` with
  `PRAGMA query_only=ON`; a mutation attempt raises instead of succeeding.
  No route ever calls a write method.
- **Namespace-isolated.** A namespace digest is an identifier for
  re-selecting which existing namespace to view, re-derived from the real
  data on every request — never an authentication credential, and never a
  raw namespace string accepted from a client.
- **Browser-hardened.** Host/Origin header validation (defense against DNS
  rebinding and cross-origin requests — not authentication), security
  response headers (CSP, `X-Frame-Options`, etc.), request-size and
  concurrency limits, and a SQLite execution deadline on every request.
- **Redacted by default.** Fact content, tags, entity names, and
  relationship labels are passed through the same secret-shape detector the
  capture-time filter uses, before being rendered.

## Remote access: SSH tunneling (recommended), never a public bind

The viewer deliberately has no way to bind to a public interface. To look at
it from a laptop while it runs on a remote machine (e.g. a GCP VM), use SSH
local port forwarding — the viewer never needs to know anything changed:

```bash
# On the remote machine:
hermes memory viewer --port 8765

# On your laptop, forward a local port to the remote loopback port:
gcloud compute ssh hermes-vm \
  --project=<your-project> \
  --zone=<your-zone> \
  -- -N -L 8765:127.0.0.1:8765
```

Then open `http://localhost:8765` in a local browser. Everything travels
over the SSH-encrypted tunnel; the viewer itself never leaves loopback on
either end.

Plain `ssh` works identically if you're not using `gcloud`:

```bash
ssh -N -L 8765:127.0.0.1:8765 user@remote-host
```

**Do not**, as a substitute for this:
- Bind the viewer to `0.0.0.0` or any non-loopback address — it's refused
  by the code itself (`NonLoopbackBindingNotSupported`), not just discouraged.
- Open a firewall rule / security-group ingress for the viewer's port.
- Put the viewer behind a public load balancer or reverse proxy.

Stopping the SSH tunnel immediately removes the remote access path (the
viewer was never reachable except through it); stopping the viewer
(`Ctrl+C`) closes its listening socket.

## Known limitations

- `--host ::1` (IPv6 loopback) is accepted by the host-validation check, but
  the server itself currently always binds an `AF_INET` (IPv4) socket, so a
  real IPv6 bind isn't exercised end-to-end yet. `127.0.0.1`/`localhost`
  (IPv4) are the supported path today.
- No authentication exists. Anything that can reach the bound port (i.e.
  anyone with shell access to the same machine, or anyone the tunnel is
  shared with) can view the data. Treat SSH access to the host as the trust
  boundary.

## Related docs

- `plugins/memory/holographic/viewer.py` — full implementation and the
  canonical, line-by-line safety model.
- `docs/prompts/holographic-validation-prompts.md` — guidance for an AI
  coding assistant working on this codebase, including why *not* to
  improvise around the non-loopback refusal.
