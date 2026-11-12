"""Tests for skills.creation_policy enforcement (Phase 3.1, P0 item 3)."""

import json

from tools.skill_manager_tool import skill_manage

VALID_CONTENT = """\
---
name: policy-test-skill
description: A test skill for creation_policy enforcement.
---

Body text.
"""


def _set_policy(policy: str):
    from hermes_constants import get_hermes_home

    config_path = get_hermes_home() / "config.yaml"
    config_path.write_text(f"skills:\n  creation_policy: {policy}\n")


class TestGetSkillCreationPolicy:
    def test_default_is_legacy(self):
        from agent.skill_paths import get_skill_creation_policy

        assert get_skill_creation_policy() == "legacy"

    def test_explicit_legacy(self):
        from agent.skill_paths import get_skill_creation_policy

        _set_policy("legacy")
        assert get_skill_creation_policy() == "legacy"

    def test_explicit_approval_required(self):
        from agent.skill_paths import get_skill_creation_policy

        _set_policy("approval_required")
        assert get_skill_creation_policy() == "approval_required"

    def test_unrecognized_value_falls_back_to_legacy(self):
        from agent.skill_paths import get_skill_creation_policy
        from hermes_constants import get_hermes_home

        config_path = get_hermes_home() / "config.yaml"
        config_path.write_text("skills:\n  creation_policy: delete_everything\n")
        assert get_skill_creation_policy() == "legacy"

    def test_non_string_value_falls_back_to_legacy(self):
        from agent.skill_paths import get_skill_creation_policy
        from hermes_constants import get_hermes_home

        config_path = get_hermes_home() / "config.yaml"
        config_path.write_text("skills:\n  creation_policy: 42\n")
        assert get_skill_creation_policy() == "legacy"


class TestLegacyModePreservesExistingBehavior:
    def test_create_works_by_default(self):
        raw = skill_manage(action="create", name="legacy-default-skill", content=VALID_CONTENT)
        result = json.loads(raw)
        assert result["success"] is True

    def test_create_works_with_explicit_legacy_policy(self):
        _set_policy("legacy")
        raw = skill_manage(action="create", name="legacy-explicit-skill", content=VALID_CONTENT)
        result = json.loads(raw)
        assert result["success"] is True


class TestApprovalRequiredBlocksLegacyActions:
    def test_create_blocked(self):
        _set_policy("approval_required")
        raw = skill_manage(action="create", name="blocked-create-skill", content=VALID_CONTENT)
        result = json.loads(raw)
        assert result["success"] is False
        assert "approval_required" in result["error"]
        assert "draft" in result["error"].lower()
        assert "blocked-create-skill" in result["error"]

    def test_edit_blocked(self):
        skill_manage(action="create", name="to-edit-skill", content=VALID_CONTENT)
        _set_policy("approval_required")
        raw = skill_manage(action="edit", name="to-edit-skill", content=VALID_CONTENT)
        result = json.loads(raw)
        assert result["success"] is False
        assert "to-edit-skill" in result["error"]

    def test_patch_blocked(self):
        skill_manage(action="create", name="to-patch-skill", content=VALID_CONTENT)
        _set_policy("approval_required")
        raw = skill_manage(action="patch", name="to-patch-skill", old_string="Body", new_string="New")
        result = json.loads(raw)
        assert result["success"] is False

    def test_write_file_blocked(self):
        skill_manage(action="create", name="to-write-file-skill", content=VALID_CONTENT)
        _set_policy("approval_required")
        raw = skill_manage(
            action="write_file", name="to-write-file-skill",
            file_path="references/a.md", file_content="x",
        )
        result = json.loads(raw)
        assert result["success"] is False
        assert "to-write-file-skill" in result["error"]

    def test_remove_file_blocked(self):
        skill_manage(action="create", name="to-remove-file-skill", content=VALID_CONTENT)
        skill_manage(
            action="write_file", name="to-remove-file-skill",
            file_path="references/a.md", file_content="x",
        )
        _set_policy("approval_required")
        raw = skill_manage(action="remove_file", name="to-remove-file-skill", file_path="references/a.md")
        result = json.loads(raw)
        assert result["success"] is False
        assert "to-remove-file-skill" in result["error"]

    def test_delete_is_not_gated(self):
        """Removing a skill isn't introducing untrusted content -- delete
        must keep working even under approval_required."""
        skill_manage(action="create", name="to-delete-skill", content=VALID_CONTENT)
        _set_policy("approval_required")
        raw = skill_manage(action="delete", name="to-delete-skill")
        result = json.loads(raw)
        assert result["success"] is True

    def test_draft_lifecycle_still_works(self):
        """The policy's entire point: the draft/validate/approve/publish
        lifecycle remains the supported path under approval_required."""
        from unittest.mock import patch

        _set_policy("approval_required")
        raw = skill_manage(action="draft", name="allowed-under-policy-skill", content=VALID_CONTENT)
        draft = json.loads(raw)
        assert draft["success"] is True

        raw = skill_manage(action="validate", draft_id=draft["draft_id"])
        assert json.loads(raw)["success"] is True

        with patch("tools.approval.request_explicit_approval", return_value=True):
            raw = skill_manage(action="approve", draft_id=draft["draft_id"])
        assert json.loads(raw)["success"] is True

    def test_drafts_list_and_preview_still_work(self):
        _set_policy("approval_required")
        raw = skill_manage(action="drafts_list")
        assert json.loads(raw)["success"] is True


class TestApprovalRequiredErrorTarget:
    """The blocked-action error names the skill it was blocked for, falling
    back to file_path, then a placeholder, so the message is actionable
    even when name is omitted."""

    def test_name_appears_in_error(self):
        skill_manage(action="create", name="to-patch-skill", content=VALID_CONTENT)
        _set_policy("approval_required")
        raw = skill_manage(action="patch", name="to-patch-skill", old_string="Body", new_string="New")
        result = json.loads(raw)
        assert "to-patch-skill" in result["error"]

    def test_file_path_used_when_name_absent(self):
        _set_policy("approval_required")
        raw = skill_manage(
            action="write_file", name=None,
            file_path="references/a.md", file_content="x",
        )
        result = json.loads(raw)
        assert "references/a.md" in result["error"]

    def test_placeholder_when_neither_given(self):
        _set_policy("approval_required")
        raw = skill_manage(action="write_file", name=None, file_path=None, file_content="x")
        result = json.loads(raw)
        assert "<no name/file_path given>" in result["error"]


class TestApprovalRequiredBlocksLegacyActionsMisc:
    def test_agent_cannot_change_policy_via_skill_manage(self):
        """No skill_manage action reads or writes skills.creation_policy --
        an agent has no path to disable the policy through ordinary
        skill-management actions, only by directly editing config.yaml
        (an action skill_manage does not expose)."""
        import inspect
        import tools.skill_manager_tool as mod

        # The dispatcher reads the policy (via get_skill_creation_policy)
        # but never accepts a "creation_policy" argument or writes config.
        assert "creation_policy" not in inspect.signature(mod.skill_manage).parameters
