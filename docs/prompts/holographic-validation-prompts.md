# Holographic Memory — Production Validation Prompts

Audience: an **operator** validating Holographic memory against a **real
production `HERMES_HOME`** — either typing `hermes memory ...` commands
directly, or asking an agent (e.g. Claude Code) to run them. These are not
chat prompts for end users; see `docs/prompts/holographic-prompts.md` for
that.

Every command below resolves against the active profile automatically via
the global `-p`/`--profile` flag (position-independent: `hermes -p NAME
memory health` and `hermes memory health -p NAME` are identical). Omit `-p`
to target the default profile.

**Golden rule for production: read-only first, always.** Every write/apply
command in this doc is preceded by a backup step and a dry-run review step.
Do not skip straight to `--apply` on a production database just because a
prompt further down in this doc mentions it — the ordering matters.

---

## 1. Read-only validation (safe to run anytime, no backup needed)

These never write to the database. Run them first, and whenever you just
want a status check.

### Health check

> "Run a health check on the production Holographic memory database and
> tell me if there are any issues."

```bash
hermes memory health --json
```

Exit codes: `0` clean, `1` warnings, `2` errors, `3` cannot run (e.g. db
missing). Checks SQLite integrity, foreign keys, FTS/facts parity, orphaned
rows, malformed HRR vectors, and bank-count consistency — all without
touching the data. Never prints raw fact content or raw chat/namespace
identities, only namespace digests and counts, so it's safe to run against
real user data and paste the output somewhere for review.

> "If the health check finds anything other than a clean result, show me
> the exact issue codes and ask me before doing anything else."

### Alias check (should match health exactly)

> "Run `hermes memory verify` and confirm it reports the same result as
> `hermes memory health`."

```bash
hermes memory verify --json
```

`verify` is a thin alias for `health` by design — if the two ever disagree,
that itself is a bug worth reporting, not something to "fix" by picking one.

### Retrieval-quality benchmark (read-only, no counters touched)

> "Run the Holographic memory benchmark against production using
> `<gold-set path>` and report Recall@1, Recall@5, MRR, and the
> namespace-leakage count. Namespace leakage must be exactly zero — treat
> anything else as a hard failure."

```bash
hermes memory benchmark --gold-set ./gold.json --json
```

If you don't already have a production-shaped gold set, ask first — do not
let an agent invent one from real user content. (A synthetic gold set built
from fixture data, like the one used in this project's own test suite, is
fine; a gold set scraped from real production facts is not, since gold-set
files aren't subject to the same redaction as other tooling output.)

### Backup validity check (read-only — checks an existing backup, makes no new one)

> "Verify that the most recent production backup at `<path>` is valid
> before I rely on it."

```bash
hermes memory health --backup-path /path/to/backup.db --json
```

### Read-only diagnostic viewer

> "Start the local Holographic memory viewer against production, read-only,
> loopback only, and let me browse namespaces without seeing raw chat
> identities."

```bash
hermes memory viewer
# http://127.0.0.1:8787/ — Ctrl+C to stop
```

Only add `--show-raw-identity` if you specifically need to map a digest back
to a real namespace string for an investigation — it's off by default for a
reason. Never bind to a non-loopback host; the command refuses this outright
(no auth/CSRF exists yet).

> "Do not pass --show-raw-identity unless I explicitly ask for it."

---

## 2. Backup-before-apply sequence (required before any write below)

Always do this immediately before any `--apply`/write command in section 3,
against the production database you're about to touch:

> "Before you run anything with --apply against production, create a fresh
> backup and verify it, then show me the verification result. Do not
> proceed to the write step until I've seen that the backup is valid."

The maintenance commands in section 3 already create and verify a backup
internally before writing anything — but for a production run, also take an
explicit, separate backup first (via `fact_store`'s `backup` action from a
real session, or by copying the db file while the process is stopped) so you
have a recovery point that exists independently of the command you're about
to run.

> "After the backup is verified, show me the dry-run output for
> `<command>` and wait for me to say 'apply' before you add --apply."

---

## 3. Gated write operations (dry-run → review → explicit apply)

For every command in this section, use this exact three-step prompt
pattern. Do not combine steps 1 and 3 into one prompt.

**Step 1 — dry run, always first:**

> "Show me what `<command>` would do on production, dry-run only, no writes."

**Step 2 — human review (you, not the agent, decide):**

Read the planned counts/candidates. If anything looks larger or different
than expected, stop and investigate before continuing.

**Step 3 — explicit apply, only after you've reviewed step 2's output:**

> "I've reviewed the dry-run output and it looks correct. Now run
> `<command> --apply` against production."

### Rebuild HRR vectors

```bash
hermes memory rebuild-vectors              # dry-run (default)
hermes memory rebuild-vectors --apply --json
```

> "Dry-run `rebuild-vectors` on production and tell me how many facts would
> be affected."
> *(review)*
> "Apply it."

### Rebuild namespace/category banks

```bash
hermes memory rebuild-banks                # dry-run (default)
hermes memory rebuild-banks --apply --json
```

> "Dry-run `rebuild-banks` on production and tell me which namespace/
> category pairs would be rebuilt."
> *(review)*
> "Apply it."

### Backfill entity relationships

```bash
hermes memory backfill-relationships --dry-run
hermes memory backfill-relationships --apply --json
```

> "Dry-run `backfill-relationships` on production and tell me how many
> candidate relationships it found, and in how many namespaces."
> *(review)*
> "Apply it."

### Migrate legacy MEMORY.md/USER.md into Holographic

```bash
hermes memory import --from builtin --provider holographic             # preview (no --apply)
hermes memory import --from builtin --provider holographic --apply
```

> "Preview the built-in memory import against production and show me
> exactly what would be imported, what's a duplicate, and what's rejected."
> *(review)*
> "Apply the import."

All four of the above acquire a maintenance lock and internally create +
verify their own backup before writing — if that internal backup
verification fails, the command aborts with zero writes. The explicit
backup in section 2 is a second, independent safety net on top of that, not
a substitute for it.

---

## 4. Post-change validation (after any `--apply` above)

> "Now that you've applied `<command>`, run `hermes memory health --json`
> again and confirm it's still clean. Then run the benchmark again if one
> is available, and confirm Recall@1/@5 and namespace-leakage count haven't
> regressed from before the change."

```bash
hermes memory health --json
hermes memory benchmark --gold-set ./gold.json --json   # if available
```

If health reports anything new, or namespace leakage is nonzero, treat that
as a production incident, not a follow-up item — stop and restore from the
backup taken in section 2 rather than attempting a live fix.

> "If the post-change health check or benchmark looks worse than before,
> stop immediately and tell me — don't try to fix it yourself, and don't
> run any further --apply commands."

---

## 5. Things to never ask an agent to do against production

- "Run raw SQL against the production database." — no tool/command exposes
  this, by design; don't try to work around that.
- "Restore production from backup without showing me the plan first." —
  restore is destructive; always review the backup's verification result
  and get explicit confirmation before overwriting a live database.
- "Skip the dry-run and just apply `<command>` to save time." — don't.
  Every write command's --apply step exists specifically for production
  use; the dry-run/review/apply pattern in section 3 is the point, not
  overhead.
- "Bind the viewer to a non-loopback host so I can access it remotely." —
  unsupported outright (no auth/CSRF exists yet); don't attempt a
  workaround like SSH-tunneling around this without separately confirming
  that's actually what you want to do and why.
- "Enable --show-raw-identity by default." — it's an explicit, narrow,
  server-start-only opt-in for a reason; don't make it a habit.
