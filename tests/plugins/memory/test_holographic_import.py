"""Explicit migration from profile-scoped built-in memory files."""

from plugins.memory.holographic import HolographicMemoryProvider


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
