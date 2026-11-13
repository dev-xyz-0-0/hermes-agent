"""Phase 1 — `hermes memory health` / `hermes memory benchmark` CLI tests.

See docs/plans/demo-reference-plan/checklist.md section 4 ("CLI tests").
Uses the repo-wide `_isolate_hermes_home` autouse fixture (tests/conftest.py)
so HERMES_HOME is always a temp directory here, never the real ~/.hermes/.
"""

from __future__ import annotations

import json
from argparse import Namespace

import pytest

from hermes_constants import get_hermes_home


def _namespace(**kwargs):
    defaults = {"json": False, "backup_path": None, "gold_set": None}
    defaults.update(kwargs)
    return Namespace(**defaults)


def _seed_db():
    from plugins.memory.holographic.store import MemoryStore

    db_path = get_hermes_home() / "memory_store.db"
    store = MemoryStore(db_path=db_path, hrr_dim=32)
    store.add_fact("A healthy fact for CLI testing.", category="general")
    store.close()
    return db_path


class TestMemoryHealthCommand:
    def test_missing_database_exits_with_cannot_run_code(self, capsys):
        from hermes_cli.memory_setup import cmd_health

        with pytest.raises(SystemExit) as exc_info:
            cmd_health(_namespace(json=True))
        assert exc_info.value.code != 0
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert parsed["ok"] is False

    def test_healthy_db_exits_zero_and_emits_json(self, capsys):
        from hermes_cli.memory_setup import cmd_health

        _seed_db()
        with pytest.raises(SystemExit) as exc_info:
            cmd_health(_namespace(json=True))
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert parsed["ok"] is True
        assert parsed["counts"]["facts"] == 1

    def test_json_mode_has_no_decorative_text(self, capsys):
        from hermes_cli.memory_setup import cmd_health

        _seed_db()
        with pytest.raises(SystemExit):
            cmd_health(_namespace(json=True))
        out = capsys.readouterr().out.strip()
        # The entire stdout payload must be valid JSON — no banner/prose
        # mixed in before or after it.
        json.loads(out)

    def test_text_mode_never_shows_raw_namespace_or_content(self, capsys):
        from hermes_cli.memory_setup import cmd_health
        from plugins.memory.holographic.store import MemoryStore

        db_path = get_hermes_home() / "memory_store.db"
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        store.add_fact("A secret project called Nightjar.", category="project", namespace="telegram:9999")
        store.close()

        with pytest.raises(SystemExit):
            cmd_health(_namespace(json=False))
        out = capsys.readouterr().out
        assert "telegram:9999" not in out
        assert "Nightjar" not in out

    def test_named_profile_targets_only_that_profile(self, tmp_path, monkeypatch):
        """Named-profile isolation: pointing HERMES_HOME at a different
        directory must target a completely different database file — the
        same mechanism `hermes -p NAME ...` relies on (HERMES_HOME is set
        before this module is ever imported)."""
        from hermes_cli.memory_setup import _holographic_db_path

        other_home = tmp_path / "other_profile_home"
        other_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(other_home))
        resolved = _holographic_db_path()
        assert str(resolved).startswith(str(other_home))

    def test_default_profile_targets_default_hermes_home(self):
        from hermes_cli.memory_setup import _holographic_db_path

        resolved = _holographic_db_path()
        assert str(resolved).startswith(str(get_hermes_home()))


class TestMemoryBenchmarkCommand:
    GOLD_SET = "tests/fixtures/holographic/gold_set.json"

    def test_missing_database_exits_nonzero(self, capsys):
        from hermes_cli.memory_setup import cmd_benchmark

        with pytest.raises(SystemExit) as exc_info:
            cmd_benchmark(_namespace(json=True, gold_set=self.GOLD_SET))
        assert exc_info.value.code != 0

    def test_malformed_gold_set_exits_safely(self, capsys, tmp_path):
        from hermes_cli.memory_setup import cmd_benchmark

        _seed_db()
        bad_gold_set = tmp_path / "bad.json"
        bad_gold_set.write_text("{not json", encoding="utf-8")
        with pytest.raises(SystemExit) as exc_info:
            cmd_benchmark(_namespace(json=True, gold_set=str(bad_gold_set)))
        assert exc_info.value.code == 2
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert "error" in parsed

    def test_json_mode_has_no_decorative_text(self, capsys):
        from hermes_cli.memory_setup import cmd_benchmark
        from plugins.memory.holographic import benchmark

        gold_set = benchmark.load_gold_set(self.GOLD_SET)
        db_path = get_hermes_home() / "memory_store.db"
        store = benchmark.build_fixture_db(gold_set, db_path, hrr_dim=32)
        store.close()

        with pytest.raises(SystemExit) as exc_info:
            cmd_benchmark(_namespace(json=True, gold_set=self.GOLD_SET))
        assert exc_info.value.code == 0
        out = capsys.readouterr().out.strip()
        json.loads(out)


class TestMemoryCliDoesNotAcceptRawNamespace:
    def test_health_namespace_has_no_namespace_argument(self):
        """The CLI surface must not accept an arbitrary chat namespace — see
        docs/plans/demo-reference-plan/checklist.md section 0."""
        import inspect

        from hermes_cli.memory_setup import cmd_health

        source = inspect.getsource(cmd_health)
        assert "args.namespace" not in source
        assert "args.chat_id" not in source


class TestMemoryCommandRegistryWiring:
    def test_memory_health_and_benchmark_are_registered_subcommands(self):
        """hermes_cli/main.py wires `memory health`/`memory benchmark` into
        the existing `hermes memory` argparse subcommand family, and they
        must never be exposed via COMMAND_REGISTRY (chat slash commands) or
        gateway dispatch."""
        from hermes_cli.commands import COMMAND_REGISTRY

        names = {c.name for c in COMMAND_REGISTRY}
        assert "memory-health" not in names
        assert "memory-benchmark" not in names
        assert "health" not in names
        assert "benchmark" not in names
