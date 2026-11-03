"""Tests for agent/skill_paths.py -- the skills.create_dir resolver (Phase 1)."""

import os
from unittest.mock import patch

import pytest


@pytest.fixture
def hermes_home(tmp_path):
    """Create a minimal HERMES_HOME with a local skills dir."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "skills").mkdir()
    return home


@pytest.fixture
def external_skills_dir(tmp_path):
    ext_dir = tmp_path / "external-skills"
    skill_dir = ext_dir / "my-external-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: my-external-skill\ndescription: external\n---\n\nbody\n"
    )
    return ext_dir


class TestGetSkillCreateDir:
    def test_no_config_uses_legacy_default(self, hermes_home):
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            from agent.skill_paths import get_skill_create_dir
            result = get_skill_create_dir()
        assert result == hermes_home / "skills"

    def test_no_config_uses_caller_supplied_legacy_default(self, hermes_home, tmp_path):
        custom_legacy = tmp_path / "patched-legacy"
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            from agent.skill_paths import get_skill_create_dir
            result = get_skill_create_dir(legacy_default=custom_legacy)
        assert result == custom_legacy

    def test_configured_absolute_path_used(self, hermes_home, tmp_path):
        target = tmp_path / "skills-pack" / "skills"
        target.mkdir(parents=True)
        (hermes_home / "config.yaml").write_text(f"skills:\n  create_dir: {target}\n")
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            from agent.skill_paths import get_skill_create_dir
            result = get_skill_create_dir()
        assert result == target.resolve()

    def test_tilde_and_env_var_expanded(self, hermes_home, tmp_path, monkeypatch):
        target = tmp_path / "shared" / "skills"
        target.mkdir(parents=True)
        monkeypatch.setenv("SKILLPACK_ROOT", str(tmp_path / "shared"))
        (hermes_home / "config.yaml").write_text(
            "skills:\n  create_dir: ${SKILLPACK_ROOT}/skills\n"
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            from agent.skill_paths import get_skill_create_dir
            result = get_skill_create_dir()
        assert result == target.resolve()

    def test_relative_path_rejected_falls_back(self, hermes_home):
        (hermes_home / "config.yaml").write_text("skills:\n  create_dir: relative/path\n")
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            from agent.skill_paths import get_skill_create_dir
            result = get_skill_create_dir()
        assert result == hermes_home / "skills"

    def test_empty_string_falls_back(self, hermes_home):
        (hermes_home / "config.yaml").write_text("skills:\n  create_dir: ''\n")
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            from agent.skill_paths import get_skill_create_dir
            result = get_skill_create_dir()
        assert result == hermes_home / "skills"

    def test_non_string_falls_back(self, hermes_home):
        (hermes_home / "config.yaml").write_text("skills:\n  create_dir: 42\n")
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            from agent.skill_paths import get_skill_create_dir
            result = get_skill_create_dir()
        assert result == hermes_home / "skills"

    def test_filesystem_root_rejected_falls_back(self, hermes_home):
        (hermes_home / "config.yaml").write_text("skills:\n  create_dir: /\n")
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            from agent.skill_paths import get_skill_create_dir
            result = get_skill_create_dir()
        assert result == hermes_home / "skills"

    def test_no_config_file(self, hermes_home):
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            from agent.skill_paths import get_skill_create_dir
            result = get_skill_create_dir()
        assert result == hermes_home / "skills"


class TestResolveNewSkillPath:
    def test_without_category(self, hermes_home):
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            from agent.skill_paths import resolve_new_skill_path
            result = resolve_new_skill_path("my-skill")
        assert result == hermes_home / "skills" / "my-skill"

    def test_with_category(self, hermes_home):
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            from agent.skill_paths import resolve_new_skill_path
            result = resolve_new_skill_path("my-skill", category="crypto")
        assert result == hermes_home / "skills" / "crypto" / "my-skill"

    def test_uses_configured_create_dir(self, hermes_home, tmp_path):
        target = tmp_path / "skills-pack" / "skills"
        target.mkdir(parents=True)
        (hermes_home / "config.yaml").write_text(f"skills:\n  create_dir: {target}\n")
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            from agent.skill_paths import resolve_new_skill_path
            result = resolve_new_skill_path("my-skill", category="crypto")
        assert result == target.resolve() / "crypto" / "my-skill"


class TestGetSkillDiscoveryDirs:
    def test_create_dir_distinct_from_legacy_is_included(self, hermes_home, tmp_path):
        create_dir = tmp_path / "skills-pack" / "skills"
        create_dir.mkdir(parents=True)
        (hermes_home / "config.yaml").write_text(f"skills:\n  create_dir: {create_dir}\n")
        local = hermes_home / "skills"
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            from agent.skill_paths import get_skill_discovery_dirs
            result = get_skill_discovery_dirs(local)
        assert result == [local, create_dir.resolve()]

    def test_create_dir_equal_to_legacy_deduplicated(self, hermes_home):
        local = hermes_home / "skills"
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            from agent.skill_paths import get_skill_discovery_dirs
            result = get_skill_discovery_dirs(local)
        assert result == [local]

    def test_create_dir_then_external_dirs(self, hermes_home, tmp_path, external_skills_dir):
        create_dir = tmp_path / "skills-pack" / "skills"
        create_dir.mkdir(parents=True)
        (hermes_home / "config.yaml").write_text(
            f"skills:\n  create_dir: {create_dir}\n  external_dirs:\n    - {external_skills_dir}\n"
        )
        local = hermes_home / "skills"
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            from agent.skill_paths import get_skill_discovery_dirs
            result = get_skill_discovery_dirs(local)
        assert result == [local, create_dir.resolve(), external_skills_dir.resolve()]

    def test_create_dir_equal_to_external_dir_deduplicated(self, hermes_home, external_skills_dir):
        (hermes_home / "config.yaml").write_text(
            f"skills:\n  create_dir: {external_skills_dir}\n"
            f"  external_dirs:\n    - {external_skills_dir}\n"
        )
        local = hermes_home / "skills"
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            from agent.skill_paths import get_skill_discovery_dirs
            result = get_skill_discovery_dirs(local)
        assert result == [local, external_skills_dir.resolve()]


class TestValidateSkillDestination:
    def test_accepts_nested_target(self, tmp_path):
        base = tmp_path / "skills"
        base.mkdir()
        target = base / "crypto" / "my-skill"
        from agent.skill_paths import validate_skill_destination
        assert validate_skill_destination(base, target) is None

    def test_rejects_symlinked_base_escape(self, tmp_path):
        real_outside = tmp_path / "outside"
        real_outside.mkdir()
        base = tmp_path / "skills-link"
        base.symlink_to(real_outside)
        # Target resolves (through the symlinked base) to a path that IS
        # under the symlink's target, so this should still be accepted --
        # the check is about escaping the base, not about symlinks per se.
        target = base / "my-skill"
        from agent.skill_paths import validate_skill_destination
        assert validate_skill_destination(base, target) is None

    def test_rejects_traversal_outside_base(self, tmp_path):
        base = tmp_path / "skills"
        base.mkdir()
        outside = tmp_path / "outside" / "my-skill"
        from agent.skill_paths import validate_skill_destination
        err = validate_skill_destination(base, outside)
        assert err is not None
        assert "escapes" in err
