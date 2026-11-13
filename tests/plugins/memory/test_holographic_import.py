"""Explicit migration from profile-scoped built-in memory files."""

import pytest

from plugins.memory.holographic import HolographicMemoryProvider
from plugins.memory.holographic import consolidation as _consolidation


def test_builtin_import_is_previewed_applied_and_idempotent(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    memories = hermes_home / "memories"
    memories.mkdir(parents=True)
    (memories / "USER.md").write_text(
        "Uses Python uv for MCP dependencies.\n§\nMy API key is sk-proj-abc123def456ghi789jkl012.",
        encoding="utf-8",
    )
    (memories / "MEMORY.md").write_text("Project tests run with pytest.", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    provider = HolographicMemoryProvider(config={
        "db_path": str(hermes_home / "memory_store.db"),
        "hrr_dim": 32,
        "hrr_weight": 0.0,
    })
    provider.initialize(session_id="import-test", platform="cli")

    preview = provider.import_builtin_memories(apply=False)
    assert preview["counts"] == {"proposed": 2, "duplicates": 0, "rejected": 1, "imported": 0}
    assert provider._store.list_facts(limit=20) == []

    applied = provider.import_builtin_memories(apply=True)
    assert applied["counts"]["imported"] == 2
    assert {fact["category"] for fact in provider._store.list_facts(limit=20)} == {"user_pref", "general"}

    repeated = provider.import_builtin_memories(apply=True)
    assert repeated["counts"]["imported"] == 0
    assert repeated["counts"]["duplicates"] == 2
    provider.shutdown()


def _make_provider(tmp_path, monkeypatch, memory_entries):
    """Set up a provider with MEMORY.md containing the given entries (all
    same category, no duplicates/rejects) and return it."""
    hermes_home = tmp_path / ".hermes"
    memories = hermes_home / "memories"
    memories.mkdir(parents=True)
    (memories / "MEMORY.md").write_text("\n§\n".join(memory_entries), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    provider = HolographicMemoryProvider(config={
        "db_path": str(hermes_home / "memory_store.db"),
        "hrr_dim": 32,
        "hrr_weight": 0.0,
    })
    provider.initialize(session_id="import-test", platform="cli")
    return provider


def test_import_failure_partway_through_rolls_back_everything(tmp_path, monkeypatch):
    """Phase 4's transaction()/defer_bank_rebuilds() wrap should make a
    failing apply=True import atomic: if any entry raises, NONE of the
    entries from that call should be persisted — not just the ones after
    the failure point."""
    entries = [
        "Entry one about the project.",
        "Entry two about the project.",
        "Entry three about the project.",
        "Entry four about the project.",
    ]
    provider = _make_provider(tmp_path, monkeypatch, entries)

    real_classify_and_apply = _consolidation.classify_and_apply
    call_count = {"n": 0}

    def _flaky_classify_and_apply(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 3:
            raise RuntimeError("injected failure on 3rd entry")
        return real_classify_and_apply(*args, **kwargs)

    monkeypatch.setattr(_consolidation, "classify_and_apply", _flaky_classify_and_apply)

    with pytest.raises(RuntimeError, match="injected failure on 3rd entry"):
        provider.import_builtin_memories(apply=True)

    # True rollback: zero facts persisted from this call, not just the
    # entries processed before the failure.
    assert provider._store.list_facts(limit=20) == []
    provider.shutdown()


def test_import_bank_rebuild_count_is_bounded(tmp_path, monkeypatch):
    """Phase 4's defer_bank_rebuilds() wrap should collapse the per-entry
    rebuild_bank() calls that add_fact() triggers into one rebuild per
    affected (namespace, category) pair for the whole import call."""
    entries = [
        "Project tests run with pytest.",
        "The build uses uv for dependency management.",
        "Deployments happen through a GitHub Actions workflow.",
    ]
    provider = _make_provider(tmp_path, monkeypatch, entries)

    rebuild_calls = []
    real_rebuild_bank = provider._store.rebuild_bank
    store = provider._store

    def _counting_rebuild_bank(category, namespace=""):
        # `rebuild_bank()` itself early-returns (just queuing the pair)
        # while a `defer_bank_rebuilds()` block is active; it only does the
        # real recompute work when `_defer_bank_rebuild_depth == 0` at call
        # time (exactly the condition the real method checks). Count only
        # those actual-rebuild invocations, not every queuing call.
        if store._defer_bank_rebuild_depth == 0:
            rebuild_calls.append((namespace, category))
        return real_rebuild_bank(category, namespace)

    monkeypatch.setattr(provider._store, "rebuild_bank", _counting_rebuild_bank)

    applied = provider.import_builtin_memories(apply=True)
    assert applied["counts"]["imported"] == 3

    # All three entries land in the same (namespace, category) pair
    # ("", "general"), so the rebuild should happen exactly once for the
    # whole call rather than once per inserted fact.
    assert rebuild_calls == [("", "general")]
    provider.shutdown()
