# Hermes Skill Creation — Feature Guide

Skills are Hermes's procedural memory: reusable, narrow, actionable
instructions for recurring task types (as opposed to `MEMORY.md`/`USER.md`,
which are broad and declarative). This guide documents the **implemented**
skill creation lifecycle — how an agent creates, drafts, validates, gets
human approval for, and publishes a skill, and the security model that
makes agent-initiated skill writes safe to leave enabled.

**Status:** Phases 1–3 plus the 3.1 hardening pass are implemented, tested,
and in the codebase today. Phases 4 (autonomous candidate detection) and 5
(holographic memory integration) are specs only — see
[Sequence](#sequence-and-phase-docs) below. Nothing in this guide describes
unimplemented behavior.

## Two ways to create a skill

There are two independent paths, both exposed through the single
`skill_manage` tool (`tools/skill_manager_tool.py`):

### 1. Direct creation (`create`/`edit`/`patch`/`write_file`/`remove_file`)

The original, simple path. `skill_manage(action="create", name=..., content=...)`
writes a new skill straight to disk — it's immediately discoverable. Use
this for everyday "save this as a skill" work where the user is right there
approving each step conversationally, same as any other tool call.

### 2. Draft lifecycle (`draft` → `validate` → `approve` → `publish`)

A staged pipeline for when you want the skill checked and a human to
explicitly sign off before it becomes real:

```
draft ──validate──▶ validating ──▶ validated ──▶ awaiting_approval ──▶ published
  │                                   │                  │
  └──────────────cancelled◀───────────┴──────────────────┘
                (discard, any non-terminal state)
```

| Action | What it does |
|---|---|
| `draft` | Stage a candidate skill in `~/.hermes/skill-drafts/<id>/` — never discoverable, never runnable. |
| `validate` | Run structure / destination / security / dependency / duplicate checks; persist a machine-readable report. |
| `preview` | View a draft's content, last validation report, and whether that report is now stale. |
| `discard` | Remove a draft. Refuses once `published` — use `delete` for a live skill instead. |
| `drafts_list` | List this profile's drafts (never leaks other profiles'). |
| `approve` | **Blocks on a real human** (see [Approval authority](#approval-authority) below) and binds an authorization to the draft's exact content hash + destination, with a TTL. |
| `publish` | Re-validates the approval against *current* reality (safeguard, not just trust) and atomically moves the draft into place. |
| `publish_git` | Commits the published skill in its Git repo, if any and if configured. Retryable independently of `publish`. |

A draft's content mutating after validation or approval automatically
invalidates the stale report/approval — see
[`agent/skill_creation/staging.py`](../../agent/skill_creation/staging.py).

## Why two paths, and when to require the second one

By default (`skills.creation_policy: legacy`), both paths work, matching
pre-existing behavior exactly. Set `skills.creation_policy:
approval_required` to **disable** `create`/`edit`/`patch`/`write_file`/
`remove_file` entirely — an agent then has no way to get new content onto
disk except through the draft lifecycle, which ends in a real human
clicking approve. (`delete` is never gated — removing a skill isn't
introducing untrusted content.) No `skill_manage` action can read or change
this policy; it's a config.yaml-only switch, so an agent has no path to
turn it off once an operator has turned it on.

## Approval authority

This is the security property the whole draft lifecycle exists to
guarantee: **a tool call's own arguments never constitute approval.**

`approve_draft()` doesn't just check that the caller supplied a `draft_id`
and that validation passed — before writing any approval record, it calls
`tools.approval.request_explicit_approval()`, the same one-shot
human-approval primitive MCP tools already use for sensitive actions. That
call blocks the calling thread and routes to:

- the gateway's `/approve` / `/deny` command — resolved only by a person
  typing it into the chat, or
- the CLI's interactive `input()` prompt — a real human at the terminal.

With **no** interactive channel available (an unattended/autonomous run),
it fails closed: `approve_draft` returns an error and the draft stays
`validated`, never silently advancing to `awaiting_approval`. There is
deliberately no code path by which an agent can approve its own draft.

See `tests/agent/test_skill_draft_approval.py::TestAgentCannotSelfApprove`
for the test suite proving this (including a real subprocess/thread-based
self-approval attempt that correctly fails).

## Safeguards between approval and publication

Approving a draft doesn't immediately publish it — `publish_draft()`
re-checks everything from scratch rather than trusting what `approve_draft`
recorded minutes or hours earlier:

1. Approval must not have expired (TTL set at `approve` time).
2. The draft's content hash must still match what was approved — any edit
   after approval invalidates it.
3. `skills.create_dir` is re-resolved fresh; if it changed since approval,
   publish refuses rather than writing to an unintended location.
4. The destination is re-checked for collisions.

Any of these failing demotes the draft (to `validated` for a plain expiry —
just re-approve; to `failed` for a content/destination change — re-validate
first) rather than publishing something stale.

**Locking:** a per-draft lock and a per-destination lock
(`agent/skill_creation/locking.py`) are held for the whole
check-then-publish sequence, so two concurrent publish attempts — same
draft or two different drafts targeting the same path — always yield
exactly one winner. Locks are PID+thread+start-time scoped; a dead or
stopped holder's lock is reclaimed, a live one's is waited on, and nested
acquisition by the same thread is rejected outright (there's no call site
that needs it).

**Atomic filesystem publish:** the draft's files are copied into a
same-filesystem temp directory, hash-verified, then moved into place with a
single `os.replace` — never a partially-visible skill, and a crash between
the move and the manifest update is handled idempotently on retry.

## Git integration

`publish_git` is independent of and retryable separately from `publish` —
a Git failure never re-triggers a filesystem re-copy. It:

- builds the commit against an **isolated temporary index**
  (`GIT_INDEX_FILE` pointed at a fresh temp file) — never the repo's real
  `.git/index` — so it can never pick up or clobber whatever you already
  have staged;
- stages the exact published path only, never `git add .`/`-A`;
- writes the commit with `update-ref <branch> <new> <old>`, a
  compare-and-swap ref update that fails safely (no corrupting write) if
  `HEAD` moved concurrently;
- afterward, additively syncs just the newly-published paths into the
  *real* index too (purely cosmetic — stops `git status` from showing a
  confusing phantom delete), but only after confirming via `git status
  --porcelain` that nothing was already staged at that exact path;
- never commits with `--amend`, never force-pushes, never resets anything.

Controlled by `skills.publishing`: `git_enabled` (default `true` — skip
Git entirely if `false`), `auto_commit` (default `false`), `auto_push`
(default `false` — and even when `true`, `publish_git` also requires the
caller to pass `push=true` on that specific call; two independent signals
are required, not one).

## Validation pipeline

`skill_manage(action="validate")` runs five checks
(`agent/skill_creation/validation.py`), each reporting `pass` / `warning` /
`blocked` / `error`; the draft only reaches `validated` on
`pass`/`warning` overall:

| Check | What it verifies |
|---|---|
| Structure | Name/category format, YAML frontmatter, required fields, size limits — the same validators `create` already uses. |
| Destination | Resolves `skills.create_dir`, checks for name collisions, rejects unsafe/symlinked paths. Never creates the destination. |
| Security | Runs `tools/skills_guard.py`'s scanner. **Stricter than `create`'s policy**: any non-"allow" verdict (including "ask") blocks. Scanner unavailable or raising is `error`, not a silent pass — fails closed. |
| Dependencies | Declared `prerequisites.commands`/`env_vars` checked via `shutil.which`/env lookups only — report-only, never executes or installs anything. |
| Duplicates | Exact name collisions block; identical content hash against another draft is an advisory warning. |

No generated script is ever executed during validation, by any check.

## Configuration reference

All under the `skills:` key in `config.yaml`:

```yaml
skills:
  external_dirs: []        # read-only discovery locations, shared across tools/agents
  create_dir: ""            # where new/published skills go; "" = legacy ~/.hermes/skills/
  creation_policy: legacy   # "legacy" | "approval_required"
  publishing:
    git_enabled: true
    auto_commit: false
    auto_push: false
```

See [`configuration.yaml`](configuration.yaml) in this directory for the
fully annotated version, including which keys from the original phase
spec were deliberately **not** implemented as toggles (e.g.
`publishing.require_approval`/`require_clean_target` — those checks are
always on, unconditionally, rather than configurable).

## Tool actions reference

All through `skill_manage(action=..., ...)`:

```
create, edit, patch, delete, write_file, remove_file     # direct path
draft, validate, preview, discard, drafts_list            # draft staging
approve, publish, publish_git                             # authorization + publication
```

See the schema in `tools/skill_manager_tool.py::SKILL_MANAGE_SCHEMA` for
full parameter documentation (`draft_id`, `ttl_seconds`, `push`, etc.).

## Where things live

```
agent/skill_paths.py              # create_dir / creation_policy resolution, discovery dirs
agent/skill_creation/
├── __init__.py                   # public API: create/validate/approve/publish_draft, etc.
├── models.py                     # DraftState, DraftManifest, ValidationReport
├── registry.py                   # draft persistence (~/.hermes/skill-drafts/)
├── staging.py                    # draft filesystem writes, content-hash invalidation
├── validation.py                 # the 5 validation checks
├── locking.py                    # per-draft / per-destination locks
└── git_integration.py            # isolated-index commits
tools/skill_manager_tool.py        # skill_manage tool — all 14 actions
tools/skills_guard.py              # security scanner (reused by both paths)
```

## Sequence and phase docs

Each phase doc below is the original spec/prompt used to build that phase —
kept for historical reference and exact acceptance criteria, not as a
second source of truth once a phase is implemented (this README and the
code are current; a phase doc is a snapshot of intent before it was built).

1. [Phase 1 — Configurable creation directory](phase-01-create-dir.md) — **implemented**
2. [Phase 2 — Draft lifecycle and validation](phase-02-drafts-validation.md) — **implemented**
3. [Phase 3 — Approval, publication and Git](phase-03-publication-git.md) — **implemented**, hardened in 3.1 (see [architecture.md](architecture.md) invariant 4 and the Phase 3.1 section of [acceptance-checklist.md](acceptance-checklist.md))
4. [Phase 4 — Autonomous workflow candidate detection](phase-04-autonomous-detection.md) — not started
5. [Phase 5 — Holographic integration](phase-05-holographic.md) — not started

See also [Architecture](architecture.md) (invariants and flow),
[Configuration](configuration.yaml) (annotated sample), and
[Acceptance checklist](acceptance-checklist.md) (per-phase sign-off status).

## Ground rules for extending this feature

- Reuse existing validators/resolvers — `agent/skill_paths.py` and
  `tools/skill_manager_tool.py`'s structure/destination validators are the
  single source of truth for both creation paths; don't fork them.
- No unapproved execution of generated scripts, ever, at any stage.
- `approve_draft`'s human-authority check is not optional and must not be
  satisfiable by anything the agent itself controls — if you touch that
  function, re-read `TestAgentCannotSelfApprove` first.
- Test with temporary Hermes homes; never against production directories.
  `tests/conftest.py`'s autouse `_isolate_hermes_home` fixture now also
  patches the known stale `SKILLS_DIR` module constants — see
  `tests/test_skills_dir_isolation.py` if you add a new module with its own
  `SKILLS_DIR`-style constant.
- Before starting Phase 4: candidate detection must only ever reach
  `draft`/`validate` automatically — it must never be able to bypass
  `approve_draft`'s human gate.
