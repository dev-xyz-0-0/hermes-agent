# Holographic Memory — Prompts Reference

Two different audiences use Holographic memory, with two different interfaces:

1. **End users**, talking to Hermes in chat. They never type a tool name — they
   just say what they want in plain language, and the model decides whether to
   call `fact_store` or `fact_feedback` (and with which `action`). The prompts
   in [Chat prompts](#chat-prompts) below are exactly that: things to type to
   Hermes, not commands.
2. **Operators**, running `hermes memory ...` from a shell. These are real
   commands, not prompts — see [CLI / operator commands](#cli--operator-commands).

Everything here reflects the actual shipped `fact_store`/`fact_feedback` tool
schemas and `hermes memory` subcommands in this repo — not the demo reference
implementation, which is never imported at runtime (see
`plugins/memory/holographic/README.md`).

---

## Chat prompts

These are natural-language things a user can say to Hermes. The model maps
them onto `fact_store`'s `action` enum (`add`, `search`, `probe`, `related`,
`reason`, `contradict`, `update`, `remove`, `list`, `stats`, `history`,
`diagnostics`, `backup`, `share`) or `fact_feedback` (`helpful`, `unhelpful`).
You do not need to mention the tool or action names — Hermes resolves intent
from the sentence. Facts stated in conversation are usually captured
automatically anyway (if `auto_capture` is on); these prompts are for
*explicit* control.

### Adding facts (`add`)

- "Remember that I prefer dark mode in every editor."
- "My default AWS region is us-east-1 — remember that."
- "Note for this project: we deploy on Fridays only with sign-off from Priya."
- "Remember: the staging database password rotates every 90 days." *(a prompt
  like this with an actual secret value would be rejected by the capture
  security filter — only the fact *about* the policy is safe to store)*
- "File this under my preferences: I always want commit messages in
  imperative mood."

### Correcting / superseding a fact (`add` with an implicit correction)

- "Actually, my default AWS region is now eu-west-1, not us-east-1."
- "Update what you know — I switched from Postgres to MySQL for this
  project."
- "Correction: the deploy day is now Thursdays, not Fridays."

Hermes' consolidation logic (token-overlap + fuzzy N-gram evidence) decides
whether this supersedes an existing fact, is treated as a near-duplicate, or
is stored as a new, independent fact when the target is ambiguous — it will
not guess at an unclear correction.

### Forgetting a fact (`add` with an implicit forget, or `remove`)

- "Forget that I ever used MySQL for this project."
- "That fact about my old phone number isn't true anymore, forget it."
- "Remove fact #42." *(needs a fact ID — see `list`/`search`/`history` below
  for how to find it)*

Forgotten facts are tombstoned — they will not silently resurface through
backup/restore, migration, or batch import.

### Searching (`search`)

- "What do you remember about my editor config?"
- "Search your memory for anything about the deploy process."
- "Do you have any notes on how I like code reviewed?"

### Probing an entity (`probe`)

- "What do you know about Priya?"
- "Tell me everything you remember about the staging environment."
- "Give me a full recall on the 'billing-service' project."

### Related facts / structural adjacency (`related`)

- "What's connected to Priya in memory?"
- "What else links to the billing-service project?"

### Reasoning across multiple entities (`reason`)

- "What facts connect both Priya and the billing-service project?"
- "Is there anything in memory that ties my AWS account to the staging
  database?"

### Finding contradictions (`contradict`)

- "Do I have any conflicting facts stored about my default region?"
- "Check your memory for contradictions about the deploy schedule."
- "Run a memory hygiene check — anything that disagrees with itself?"

### Updating trust / editing a fact (`update`)

- "That fact about my timezone is still accurate, bump its confidence up."
- "Fact #17 is wrong, lower its trust."
- "Re-tag fact #9 as 'project' instead of 'general'."

### Listing facts (`list`)

- "List everything you remember about me."
- "Show me all facts tagged 'project'."
- "What's in your memory right now, sorted by most recent?"

### Stats (`stats`)

- "How many facts do you have stored about me?"
- "Give me a summary of what's in memory — counts by category."

### History / what a correction replaced (`history`)

- "What did that last correction about my AWS region replace?"
- "Show me the revision history for fact #12."

### Diagnostics (operational counts only, never content) (`diagnostics`)

- "Run a quick health check on your memory — nothing content-specific,
  just counts."
- "Are there any structural issues with how facts are stored?"

(This is the in-chat, content-free `fact_store` diagnostics action. For the
full read-only database health check with stable diagnostic codes, see
`hermes memory health` in the CLI section — that is the operator-facing
version of the same idea.)

### Backup (`backup`)

- "Back up the memory database now."
- "Take a snapshot of memory before I ask you to do something risky with it."

(Restore is deliberately **not** available from chat — it is operator-only,
by design, to prevent a conversational mistake from reverting real data. See
`backup.py`/`maintenance.py` and the CLI section.)

### Sharing a fact into shared memory (`share`)

- "Share the fact about our on-call rotation with the shared/team memory."
- "Make fact #30 visible to shared memory, if that's allowed for this
  category."

Only works if shared memory is enabled and the fact's category is
allowlisted — Hermes will tell you if it isn't.

### Giving feedback on a recalled fact (`fact_feedback`)

- "That was exactly right, good recall." *(→ `helpful`)*
- "That's actually outdated now, don't trust it as much." *(→ `unhelpful`)*
- "Thanks, that fact about my config was spot on." *(→ `helpful`)*

Feedback nudges the fact's trust score and, if retrieval telemetry is enabled
(off by default — see below), links back to the specific retrieval event it
came from.

### Things that are *not* available in chat, by design

- "Restore memory from that backup." — operator-only (`hermes memory ...`
  maintenance commands), never a model tool action.
- "Run raw SQL against the memory database." — never exposed anywhere, chat
  or CLI.
- "Rebuild all the memory banks / backfill relationships / show me the raw
  database file." — operator CLI only (see below), not a chat action.
- "Turn off namespace isolation so you can see every chat's memory." — not
  possible; there is no action or flag for this.

---

## CLI / operator commands

These are real shell commands (`hermes memory <subcommand>`), run by whoever
operates the Hermes installation — not something end users type in chat.
Every one of them resolves to the active profile's database via the global
`-p`/`--profile` flag, which can go anywhere in the command line:
`hermes -p work memory health` and `hermes memory health -p work` are
identical. Omit it to target the default profile's `HERMES_HOME`.

### Setup / status

```bash
hermes memory setup                 # interactive provider setup wizard
hermes memory status                 # show which memory provider is active
hermes memory off                    # disable the configured memory provider
```

### Migrating legacy MEMORY.md/USER.md into Holographic

```bash
hermes memory import --from builtin --provider holographic            # preview only, no writes
hermes memory import --from builtin --provider holographic --apply    # actually import
```

Preview and apply both run inside one transaction with deferred bank
rebuilds — a failure partway through an `--apply` run leaves nothing
partially imported.

### Health / diagnostics (read-only)

```bash
hermes memory health                              # human-readable report
hermes memory health --json                       # machine-readable, no decorative text
hermes memory health --backup-path ./backup.db    # diagnose that backup DB itself (and verify its checksum/integrity)
hermes memory verify                               # alias for health — same output
```

Exit codes: `0` clean, `1` warnings, `2` errors, `3` cannot run. Never prints
raw fact content or raw chat/namespace identities — only namespace digests
and counts.

### Benchmark retrieval quality (read-only, no side effects)

```bash
hermes memory benchmark --gold-set ./gold.json
hermes memory benchmark --gold-set ./gold.json --json
```

Reports Recall@1/@5, mean reciprocal rank, per-strategy (FTS/Jaccard/HRR)
contribution, namespace-leakage count (must be zero), and P50/P95 latency.
Never mutates retrieval counters, trust scores, or feedback.

### Maintenance (writes, behind explicit flags + backup)

```bash
hermes memory rebuild-vectors                 # dry-run by default — reports planned counts
hermes memory rebuild-vectors --apply         # actually recompute HRR vectors
hermes memory rebuild-vectors --apply --json

hermes memory rebuild-banks                   # dry-run by default
hermes memory rebuild-banks --apply           # reconcile: rebuild every active namespace/category bank pair and delete orphan bank rows

hermes memory backfill-relationships --dry-run    # report planned candidate relationships
hermes memory backfill-relationships --apply      # write co-occurrence relationships
hermes memory backfill-relationships --apply --json
```

Every `--apply` path acquires a maintenance lock, then creates and verifies a
backup before writing anything — if backup verification fails, nothing is
written and the command exits non-zero. All three are idempotent: running
`--apply` twice in a row converges to the same end state, not duplicate work.

### Local diagnostic viewer (read-only, loopback-only)

```bash
hermes memory viewer                                      # http://127.0.0.1:8787/
hermes memory viewer --port 9000 --host localhost
hermes memory viewer --show-raw-identity                  # off by default; shows raw namespace strings next to their digests
```

Disabled unless explicitly started this way. Binds to loopback only —
`--host` accepts `127.0.0.1`/`localhost`/`::1` and refuses anything else
before opening a socket (no auth/CSRF exists yet, so non-loopback binding is
unsupported outright, not just discouraged). Every route is GET-only; no
restore, SQL, or mutation endpoint exists anywhere in it. Runs until
Ctrl+C.

---

## Notes

- None of the maintenance/viewer/benchmark/health commands are reachable
  from a chat/gateway slash command or a model tool call — they exist only
  as `hermes memory ...` shell commands, on purpose.
- Retrieval telemetry (`retrieval_telemetry` config key) is off by default
  and has no CLI surface at all in this release — it's pure background
  instrumentation for a future, separate weight-learning design, not
  something to prompt for or operate today.
- See `plugins/memory/holographic/README.md` for the full configuration
  reference, rollback instructions, and NumPy-degradation behavior behind
  every feature mentioned above.
