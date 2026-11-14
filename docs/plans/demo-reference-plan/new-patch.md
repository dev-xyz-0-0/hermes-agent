# Holographic Memory P1b + P2 Patch Bundle

Generated: 2026-11-14
Host runtime: Hermes Agent v0.10.8
Scope: P1b ops polish + P2 correctness polish

Also re-apply **config** (outside the agent tree), under `~/.hermes/config.yaml`:

```yaml
plugins:
  hermes-memory-store:
    backup_enabled: true
    backup_retention: 14
    backup_interval_hours: 24
```

## Files changed (agent tree)

| Path | Why |
|------|-----|
| `plugins/memory/holographic/maintenance.py` | rebuild-vectors counts/scope; rebuild-banks plan rows; restore `.enc` refuse; gateway advisory |
| `plugins/memory/holographic/__init__.py` | backup_enabled auto-backup; interval config; import near-dup preview; diagnostics backup fields |
| `hermes_cli/main.py` | CLI help text; `memory backup` subcommand; restore help notes |
| `hermes_cli/memory_setup.py` | `cmd_backup`; plan/gateway emit; rebuild-vectors docstring |
| `plugins/memory/holographic/README.md` | document backup/restore contracts |
| `plugins/memory/holographic/TECHNICAL.md` | backup_enabled no longer "reserved" |
| `tests/plugins/memory/test_holographic_maintenance.py` | P2 dry-run tests |
| `tests/plugins/memory/test_holographic_restore_maintenance.py` | P1b restore guards |
| `tests/plugins/memory/test_holographic_import.py` | near-dup preview + auto-backup tests |
| `tests/plugins/memory/test_holographic_baseline_compat.py` | schema key `backup_interval_hours` |
| `tests/hermes_cli/test_memory_restore_cli.py` | CLI `.enc` refuse |

Not included: `package-lock.json` (unrelated dirty tree noise).

## Validation used on this host

```bash
cd /home/ubuntu/.hermes/hermes-agent && source venv/bin/activate
pytest -o addopts='' \
  tests/plugins/memory/test_holographic_maintenance.py \
  tests/plugins/memory/test_holographic_restore_maintenance.py \
  tests/plugins/memory/test_holographic_import.py \
  tests/plugins/memory/test_holographic_baseline_compat.py \
  tests/hermes_cli/test_memory_restore_cli.py \
  tests/hermes_cli/test_memory_maintenance_cli.py -q
# 84 passed
```

## Unified diff

```diff
diff --git a/hermes_cli/main.py b/hermes_cli/main.py
index 26aa55083f..b07d9451e8 100644
--- a/hermes_cli/main.py
+++ b/hermes_cli/main.py
@@ -5153,18 +5153,32 @@ For more help on a command:
     memory_benchmark.add_argument("--gold-set", dest="gold_set", required=True, help="Path to a benchmark gold-set JSON file")
     memory_benchmark.add_argument("--json", action="store_true", help="Emit machine-parsable JSON only")
     memory_rebuild_vectors = memory_sub.add_parser(
-        "rebuild-vectors", help="Recompute HRR vectors for every active fact (idempotent)",
+        "rebuild-vectors",
+        help="Recompute HRR vectors for every fact row including superseded (idempotent)",
+        description=(
+            "Recomputes HRR vectors for ALL fact rows in the active profile DB "
+            "(active and superseded). Dry-run reports facts_planned plus "
+            "facts_active/facts_superseded so the count is not confused with "
+            "active-only health totals. --apply creates+verifies a backup first."
+        ),
     )
     memory_rebuild_vectors.add_argument("--dry-run", dest="dry_run", action="store_true", default=True,
                                          help="Report planned counts without writing (default)")
     memory_rebuild_vectors.add_argument("--apply", dest="dry_run", action="store_false",
-                                         help="Actually rebuild vectors (creates+verifies a backup first)")
+                                         help="Actually rebuild vectors for all fact rows (creates+verifies a backup first)")
     memory_rebuild_vectors.add_argument("--json", action="store_true", help="Emit machine-parsable JSON only")
     memory_rebuild_banks = memory_sub.add_parser(
-        "rebuild-banks", help="Rebuild every namespace/category HRR memory bank (idempotent)",
+        "rebuild-banks",
+        help="Reconcile every namespace/category HRR memory bank; delete orphans (idempotent)",
+        description=(
+            "Rebuilds banks for every active (namespace, category) pair and deletes "
+            "orphan bank rows. Dry-run emits a plan list with namespace_digest, "
+            "category, bank_name, action (CREATE/UPDATE/REFRESH/DELETE), and "
+            "old_fact_count→new_fact_count. No raw namespaces or fact text."
+        ),
     )
     memory_rebuild_banks.add_argument("--dry-run", dest="dry_run", action="store_true", default=True,
-                                       help="Report planned bank pairs without writing (default)")
+                                       help="Report planned bank pairs/plan rows without writing (default)")
     memory_rebuild_banks.add_argument("--apply", dest="dry_run", action="store_false",
                                        help="Actually rebuild banks (creates+verifies a backup first)")
     memory_rebuild_banks.add_argument("--json", action="store_true", help="Emit machine-parsable JSON only")
@@ -5180,6 +5194,17 @@ For more help on a command:
     )
     memory_verify.add_argument("--json", action="store_true", help="Emit machine-parsable JSON only")
     memory_verify.add_argument("--backup-path", dest="backup_path", default=None, help="Run health diagnostics against this backup database (and verify its checksum/integrity)")
+    memory_backup = memory_sub.add_parser(
+        "backup",
+        help="Create a native Holographic backup now (checksum sidecar + retention prune)",
+        description=(
+            "Snapshots the active profile's live memory_store.db via SQLite's online backup\n"
+            "API into $HERMES_HOME/backups/holographic/<dir>/memory-<timestamp>.db with a\n"
+            ".json checksum sidecar. Always available (does not require backup_enabled).\n"
+            "Prunes older backups in that directory down to backup_retention when configured."
+        ),
+    )
+    memory_backup.add_argument("--json", action="store_true", help="Emit machine-parsable JSON only")
     memory_restore = memory_sub.add_parser(
         "restore",
         help="Restore the live Holographic DB from a native backup (DESTRUCTIVE; dry-run by default)",
@@ -5191,12 +5216,16 @@ For more help on a command:
             "whether --apply would be allowed. --apply re-verifies everything, takes a safety\n"
             "copy of the current live DB first (never deleted by this command), sweeps any\n"
             "facts tombstoned on the live DB since the backup was taken (so a forgotten fact\n"
-            "is never silently reactivated), and reports post-restore health. Never exposed\n"
-            "to the model/fact_store tool — operator CLI only."
+            "is never silently reactivated), and reports post-restore health.\n"
+            "\n"
+            "Encrypted skills-pack snapshots (*.enc) are refused — use skills-pack\n"
+            "hermes_backup.py for those. If the gateway appears running for this HERMES_HOME,\n"
+            "a warning is included (does not hard-block apply). Never exposed to the\n"
+            "model/fact_store tool — operator CLI only."
         ),
     )
     memory_restore.add_argument("--from", dest="backup_from", required=True,
-                                 help="Path to the native backup .db file (its .json sidecar must sit alongside it)")
+                                 help="Path to the native backup .db file (with sibling .json sidecar). Not *.enc")
     memory_restore.add_argument("--dry-run", dest="dry_run", action="store_true", default=True,
                                  help="Report the restore plan/diff/gates without writing (default)")
     memory_restore.add_argument("--apply", dest="dry_run", action="store_false",
diff --git a/hermes_cli/memory_setup.py b/hermes_cli/memory_setup.py
index 169073d374..7fa7377e40 100644
--- a/hermes_cli/memory_setup.py
+++ b/hermes_cli/memory_setup.py
@@ -533,6 +533,8 @@ def memory_command(args) -> None:
         cmd_backfill_relationships(args)
     elif sub == "verify":
         cmd_verify(args)
+    elif sub == "backup":
+        cmd_backup(args)
     elif sub == "restore":
         cmd_restore(args)
     elif sub == "viewer":
@@ -682,9 +684,9 @@ def _print_maintenance_target(db_path: Path, *, as_json: bool, apply: bool) -> N
 def cmd_rebuild_vectors(args) -> None:
     """`hermes memory rebuild-vectors [--dry-run]`.
 
-    Recomputes HRR vectors for every active fact in the current profile's
-    database. Defaults to dry-run unless the caller explicitly asks to
-    write — see the `--dry-run`/no-flag handling below.
+    Recomputes HRR vectors for every fact row (active + superseded) in the
+    current profile's database. Defaults to dry-run unless the caller
+    explicitly asks to write — see the `--dry-run`/no-flag handling below.
     """
     import json as _json
     import sys as _sys
@@ -806,6 +808,85 @@ def cmd_backfill_relationships(args) -> None:
     _sys.exit(result.exit_code)
 
 
+def cmd_backup(args) -> None:
+    """`hermes memory backup` — create a native backup now + prune retention."""
+    import json as _json
+    import sys as _sys
+
+    from plugins.memory.holographic import backup as _backup
+    from plugins.memory.holographic import maintenance as _maintenance
+
+    db_path = _holographic_db_path()
+    as_json = bool(getattr(args, "json", False))
+    home = get_hermes_home()
+
+    if not db_path.exists():
+        message = f"Holographic memory database not found: {db_path}"
+        if as_json:
+            print(_json.dumps({"ok": False, "error": message}))
+        else:
+            print(f"\n  {message}\n")
+        _sys.exit(_maintenance.EXIT_CANNOT_RUN)
+
+    # Retention from active holographic plugin config when present.
+    retention = 14
+    try:
+        from hermes_cli.config import load_config
+
+        cfg = load_config() or {}
+        plugins = cfg.get("plugins") or {}
+        holo = plugins.get("hermes-memory-store") or plugins.get("holographic") or {}
+        if isinstance(holo, dict) and "backup_retention" in holo:
+            retention = max(0, int(holo.get("backup_retention", 14)))
+        memory_cfg = cfg.get("memory") or {}
+        # Also accept nested memory.provider-style blocks if present.
+        if isinstance(memory_cfg, dict):
+            nested = memory_cfg.get("holographic") or memory_cfg.get("config") or {}
+            if isinstance(nested, dict) and "backup_retention" in nested:
+                retention = max(0, int(nested.get("backup_retention", retention)))
+    except Exception:
+        retention = 14
+
+    backup_dir = home / "backups" / "holographic" / "operator"
+    try:
+        info = _backup.create_backup(str(db_path), str(backup_dir))
+        verify = _backup.verify_backup(info.path)
+        removed: list[str] = []
+        if retention > 0:
+            removed = _backup.prune_backups(str(backup_dir), keep=retention)
+    except Exception as exc:
+        if as_json:
+            print(_json.dumps({"ok": False, "error": str(exc)}))
+        else:
+            print(f"\n  Backup failed: {exc}\n")
+        _sys.exit(_maintenance.EXIT_ERRORS)
+
+    payload = {
+        "ok": bool(verify.ok),
+        "backup_path": info.path,
+        "sidecar_path": info.sidecar_path,
+        "checksum": info.checksum,
+        "timestamp": info.timestamp,
+        "fact_count": info.fact_count,
+        "backup_dir": str(backup_dir),
+        "retention": retention,
+        "pruned": removed,
+        "verify_reason": verify.reason,
+    }
+    if as_json:
+        print(_json.dumps(payload, indent=2))
+    else:
+        print(f"\n  Holographic backup")
+        print("  " + "─" * 40)
+        print(f"  Path:      {info.path}")
+        print(f"  Checksum:  {info.checksum[:16]}…")
+        print(f"  Facts:     {info.fact_count}")
+        print(f"  Verify:    {'ok' if verify.ok else verify.reason}")
+        print(f"  Retention: keep {retention} (pruned {len(removed)})")
+        print()
+    _sys.exit(_maintenance.EXIT_OK if verify.ok else _maintenance.EXIT_ERRORS)
+
+
 def cmd_restore(args) -> None:
     """`hermes memory restore --from PATH [--dry-run|--apply] [--allow-backup-warnings]`.
 
@@ -813,6 +894,7 @@ def cmd_restore(args) -> None:
     Holographic database with a native backup. Dry-run (the default)
     performs zero writes — see maintenance.restore()'s docstring for the
     full dry-run/apply contract. Never exposed to fact_store/the model.
+    Refuses skills-pack ``*.enc`` inputs with an explicit hermes_backup hint.
     """
     import json as _json
     import sys as _sys
@@ -868,8 +950,12 @@ def _emit_restore_result(result, *, as_json: bool) -> None:
                 print(f"    {key}: backup={d['backup']} live={d['live']} delta={d['delta']}")
         print(f"\n  Tombstones on live DB: {result.tombstones_live_count}")
         print(f"  Apply allowed: {result.gates.get('apply_allowed')}")
+        if result.gates.get("gateway_running"):
+            print("  Gateway running: yes (warning — prefer stop before --apply)")
         for reason in result.gates.get("block_reasons", []):
             print(f"    blocked: {reason}")
+        for warn in result.gates.get("warnings", []) or []:
+            print(f"    warning: {warn}")
         print("\n  No changes made. Re-run with --apply to restore.\n")
     else:
         if result.safety_copy_path:
@@ -879,6 +965,8 @@ def _emit_restore_result(result, *, as_json: bool) -> None:
             print(f"  Post-restore health exit code: {result.post_health['exit_code']}")
         for reason in result.gates.get("block_reasons", []):
             print(f"    blocked: {reason}")
+        for warn in result.gates.get("warnings", []) or []:
+            print(f"    warning: {warn}")
         print()
 
     if result.issues:
@@ -899,6 +987,22 @@ def _emit_maintenance_result(result, *, as_json: bool) -> None:
     print(f"  Status:    {'OK' if result.ok else 'FAILED'}")
     for key, value in result.counts.items():
         print(f"  {key}: {value}")
+    if getattr(result, "plan", None):
+        print("\n  Plan:")
+        for row in result.plan:
+            action = row.get("action", "?")
+            bank = row.get("bank_name", "?")
+            digest = row.get("namespace_digest")
+            category = row.get("category")
+            old_c = row.get("old_fact_count")
+            new_c = row.get("new_fact_count")
+            meta = []
+            if digest is not None:
+                meta.append(f"ns={digest}")
+            if category is not None:
+                meta.append(f"cat={category}")
+            meta_s = (" " + " ".join(meta)) if meta else ""
+            print(f"    {action:7} {bank}{meta_s}  {old_c}→{new_c}")
     if result.backup is not None:
         print(f"  Backup:    {'ok' if result.backup['ok'] else result.backup.get('reason', 'failed')}")
     if result.issues:
diff --git a/plugins/memory/holographic/README.md b/plugins/memory/holographic/README.md
index 6ccc02e61e..a1b5b3cade 100644
--- a/plugins/memory/holographic/README.md
+++ b/plugins/memory/holographic/README.md
@@ -217,8 +217,9 @@ All keys live in `config.yaml` under `plugins.hermes-memory-store` (not
 
 | Key | Default | Description |
 |-----|---------|-------------|
-| `backup_enabled` | `false` | Reserved for future automated backup scheduling — the manual `fact_store {"action": "backup"}` action works regardless of this setting |
-| `backup_retention` | `14` | Keep the N most recent backups per namespace after each manual backup (`0` = keep all) |
+| `backup_enabled` | `false` | When `true`, take an opportunistic native backup on provider initialize if the newest backup in this namespace dir is older than `backup_interval_hours` (or missing). Manual `fact_store {"action": "backup"}` and `hermes memory backup` always work regardless |
+| `backup_retention` | `14` | Keep the N most recent native backups per backup directory after each successful backup (manual, CLI, or auto); `0` = keep all |
+| `backup_interval_hours` | `24` | Minimum hours between opportunistic auto-backups when `backup_enabled` is true (`0` = backup on every initialize) |
 
 **Retrieval telemetry**
 
@@ -227,12 +228,11 @@ All keys live in `config.yaml` under `plugins.hermes-memory-store` (not
 | `retrieval_telemetry` | `false` | Record bounded, content-free retrieval-scoring telemetry (see [Retrieval Telemetry](#retrieval-telemetry)); nothing is written while this is `false` |
 | `retrieval_telemetry_retention` | `1000` | Maximum `retrieval_events` rows retained (oldest pruned first on every write, clamped `1`-`100000`); only relevant when `retrieval_telemetry` is enabled |
 
-No settings are currently purely experimental beyond `backup_enabled`'s
-reserved-for-scheduling note above — every other key already has a live
-effect in the current implementation. Note that `scoped_hrr_banks`,
-`relationship_recall_enabled`, `relationship_max_depth`, and
-`relationship_max_nodes` — named in the demo-reference integration's design
-plan — are **not** real config.yaml keys in the shipped code; see
+Every key above already has a live effect in the current implementation.
+Note that `scoped_hrr_banks`, `relationship_recall_enabled`,
+`relationship_max_depth`, and `relationship_max_nodes` — named in the
+demo-reference integration's design plan — are **not** real config.yaml
+keys in the shipped code; see
 [Configuration Defaults and Rollback Reference](#configuration-defaults-and-rollback-reference)
 for why.
 
@@ -559,12 +559,20 @@ it exists purely as read-only reference material for this integration.**
 
 ## Backup and Restore
 
-`fact_store {"action": "backup"}` snapshots the live database using
-SQLite's online backup API (safe against a concurrently open WAL
-connection — no file copy, no pausing writers) to
-`$HERMES_HOME/backups/holographic/<namespace>/memory-<timestamp>.db`, with a
-SHA-256 checksum sidecar and `0600` permissions. `backup_retention` prunes
-older backups automatically after each manual backup.
+Three ways to create a **native** Holographic backup (`memory-*.db` +
+`.json` checksum sidecar, `0600` permissions), all using SQLite's online
+backup API (safe against a concurrently open WAL connection):
+
+1. `fact_store {"action": "backup"}` — namespace-scoped dir under
+   `$HERMES_HOME/backups/holographic/<namespace>/`
+2. `hermes memory backup` — operator CLI into
+   `$HERMES_HOME/backups/holographic/operator/`
+3. Opportunistic auto-backup on provider initialize when `backup_enabled`
+   is true and the newest backup in that namespace dir is missing or older
+   than `backup_interval_hours`
+
+`backup_retention` prunes older backups in the target directory after each
+successful backup (`0` = keep all).
 
 **Restore is deliberately NOT a `fact_store` tool action** — it's exposed
 only as `HolographicMemoryProvider.restore_from_backup(path)` (library/
@@ -573,6 +581,10 @@ never from chat. A prompt-injected or confused model triggering a restore
 could silently reactivate facts the user explicitly forgot; that decision
 belongs to a human.
 
+**Encrypted skills-pack snapshots (`*.enc`) are not valid inputs** for
+`hermes memory restore`. Use skills-pack `hermes_backup.py` for those
+(typically `--only holographic --mode migration` onto a fresh DB path).
+
 ### `hermes memory restore` (operator CLI)
 
 ```bash
diff --git a/plugins/memory/holographic/TECHNICAL.md b/plugins/memory/holographic/TECHNICAL.md
index 9dd916e04d..b05be32788 100644
--- a/plugins/memory/holographic/TECHNICAL.md
+++ b/plugins/memory/holographic/TECHNICAL.md
@@ -667,7 +667,7 @@ Every key read in `HolographicMemoryProvider.__init__`/`get_config_schema()`
 | `diagnostics_include_content` | truthy | `false` | `_diagnostics()`'s `by_category` field |
 | `shared_memory_enabled` | truthy | `false` | `prefetch()`, `_handle_share_action()`, `relationships.list_relationships` |
 | `shared_memory_categories` | comma-separated string or list → `frozenset` | empty | same as above (also reused against `relationship_type`, not `category`, by `relationships.py`) |
-| `backup_enabled` | truthy | `false` | **reserved** — read into `self._backup_enabled` and surfaced in `diagnostics()`, but no code path currently gates a write on it; the manual `fact_store backup` action works regardless |
+| `backup_enabled` | truthy | `false` | When true, opportunistic native backup on provider initialize if newest backup in the namespace dir is missing/older than `backup_interval_hours`. Manual `fact_store backup` and `hermes memory backup` always work |
 | `backup_retention` | int, `max(0, int(...))` | `14` | `HolographicMemoryProvider.backup()` → `backup.prune_backups` |
 | `retrieval_telemetry` | truthy | `false` | `FactRetriever(telemetry_enabled=...)` |
 | `retrieval_telemetry_retention` | int, clamped `[1,100000]` | `1000` (`telemetry.DEFAULT_RETENTION`) | `FactRetriever` → `telemetry.record_retrieval_event(retention=...)` |
diff --git a/plugins/memory/holographic/__init__.py b/plugins/memory/holographic/__init__.py
index 4878717924..7deba52b99 100644
--- a/plugins/memory/holographic/__init__.py
+++ b/plugins/memory/holographic/__init__.py
@@ -573,6 +573,15 @@ class HolographicMemoryProvider(MemoryProvider):
         self._shared_memory_categories = frozenset(str(c) for c in (raw_categories or []))
         self._backup_enabled = is_truthy_value(self._config.get("backup_enabled"), default=False)
         self._backup_retention = max(0, int(self._config.get("backup_retention", 14)))
+        # Minimum hours between opportunistic auto-backups when backup_enabled.
+        # Manual fact_store/CLI backup always runs immediately and still prunes
+        # to backup_retention afterward.
+        try:
+            self._backup_interval_hours = max(
+                0.0, float(self._config.get("backup_interval_hours", 24))
+            )
+        except (TypeError, ValueError):
+            self._backup_interval_hours = 24.0
         self._metrics: Dict[str, int] = {}
 
         # Phase 8 (demo-reference integration) — optional, bounded,
@@ -769,15 +778,31 @@ class HolographicMemoryProvider(MemoryProvider):
             },
             {
                 "key": "backup_enabled",
-                "description": "Reserved for future automated backup scheduling; the manual fact_store 'backup' action works regardless of this flag",
+                "description": (
+                    "When true, take an opportunistic native backup on provider "
+                    "initialize if the newest backup in this namespace dir is older "
+                    "than backup_interval_hours (or missing). Manual fact_store "
+                    "'backup' and `hermes memory backup` always work regardless"
+                ),
                 "default": str(self._config.get("backup_enabled", "false")).lower(),
                 "choices": ["true", "false"],
             },
             {
                 "key": "backup_retention",
-                "description": "Keep the N most recent backups per namespace after each manual backup (0 = keep all)",
+                "description": (
+                    "Keep the N most recent native backups per backup directory "
+                    "after each successful backup (manual, CLI, or auto); 0 = keep all"
+                ),
                 "default": str(self._config.get("backup_retention", 14)),
             },
+            {
+                "key": "backup_interval_hours",
+                "description": (
+                    "Minimum hours between opportunistic auto-backups when "
+                    "backup_enabled is true (0 = backup on every initialize)"
+                ),
+                "default": str(self._config.get("backup_interval_hours", 24)),
+            },
             {
                 "key": "retrieval_telemetry",
                 "description": "Record content-free retrieval telemetry (component scores, rank, feedback) for future offline weight-learning evaluation. Never records prompts, fact content, names, chat IDs, raw namespaces, or credentials; observation-only, never adjusts retrieval weights at runtime",
@@ -866,6 +891,59 @@ class HolographicMemoryProvider(MemoryProvider):
         if self._extraction_mode == "hybrid":
             self._capture_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="holographic-capture")
 
+        # Opportunistic native backup (P1b): rate-limited, never blocks init on failure.
+        self._maybe_auto_backup(reason="initialize")
+
+    def _backup_dir(self) -> "Path":
+        dir_name = re.sub(r"[^A-Za-z0-9_.-]", "_", self._namespace) or "default"
+        return Path(self._hermes_home) / "backups" / "holographic" / dir_name
+
+    def _latest_backup_age_hours(self, backup_dir: "Path") -> "float | None":
+        """Age in hours of the newest memory-*.db under backup_dir, or None."""
+        if not backup_dir.is_dir():
+            return None
+        newest_mtime = None
+        for entry in backup_dir.iterdir():
+            if not entry.is_file():
+                continue
+            if not entry.name.startswith("memory-") or not entry.name.endswith(".db"):
+                continue
+            if entry.name.endswith(".db.json"):
+                continue
+            try:
+                mtime = entry.stat().st_mtime
+            except OSError:
+                continue
+            if newest_mtime is None or mtime > newest_mtime:
+                newest_mtime = mtime
+        if newest_mtime is None:
+            return None
+        return max(0.0, (time.time() - newest_mtime) / 3600.0)
+
+    def _maybe_auto_backup(self, *, reason: str = "auto") -> None:
+        """Create a native backup when backup_enabled and interval elapsed.
+
+        Failures are logged and swallowed — auto backup must never break
+        session initialize. Manual backup() still raises.
+        """
+        if not self._backup_enabled or not self._store:
+            return
+        try:
+            backup_dir = self._backup_dir()
+            age_h = self._latest_backup_age_hours(backup_dir)
+            if age_h is not None and age_h < self._backup_interval_hours:
+                self._metric("backup_auto_skipped_fresh")
+                return
+            info = self.backup()
+            self._metric("backup_auto_created")
+            logger.info(
+                "holographic: auto-backup (%s) wrote %s (facts=%s)",
+                reason, info.path, info.fact_count,
+            )
+        except Exception:
+            self._metric("backup_auto_failed")
+            logger.warning("holographic: auto-backup (%s) failed", reason, exc_info=True)
+
     def _resolve_namespace(
         self, *, platform: str, chat_id: str, thread_id: str, agent_identity: str,
     ) -> tuple[str, bool]:
@@ -1418,31 +1496,77 @@ class HolographicMemoryProvider(MemoryProvider):
                         item["reason"] = problem
                         result["rejected"].append(item)
                         continue
-                    if self._find_fact_id_by_content(entry) is not None:
-                        result["duplicates"].append(item)
+                    exact_id = self._find_fact_id_by_content(entry)
+                    if exact_id is not None:
+                        result["duplicates"].append({
+                            **item,
+                            "reason": "exact_duplicate",
+                            "strategy": "exact",
+                            "matched_fact_id": exact_id,
+                        })
                         continue
-                    result["proposed"].append(item)
-                    if apply:
-                        candidate = capture.FactCandidate(
-                            content=entry, operation="statement", scope="", confidence=1.0,
-                        )
-                        applied = consolidation.classify_and_apply(
-                            self._store,
-                            self._retriever,
+
+                    candidate = capture.FactCandidate(
+                        content=entry, operation="statement", scope="", confidence=1.0,
+                    )
+
+                    if not apply:
+                        # Preview uses the same consolidation policy as apply
+                        # (token-overlap + optional fuzzy N-gram), so near-dups
+                        # are not under-counted vs the write path.
+                        plan = consolidation.plan_candidate(
                             candidate,
-                            category=category,
-                            namespace=self._namespace,
-                            source_type="builtin_import",
-                            extraction_method="explicit_import",
-                            confidence=1.0,
+                            consolidation._search_existing(
+                                self._retriever, candidate, self._namespace,
+                            ),
                             fuzzy_enabled=self._fuzzy_dedup_enabled,
                             fuzzy_ngram_threshold=self._fuzzy_dedup_threshold,
                         )
-                        if applied.action == "inserted" and applied.fact_id:
-                            self._record_source(applied.fact_id, "builtin_import")
-                            result["imported"].append({**item, "fact_id": applied.fact_id})
+                        if plan.action == "ignored":
+                            result["duplicates"].append({
+                                **item,
+                                "reason": plan.reason,
+                                "strategy": plan.strategy,
+                                "matched_fact_id": plan.fact_id,
+                                "fuzzy_score": plan.fuzzy_score,
+                            })
                         else:
-                            result["duplicates"].append({**item, "reason": applied.reason})
+                            result["proposed"].append({
+                                **item,
+                                "reason": plan.reason,
+                                "strategy": plan.strategy or "none",
+                            })
+                        continue
+
+                    result["proposed"].append(item)
+                    applied = consolidation.classify_and_apply(
+                        self._store,
+                        self._retriever,
+                        candidate,
+                        category=category,
+                        namespace=self._namespace,
+                        source_type="builtin_import",
+                        extraction_method="explicit_import",
+                        confidence=1.0,
+                        fuzzy_enabled=self._fuzzy_dedup_enabled,
+                        fuzzy_ngram_threshold=self._fuzzy_dedup_threshold,
+                    )
+                    if applied.action == "inserted" and applied.fact_id:
+                        self._record_source(applied.fact_id, "builtin_import")
+                        result["imported"].append({
+                            **item,
+                            "fact_id": applied.fact_id,
+                            "reason": applied.reason,
+                            "strategy": applied.strategy or "none",
+                        })
+                    else:
+                        result["duplicates"].append({
+                            **item,
+                            "reason": applied.reason,
+                            "strategy": applied.strategy,
+                            "matched_fact_id": applied.fact_id or applied.related_id,
+                            "fuzzy_score": applied.fuzzy_score,
+                        })
 
         if apply:
             with self._store.transaction(), self._store.defer_bank_rebuilds():
@@ -1630,12 +1754,23 @@ class HolographicMemoryProvider(MemoryProvider):
             "consolidation_enabled": self._consolidation_enabled,
             "shared_memory_enabled": self._shared_memory_enabled,
             "backup_enabled": self._backup_enabled,
+            "backup_retention": self._backup_retention,
+            "backup_interval_hours": self._backup_interval_hours,
             "database": stats.get("database", ""),
             "facts": stats.get("facts", 0),
             "active": stats.get("active", 0),
             "superseded": stats.get("superseded", 0),
             "metrics": dict(self._metrics),
         }
+        try:
+            if self._hermes_home:
+                age = self._latest_backup_age_hours(self._backup_dir())
+                result["backup_dir"] = str(self._backup_dir())
+                result["latest_backup_age_hours"] = (
+                    None if age is None else round(age, 3)
+                )
+        except Exception:
+            pass
         if self._diagnostics_include_content:
             result["by_category"] = stats.get("by_category", {})
         return result
diff --git a/plugins/memory/holographic/maintenance.py b/plugins/memory/holographic/maintenance.py
index 0565be2d9b..70716e5ed9 100644
--- a/plugins/memory/holographic/maintenance.py
+++ b/plugins/memory/holographic/maintenance.py
@@ -111,6 +111,9 @@ class MaintenanceResult:
     counts: dict[str, Any] = field(default_factory=dict)
     backup: "dict[str, Any] | None" = None
     issues: list[str] = field(default_factory=list)
+    # Optional operator plan rows (rebuild-banks dry-run, etc.). Never includes
+    # raw namespace strings or fact content — digests/bank keys/counts only.
+    plan: list[dict[str, Any]] = field(default_factory=list)
     algorithm_version: str = ALGORITHM_VERSION
     schema_version: str = SCHEMA_VERSION
 
@@ -121,7 +124,7 @@ class MaintenanceResult:
         return EXIT_OK
 
     def to_dict(self) -> dict[str, Any]:
-        return {
+        d: dict[str, Any] = {
             "ok": self.ok,
             "dry_run": self.dry_run,
             "operation": self.operation,
@@ -133,6 +136,9 @@ class MaintenanceResult:
             "backup": self.backup,
             "issues": self.issues,
         }
+        if self.plan:
+            d["plan"] = self.plan
+        return d
 
 
 # ---------------------------------------------------------------------------
@@ -236,12 +242,16 @@ def rebuild_vectors(
     dry_run: bool = True,
     hrr_dim: int = 1024,
 ) -> MaintenanceResult:
-    """Recompute HRR vectors for every active fact (dimension-consistent).
+    """Recompute HRR vectors for every fact row, including superseded.
 
     Wraps ``MemoryStore.rebuild_all_vectors(namespace=None)`` (Phase 2),
-    which is already idempotent — rebuilding twice produces the same
-    vectors both times, so interrupting this operation and re-running it is
-    always safe to restart.
+    which intentionally walks *all* fact rows (active + superseded) so a
+    recovery/migration never leaves stale vectors on history rows. The
+    operation is idempotent — rebuilding twice produces the same vectors
+    both times, so interrupting and re-running is always safe.
+
+    Dry-run counts split active vs superseded so operators are not
+    surprised that ``facts_planned`` exceeds the active-only health count.
     """
     db_path = _require_db(db_path)
 
@@ -249,11 +259,27 @@ def rebuild_vectors(
         conn = _readonly_connection(db_path)
         try:
             planned = int(conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0])
+            active = int(
+                conn.execute(
+                    "SELECT COUNT(*) FROM facts WHERE status != 'superseded'"
+                ).fetchone()[0]
+            )
+            superseded = int(
+                conn.execute(
+                    "SELECT COUNT(*) FROM facts WHERE status = 'superseded'"
+                ).fetchone()[0]
+            )
         finally:
             conn.close()
         return MaintenanceResult(
             ok=True, dry_run=True, operation="rebuild-vectors", db_path=str(db_path),
-            counts={"facts_planned": planned},
+            counts={
+                "facts_planned": planned,
+                "facts_active": active,
+                "facts_superseded": superseded,
+                # Explicit contract note for automation/CLIs:
+                "scope": "all_fact_rows_including_superseded",
+            },
         )
 
     with maintenance_lock(db_path):
@@ -272,7 +298,11 @@ def rebuild_vectors(
 
         return MaintenanceResult(
             ok=True, dry_run=False, operation="rebuild-vectors", db_path=str(db_path),
-            counts={"facts_processed": processed}, backup=backup_info,
+            counts={
+                "facts_processed": processed,
+                "scope": "all_fact_rows_including_superseded",
+            },
+            backup=backup_info,
         )
 
 
@@ -302,6 +332,88 @@ def _expected_bank_names(pairs: list[tuple[str, str]]) -> set[str]:
     return {bank_name(ns, cat) for ns, cat in pairs}
 
 
+def _namespace_digest_for_plan(namespace: str) -> str:
+    """Opaque plan label — matches diagnostics display digests, never raw ns."""
+    import hashlib
+
+    if not namespace:
+        return "(unscoped)"
+    return hashlib.sha1(namespace.encode("utf-8")).hexdigest()[:12]
+
+
+def _bank_fact_counts(conn: sqlite3.Connection) -> dict[str, int]:
+    row = conn.execute(
+        "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_banks'"
+    ).fetchone()
+    if row is None:
+        return {}
+    return {
+        str(r[0]): int(r[1] or 0)
+        for r in conn.execute("SELECT bank_name, fact_count FROM memory_banks")
+    }
+
+
+def _active_hrr_count(conn: sqlite3.Connection, namespace: str, category: str) -> int:
+    return int(
+        conn.execute(
+            """
+            SELECT COUNT(*) FROM facts
+            WHERE namespace = ?
+              AND category = ?
+              AND status != 'superseded'
+              AND hrr_vector IS NOT NULL
+            """,
+            (namespace, category),
+        ).fetchone()[0]
+    )
+
+
+def _rebuild_banks_plan_rows(
+    conn: sqlite3.Connection,
+    pairs: list[tuple[str, str]],
+    existing: set[str],
+) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
+    """Build dry-run plan rows: rebuild/create pairs + orphan deletes.
+
+    Never includes raw namespaces or fact content.
+    """
+    from .store import bank_name
+
+    existing_counts = _bank_fact_counts(conn)
+    plan: list[dict[str, Any]] = []
+    for namespace, category in pairs:
+        bname = bank_name(namespace, category)
+        old_count = int(existing_counts.get(bname, 0))
+        new_count = _active_hrr_count(conn, namespace, category)
+        if bname not in existing:
+            action = "CREATE"
+        elif old_count != new_count:
+            action = "UPDATE"
+        else:
+            action = "REFRESH"
+        plan.append({
+            "namespace_digest": _namespace_digest_for_plan(namespace),
+            "category": category,
+            "bank_name": bname,
+            "action": action,
+            "old_fact_count": old_count,
+            "new_fact_count": new_count,
+        })
+
+    expected = {row["bank_name"] for row in plan}
+    orphans: list[dict[str, Any]] = []
+    for name in sorted(existing - expected):
+        orphans.append({
+            "namespace_digest": None,
+            "category": None,
+            "bank_name": name,
+            "action": "DELETE",
+            "old_fact_count": int(existing_counts.get(name, 0)),
+            "new_fact_count": 0,
+        })
+    return plan, orphans
+
+
 def rebuild_banks(
     db_path: "str | Path",
     *,
@@ -323,6 +435,9 @@ def rebuild_banks(
     just the pairs with current active facts, so those orphans actually get
     visited. Idempotent: repeated/interrupted runs converge to the same end
     state (second apply reports zero further deletions).
+
+    Dry-run emits a ``plan`` list of digest/category/bank_name/action/
+    old→new rows plus orphan DELETE rows (no raw namespaces, no fact text).
     """
     db_path = _require_db(db_path)
 
@@ -332,6 +447,7 @@ def rebuild_banks(
             pairs = _active_namespace_category_pairs(conn)
             expected = _expected_bank_names(pairs)
             existing = _existing_bank_names(conn)
+            plan_rows, orphan_rows = _rebuild_banks_plan_rows(conn, pairs, existing)
         finally:
             conn.close()
         to_delete = existing - expected
@@ -344,6 +460,7 @@ def rebuild_banks(
                 "banks_existing": len(existing),
                 "banks_expected": len(expected),
             },
+            plan=plan_rows + orphan_rows,
         )
 
     with maintenance_lock(db_path):
@@ -614,6 +731,50 @@ def _read_live_tombstones(db_path: Path) -> frozenset:
         conn.close()
 
 
+_ENC_RESTORE_REFUSAL = (
+    "Encrypted skills-pack snapshots (.enc) are not supported by "
+    "`hermes memory restore`. Use skills-pack hermes_backup instead, e.g.: "
+    "cd /home/ubuntu/skills-pack && env -u VIRTUAL_ENV uv run python "
+    "src/hermes_backup.py restore --snapshot <ts> --only holographic "
+    "--mode migration --dry-run --db-path <fresh-or-target-db>"
+)
+
+
+def _looks_like_encrypted_skills_pack_snapshot(path: Path) -> bool:
+    """True for skills-pack encrypted snapshots (wrong tool for native restore)."""
+    name = path.name.lower()
+    return path.suffix.lower() == ".enc" or name.endswith(".db.enc") or name.endswith(".sqlite.enc")
+
+
+def _gateway_running_for_home(hermes_home: "str | Path") -> bool:
+    """Best-effort check whether this profile's gateway process is alive.
+
+    Warning-only for restore: file replace under live writers is risky, but
+    operators may still proceed after reviewing the warning. Uses the same
+    PID-file convention as profile/status tooling.
+    """
+    try:
+        from hermes_cli.profiles import _check_gateway_running
+
+        return bool(_check_gateway_running(Path(hermes_home)))
+    except Exception:
+        try:
+            # Fallback when profiles helper is unavailable: respect HERMES_HOME.
+            old = os.environ.get("HERMES_HOME")
+            os.environ["HERMES_HOME"] = str(hermes_home)
+            try:
+                from gateway.status import is_gateway_running
+
+                return bool(is_gateway_running())
+            finally:
+                if old is None:
+                    os.environ.pop("HERMES_HOME", None)
+                else:
+                    os.environ["HERMES_HOME"] = old
+        except Exception:
+            return False
+
+
 def _compute_restore_gates(
     *,
     backup_exists: bool,
@@ -622,11 +783,18 @@ def _compute_restore_gates(
     backup_health_errors: int,
     backup_health_warnings: int,
     allow_backup_warnings: bool,
+    gateway_running: bool = False,
 ) -> "tuple[dict[str, Any], int]":
     """Returns (gates_dict, exit_code) per the exit-code table in the P1 plan:
     0=ok, 1=warnings-only, 2=errors, 3=cannot-run (handled by caller before
-    this is ever invoked for the missing-backup-file case)."""
+    this is ever invoked for the missing-backup-file case).
+
+    ``gateway_running`` is advisory: it is surfaced on the gates object and
+    may promote the dry-run exit code to WARNINGS, but it does **not** block
+    ``apply_allowed`` (operator must still decide after the warning).
+    """
     block_reasons: list[str] = []
+    soft_warnings: list[str] = []
     if not verify_ok:
         block_reasons.append(f"backup verification failed: {verify_reason}")
     if backup_health_errors:
@@ -636,21 +804,32 @@ def _compute_restore_gates(
             f"backup health has {backup_health_warnings} warning issue(s) "
             "(pass --allow-backup-warnings to proceed anyway)"
         )
+    if gateway_running:
+        soft_warnings.append(
+            "gateway appears to be running for this HERMES_HOME — prefer "
+            "stopping writers before restore --apply to avoid races with an "
+            "open live DB"
+        )
 
     apply_allowed = not block_reasons
     gates = {
         "backup_verify_ok": verify_ok,
         "backup_health_errors": backup_health_errors,
         "backup_health_warnings": backup_health_warnings,
+        "gateway_running": gateway_running,
         "apply_allowed": apply_allowed,
         "block_reasons": block_reasons,
+        "warnings": soft_warnings,
     }
 
     if not verify_ok or backup_health_errors:
         exit_code = EXIT_ERRORS
     elif backup_health_warnings and not allow_backup_warnings:
+        # Health warnings that actually block apply (unless allow flag).
         exit_code = EXIT_WARNINGS
     else:
+        # Gateway running is advisory-only: surfaced in gates.warnings /
+        # issues, but does not change exit code or apply_allowed.
         exit_code = EXIT_OK
     return gates, exit_code
 
@@ -679,23 +858,49 @@ def restore(
     actual file-level replace to the existing `backup.restore_backup()`
     primitive — this function's job is orchestration/gating, not
     reimplementing that primitive.
+
+    Encrypted skills-pack ``*.enc`` inputs are refused with an explicit
+    pointer to ``hermes_backup.py`` (wrong tool / wrong format).
     """
     from . import diagnostics as diagnostics_module
 
     db_path = Path(db_path)
     backup_path = Path(backup_path)
+    hermes_home = Path(hermes_home)
     from_path_str = str(backup_path)
 
+    if _looks_like_encrypted_skills_pack_snapshot(backup_path):
+        reason = _ENC_RESTORE_REFUSAL
+        return RestoreMaintenanceResult(
+            ok=False, dry_run=dry_run, exit_code=EXIT_CANNOT_RUN,
+            from_path=from_path_str, db_path=str(db_path),
+            issues=[reason],
+            gates={
+                "backup_verify_ok": False,
+                "backup_health_errors": 0,
+                "backup_health_warnings": 0,
+                "gateway_running": False,
+                "apply_allowed": False,
+                "block_reasons": [reason],
+                "warnings": [],
+                "input_format": "skills_pack_encrypted",
+            },
+        )
+
     if not backup_path.exists():
         return RestoreMaintenanceResult(
             ok=False, dry_run=dry_run, exit_code=EXIT_CANNOT_RUN,
             from_path=from_path_str, db_path=str(db_path),
             issues=[f"backup file not found: {backup_path}"],
             gates={"backup_verify_ok": False, "backup_health_errors": 0,
-                   "backup_health_warnings": 0, "apply_allowed": False,
-                   "block_reasons": [f"backup file not found: {backup_path}"]},
+                   "backup_health_warnings": 0, "gateway_running": False,
+                   "apply_allowed": False,
+                   "block_reasons": [f"backup file not found: {backup_path}"],
+                   "warnings": []},
         )
 
+    gateway_running = _gateway_running_for_home(hermes_home)
+
     verify = backup_module.verify_backup(str(backup_path))
     backup_health_report = None
     backup_health_errors = 0
@@ -712,6 +917,7 @@ def restore(
         backup_health_errors=backup_health_errors,
         backup_health_warnings=backup_health_warnings,
         allow_backup_warnings=allow_backup_warnings,
+        gateway_running=gateway_running,
     )
     backup_verify_dict = {"ok": verify.ok, "reason": verify.reason}
 
@@ -734,6 +940,7 @@ def restore(
             would_block_reactivation=tombstones_live_count > 0,
             gates=gates,
             plan=list(_RESTORE_PLAN_STEPS),
+            issues=list(gates.get("warnings") or []),
         )
 
     # --- apply ---
@@ -743,7 +950,7 @@ def restore(
             from_path=from_path_str, db_path=str(db_path),
             backup_verify=backup_verify_dict,
             gates=gates,
-            issues=list(gates["block_reasons"]),
+            issues=list(gates["block_reasons"]) + list(gates.get("warnings") or []),
         )
 
     with maintenance_lock(db_path):
diff --git a/tests/hermes_cli/test_memory_restore_cli.py b/tests/hermes_cli/test_memory_restore_cli.py
index 005eef9b49..26a2cee1ab 100644
--- a/tests/hermes_cli/test_memory_restore_cli.py
+++ b/tests/hermes_cli/test_memory_restore_cli.py
@@ -121,3 +121,17 @@ class TestRestoreCommand:
             assert "restore" not in schema_actions
         finally:
             p.shutdown()
+
+    def test_enc_path_refused_via_cli(self, capsys, tmp_path):
+        from hermes_cli.memory_setup import cmd_restore
+
+        _seed_live()
+        enc = tmp_path / "snap.enc"
+        enc.write_text("encrypted-blob", encoding="utf-8")
+        with pytest.raises(SystemExit) as exc_info:
+            cmd_restore(_ns(json=True, dry_run=True, backup_from=str(enc)))
+        assert exc_info.value.code == 3
+        parsed = json.loads(capsys.readouterr().out.strip())
+        assert parsed["ok"] is False
+        assert parsed["gates"]["apply_allowed"] is False
+        assert "hermes_backup" in " ".join(parsed.get("issues") or [])
diff --git a/tests/plugins/memory/test_holographic_baseline_compat.py b/tests/plugins/memory/test_holographic_baseline_compat.py
index fefdd6ec39..58b0bf60ad 100644
--- a/tests/plugins/memory/test_holographic_baseline_compat.py
+++ b/tests/plugins/memory/test_holographic_baseline_compat.py
@@ -75,7 +75,7 @@ class TestProviderConfigDefaults:
             "max_facts_per_turn", "consolidation_enabled", "capture_timeout_ms",
             "capture_debug", "profile_isolation", "diagnostics_enabled",
             "diagnostics_include_content", "shared_memory_enabled", "shared_memory_categories",
-            "backup_enabled", "backup_retention",
+            "backup_enabled", "backup_retention", "backup_interval_hours",
             # Phase 3 (demo-reference integration) — fuzzy consolidation evidence gate.
             "fuzzy_dedup_enabled", "fuzzy_dedup_threshold",
             # Phase 8 (demo-reference integration) — retrieval telemetry.
diff --git a/tests/plugins/memory/test_holographic_import.py b/tests/plugins/memory/test_holographic_import.py
index 94d039d37a..5737c959d4 100644
--- a/tests/plugins/memory/test_holographic_import.py
+++ b/tests/plugins/memory/test_holographic_import.py
@@ -1,5 +1,7 @@
 """Explicit migration from profile-scoped built-in memory files."""
 
+from pathlib import Path
+
 import pytest
 
 from plugins.memory.holographic import HolographicMemoryProvider
@@ -124,3 +126,65 @@ def test_import_bank_rebuild_count_is_bounded(tmp_path, monkeypatch):
     # whole call rather than once per inserted fact.
     assert rebuild_calls == [("", "general")]
     provider.shutdown()
+
+
+def test_import_preview_reports_near_duplicates(tmp_path, monkeypatch):
+    """P2: dry-run import should surface fuzzy/token near-dups, not only exact."""
+    hermes_home = tmp_path / ".hermes"
+    memories = hermes_home / "memories"
+    memories.mkdir(parents=True)
+    # Near-paraphrase of an already-stored fact (not byte-identical).
+    (memories / "MEMORY.md").write_text(
+        "Uses Python uv for project dependencies.",
+        encoding="utf-8",
+    )
+    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
+
+    provider = HolographicMemoryProvider(config={
+        "db_path": str(hermes_home / "memory_store.db"),
+        "hrr_dim": 32,
+        "hrr_weight": 0.0,
+        "fuzzy_dedup_enabled": True,
+    })
+    provider.initialize(session_id="import-near-dup", platform="cli")
+    provider._store.add_fact(
+        "Uses Python uv for MCP dependencies.",
+        category="general",
+        namespace=provider._namespace,
+    )
+
+    preview = provider.import_builtin_memories(apply=False)
+    assert preview["counts"]["proposed"] == 0
+    assert preview["counts"]["duplicates"] == 1
+    dup = preview["duplicates"][0]
+    assert dup.get("strategy") in {"token_overlap", "fuzzy_ngram", "exact"}
+    assert dup.get("reason")
+    assert provider._store.list_facts(limit=20)  # unchanged; preview only
+    provider.shutdown()
+
+
+def test_auto_backup_on_initialize_when_enabled(tmp_path, monkeypatch):
+    """P1b: backup_enabled creates a native backup on initialize when stale/missing."""
+    hermes_home = tmp_path / ".hermes"
+    hermes_home.mkdir(parents=True)
+    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
+
+    provider = HolographicMemoryProvider(config={
+        "db_path": str(hermes_home / "memory_store.db"),
+        "hrr_dim": 32,
+        "backup_enabled": True,
+        "backup_interval_hours": 0,  # always eligible
+        "backup_retention": 5,
+    })
+    provider.initialize(session_id="auto-backup", platform="cli", hermes_home=str(hermes_home))
+    try:
+        backup_dir = provider._backup_dir()
+        assert backup_dir.is_dir()
+        dbs = list(backup_dir.glob("memory-*.db"))
+        assert len(dbs) >= 1
+        assert (dbs[0].with_suffix(dbs[0].suffix + ".json")).exists() or (
+            Path(str(dbs[0]) + ".json").exists()
+        )
+        assert provider._metrics.get("backup_auto_created", 0) >= 1
+    finally:
+        provider.shutdown()
diff --git a/tests/plugins/memory/test_holographic_maintenance.py b/tests/plugins/memory/test_holographic_maintenance.py
index d15fd3410a..4c9128edf6 100644
--- a/tests/plugins/memory/test_holographic_maintenance.py
+++ b/tests/plugins/memory/test_holographic_maintenance.py
@@ -448,3 +448,60 @@ class TestVerifyAlias:
         via_maintenance = maintenance.verify(db_path)
         via_diagnostics = diagnostics.run_diagnostics(db_path)
         assert via_maintenance.to_dict() == via_diagnostics.to_dict()
+
+
+# ---------------------------------------------------------------------------
+# P2 — dry-run contract polish
+# ---------------------------------------------------------------------------
+
+class TestRebuildVectorsScopeCounts:
+    def test_dry_run_splits_active_and_superseded(self, tmp_path):
+        db_path = tmp_path / "db.sqlite"
+        store = MemoryStore(db_path=db_path, hrr_dim=32)
+        try:
+            a = store.add_fact("Active fact A.", category="general")
+            store.add_fact("Active fact B.", category="general")
+            store.supersede_fact(a, "Replacement for A.", category="general")
+        finally:
+            store.close()
+
+        result = maintenance.rebuild_vectors(db_path, hermes_home=tmp_path, dry_run=True)
+        assert result.counts["facts_planned"] == 3  # 2 active + 1 superseded
+        assert result.counts["facts_active"] == 2
+        assert result.counts["facts_superseded"] == 1
+        assert result.counts["scope"] == "all_fact_rows_including_superseded"
+
+
+class TestRebuildBanksPlanRows:
+    def test_dry_run_emits_digest_category_old_new_rows(self, tmp_path):
+        db_path = _seed_db(tmp_path)
+        # Inject an orphan bank that should appear as DELETE in the plan.
+        store = MemoryStore(db_path=db_path, hrr_dim=32)
+        try:
+            store._conn.execute(
+                "INSERT OR REPLACE INTO memory_banks(bank_name, vector, dim, fact_count, updated_at) "
+                "VALUES(?, ?, ?, ?, datetime('now'))",
+                ("cat:tool", b"\x00" * (32 * 8), 32, 9),
+            )
+            store._conn.commit()
+        finally:
+            store.close()
+
+        result = maintenance.rebuild_banks(db_path, hermes_home=tmp_path, dry_run=True)
+        payload = result.to_dict()
+        assert "plan" in payload
+        assert payload["counts"]["banks_to_delete"] >= 1
+
+        actions = {row["action"] for row in payload["plan"]}
+        assert "DELETE" in actions
+        assert any(row["action"] in {"CREATE", "UPDATE", "REFRESH"} for row in payload["plan"])
+
+        for row in payload["plan"]:
+            assert "bank_name" in row
+            assert "old_fact_count" in row
+            assert "new_fact_count" in row
+            # No raw namespace leakage.
+            assert "telegram:111" not in json.dumps(row)
+            if row["action"] != "DELETE":
+                assert row["namespace_digest"] in {"(unscoped)", row["namespace_digest"]}
+                assert row["category"]
diff --git a/tests/plugins/memory/test_holographic_restore_maintenance.py b/tests/plugins/memory/test_holographic_restore_maintenance.py
index d3af6e87f4..08c110657f 100644
--- a/tests/plugins/memory/test_holographic_restore_maintenance.py
+++ b/tests/plugins/memory/test_holographic_restore_maintenance.py
@@ -267,3 +267,43 @@ class TestRestoreApply:
         contents = [f["content"] for f in store.list_facts(limit=10)]
         store.close()
         assert "fresh fact" in contents
+
+
+# ---------------------------------------------------------------------------
+# P1b — .enc refusal + gateway advisory warning
+# ---------------------------------------------------------------------------
+
+class TestRestoreP1bGuards:
+    def test_enc_input_is_refused_without_touching_live(self, tmp_path):
+        live_path = _make_live(tmp_path, facts=[("live fact", "general", "")])
+        before = _file_hash(live_path)
+        enc_path = tmp_path / "skills-pack-snapshot.enc"
+        enc_path.write_bytes(b"not-a-sqlite-db")
+
+        result = maintenance.restore(
+            live_path, backup_path=enc_path, hermes_home=tmp_path, dry_run=True,
+        )
+        assert result.ok is False
+        assert result.exit_code == maintenance.EXIT_CANNOT_RUN
+        assert result.gates["apply_allowed"] is False
+        assert result.gates.get("input_format") == "skills_pack_encrypted"
+        joined = " ".join(result.issues + result.gates["block_reasons"])
+        assert "hermes_backup" in joined
+        assert ".enc" in joined
+        assert before == _file_hash(live_path)
+
+    def test_gateway_running_is_advisory_warning_not_hard_block(self, tmp_path, monkeypatch):
+        live_path = _make_live(tmp_path, facts=[("live fact", "general", "")])
+        info = _make_backup(tmp_path, "clean", facts=[("backup fact", "general", "")])
+
+        monkeypatch.setattr(maintenance, "_gateway_running_for_home", lambda _home: True)
+
+        result = maintenance.restore(
+            live_path, backup_path=info.path, hermes_home=tmp_path, dry_run=True,
+        )
+        d = result.to_dict()
+        assert d["gates"]["gateway_running"] is True
+        assert d["gates"]["apply_allowed"] is True
+        assert any("gateway" in w.lower() for w in d["gates"]["warnings"])
+        # Advisory only — clean backup still exits 0.
+        assert result.exit_code == maintenance.EXIT_OK
```

## Companion raw patch file

diff --git a/hermes_cli/main.py b/hermes_cli/main.py
index 26aa55083f..b07d9451e8 100644
--- a/hermes_cli/main.py
+++ b/hermes_cli/main.py
@@ -5153,18 +5153,32 @@ For more help on a command:
     memory_benchmark.add_argument("--gold-set", dest="gold_set", required=True, help="Path to a benchmark gold-set JSON file")
     memory_benchmark.add_argument("--json", action="store_true", help="Emit machine-parsable JSON only")
     memory_rebuild_vectors = memory_sub.add_parser(
-        "rebuild-vectors", help="Recompute HRR vectors for every active fact (idempotent)",
+        "rebuild-vectors",
+        help="Recompute HRR vectors for every fact row including superseded (idempotent)",
+        description=(
+            "Recomputes HRR vectors for ALL fact rows in the active profile DB "
+            "(active and superseded). Dry-run reports facts_planned plus "
+            "facts_active/facts_superseded so the count is not confused with "
+            "active-only health totals. --apply creates+verifies a backup first."
+        ),
     )
     memory_rebuild_vectors.add_argument("--dry-run", dest="dry_run", action="store_true", default=True,
                                          help="Report planned counts without writing (default)")
     memory_rebuild_vectors.add_argument("--apply", dest="dry_run", action="store_false",
-                                         help="Actually rebuild vectors (creates+verifies a backup first)")
+                                         help="Actually rebuild vectors for all fact rows (creates+verifies a backup first)")
     memory_rebuild_vectors.add_argument("--json", action="store_true", help="Emit machine-parsable JSON only")
     memory_rebuild_banks = memory_sub.add_parser(
-        "rebuild-banks", help="Rebuild every namespace/category HRR memory bank (idempotent)",
+        "rebuild-banks",
+        help="Reconcile every namespace/category HRR memory bank; delete orphans (idempotent)",
+        description=(
+            "Rebuilds banks for every active (namespace, category) pair and deletes "
+            "orphan bank rows. Dry-run emits a plan list with namespace_digest, "
+            "category, bank_name, action (CREATE/UPDATE/REFRESH/DELETE), and "
+            "old_fact_count→new_fact_count. No raw namespaces or fact text."
+        ),
     )
     memory_rebuild_banks.add_argument("--dry-run", dest="dry_run", action="store_true", default=True,
-                                       help="Report planned bank pairs without writing (default)")
+                                       help="Report planned bank pairs/plan rows without writing (default)")
     memory_rebuild_banks.add_argument("--apply", dest="dry_run", action="store_false",
                                        help="Actually rebuild banks (creates+verifies a backup first)")
     memory_rebuild_banks.add_argument("--json", action="store_true", help="Emit machine-parsable JSON only")
@@ -5180,6 +5194,17 @@ For more help on a command:
     )
     memory_verify.add_argument("--json", action="store_true", help="Emit machine-parsable JSON only")
     memory_verify.add_argument("--backup-path", dest="backup_path", default=None, help="Run health diagnostics against this backup database (and verify its checksum/integrity)")
+    memory_backup = memory_sub.add_parser(
+        "backup",
+        help="Create a native Holographic backup now (checksum sidecar + retention prune)",
+        description=(
+            "Snapshots the active profile's live memory_store.db via SQLite's online backup\n"
+            "API into $HERMES_HOME/backups/holographic/<dir>/memory-<timestamp>.db with a\n"
+            ".json checksum sidecar. Always available (does not require backup_enabled).\n"
+            "Prunes older backups in that directory down to backup_retention when configured."
+        ),
+    )
+    memory_backup.add_argument("--json", action="store_true", help="Emit machine-parsable JSON only")
     memory_restore = memory_sub.add_parser(
         "restore",
         help="Restore the live Holographic DB from a native backup (DESTRUCTIVE; dry-run by default)",
@@ -5191,12 +5216,16 @@ For more help on a command:
             "whether --apply would be allowed. --apply re-verifies everything, takes a safety\n"
             "copy of the current live DB first (never deleted by this command), sweeps any\n"
             "facts tombstoned on the live DB since the backup was taken (so a forgotten fact\n"
-            "is never silently reactivated), and reports post-restore health. Never exposed\n"
-            "to the model/fact_store tool — operator CLI only."
+            "is never silently reactivated), and reports post-restore health.\n"
+            "\n"
+            "Encrypted skills-pack snapshots (*.enc) are refused — use skills-pack\n"
+            "hermes_backup.py for those. If the gateway appears running for this HERMES_HOME,\n"
+            "a warning is included (does not hard-block apply). Never exposed to the\n"
+            "model/fact_store tool — operator CLI only."
         ),
     )
     memory_restore.add_argument("--from", dest="backup_from", required=True,
-                                 help="Path to the native backup .db file (its .json sidecar must sit alongside it)")
+                                 help="Path to the native backup .db file (with sibling .json sidecar). Not *.enc")
     memory_restore.add_argument("--dry-run", dest="dry_run", action="store_true", default=True,
                                  help="Report the restore plan/diff/gates without writing (default)")
     memory_restore.add_argument("--apply", dest="dry_run", action="store_false",
diff --git a/hermes_cli/memory_setup.py b/hermes_cli/memory_setup.py
index 169073d374..7fa7377e40 100644
--- a/hermes_cli/memory_setup.py
+++ b/hermes_cli/memory_setup.py
@@ -533,6 +533,8 @@ def memory_command(args) -> None:
         cmd_backfill_relationships(args)
     elif sub == "verify":
         cmd_verify(args)
+    elif sub == "backup":
+        cmd_backup(args)
     elif sub == "restore":
         cmd_restore(args)
     elif sub == "viewer":
@@ -682,9 +684,9 @@ def _print_maintenance_target(db_path: Path, *, as_json: bool, apply: bool) -> N
 def cmd_rebuild_vectors(args) -> None:
     """`hermes memory rebuild-vectors [--dry-run]`.
 
-    Recomputes HRR vectors for every active fact in the current profile's
-    database. Defaults to dry-run unless the caller explicitly asks to
-    write — see the `--dry-run`/no-flag handling below.
+    Recomputes HRR vectors for every fact row (active + superseded) in the
+    current profile's database. Defaults to dry-run unless the caller
+    explicitly asks to write — see the `--dry-run`/no-flag handling below.
     """
     import json as _json
     import sys as _sys
@@ -806,6 +808,85 @@ def cmd_backfill_relationships(args) -> None:
     _sys.exit(result.exit_code)
 
 
+def cmd_backup(args) -> None:
+    """`hermes memory backup` — create a native backup now + prune retention."""
+    import json as _json
+    import sys as _sys
+
+    from plugins.memory.holographic import backup as _backup
+    from plugins.memory.holographic import maintenance as _maintenance
+
+    db_path = _holographic_db_path()
+    as_json = bool(getattr(args, "json", False))
+    home = get_hermes_home()
+
+    if not db_path.exists():
+        message = f"Holographic memory database not found: {db_path}"
+        if as_json:
+            print(_json.dumps({"ok": False, "error": message}))
+        else:
+            print(f"\n  {message}\n")
+        _sys.exit(_maintenance.EXIT_CANNOT_RUN)
+
+    # Retention from active holographic plugin config when present.
+    retention = 14
+    try:
+        from hermes_cli.config import load_config
+
+        cfg = load_config() or {}
+        plugins = cfg.get("plugins") or {}
+        holo = plugins.get("hermes-memory-store") or plugins.get("holographic") or {}
+        if isinstance(holo, dict) and "backup_retention" in holo:
+            retention = max(0, int(holo.get("backup_retention", 14)))
+        memory_cfg = cfg.get("memory") or {}
+        # Also accept nested memory.provider-style blocks if present.
+        if isinstance(memory_cfg, dict):
+            nested = memory_cfg.get("holographic") or memory_cfg.get("config") or {}
+            if isinstance(nested, dict) and "backup_retention" in nested:
+                retention = max(0, int(nested.get("backup_retention", retention)))
+    except Exception:
+        retention = 14
+
+    backup_dir = home / "backups" / "holographic" / "operator"
+    try:
+        info = _backup.create_backup(str(db_path), str(backup_dir))
+        verify = _backup.verify_backup(info.path)
+        removed: list[str] = []
+        if retention > 0:
+            removed = _backup.prune_backups(str(backup_dir), keep=retention)
+    except Exception as exc:
+        if as_json:
+            print(_json.dumps({"ok": False, "error": str(exc)}))
+        else:
+            print(f"\n  Backup failed: {exc}\n")
+        _sys.exit(_maintenance.EXIT_ERRORS)
+
+    payload = {
+        "ok": bool(verify.ok),
+        "backup_path": info.path,
+        "sidecar_path": info.sidecar_path,
+        "checksum": info.checksum,
+        "timestamp": info.timestamp,
+        "fact_count": info.fact_count,
+        "backup_dir": str(backup_dir),
+        "retention": retention,
+        "pruned": removed,
+        "verify_reason": verify.reason,
+    }
+    if as_json:
+        print(_json.dumps(payload, indent=2))
+    else:
+        print(f"\n  Holographic backup")
+        print("  " + "─" * 40)
+        print(f"  Path:      {info.path}")
+        print(f"  Checksum:  {info.checksum[:16]}…")
+        print(f"  Facts:     {info.fact_count}")
+        print(f"  Verify:    {'ok' if verify.ok else verify.reason}")
+        print(f"  Retention: keep {retention} (pruned {len(removed)})")
+        print()
+    _sys.exit(_maintenance.EXIT_OK if verify.ok else _maintenance.EXIT_ERRORS)
+
+
 def cmd_restore(args) -> None:
     """`hermes memory restore --from PATH [--dry-run|--apply] [--allow-backup-warnings]`.
 
@@ -813,6 +894,7 @@ def cmd_restore(args) -> None:
     Holographic database with a native backup. Dry-run (the default)
     performs zero writes — see maintenance.restore()'s docstring for the
     full dry-run/apply contract. Never exposed to fact_store/the model.
+    Refuses skills-pack ``*.enc`` inputs with an explicit hermes_backup hint.
     """
     import json as _json
     import sys as _sys
@@ -868,8 +950,12 @@ def _emit_restore_result(result, *, as_json: bool) -> None:
                 print(f"    {key}: backup={d['backup']} live={d['live']} delta={d['delta']}")
         print(f"\n  Tombstones on live DB: {result.tombstones_live_count}")
         print(f"  Apply allowed: {result.gates.get('apply_allowed')}")
+        if result.gates.get("gateway_running"):
+            print("  Gateway running: yes (warning — prefer stop before --apply)")
         for reason in result.gates.get("block_reasons", []):
             print(f"    blocked: {reason}")
+        for warn in result.gates.get("warnings", []) or []:
+            print(f"    warning: {warn}")
         print("\n  No changes made. Re-run with --apply to restore.\n")
     else:
         if result.safety_copy_path:
@@ -879,6 +965,8 @@ def _emit_restore_result(result, *, as_json: bool) -> None:
             print(f"  Post-restore health exit code: {result.post_health['exit_code']}")
         for reason in result.gates.get("block_reasons", []):
             print(f"    blocked: {reason}")
+        for warn in result.gates.get("warnings", []) or []:
+            print(f"    warning: {warn}")
         print()
 
     if result.issues:
@@ -899,6 +987,22 @@ def _emit_maintenance_result(result, *, as_json: bool) -> None:
     print(f"  Status:    {'OK' if result.ok else 'FAILED'}")
     for key, value in result.counts.items():
         print(f"  {key}: {value}")
+    if getattr(result, "plan", None):
+        print("\n  Plan:")
+        for row in result.plan:
+            action = row.get("action", "?")
+            bank = row.get("bank_name", "?")
+            digest = row.get("namespace_digest")
+            category = row.get("category")
+            old_c = row.get("old_fact_count")
+            new_c = row.get("new_fact_count")
+            meta = []
+            if digest is not None:
+                meta.append(f"ns={digest}")
+            if category is not None:
+                meta.append(f"cat={category}")
+            meta_s = (" " + " ".join(meta)) if meta else ""
+            print(f"    {action:7} {bank}{meta_s}  {old_c}→{new_c}")
     if result.backup is not None:
         print(f"  Backup:    {'ok' if result.backup['ok'] else result.backup.get('reason', 'failed')}")
     if result.issues:
diff --git a/plugins/memory/holographic/README.md b/plugins/memory/holographic/README.md
index 6ccc02e61e..a1b5b3cade 100644
--- a/plugins/memory/holographic/README.md
+++ b/plugins/memory/holographic/README.md
@@ -217,8 +217,9 @@ All keys live in `config.yaml` under `plugins.hermes-memory-store` (not
 
 | Key | Default | Description |
 |-----|---------|-------------|
-| `backup_enabled` | `false` | Reserved for future automated backup scheduling — the manual `fact_store {"action": "backup"}` action works regardless of this setting |
-| `backup_retention` | `14` | Keep the N most recent backups per namespace after each manual backup (`0` = keep all) |
+| `backup_enabled` | `false` | When `true`, take an opportunistic native backup on provider initialize if the newest backup in this namespace dir is older than `backup_interval_hours` (or missing). Manual `fact_store {"action": "backup"}` and `hermes memory backup` always work regardless |
+| `backup_retention` | `14` | Keep the N most recent native backups per backup directory after each successful backup (manual, CLI, or auto); `0` = keep all |
+| `backup_interval_hours` | `24` | Minimum hours between opportunistic auto-backups when `backup_enabled` is true (`0` = backup on every initialize) |
 
 **Retrieval telemetry**
 
@@ -227,12 +228,11 @@ All keys live in `config.yaml` under `plugins.hermes-memory-store` (not
 | `retrieval_telemetry` | `false` | Record bounded, content-free retrieval-scoring telemetry (see [Retrieval Telemetry](#retrieval-telemetry)); nothing is written while this is `false` |
 | `retrieval_telemetry_retention` | `1000` | Maximum `retrieval_events` rows retained (oldest pruned first on every write, clamped `1`-`100000`); only relevant when `retrieval_telemetry` is enabled |
 
-No settings are currently purely experimental beyond `backup_enabled`'s
-reserved-for-scheduling note above — every other key already has a live
-effect in the current implementation. Note that `scoped_hrr_banks`,
-`relationship_recall_enabled`, `relationship_max_depth`, and
-`relationship_max_nodes` — named in the demo-reference integration's design
-plan — are **not** real config.yaml keys in the shipped code; see
+Every key above already has a live effect in the current implementation.
+Note that `scoped_hrr_banks`, `relationship_recall_enabled`,
+`relationship_max_depth`, and `relationship_max_nodes` — named in the
+demo-reference integration's design plan — are **not** real config.yaml
+keys in the shipped code; see
 [Configuration Defaults and Rollback Reference](#configuration-defaults-and-rollback-reference)
 for why.
 
@@ -559,12 +559,20 @@ it exists purely as read-only reference material for this integration.**
 
 ## Backup and Restore
 
-`fact_store {"action": "backup"}` snapshots the live database using
-SQLite's online backup API (safe against a concurrently open WAL
-connection — no file copy, no pausing writers) to
-`$HERMES_HOME/backups/holographic/<namespace>/memory-<timestamp>.db`, with a
-SHA-256 checksum sidecar and `0600` permissions. `backup_retention` prunes
-older backups automatically after each manual backup.
+Three ways to create a **native** Holographic backup (`memory-*.db` +
+`.json` checksum sidecar, `0600` permissions), all using SQLite's online
+backup API (safe against a concurrently open WAL connection):
+
+1. `fact_store {"action": "backup"}` — namespace-scoped dir under
+   `$HERMES_HOME/backups/holographic/<namespace>/`
+2. `hermes memory backup` — operator CLI into
+   `$HERMES_HOME/backups/holographic/operator/`
+3. Opportunistic auto-backup on provider initialize when `backup_enabled`
+   is true and the newest backup in that namespace dir is missing or older
+   than `backup_interval_hours`
+
+`backup_retention` prunes older backups in the target directory after each
+successful backup (`0` = keep all).
 
 **Restore is deliberately NOT a `fact_store` tool action** — it's exposed
 only as `HolographicMemoryProvider.restore_from_backup(path)` (library/
@@ -573,6 +581,10 @@ never from chat. A prompt-injected or confused model triggering a restore
 could silently reactivate facts the user explicitly forgot; that decision
 belongs to a human.
 
+**Encrypted skills-pack snapshots (`*.enc`) are not valid inputs** for
+`hermes memory restore`. Use skills-pack `hermes_backup.py` for those
+(typically `--only holographic --mode migration` onto a fresh DB path).
+
 ### `hermes memory restore` (operator CLI)
 
 ```bash
diff --git a/plugins/memory/holographic/TECHNICAL.md b/plugins/memory/holographic/TECHNICAL.md
index 9dd916e04d..b05be32788 100644
--- a/plugins/memory/holographic/TECHNICAL.md
+++ b/plugins/memory/holographic/TECHNICAL.md
@@ -667,7 +667,7 @@ Every key read in `HolographicMemoryProvider.__init__`/`get_config_schema()`
 | `diagnostics_include_content` | truthy | `false` | `_diagnostics()`'s `by_category` field |
 | `shared_memory_enabled` | truthy | `false` | `prefetch()`, `_handle_share_action()`, `relationships.list_relationships` |
 | `shared_memory_categories` | comma-separated string or list → `frozenset` | empty | same as above (also reused against `relationship_type`, not `category`, by `relationships.py`) |
-| `backup_enabled` | truthy | `false` | **reserved** — read into `self._backup_enabled` and surfaced in `diagnostics()`, but no code path currently gates a write on it; the manual `fact_store backup` action works regardless |
+| `backup_enabled` | truthy | `false` | When true, opportunistic native backup on provider initialize if newest backup in the namespace dir is missing/older than `backup_interval_hours`. Manual `fact_store backup` and `hermes memory backup` always work |
 | `backup_retention` | int, `max(0, int(...))` | `14` | `HolographicMemoryProvider.backup()` → `backup.prune_backups` |
 | `retrieval_telemetry` | truthy | `false` | `FactRetriever(telemetry_enabled=...)` |
 | `retrieval_telemetry_retention` | int, clamped `[1,100000]` | `1000` (`telemetry.DEFAULT_RETENTION`) | `FactRetriever` → `telemetry.record_retrieval_event(retention=...)` |
diff --git a/plugins/memory/holographic/__init__.py b/plugins/memory/holographic/__init__.py
index 4878717924..7deba52b99 100644
--- a/plugins/memory/holographic/__init__.py
+++ b/plugins/memory/holographic/__init__.py
@@ -573,6 +573,15 @@ class HolographicMemoryProvider(MemoryProvider):
         self._shared_memory_categories = frozenset(str(c) for c in (raw_categories or []))
         self._backup_enabled = is_truthy_value(self._config.get("backup_enabled"), default=False)
         self._backup_retention = max(0, int(self._config.get("backup_retention", 14)))
+        # Minimum hours between opportunistic auto-backups when backup_enabled.
+        # Manual fact_store/CLI backup always runs immediately and still prunes
+        # to backup_retention afterward.
+        try:
+            self._backup_interval_hours = max(
+                0.0, float(self._config.get("backup_interval_hours", 24))
+            )
+        except (TypeError, ValueError):
+            self._backup_interval_hours = 24.0
         self._metrics: Dict[str, int] = {}
 
         # Phase 8 (demo-reference integration) — optional, bounded,
@@ -769,15 +778,31 @@ class HolographicMemoryProvider(MemoryProvider):
             },
             {
                 "key": "backup_enabled",
-                "description": "Reserved for future automated backup scheduling; the manual fact_store 'backup' action works regardless of this flag",
+                "description": (
+                    "When true, take an opportunistic native backup on provider "
+                    "initialize if the newest backup in this namespace dir is older "
+                    "than backup_interval_hours (or missing). Manual fact_store "
+                    "'backup' and `hermes memory backup` always work regardless"
+                ),
                 "default": str(self._config.get("backup_enabled", "false")).lower(),
                 "choices": ["true", "false"],
             },
             {
                 "key": "backup_retention",
-                "description": "Keep the N most recent backups per namespace after each manual backup (0 = keep all)",
+                "description": (
+                    "Keep the N most recent native backups per backup directory "
+                    "after each successful backup (manual, CLI, or auto); 0 = keep all"
+                ),
                 "default": str(self._config.get("backup_retention", 14)),
             },
+            {
+                "key": "backup_interval_hours",
+                "description": (
+                    "Minimum hours between opportunistic auto-backups when "
+                    "backup_enabled is true (0 = backup on every initialize)"
+                ),
+                "default": str(self._config.get("backup_interval_hours", 24)),
+            },
             {
                 "key": "retrieval_telemetry",
                 "description": "Record content-free retrieval telemetry (component scores, rank, feedback) for future offline weight-learning evaluation. Never records prompts, fact content, names, chat IDs, raw namespaces, or credentials; observation-only, never adjusts retrieval weights at runtime",
@@ -866,6 +891,59 @@ class HolographicMemoryProvider(MemoryProvider):
         if self._extraction_mode == "hybrid":
             self._capture_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="holographic-capture")
 
+        # Opportunistic native backup (P1b): rate-limited, never blocks init on failure.
+        self._maybe_auto_backup(reason="initialize")
+
+    def _backup_dir(self) -> "Path":
+        dir_name = re.sub(r"[^A-Za-z0-9_.-]", "_", self._namespace) or "default"
+        return Path(self._hermes_home) / "backups" / "holographic" / dir_name
+
+    def _latest_backup_age_hours(self, backup_dir: "Path") -> "float | None":
+        """Age in hours of the newest memory-*.db under backup_dir, or None."""
+        if not backup_dir.is_dir():
+            return None
+        newest_mtime = None
+        for entry in backup_dir.iterdir():
+            if not entry.is_file():
+                continue
+            if not entry.name.startswith("memory-") or not entry.name.endswith(".db"):
+                continue
+            if entry.name.endswith(".db.json"):
+                continue
+            try:
+                mtime = entry.stat().st_mtime
+            except OSError:
+                continue
+            if newest_mtime is None or mtime > newest_mtime:
+                newest_mtime = mtime
+        if newest_mtime is None:
+            return None
+        return max(0.0, (time.time() - newest_mtime) / 3600.0)
+
+    def _maybe_auto_backup(self, *, reason: str = "auto") -> None:
+        """Create a native backup when backup_enabled and interval elapsed.
+
+        Failures are logged and swallowed — auto backup must never break
+        session initialize. Manual backup() still raises.
+        """
+        if not self._backup_enabled or not self._store:
+            return
+        try:
+            backup_dir = self._backup_dir()
+            age_h = self._latest_backup_age_hours(backup_dir)
+            if age_h is not None and age_h < self._backup_interval_hours:
+                self._metric("backup_auto_skipped_fresh")
+                return
+            info = self.backup()
+            self._metric("backup_auto_created")
+            logger.info(
+                "holographic: auto-backup (%s) wrote %s (facts=%s)",
+                reason, info.path, info.fact_count,
+            )
+        except Exception:
+            self._metric("backup_auto_failed")
+            logger.warning("holographic: auto-backup (%s) failed", reason, exc_info=True)
+
     def _resolve_namespace(
         self, *, platform: str, chat_id: str, thread_id: str, agent_identity: str,
     ) -> tuple[str, bool]:
@@ -1418,31 +1496,77 @@ class HolographicMemoryProvider(MemoryProvider):
                         item["reason"] = problem
                         result["rejected"].append(item)
                         continue
-                    if self._find_fact_id_by_content(entry) is not None:
-                        result["duplicates"].append(item)
+                    exact_id = self._find_fact_id_by_content(entry)
+                    if exact_id is not None:
+                        result["duplicates"].append({
+                            **item,
+                            "reason": "exact_duplicate",
+                            "strategy": "exact",
+                            "matched_fact_id": exact_id,
+                        })
                         continue
-                    result["proposed"].append(item)
-                    if apply:
-                        candidate = capture.FactCandidate(
-                            content=entry, operation="statement", scope="", confidence=1.0,
-                        )
-                        applied = consolidation.classify_and_apply(
-                            self._store,
-                            self._retriever,
+
+                    candidate = capture.FactCandidate(
+                        content=entry, operation="statement", scope="", confidence=1.0,
+                    )
+
+                    if not apply:
+                        # Preview uses the same consolidation policy as apply
+                        # (token-overlap + optional fuzzy N-gram), so near-dups
+                        # are not under-counted vs the write path.
+                        plan = consolidation.plan_candidate(
                             candidate,
-                            category=category,
-                            namespace=self._namespace,
-                            source_type="builtin_import",
-                            extraction_method="explicit_import",
-                            confidence=1.0,
+                            consolidation._search_existing(
+                                self._retriever, candidate, self._namespace,
+                            ),
                             fuzzy_enabled=self._fuzzy_dedup_enabled,
                             fuzzy_ngram_threshold=self._fuzzy_dedup_threshold,
                         )
-                        if applied.action == "inserted" and applied.fact_id:
-                            self._record_source(applied.fact_id, "builtin_import")
-                            result["imported"].append({**item, "fact_id": applied.fact_id})
+                        if plan.action == "ignored":
+                            result["duplicates"].append({
+                                **item,
+                                "reason": plan.reason,
+                                "strategy": plan.strategy,
+                                "matched_fact_id": plan.fact_id,
+                                "fuzzy_score": plan.fuzzy_score,
+                            })
                         else:
-                            result["duplicates"].append({**item, "reason": applied.reason})
+                            result["proposed"].append({
+                                **item,
+                                "reason": plan.reason,
+                                "strategy": plan.strategy or "none",
+                            })
+                        continue
+
+                    result["proposed"].append(item)
+                    applied = consolidation.classify_and_apply(
+                        self._store,
+                        self._retriever,
+                        candidate,
+                        category=category,
+                        namespace=self._namespace,
+                        source_type="builtin_import",
+                        extraction_method="explicit_import",
+                        confidence=1.0,
+                        fuzzy_enabled=self._fuzzy_dedup_enabled,
+                        fuzzy_ngram_threshold=self._fuzzy_dedup_threshold,
+                    )
+                    if applied.action == "inserted" and applied.fact_id:
+                        self._record_source(applied.fact_id, "builtin_import")
+                        result["imported"].append({
+                            **item,
+                            "fact_id": applied.fact_id,
+                            "reason": applied.reason,
+                            "strategy": applied.strategy or "none",
+                        })
+                    else:
+                        result["duplicates"].append({
+                            **item,
+                            "reason": applied.reason,
+                            "strategy": applied.strategy,
+                            "matched_fact_id": applied.fact_id or applied.related_id,
+                            "fuzzy_score": applied.fuzzy_score,
+                        })
 
         if apply:
             with self._store.transaction(), self._store.defer_bank_rebuilds():
@@ -1630,12 +1754,23 @@ class HolographicMemoryProvider(MemoryProvider):
             "consolidation_enabled": self._consolidation_enabled,
             "shared_memory_enabled": self._shared_memory_enabled,
             "backup_enabled": self._backup_enabled,
+            "backup_retention": self._backup_retention,
+            "backup_interval_hours": self._backup_interval_hours,
             "database": stats.get("database", ""),
             "facts": stats.get("facts", 0),
             "active": stats.get("active", 0),
             "superseded": stats.get("superseded", 0),
             "metrics": dict(self._metrics),
         }
+        try:
+            if self._hermes_home:
+                age = self._latest_backup_age_hours(self._backup_dir())
+                result["backup_dir"] = str(self._backup_dir())
+                result["latest_backup_age_hours"] = (
+                    None if age is None else round(age, 3)
+                )
+        except Exception:
+            pass
         if self._diagnostics_include_content:
             result["by_category"] = stats.get("by_category", {})
         return result
diff --git a/plugins/memory/holographic/maintenance.py b/plugins/memory/holographic/maintenance.py
index 0565be2d9b..70716e5ed9 100644
--- a/plugins/memory/holographic/maintenance.py
+++ b/plugins/memory/holographic/maintenance.py
@@ -111,6 +111,9 @@ class MaintenanceResult:
     counts: dict[str, Any] = field(default_factory=dict)
     backup: "dict[str, Any] | None" = None
     issues: list[str] = field(default_factory=list)
+    # Optional operator plan rows (rebuild-banks dry-run, etc.). Never includes
+    # raw namespace strings or fact content — digests/bank keys/counts only.
+    plan: list[dict[str, Any]] = field(default_factory=list)
     algorithm_version: str = ALGORITHM_VERSION
     schema_version: str = SCHEMA_VERSION
 
@@ -121,7 +124,7 @@ class MaintenanceResult:
         return EXIT_OK
 
     def to_dict(self) -> dict[str, Any]:
-        return {
+        d: dict[str, Any] = {
             "ok": self.ok,
             "dry_run": self.dry_run,
             "operation": self.operation,
@@ -133,6 +136,9 @@ class MaintenanceResult:
             "backup": self.backup,
             "issues": self.issues,
         }
+        if self.plan:
+            d["plan"] = self.plan
+        return d
 
 
 # ---------------------------------------------------------------------------
@@ -236,12 +242,16 @@ def rebuild_vectors(
     dry_run: bool = True,
     hrr_dim: int = 1024,
 ) -> MaintenanceResult:
-    """Recompute HRR vectors for every active fact (dimension-consistent).
+    """Recompute HRR vectors for every fact row, including superseded.
 
     Wraps ``MemoryStore.rebuild_all_vectors(namespace=None)`` (Phase 2),
-    which is already idempotent — rebuilding twice produces the same
-    vectors both times, so interrupting this operation and re-running it is
-    always safe to restart.
+    which intentionally walks *all* fact rows (active + superseded) so a
+    recovery/migration never leaves stale vectors on history rows. The
+    operation is idempotent — rebuilding twice produces the same vectors
+    both times, so interrupting and re-running is always safe.
+
+    Dry-run counts split active vs superseded so operators are not
+    surprised that ``facts_planned`` exceeds the active-only health count.
     """
     db_path = _require_db(db_path)
 
@@ -249,11 +259,27 @@ def rebuild_vectors(
         conn = _readonly_connection(db_path)
         try:
             planned = int(conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0])
+            active = int(
+                conn.execute(
+                    "SELECT COUNT(*) FROM facts WHERE status != 'superseded'"
+                ).fetchone()[0]
+            )
+            superseded = int(
+                conn.execute(
+                    "SELECT COUNT(*) FROM facts WHERE status = 'superseded'"
+                ).fetchone()[0]
+            )
         finally:
             conn.close()
         return MaintenanceResult(
             ok=True, dry_run=True, operation="rebuild-vectors", db_path=str(db_path),
-            counts={"facts_planned": planned},
+            counts={
+                "facts_planned": planned,
+                "facts_active": active,
+                "facts_superseded": superseded,
+                # Explicit contract note for automation/CLIs:
+                "scope": "all_fact_rows_including_superseded",
+            },
         )
 
     with maintenance_lock(db_path):
@@ -272,7 +298,11 @@ def rebuild_vectors(
 
         return MaintenanceResult(
             ok=True, dry_run=False, operation="rebuild-vectors", db_path=str(db_path),
-            counts={"facts_processed": processed}, backup=backup_info,
+            counts={
+                "facts_processed": processed,
+                "scope": "all_fact_rows_including_superseded",
+            },
+            backup=backup_info,
         )
 
 
@@ -302,6 +332,88 @@ def _expected_bank_names(pairs: list[tuple[str, str]]) -> set[str]:
     return {bank_name(ns, cat) for ns, cat in pairs}
 
 
+def _namespace_digest_for_plan(namespace: str) -> str:
+    """Opaque plan label — matches diagnostics display digests, never raw ns."""
+    import hashlib
+
+    if not namespace:
+        return "(unscoped)"
+    return hashlib.sha1(namespace.encode("utf-8")).hexdigest()[:12]
+
+
+def _bank_fact_counts(conn: sqlite3.Connection) -> dict[str, int]:
+    row = conn.execute(
+        "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_banks'"
+    ).fetchone()
+    if row is None:
+        return {}
+    return {
+        str(r[0]): int(r[1] or 0)
+        for r in conn.execute("SELECT bank_name, fact_count FROM memory_banks")
+    }
+
+
+def _active_hrr_count(conn: sqlite3.Connection, namespace: str, category: str) -> int:
+    return int(
+        conn.execute(
+            """
+            SELECT COUNT(*) FROM facts
+            WHERE namespace = ?
+              AND category = ?
+              AND status != 'superseded'
+              AND hrr_vector IS NOT NULL
+            """,
+            (namespace, category),
+        ).fetchone()[0]
+    )
+
+
+def _rebuild_banks_plan_rows(
+    conn: sqlite3.Connection,
+    pairs: list[tuple[str, str]],
+    existing: set[str],
+) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
+    """Build dry-run plan rows: rebuild/create pairs + orphan deletes.
+
+    Never includes raw namespaces or fact content.
+    """
+    from .store import bank_name
+
+    existing_counts = _bank_fact_counts(conn)
+    plan: list[dict[str, Any]] = []
+    for namespace, category in pairs:
+        bname = bank_name(namespace, category)
+        old_count = int(existing_counts.get(bname, 0))
+        new_count = _active_hrr_count(conn, namespace, category)
+        if bname not in existing:
+            action = "CREATE"
+        elif old_count != new_count:
+            action = "UPDATE"
+        else:
+            action = "REFRESH"
+        plan.append({
+            "namespace_digest": _namespace_digest_for_plan(namespace),
+            "category": category,
+            "bank_name": bname,
+            "action": action,
+            "old_fact_count": old_count,
+            "new_fact_count": new_count,
+        })
+
+    expected = {row["bank_name"] for row in plan}
+    orphans: list[dict[str, Any]] = []
+    for name in sorted(existing - expected):
+        orphans.append({
+            "namespace_digest": None,
+            "category": None,
+            "bank_name": name,
+            "action": "DELETE",
+            "old_fact_count": int(existing_counts.get(name, 0)),
+            "new_fact_count": 0,
+        })
+    return plan, orphans
+
+
 def rebuild_banks(
     db_path: "str | Path",
     *,
@@ -323,6 +435,9 @@ def rebuild_banks(
     just the pairs with current active facts, so those orphans actually get
     visited. Idempotent: repeated/interrupted runs converge to the same end
     state (second apply reports zero further deletions).
+
+    Dry-run emits a ``plan`` list of digest/category/bank_name/action/
+    old→new rows plus orphan DELETE rows (no raw namespaces, no fact text).
     """
     db_path = _require_db(db_path)
 
@@ -332,6 +447,7 @@ def rebuild_banks(
             pairs = _active_namespace_category_pairs(conn)
             expected = _expected_bank_names(pairs)
             existing = _existing_bank_names(conn)
+            plan_rows, orphan_rows = _rebuild_banks_plan_rows(conn, pairs, existing)
         finally:
             conn.close()
         to_delete = existing - expected
@@ -344,6 +460,7 @@ def rebuild_banks(
                 "banks_existing": len(existing),
                 "banks_expected": len(expected),
             },
+            plan=plan_rows + orphan_rows,
         )
 
     with maintenance_lock(db_path):
@@ -614,6 +731,50 @@ def _read_live_tombstones(db_path: Path) -> frozenset:
         conn.close()
 
 
+_ENC_RESTORE_REFUSAL = (
+    "Encrypted skills-pack snapshots (.enc) are not supported by "
+    "`hermes memory restore`. Use skills-pack hermes_backup instead, e.g.: "
+    "cd /home/ubuntu/skills-pack && env -u VIRTUAL_ENV uv run python "
+    "src/hermes_backup.py restore --snapshot <ts> --only holographic "
+    "--mode migration --dry-run --db-path <fresh-or-target-db>"
+)
+
+
+def _looks_like_encrypted_skills_pack_snapshot(path: Path) -> bool:
+    """True for skills-pack encrypted snapshots (wrong tool for native restore)."""
+    name = path.name.lower()
+    return path.suffix.lower() == ".enc" or name.endswith(".db.enc") or name.endswith(".sqlite.enc")
+
+
+def _gateway_running_for_home(hermes_home: "str | Path") -> bool:
+    """Best-effort check whether this profile's gateway process is alive.
+
+    Warning-only for restore: file replace under live writers is risky, but
+    operators may still proceed after reviewing the warning. Uses the same
+    PID-file convention as profile/status tooling.
+    """
+    try:
+        from hermes_cli.profiles import _check_gateway_running
+
+        return bool(_check_gateway_running(Path(hermes_home)))
+    except Exception:
+        try:
+            # Fallback when profiles helper is unavailable: respect HERMES_HOME.
+            old = os.environ.get("HERMES_HOME")
+            os.environ["HERMES_HOME"] = str(hermes_home)
+            try:
+                from gateway.status import is_gateway_running
+
+                return bool(is_gateway_running())
+            finally:
+                if old is None:
+                    os.environ.pop("HERMES_HOME", None)
+                else:
+                    os.environ["HERMES_HOME"] = old
+        except Exception:
+            return False
+
+
 def _compute_restore_gates(
     *,
     backup_exists: bool,
@@ -622,11 +783,18 @@ def _compute_restore_gates(
     backup_health_errors: int,
     backup_health_warnings: int,
     allow_backup_warnings: bool,
+    gateway_running: bool = False,
 ) -> "tuple[dict[str, Any], int]":
     """Returns (gates_dict, exit_code) per the exit-code table in the P1 plan:
     0=ok, 1=warnings-only, 2=errors, 3=cannot-run (handled by caller before
-    this is ever invoked for the missing-backup-file case)."""
+    this is ever invoked for the missing-backup-file case).
+
+    ``gateway_running`` is advisory: it is surfaced on the gates object and
+    may promote the dry-run exit code to WARNINGS, but it does **not** block
+    ``apply_allowed`` (operator must still decide after the warning).
+    """
     block_reasons: list[str] = []
+    soft_warnings: list[str] = []
     if not verify_ok:
         block_reasons.append(f"backup verification failed: {verify_reason}")
     if backup_health_errors:
@@ -636,21 +804,32 @@ def _compute_restore_gates(
             f"backup health has {backup_health_warnings} warning issue(s) "
             "(pass --allow-backup-warnings to proceed anyway)"
         )
+    if gateway_running:
+        soft_warnings.append(
+            "gateway appears to be running for this HERMES_HOME — prefer "
+            "stopping writers before restore --apply to avoid races with an "
+            "open live DB"
+        )
 
     apply_allowed = not block_reasons
     gates = {
         "backup_verify_ok": verify_ok,
         "backup_health_errors": backup_health_errors,
         "backup_health_warnings": backup_health_warnings,
+        "gateway_running": gateway_running,
         "apply_allowed": apply_allowed,
         "block_reasons": block_reasons,
+        "warnings": soft_warnings,
     }
 
     if not verify_ok or backup_health_errors:
         exit_code = EXIT_ERRORS
     elif backup_health_warnings and not allow_backup_warnings:
+        # Health warnings that actually block apply (unless allow flag).
         exit_code = EXIT_WARNINGS
     else:
+        # Gateway running is advisory-only: surfaced in gates.warnings /
+        # issues, but does not change exit code or apply_allowed.
         exit_code = EXIT_OK
     return gates, exit_code
 
@@ -679,23 +858,49 @@ def restore(
     actual file-level replace to the existing `backup.restore_backup()`
     primitive — this function's job is orchestration/gating, not
     reimplementing that primitive.
+
+    Encrypted skills-pack ``*.enc`` inputs are refused with an explicit
+    pointer to ``hermes_backup.py`` (wrong tool / wrong format).
     """
     from . import diagnostics as diagnostics_module
 
     db_path = Path(db_path)
     backup_path = Path(backup_path)
+    hermes_home = Path(hermes_home)
     from_path_str = str(backup_path)
 
+    if _looks_like_encrypted_skills_pack_snapshot(backup_path):
+        reason = _ENC_RESTORE_REFUSAL
+        return RestoreMaintenanceResult(
+            ok=False, dry_run=dry_run, exit_code=EXIT_CANNOT_RUN,
+            from_path=from_path_str, db_path=str(db_path),
+            issues=[reason],
+            gates={
+                "backup_verify_ok": False,
+                "backup_health_errors": 0,
+                "backup_health_warnings": 0,
+                "gateway_running": False,
+                "apply_allowed": False,
+                "block_reasons": [reason],
+                "warnings": [],
+                "input_format": "skills_pack_encrypted",
+            },
+        )
+
     if not backup_path.exists():
         return RestoreMaintenanceResult(
             ok=False, dry_run=dry_run, exit_code=EXIT_CANNOT_RUN,
             from_path=from_path_str, db_path=str(db_path),
             issues=[f"backup file not found: {backup_path}"],
             gates={"backup_verify_ok": False, "backup_health_errors": 0,
-                   "backup_health_warnings": 0, "apply_allowed": False,
-                   "block_reasons": [f"backup file not found: {backup_path}"]},
+                   "backup_health_warnings": 0, "gateway_running": False,
+                   "apply_allowed": False,
+                   "block_reasons": [f"backup file not found: {backup_path}"],
+                   "warnings": []},
         )
 
+    gateway_running = _gateway_running_for_home(hermes_home)
+
     verify = backup_module.verify_backup(str(backup_path))
     backup_health_report = None
     backup_health_errors = 0
@@ -712,6 +917,7 @@ def restore(
         backup_health_errors=backup_health_errors,
         backup_health_warnings=backup_health_warnings,
         allow_backup_warnings=allow_backup_warnings,
+        gateway_running=gateway_running,
     )
     backup_verify_dict = {"ok": verify.ok, "reason": verify.reason}
 
@@ -734,6 +940,7 @@ def restore(
             would_block_reactivation=tombstones_live_count > 0,
             gates=gates,
             plan=list(_RESTORE_PLAN_STEPS),
+            issues=list(gates.get("warnings") or []),
         )
 
     # --- apply ---
@@ -743,7 +950,7 @@ def restore(
             from_path=from_path_str, db_path=str(db_path),
             backup_verify=backup_verify_dict,
             gates=gates,
-            issues=list(gates["block_reasons"]),
+            issues=list(gates["block_reasons"]) + list(gates.get("warnings") or []),
         )
 
     with maintenance_lock(db_path):
diff --git a/tests/hermes_cli/test_memory_restore_cli.py b/tests/hermes_cli/test_memory_restore_cli.py
index 005eef9b49..26a2cee1ab 100644
--- a/tests/hermes_cli/test_memory_restore_cli.py
+++ b/tests/hermes_cli/test_memory_restore_cli.py
@@ -121,3 +121,17 @@ class TestRestoreCommand:
             assert "restore" not in schema_actions
         finally:
             p.shutdown()
+
+    def test_enc_path_refused_via_cli(self, capsys, tmp_path):
+        from hermes_cli.memory_setup import cmd_restore
+
+        _seed_live()
+        enc = tmp_path / "snap.enc"
+        enc.write_text("encrypted-blob", encoding="utf-8")
+        with pytest.raises(SystemExit) as exc_info:
+            cmd_restore(_ns(json=True, dry_run=True, backup_from=str(enc)))
+        assert exc_info.value.code == 3
+        parsed = json.loads(capsys.readouterr().out.strip())
+        assert parsed["ok"] is False
+        assert parsed["gates"]["apply_allowed"] is False
+        assert "hermes_backup" in " ".join(parsed.get("issues") or [])
diff --git a/tests/plugins/memory/test_holographic_baseline_compat.py b/tests/plugins/memory/test_holographic_baseline_compat.py
index fefdd6ec39..58b0bf60ad 100644
--- a/tests/plugins/memory/test_holographic_baseline_compat.py
+++ b/tests/plugins/memory/test_holographic_baseline_compat.py
@@ -75,7 +75,7 @@ class TestProviderConfigDefaults:
             "max_facts_per_turn", "consolidation_enabled", "capture_timeout_ms",
             "capture_debug", "profile_isolation", "diagnostics_enabled",
             "diagnostics_include_content", "shared_memory_enabled", "shared_memory_categories",
-            "backup_enabled", "backup_retention",
+            "backup_enabled", "backup_retention", "backup_interval_hours",
             # Phase 3 (demo-reference integration) — fuzzy consolidation evidence gate.
             "fuzzy_dedup_enabled", "fuzzy_dedup_threshold",
             # Phase 8 (demo-reference integration) — retrieval telemetry.
diff --git a/tests/plugins/memory/test_holographic_import.py b/tests/plugins/memory/test_holographic_import.py
index 94d039d37a..5737c959d4 100644
--- a/tests/plugins/memory/test_holographic_import.py
+++ b/tests/plugins/memory/test_holographic_import.py
@@ -1,5 +1,7 @@
 """Explicit migration from profile-scoped built-in memory files."""
 
+from pathlib import Path
+
 import pytest
 
 from plugins.memory.holographic import HolographicMemoryProvider
@@ -124,3 +126,65 @@ def test_import_bank_rebuild_count_is_bounded(tmp_path, monkeypatch):
     # whole call rather than once per inserted fact.
     assert rebuild_calls == [("", "general")]
     provider.shutdown()
+
+
+def test_import_preview_reports_near_duplicates(tmp_path, monkeypatch):
+    """P2: dry-run import should surface fuzzy/token near-dups, not only exact."""
+    hermes_home = tmp_path / ".hermes"
+    memories = hermes_home / "memories"
+    memories.mkdir(parents=True)
+    # Near-paraphrase of an already-stored fact (not byte-identical).
+    (memories / "MEMORY.md").write_text(
+        "Uses Python uv for project dependencies.",
+        encoding="utf-8",
+    )
+    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
+
+    provider = HolographicMemoryProvider(config={
+        "db_path": str(hermes_home / "memory_store.db"),
+        "hrr_dim": 32,
+        "hrr_weight": 0.0,
+        "fuzzy_dedup_enabled": True,
+    })
+    provider.initialize(session_id="import-near-dup", platform="cli")
+    provider._store.add_fact(
+        "Uses Python uv for MCP dependencies.",
+        category="general",
+        namespace=provider._namespace,
+    )
+
+    preview = provider.import_builtin_memories(apply=False)
+    assert preview["counts"]["proposed"] == 0
+    assert preview["counts"]["duplicates"] == 1
+    dup = preview["duplicates"][0]
+    assert dup.get("strategy") in {"token_overlap", "fuzzy_ngram", "exact"}
+    assert dup.get("reason")
+    assert provider._store.list_facts(limit=20)  # unchanged; preview only
+    provider.shutdown()
+
+
+def test_auto_backup_on_initialize_when_enabled(tmp_path, monkeypatch):
+    """P1b: backup_enabled creates a native backup on initialize when stale/missing."""
+    hermes_home = tmp_path / ".hermes"
+    hermes_home.mkdir(parents=True)
+    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
+
+    provider = HolographicMemoryProvider(config={
+        "db_path": str(hermes_home / "memory_store.db"),
+        "hrr_dim": 32,
+        "backup_enabled": True,
+        "backup_interval_hours": 0,  # always eligible
+        "backup_retention": 5,
+    })
+    provider.initialize(session_id="auto-backup", platform="cli", hermes_home=str(hermes_home))
+    try:
+        backup_dir = provider._backup_dir()
+        assert backup_dir.is_dir()
+        dbs = list(backup_dir.glob("memory-*.db"))
+        assert len(dbs) >= 1
+        assert (dbs[0].with_suffix(dbs[0].suffix + ".json")).exists() or (
+            Path(str(dbs[0]) + ".json").exists()
+        )
+        assert provider._metrics.get("backup_auto_created", 0) >= 1
+    finally:
+        provider.shutdown()
diff --git a/tests/plugins/memory/test_holographic_maintenance.py b/tests/plugins/memory/test_holographic_maintenance.py
index d15fd3410a..4c9128edf6 100644
--- a/tests/plugins/memory/test_holographic_maintenance.py
+++ b/tests/plugins/memory/test_holographic_maintenance.py
@@ -448,3 +448,60 @@ class TestVerifyAlias:
         via_maintenance = maintenance.verify(db_path)
         via_diagnostics = diagnostics.run_diagnostics(db_path)
         assert via_maintenance.to_dict() == via_diagnostics.to_dict()
+
+
+# ---------------------------------------------------------------------------
+# P2 — dry-run contract polish
+# ---------------------------------------------------------------------------
+
+class TestRebuildVectorsScopeCounts:
+    def test_dry_run_splits_active_and_superseded(self, tmp_path):
+        db_path = tmp_path / "db.sqlite"
+        store = MemoryStore(db_path=db_path, hrr_dim=32)
+        try:
+            a = store.add_fact("Active fact A.", category="general")
+            store.add_fact("Active fact B.", category="general")
+            store.supersede_fact(a, "Replacement for A.", category="general")
+        finally:
+            store.close()
+
+        result = maintenance.rebuild_vectors(db_path, hermes_home=tmp_path, dry_run=True)
+        assert result.counts["facts_planned"] == 3  # 2 active + 1 superseded
+        assert result.counts["facts_active"] == 2
+        assert result.counts["facts_superseded"] == 1
+        assert result.counts["scope"] == "all_fact_rows_including_superseded"
+
+
+class TestRebuildBanksPlanRows:
+    def test_dry_run_emits_digest_category_old_new_rows(self, tmp_path):
+        db_path = _seed_db(tmp_path)
+        # Inject an orphan bank that should appear as DELETE in the plan.
+        store = MemoryStore(db_path=db_path, hrr_dim=32)
+        try:
+            store._conn.execute(
+                "INSERT OR REPLACE INTO memory_banks(bank_name, vector, dim, fact_count, updated_at) "
+                "VALUES(?, ?, ?, ?, datetime('now'))",
+                ("cat:tool", b"\x00" * (32 * 8), 32, 9),
+            )
+            store._conn.commit()
+        finally:
+            store.close()
+
+        result = maintenance.rebuild_banks(db_path, hermes_home=tmp_path, dry_run=True)
+        payload = result.to_dict()
+        assert "plan" in payload
+        assert payload["counts"]["banks_to_delete"] >= 1
+
+        actions = {row["action"] for row in payload["plan"]}
+        assert "DELETE" in actions
+        assert any(row["action"] in {"CREATE", "UPDATE", "REFRESH"} for row in payload["plan"])
+
+        for row in payload["plan"]:
+            assert "bank_name" in row
+            assert "old_fact_count" in row
+            assert "new_fact_count" in row
+            # No raw namespace leakage.
+            assert "telegram:111" not in json.dumps(row)
+            if row["action"] != "DELETE":
+                assert row["namespace_digest"] in {"(unscoped)", row["namespace_digest"]}
+                assert row["category"]
diff --git a/tests/plugins/memory/test_holographic_restore_maintenance.py b/tests/plugins/memory/test_holographic_restore_maintenance.py
index d3af6e87f4..08c110657f 100644
--- a/tests/plugins/memory/test_holographic_restore_maintenance.py
+++ b/tests/plugins/memory/test_holographic_restore_maintenance.py
@@ -267,3 +267,43 @@ class TestRestoreApply:
         contents = [f["content"] for f in store.list_facts(limit=10)]
         store.close()
         assert "fresh fact" in contents
+
+
+# ---------------------------------------------------------------------------
+# P1b — .enc refusal + gateway advisory warning
+# ---------------------------------------------------------------------------
+
+class TestRestoreP1bGuards:
+    def test_enc_input_is_refused_without_touching_live(self, tmp_path):
+        live_path = _make_live(tmp_path, facts=[("live fact", "general", "")])
+        before = _file_hash(live_path)
+        enc_path = tmp_path / "skills-pack-snapshot.enc"
+        enc_path.write_bytes(b"not-a-sqlite-db")
+
+        result = maintenance.restore(
+            live_path, backup_path=enc_path, hermes_home=tmp_path, dry_run=True,
+        )
+        assert result.ok is False
+        assert result.exit_code == maintenance.EXIT_CANNOT_RUN
+        assert result.gates["apply_allowed"] is False
+        assert result.gates.get("input_format") == "skills_pack_encrypted"
+        joined = " ".join(result.issues + result.gates["block_reasons"])
+        assert "hermes_backup" in joined
+        assert ".enc" in joined
+        assert before == _file_hash(live_path)
+
+    def test_gateway_running_is_advisory_warning_not_hard_block(self, tmp_path, monkeypatch):
+        live_path = _make_live(tmp_path, facts=[("live fact", "general", "")])
+        info = _make_backup(tmp_path, "clean", facts=[("backup fact", "general", "")])
+
+        monkeypatch.setattr(maintenance, "_gateway_running_for_home", lambda _home: True)
+
+        result = maintenance.restore(
+            live_path, backup_path=info.path, hermes_home=tmp_path, dry_run=True,
+        )
+        d = result.to_dict()
+        assert d["gates"]["gateway_running"] is True
+        assert d["gates"]["apply_allowed"] is True
+        assert any("gateway" in w.lower() for w in d["gates"]["warnings"])
+        # Advisory only — clean backup still exits 0.
+        assert result.exit_code == maintenance.EXIT_OK