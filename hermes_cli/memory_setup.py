"""hermes memory setup|status — configure memory provider plugins.

Auto-detects installed memory providers via the plugin system.
Interactive curses-based UI for provider selection, then walks through
the provider's config schema. Writes config to config.yaml + .env.
"""

from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

from hermes_constants import get_hermes_home


# ---------------------------------------------------------------------------
# Curses-based interactive picker (same pattern as hermes tools)
# ---------------------------------------------------------------------------

def _curses_select(title: str, items: list[tuple[str, str]], default: int = 0) -> int:
    """Interactive single-select with arrow keys.

    items: list of (label, description) tuples.
    Returns selected index, or default on escape/quit.
    """
    try:
        import curses
        result = [default]

        def _menu(stdscr):
            curses.curs_set(0)
            if curses.has_colors():
                curses.start_color()
                curses.use_default_colors()
                curses.init_pair(1, curses.COLOR_GREEN, -1)
                curses.init_pair(2, curses.COLOR_YELLOW, -1)
                curses.init_pair(3, curses.COLOR_CYAN, -1)
            cursor = default

            while True:
                stdscr.clear()
                max_y, max_x = stdscr.getmaxyx()

                # Title
                try:
                    stdscr.addnstr(0, 0, title, max_x - 1,
                                   curses.A_BOLD | (curses.color_pair(2) if curses.has_colors() else 0))
                    stdscr.addnstr(1, 0, "  ↑↓ navigate  ⏎ select  q quit", max_x - 1,
                                   curses.color_pair(3) if curses.has_colors() else curses.A_DIM)
                except curses.error:
                    pass

                for i, (label, desc) in enumerate(items):
                    y = i + 3
                    if y >= max_y - 1:
                        break
                    arrow = "→" if i == cursor else " "
                    line = f" {arrow}  {label}"
                    if desc:
                        line += f"  {desc}"

                    attr = curses.A_NORMAL
                    if i == cursor:
                        attr = curses.A_BOLD
                        if curses.has_colors():
                            attr |= curses.color_pair(1)
                    try:
                        stdscr.addnstr(y, 0, line[:max_x - 1], max_x - 1, attr)
                    except curses.error:
                        pass

                stdscr.refresh()
                key = stdscr.getch()

                if key in (curses.KEY_UP, ord('k')):
                    cursor = (cursor - 1) % len(items)
                elif key in (curses.KEY_DOWN, ord('j')):
                    cursor = (cursor + 1) % len(items)
                elif key in (curses.KEY_ENTER, 10, 13):
                    result[0] = cursor
                    return
                elif key in (27, ord('q')):
                    return

        curses.wrapper(_menu)
        return result[0]

    except Exception:
        # Fallback: numbered input
        print(f"\n  {title}\n")
        for i, (label, desc) in enumerate(items):
            marker = "→" if i == default else " "
            d = f"  {desc}" if desc else ""
            print(f"  {marker} {i + 1}. {label}{d}")
        while True:
            try:
                val = input(f"\n  Select [1-{len(items)}] ({default + 1}): ")
                if not val:
                    return default
                idx = int(val) - 1
                if 0 <= idx < len(items):
                    return idx
            except (ValueError, EOFError):
                return default


def _prompt(label: str, default: str | None = None, secret: bool = False) -> str:
    """Prompt for a value with optional default and secret masking."""
    suffix = f" [{default}]" if default else ""
    if secret:
        sys.stdout.write(f"  {label}{suffix}: ")
        sys.stdout.flush()
        if sys.stdin.isatty():
            val = getpass.getpass(prompt="")
        else:
            val = sys.stdin.readline().strip()
    else:
        sys.stdout.write(f"  {label}{suffix}: ")
        sys.stdout.flush()
        val = sys.stdin.readline().strip()
    return val or (default or "")


# ---------------------------------------------------------------------------
# Provider discovery
# ---------------------------------------------------------------------------

def _install_dependencies(provider_name: str) -> None:
    """Install pip dependencies declared in plugin.yaml."""
    import subprocess
    from pathlib import Path as _Path

    plugin_dir = _Path(__file__).parent.parent / "plugins" / "memory" / provider_name
    yaml_path = plugin_dir / "plugin.yaml"
    if not yaml_path.exists():
        return

    try:
        import yaml
        with open(yaml_path) as f:
            meta = yaml.safe_load(f) or {}
    except Exception:
        return

    pip_deps = meta.get("pip_dependencies", [])
    if not pip_deps:
        return

    # pip name → import name mapping for packages where they differ
    _IMPORT_NAMES = {
        "honcho-ai": "honcho",
        "mem0ai": "mem0",
        "hindsight-client": "hindsight_client",
        "hindsight-all": "hindsight",
    }

    # Check which packages are missing
    missing = []
    for dep in pip_deps:
        import_name = _IMPORT_NAMES.get(dep, dep.replace("-", "_").split("[")[0])
        try:
            __import__(import_name)
        except ImportError:
            missing.append(dep)

    if not missing:
        return

    print(f"\n  Installing dependencies: {', '.join(missing)}")

    import shutil
    uv_path = shutil.which("uv")
    if not uv_path:
        print("  ⚠ uv not found — cannot install dependencies")
        print("  Install uv: curl -LsSf https://astral.sh/uv/install.sh | sh")
        print("  Then re-run: hermes memory setup")
        return

    try:
        subprocess.run(
            [uv_path, "pip", "install", "--python", sys.executable, "--quiet"] + missing,
            check=True, timeout=120,
            capture_output=True,
        )
        print(f"  ✓ Installed {', '.join(missing)}")
    except subprocess.CalledProcessError as e:
        print(f"  ⚠ Failed to install {', '.join(missing)}")
        stderr = (e.stderr or b"").decode()[:200]
        if stderr:
            print(f"    {stderr}")
        print(f"  Run manually: uv pip install --python {sys.executable} {' '.join(missing)}")
    except Exception as e:
        print(f"  ⚠ Install failed: {e}")
        print(f"  Run manually: uv pip install --python {sys.executable} {' '.join(missing)}")

    # Also show external dependencies (non-pip) if any
    ext_deps = meta.get("external_dependencies", [])
    for dep in ext_deps:
        dep_name = dep.get("name", "")
        check_cmd = dep.get("check", "")
        install_cmd = dep.get("install", "")
        if check_cmd:
            try:
                subprocess.run(
                    check_cmd, shell=True, capture_output=True, timeout=5
                )
            except Exception:
                if install_cmd:
                    print(f"\n  ⚠ '{dep_name}' not found. Install with:")
                    print(f"    {install_cmd}")


def _get_available_providers() -> list:
    """Discover memory providers from plugins/memory/.

    Returns list of (name, description, provider_instance) tuples.
    """
    try:
        from plugins.memory import discover_memory_providers, load_memory_provider
        raw = discover_memory_providers()
    except Exception:
        raw = []

    results = []
    for name, desc, available in raw:
        try:
            provider = load_memory_provider(name)
            if not provider:
                continue
        except Exception:
            continue

        schema = provider.get_config_schema() if hasattr(provider, "get_config_schema") else []
        has_secrets = any(f.get("secret") for f in schema)
        has_non_secrets = any(not f.get("secret") for f in schema)
        if has_secrets and has_non_secrets:
            setup_hint = "API key / local"
        elif has_secrets:
            setup_hint = "requires API key"
        elif not schema:
            setup_hint = "no setup needed"
        else:
            setup_hint = "local"

        results.append((name, setup_hint, provider))
    return results


# ---------------------------------------------------------------------------
# Setup wizard
# ---------------------------------------------------------------------------

def cmd_setup_provider(provider_name: str) -> None:
    """Run memory setup for a specific provider, skipping the picker."""
    from hermes_cli.config import load_config, save_config

    providers = _get_available_providers()
    match = None
    for name, desc, provider in providers:
        if name == provider_name:
            match = (name, desc, provider)
            break

    if not match:
        print(f"\n  Memory provider '{provider_name}' not found.")
        print("  Run 'hermes memory setup' to see available providers.\n")
        return

    name, _, provider = match

    _install_dependencies(name)

    config = load_config()
    if not isinstance(config.get("memory"), dict):
        config["memory"] = {}

    if hasattr(provider, "post_setup"):
        hermes_home = str(get_hermes_home())
        provider.post_setup(hermes_home, config)
        return

    # Fallback: generic schema-based setup (same as cmd_setup)
    config["memory"]["provider"] = name
    save_config(config)
    print(f"\n  Memory provider: {name}")
    print("  Activation saved to config.yaml\n")


def cmd_setup(args) -> None:
    """Interactive memory provider setup wizard."""
    from hermes_cli.config import load_config, save_config

    providers = _get_available_providers()

    if not providers:
        print("\n  No memory provider plugins detected.")
        print("  Install a plugin to ~/.hermes/plugins/ and try again.\n")
        return

    # Build picker items
    items = []
    for name, desc, _ in providers:
        items.append((name, f"— {desc}"))
    items.append(("Built-in only", "— MEMORY.md / USER.md (default)"))

    builtin_idx = len(items) - 1
    selected = _curses_select("Memory provider setup", items, default=builtin_idx)

    config = load_config()
    if not isinstance(config.get("memory"), dict):
        config["memory"] = {}

    # Built-in only
    if selected >= len(providers) or selected < 0:
        config["memory"]["provider"] = ""
        save_config(config)
        print("\n  ✓ Memory provider: built-in only")
        print("  Saved to config.yaml\n")
        return

    name, _, provider = providers[selected]

    # Install pip dependencies if declared in plugin.yaml
    _install_dependencies(name)

    # If the provider has a post_setup hook, delegate entirely to it.
    # The hook handles its own config, connection test, and activation.
    if hasattr(provider, "post_setup"):
        hermes_home = str(get_hermes_home())
        provider.post_setup(hermes_home, config)
        return

    schema = provider.get_config_schema() if hasattr(provider, "get_config_schema") else []

    provider_config = config["memory"].get(name, {})
    if not isinstance(provider_config, dict):
        provider_config = {}

    env_path = get_hermes_home() / ".env"
    env_writes = {}

    if schema:
        print(f"\n  Configuring {name}:\n")

        for field in schema:
            key = field["key"]
            desc = field.get("description", key)
            default = field.get("default")
            # Dynamic default: look up default from another field's value
            default_from = field.get("default_from")
            if default_from and isinstance(default_from, dict):
                ref_field = default_from.get("field", "")
                ref_map = default_from.get("map", {})
                ref_value = provider_config.get(ref_field, "")
                if ref_value and ref_value in ref_map:
                    default = ref_map[ref_value]
            is_secret = field.get("secret", False)
            choices = field.get("choices")
            env_var = field.get("env_var")
            url = field.get("url")

            # Skip fields whose "when" condition doesn't match
            when = field.get("when")
            if when and isinstance(when, dict):
                if not all(provider_config.get(k) == v for k, v in when.items()):
                    continue

            if choices and not is_secret:
                # Use curses picker for choice fields
                choice_items = [(c, "") for c in choices]
                current = provider_config.get(key, default)
                current_idx = 0
                if current and current in choices:
                    current_idx = choices.index(current)
                sel = _curses_select(f"  {desc}", choice_items, default=current_idx)
                provider_config[key] = choices[sel]
            elif is_secret:
                # Prompt for secret
                existing = os.environ.get(env_var, "") if env_var else ""
                if existing:
                    masked = f"...{existing[-4:]}" if len(existing) > 4 else "set"
                    val = _prompt(f"{desc} (current: {masked}, blank to keep)", secret=True)
                else:
                    hint = f"  Get yours at {url}" if url else ""
                    if hint:
                        print(hint)
                    val = _prompt(desc, secret=True)
                if val and env_var:
                    env_writes[env_var] = val
            else:
                # Regular text prompt
                current = provider_config.get(key)
                effective_default = current or default
                val = _prompt(desc, default=str(effective_default) if effective_default else None)
                if val:
                    provider_config[key] = val

    # Write activation key to config.yaml
    config["memory"]["provider"] = name
    save_config(config)

    # Write non-secret config to provider's native location
    hermes_home = str(get_hermes_home())
    if provider_config and hasattr(provider, "save_config"):
        try:
            provider.save_config(provider_config, hermes_home)
        except Exception as e:
            print(f"  Failed to write provider config: {e}")

    # Write secrets to .env
    if env_writes:
        _write_env_vars(env_path, env_writes)

    print(f"\n  Memory provider: {name}")
    print("  Activation saved to config.yaml")
    if provider_config:
        print("  Provider config saved")
    if env_writes:
        print("  API keys saved to .env")
    print("\n  Start a new session to activate.\n")


def _write_env_vars(env_path: Path, env_writes: dict) -> None:
    """Append or update env vars in .env file."""
    env_path.parent.mkdir(parents=True, exist_ok=True)

    existing_lines = []
    if env_path.exists():
        existing_lines = env_path.read_text().splitlines()

    updated_keys = set()
    new_lines = []
    for line in existing_lines:
        key_match = line.split("=", 1)[0].strip() if "=" in line else ""
        if key_match in env_writes:
            new_lines.append(f"{key_match}={env_writes[key_match]}")
            updated_keys.add(key_match)
        else:
            new_lines.append(line)

    for key, val in env_writes.items():
        if key not in updated_keys:
            new_lines.append(f"{key}={val}")

    env_path.write_text("\n".join(new_lines) + "\n")


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def cmd_status(args) -> None:
    """Show current memory provider config."""
    from hermes_cli.config import load_config

    config = load_config()
    mem_config = config.get("memory", {})
    provider_name = mem_config.get("provider", "")

    print("\nMemory status\n" + "─" * 40)
    print("  Built-in:  always active")
    print(f"  Provider:  {provider_name or '(none — built-in only)'}")

    if provider_name:
        provider_config = mem_config.get(provider_name, {})
        if provider_config:
            print(f"\n  {provider_name} config:")
            for key, val in provider_config.items():
                print(f"    {key}: {val}")

        providers = _get_available_providers()
        found = any(name == provider_name for name, _, _ in providers)
        if found:
            print("\n  Plugin:    installed ✓")
            for pname, _, p in providers:
                if pname == provider_name:
                    if p.is_available():
                        print("  Status:    available ✓")
                    else:
                        print("  Status:    not available ✗")
                        schema = p.get_config_schema() if hasattr(p, "get_config_schema") else []
                        secrets = [f for f in schema if f.get("secret")]
                        if secrets:
                            print("  Missing:")
                            for s in secrets:
                                env_var = s.get("env_var", "")
                                url = s.get("url", "")
                                is_set = bool(os.environ.get(env_var))
                                mark = "✓" if is_set else "✗"
                                line = f"    {mark} {env_var}"
                                if url and not is_set:
                                    line += f"  → {url}"
                                print(line)
                    break
        else:
            print("\n  Plugin:    NOT installed ✗")
            print(f"  Install the '{provider_name}' memory plugin to ~/.hermes/plugins/")

    providers = _get_available_providers()
    if providers:
        print("\n  Installed plugins:")
        for pname, desc, _ in providers:
            active = " ← active" if pname == provider_name else ""
            print(f"    • {pname}  ({desc}){active}")

    print()


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

def memory_command(args) -> None:
    """Route memory subcommands."""
    sub = getattr(args, "memory_command", None)
    if sub == "setup":
        cmd_setup(args)
    elif sub == "status":
        cmd_status(args)
    elif sub == "import":
        cmd_import(args)
    elif sub == "health":
        cmd_health(args)
    elif sub == "benchmark":
        cmd_benchmark(args)
    elif sub == "rebuild-vectors":
        cmd_rebuild_vectors(args)
    elif sub == "rebuild-banks":
        cmd_rebuild_banks(args)
    elif sub == "backfill-relationships":
        cmd_backfill_relationships(args)
    elif sub == "verify":
        cmd_verify(args)
    elif sub == "viewer":
        cmd_viewer(args)
    else:
        cmd_status(args)


def _holographic_db_path() -> Path:
    """Resolve the active profile's Holographic memory database path.

    Reuses the same `$HERMES_HOME` substitution the provider itself applies
    in `HolographicMemoryProvider.initialize()` — never hardcodes
    `~/.hermes`. Honors a custom `db_path` set under
    `plugins.hermes-memory-store` in config.yaml, if present.
    """
    from plugins.memory.holographic import _DEFAULT_DB_PATH, _load_plugin_config

    hermes_home = str(get_hermes_home())
    plugin_config = _load_plugin_config()
    db_path = plugin_config.get("db_path", _DEFAULT_DB_PATH)
    if isinstance(db_path, str):
        db_path = db_path.replace("$HERMES_HOME", hermes_home).replace("${HERMES_HOME}", hermes_home)
    return Path(db_path).expanduser()


def cmd_health(args) -> None:
    """`hermes memory health [--json] [--backup-path PATH]` — read-only.

    Resolves the database for the currently active profile (default or
    named via the global `-p`/`--profile` flag, which has already set
    HERMES_HOME before this module was imported) and never accepts an
    arbitrary namespace or raw chat identity.
    """
    import json as _json
    import sys as _sys

    from plugins.memory.holographic import diagnostics as _diagnostics

    db_path = _holographic_db_path()
    as_json = bool(getattr(args, "json", False))
    backup_path = getattr(args, "backup_path", None)

    report = _diagnostics.run_diagnostics(db_path, backup_path=backup_path)

    if as_json:
        print(_json.dumps(report.to_dict(), indent=2))
    else:
        from hermes_constants import display_hermes_home

        print(f"\n  Holographic memory health ({display_hermes_home()})")
        print("  " + "─" * 40)
        print(f"  Database: {db_path}")
        print(f"  Status:   {'OK' if report.ok else 'ISSUES FOUND'}")
        for key, value in report.counts.items():
            print(f"  {key}: {value}")
        if report.namespaces:
            print(f"  Namespaces: {len(report.namespaces)} (opaque labels only)")
        if report.issues:
            print("\n  Issues:")
            for issue in report.issues:
                print(f"    [{issue.severity}] {issue.code}: {issue.message}")
        if report.backup is not None:
            print(f"\n  Backup check: {'ok' if report.backup['ok'] else report.backup['reason']}")
        print()

    _sys.exit(report.exit_code)


def cmd_benchmark(args) -> None:
    """`hermes memory benchmark --gold-set PATH [--json]` — read-only.

    Never mutates retrieval counters, trust scores, or feedback (see
    plugins/memory/holographic/benchmark.py). Operates against the active
    profile's database only; does not accept an arbitrary namespace.
    """
    import json as _json
    import sys as _sys

    from plugins.memory.holographic import benchmark as _benchmark

    db_path = _holographic_db_path()
    gold_set_path = getattr(args, "gold_set", None)
    as_json = bool(getattr(args, "json", False))

    if not db_path.exists():
        message = f"Holographic memory database not found: {db_path}"
        if as_json:
            print(_json.dumps({"error": message}))
        else:
            print(f"\n  {message}\n")
        _sys.exit(3)

    try:
        report = _benchmark.run_benchmark(db_path, gold_set_path)
    except _benchmark.BenchmarkError as exc:
        message = str(exc)
        if as_json:
            print(_json.dumps({"error": message}))
        else:
            print(f"\n  Benchmark failed: {message}\n")
        _sys.exit(2)

    if as_json:
        print(_json.dumps(report, indent=2))
    else:
        print("\n  Holographic memory benchmark")
        print("  " + "─" * 40)
        print(f"  Database:  {report['database']}")
        print(f"  Gold set:  {report['gold_set']}")
        print(f"  Queries:   {report['query_count']}")
        print(f"  Recall@1:  {report['recall_at_1']:.2%}")
        print(f"  Recall@5:  {report['recall_at_5']:.2%}")
        print(f"  MRR:       {report['mean_reciprocal_rank']:.3f}")
        print(f"  Empty-result rate: {report['empty_result_rate']:.2%}")
        print(f"  Query-error rate:  {report['query_error_rate']:.2%}")
        print(f"  Namespace leakage: {report['namespace_leakage_count']}")
        print(f"  Latency p50/p95 (ms): {report['latency_ms']['p50']} / {report['latency_ms']['p95']}")
        print()

    _sys.exit(0)


def _print_maintenance_target(db_path: Path, *, as_json: bool, apply: bool) -> None:
    """Print the exact target before proceeding — required by
    docs/plans/demo-reference-plan/implementation-plan.md Phase 6
    ("Print the exact target profile/path before confirmation"). There is
    no separate interactive confirmation prompt: passing `--apply` IS the
    confirmation (it is already an explicit, deliberate flag), so this is
    the only thing printed before a write proceeds — in both text and
    JSON mode the resolved `db_path` is always present in the final
    result payload too, so an operator can verify what was touched even
    after the fact.
    """
    if as_json:
        return
    from hermes_constants import display_hermes_home

    mode = "APPLY" if apply else "DRY RUN"
    print(f"\n  Holographic memory maintenance ({display_hermes_home()}) — {mode}")
    print(f"  Target database: {db_path}\n")


def cmd_rebuild_vectors(args) -> None:
    """`hermes memory rebuild-vectors [--dry-run]`.

    Recomputes HRR vectors for every active fact in the current profile's
    database. Defaults to dry-run unless the caller explicitly asks to
    write — see the `--dry-run`/no-flag handling below.
    """
    import json as _json
    import sys as _sys

    from plugins.memory.holographic import maintenance as _maintenance

    db_path = _holographic_db_path()
    as_json = bool(getattr(args, "json", False))
    dry_run = bool(getattr(args, "dry_run", True))

    if not db_path.exists():
        message = f"Holographic memory database not found: {db_path}"
        if as_json:
            print(_json.dumps({"ok": False, "error": message}))
        else:
            print(f"\n  {message}\n")
        _sys.exit(_maintenance.EXIT_CANNOT_RUN)

    _print_maintenance_target(db_path, as_json=as_json, apply=not dry_run)

    try:
        result = _maintenance.rebuild_vectors(
            db_path, hermes_home=get_hermes_home(), dry_run=dry_run,
        )
    except _maintenance.MaintenanceLockError as exc:
        if as_json:
            print(_json.dumps({"ok": False, "error": str(exc)}))
        else:
            print(f"\n  {exc}\n")
        _sys.exit(_maintenance.EXIT_LOCKED)

    _emit_maintenance_result(result, as_json=as_json)
    _sys.exit(result.exit_code)


def cmd_rebuild_banks(args) -> None:
    """`hermes memory rebuild-banks [--dry-run]`."""
    import json as _json
    import sys as _sys

    from plugins.memory.holographic import maintenance as _maintenance

    db_path = _holographic_db_path()
    as_json = bool(getattr(args, "json", False))
    dry_run = bool(getattr(args, "dry_run", True))

    if not db_path.exists():
        message = f"Holographic memory database not found: {db_path}"
        if as_json:
            print(_json.dumps({"ok": False, "error": message}))
        else:
            print(f"\n  {message}\n")
        _sys.exit(_maintenance.EXIT_CANNOT_RUN)

    _print_maintenance_target(db_path, as_json=as_json, apply=not dry_run)

    try:
        result = _maintenance.rebuild_banks(
            db_path, hermes_home=get_hermes_home(), dry_run=dry_run,
        )
    except _maintenance.MaintenanceLockError as exc:
        if as_json:
            print(_json.dumps({"ok": False, "error": str(exc)}))
        else:
            print(f"\n  {exc}\n")
        _sys.exit(_maintenance.EXIT_LOCKED)

    _emit_maintenance_result(result, as_json=as_json)
    _sys.exit(result.exit_code)


def cmd_backfill_relationships(args) -> None:
    """`hermes memory backfill-relationships --dry-run|--apply`.

    Requires an explicit choice: neither flag defaults to a write. See
    plugins/memory/holographic/maintenance.py's module docstring for the
    deterministic co-occurrence extraction heuristic this wraps.
    """
    import json as _json
    import sys as _sys

    from plugins.memory.holographic import maintenance as _maintenance

    db_path = _holographic_db_path()
    as_json = bool(getattr(args, "json", False))
    apply = bool(getattr(args, "apply", False))
    dry_run_flag = bool(getattr(args, "dry_run", False))

    if not apply and not dry_run_flag:
        message = "backfill-relationships requires either --dry-run or --apply"
        if as_json:
            print(_json.dumps({"ok": False, "error": message}))
        else:
            print(f"\n  {message}\n")
        _sys.exit(_maintenance.EXIT_CANNOT_RUN)

    if not db_path.exists():
        message = f"Holographic memory database not found: {db_path}"
        if as_json:
            print(_json.dumps({"ok": False, "error": message}))
        else:
            print(f"\n  {message}\n")
        _sys.exit(_maintenance.EXIT_CANNOT_RUN)

    _print_maintenance_target(db_path, as_json=as_json, apply=apply)

    try:
        result = _maintenance.backfill_relationships(
            db_path, hermes_home=get_hermes_home(), apply=apply,
        )
    except _maintenance.MaintenanceLockError as exc:
        if as_json:
            print(_json.dumps({"ok": False, "error": str(exc)}))
        else:
            print(f"\n  {exc}\n")
        _sys.exit(_maintenance.EXIT_LOCKED)

    _emit_maintenance_result(result, as_json=as_json)
    _sys.exit(result.exit_code)


def _emit_maintenance_result(result, *, as_json: bool) -> None:
    import json as _json

    if as_json:
        print(_json.dumps(result.to_dict(), indent=2))
        return

    print(f"  Operation: {result.operation}  ({'dry-run' if result.dry_run else 'applied'})")
    print(f"  Status:    {'OK' if result.ok else 'FAILED'}")
    for key, value in result.counts.items():
        print(f"  {key}: {value}")
    if result.backup is not None:
        print(f"  Backup:    {'ok' if result.backup['ok'] else result.backup.get('reason', 'failed')}")
    if result.issues:
        print("\n  Issues:")
        for issue in result.issues:
            print(f"    {issue}")
    print()


def cmd_verify(args) -> None:
    """`hermes memory verify [--json]` — thin alias for `hermes memory
    health` (see maintenance.verify's docstring for why this is an alias
    rather than a duplicate check implementation). Read-only."""
    import json as _json
    import sys as _sys

    from plugins.memory.holographic import maintenance as _maintenance

    db_path = _holographic_db_path()
    as_json = bool(getattr(args, "json", False))
    backup_path = getattr(args, "backup_path", None)

    report = _maintenance.verify(db_path, backup_path=backup_path)

    if as_json:
        print(_json.dumps(report.to_dict(), indent=2))
    else:
        from hermes_constants import display_hermes_home

        print(f"\n  Holographic memory verify ({display_hermes_home()})")
        print("  " + "─" * 40)
        print(f"  Database: {db_path}")
        print(f"  Status:   {'OK' if report.ok else 'ISSUES FOUND'}")
        for key, value in report.counts.items():
            print(f"  {key}: {value}")
        if report.issues:
            print("\n  Issues:")
            for issue in report.issues:
                print(f"    [{issue.severity}] {issue.code}: {issue.message}")
        print()

    _sys.exit(report.exit_code)


def cmd_viewer(args) -> None:
    """`hermes memory viewer [--port N] [--host HOST] [--show-raw-identity]`.

    Starts the optional, read-only, local-only Holographic memory
    diagnostic viewer (Phase 7 of
    docs/plans/demo-reference-plan/implementation-plan.md) against the
    currently active profile's database — resolved through the same
    `_holographic_db_path()` helper every other `hermes memory` subcommand
    uses. There is no way to point this at "all profiles" or an arbitrary
    namespace; see `plugins/memory/holographic/viewer.py`'s module
    docstring for the full safety model (loopback-only binding, read-only
    SQLite connections, opaque namespace digests, display redaction,
    pagination caps).

    Blocks (serves requests) until interrupted with Ctrl+C. Never invoked
    automatically by normal Hermes startup, and not reachable from any
    gateway/chat slash command.
    """
    import sys as _sys

    from hermes_constants import display_hermes_home
    from plugins.memory.holographic import viewer as _viewer

    db_path = _holographic_db_path()
    if not db_path.exists():
        print(f"\n  Holographic memory database not found: {db_path}\n")
        _sys.exit(3)

    host = getattr(args, "host", None) or "127.0.0.1"
    port = int(getattr(args, "port", 8787) or 8787)
    show_raw_identity = bool(getattr(args, "show_raw_identity", False))

    try:
        server = _viewer.create_server(db_path, host=host, port=port, show_raw_identity=show_raw_identity)
    except _viewer.NonLoopbackBindingNotSupported as exc:
        print(f"\n  {exc}\n")
        _sys.exit(3)

    bound_host, bound_port = server.server_address[:2]
    print(f"\n  Holographic memory viewer ({display_hermes_home()})")
    print(f"  Database:  {db_path}")
    print(f"  Read-only. No restore, raw SQL, or mutation endpoint exists.")
    print(f"  Serving:   http://{bound_host}:{bound_port}/")
    if show_raw_identity:
        print("  WARNING:   --show-raw-identity is enabled — namespace pages will show raw chat identities.")
    print("  Press Ctrl+C to stop.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()


def cmd_import(args) -> None:
    """Preview/apply an explicit built-in → Holographic migration."""
    if getattr(args, "import_source", None) != "builtin" or getattr(args, "provider", None) != "holographic":
        raise SystemExit("Only --from builtin --provider holographic is currently supported")

    from plugins.memory.holographic import HolographicMemoryProvider

    provider = HolographicMemoryProvider()
    provider.initialize(session_id="memory-import", platform="cli", agent_context="primary")
    try:
        result = provider.import_builtin_memories(apply=bool(getattr(args, "apply", False)))
    finally:
        provider.shutdown()

    mode = "APPLY" if result["apply"] else "DRY RUN"
    print(f"\n  Holographic memory import ({mode})")
    print(f"  Namespace: {result['namespace']}")
    for source in result["sources"]:
        print(f"  Source: {source['path']} ({source['entries']} entries)")
    for label in ("proposed", "duplicates", "rejected", "imported"):
        items = result[label]
        print(f"\n  {label.title()} ({len(items)}):")
        for item in items:
            suffix = f" — {item['reason']}" if item.get("reason") else ""
            print(f"    • [{item['source']}] {item['content']}{suffix}")
    if not result["apply"] and result["proposed"]:
        print("\n  No changes made. Re-run with --apply to import proposed entries.")
    print()
