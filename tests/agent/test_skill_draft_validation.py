"""Tests for agent/skill_creation/validation.py and the lifecycle state machine (Phase 2)."""

import pytest
from unittest.mock import patch

VALID_CONTENT = """\
---
name: validation-test-skill
description: A test skill for the validation pipeline.
---

Body text.
"""

BAD_FRONTMATTER_CONTENT = "no frontmatter here at all"

MISSING_DESCRIPTION_CONTENT = """\
---
name: missing-desc-skill
---

Body.
"""


class TestStructureCheck:
    def test_valid_skill_passes_structure(self):
        from agent.skill_creation import create_draft, validate_draft

        ok, manifest = create_draft("structure-ok-skill", VALID_CONTENT)
        assert ok
        ok2, manifest2 = validate_draft(manifest.draft_id)
        assert ok2
        assert manifest2.validation.checks["structure"]["status"] == "pass"
        assert manifest2.state.value == "validated"

    def test_malformed_frontmatter_rejected_at_creation(self):
        """create_draft itself reuses _validate_frontmatter -- bad content never
        becomes a draft in the first place."""
        from agent.skill_creation import create_draft

        ok, err = create_draft("bad-frontmatter-skill", BAD_FRONTMATTER_CONTENT)
        assert ok is False
        assert "frontmatter" in err.lower()

    def test_missing_required_metadata_rejected_at_creation(self):
        from agent.skill_creation import create_draft

        ok, err = create_draft("missing-desc-skill", MISSING_DESCRIPTION_CONTENT)
        assert ok is False
        assert "description" in err.lower()


class TestDestinationCheck:
    def test_destination_resolves_under_create_dir(self):
        from agent.skill_creation import create_draft, validate_draft

        ok, manifest = create_draft("destination-ok-skill", VALID_CONTENT, category="crypto")
        assert ok
        ok2, manifest2 = validate_draft(manifest.draft_id)
        assert ok2
        check = manifest2.validation.checks["destination"]
        assert check["status"] == "pass"
        assert check["proposed_destination"].endswith("crypto/destination-ok-skill")

    def test_duplicate_name_against_published_skill_blocks_destination(self, tmp_path):
        from agent.skill_creation import create_draft, validate_draft

        with patch("tools.skill_manager_tool.SKILLS_DIR", tmp_path), \
             patch("agent.skill_utils.get_all_skills_dirs", return_value=[tmp_path]):
            existing = tmp_path / "dup-name-skill"
            existing.mkdir(parents=True)
            (existing / "SKILL.md").write_text(VALID_CONTENT.replace(
                "validation-test-skill", "dup-name-skill"
            ))

            ok, manifest = create_draft("dup-name-skill", VALID_CONTENT.replace(
                "validation-test-skill", "dup-name-skill"
            ))
            assert ok
            ok2, manifest2 = validate_draft(manifest.draft_id)
            assert ok2
            assert manifest2.validation.checks["destination"]["status"] == "blocked"
            assert manifest2.validation.overall == "blocked"
            assert manifest2.state.value == "failed"

    def test_destination_check_does_not_create_directory(self):
        from agent.skill_creation import create_draft, validate_draft

        ok, manifest = create_draft("no-mkdir-skill", VALID_CONTENT)
        assert ok
        ok2, manifest2 = validate_draft(manifest.draft_id)
        assert ok2
        proposed = manifest2.validation.checks["destination"]["proposed_destination"]
        from pathlib import Path
        assert not Path(proposed).exists()


class TestSecurityCheckFailsClosed:
    def test_scanner_unavailable_blocks_with_error(self):
        from agent.skill_creation import create_draft, validate_draft

        ok, manifest = create_draft("scanner-unavailable-skill", VALID_CONTENT)
        assert ok

        with patch("agent.skill_creation.validation._check_security") as mock_check:
            mock_check.return_value = {
                "status": "error",
                "details": ["Security scanner unavailable: No module named 'tools.skills_guard'. Failing closed."],
            }
            ok2, manifest2 = validate_draft(manifest.draft_id)
        assert ok2
        assert manifest2.validation.checks["security"]["status"] == "error"
        assert manifest2.validation.overall == "error"
        assert manifest2.state.value == "failed"

    def test_scanner_exception_blocks_with_error(self):
        from agent.skill_creation import create_draft, validate_draft

        ok, manifest = create_draft("scanner-exception-skill", VALID_CONTENT)
        assert ok

        with patch("tools.skills_guard.scan_skill", side_effect=RuntimeError("boom")):
            ok2, manifest2 = validate_draft(manifest.draft_id)
        assert ok2
        assert manifest2.validation.checks["security"]["status"] == "error"
        assert manifest2.validation.overall == "error"
        assert manifest2.state.value == "failed"

    def test_dangerous_verdict_blocks(self):
        from agent.skill_creation import create_draft, validate_draft
        from tools.skills_guard import ScanResult, Finding

        ok, manifest = create_draft("dangerous-skill", VALID_CONTENT)
        assert ok

        dangerous_result = ScanResult(
            skill_name="dangerous-skill",
            source="agent-created",
            trust_level="agent-created",
            verdict="dangerous",
            findings=[Finding(
                pattern_id="x", severity="critical", category="exfiltration",
                file="SKILL.md", line=1, match="curl evil.com", description="exfil",
            )],
        )
        with patch("tools.skills_guard.scan_skill", return_value=dangerous_result):
            ok2, manifest2 = validate_draft(manifest.draft_id)
        assert ok2
        assert manifest2.validation.checks["security"]["status"] == "blocked"
        assert manifest2.state.value == "failed"

    def test_safe_verdict_passes(self):
        from agent.skill_creation import create_draft, validate_draft

        ok, manifest = create_draft("safe-skill", VALID_CONTENT)
        assert ok
        ok2, manifest2 = validate_draft(manifest.draft_id)
        assert ok2
        assert manifest2.validation.checks["security"]["status"] == "pass"


class TestDependenciesCheckNeverBlocks:
    def test_missing_dependency_reported_as_warning_not_blocked(self):
        from agent.skill_creation import create_draft, validate_draft

        content = """\
---
name: deps-skill
description: Has a prerequisite.
prerequisites:
  commands: [this-command-almost-certainly-does-not-exist-xyz]
  env_vars: [THIS_ENV_VAR_ALMOST_CERTAINLY_UNSET_XYZ]
---

Body.
"""
        ok, manifest = create_draft("deps-skill", content)
        assert ok
        ok2, manifest2 = validate_draft(manifest.draft_id)
        assert ok2
        deps = manifest2.validation.checks["dependencies"]
        assert deps["status"] == "warning"
        assert "command:this-command-almost-certainly-does-not-exist-xyz" in deps["missing"]
        assert "env:THIS_ENV_VAR_ALMOST_CERTAINLY_UNSET_XYZ" in deps["missing"]
        # Dependencies alone never fail the draft.
        assert manifest2.state.value == "validated"

    def test_no_prerequisites_passes(self):
        from agent.skill_creation import create_draft, validate_draft

        ok, manifest = create_draft("no-deps-skill", VALID_CONTENT)
        assert ok
        ok2, manifest2 = validate_draft(manifest.draft_id)
        assert ok2
        assert manifest2.validation.checks["dependencies"]["status"] == "pass"

    def test_never_executes_declared_commands(self):
        """Dependency checking must only probe with shutil.which -- never run anything."""
        from agent.skill_creation import create_draft, validate_draft

        content = """\
---
name: no-exec-skill
description: Declares a command that must never actually run.
prerequisites:
  commands: [rm]
---

Body.
"""
        ok, manifest = create_draft("no-exec-skill", content)
        assert ok
        with patch("subprocess.run") as mock_run, patch("os.system") as mock_system:
            ok2, manifest2 = validate_draft(manifest.draft_id)
        assert ok2
        mock_run.assert_not_called()
        mock_system.assert_not_called()


class TestDuplicatesCheck:
    def test_duplicate_content_hash_across_drafts_is_warning(self):
        from agent.skill_creation import create_draft, validate_draft

        ok1, manifest1 = create_draft("dup-content-a", VALID_CONTENT.replace(
            "validation-test-skill", "dup-content-a"
        ))
        ok2, manifest2 = create_draft("dup-content-b", VALID_CONTENT.replace(
            "validation-test-skill", "dup-content-a"
        ))
        assert ok1 and ok2
        # Same body content (only the name frontmatter differs via the skill
        # name field, but the underlying file text matches because both
        # replace to the same string) -> identical content_hash.
        assert manifest1.content_hash == manifest2.content_hash

        ok3, result = validate_draft(manifest2.draft_id)
        assert ok3
        dup_check = result.validation.checks["duplicates"]
        assert dup_check["status"] == "warning"
        assert manifest1.draft_id in dup_check["details"][0]
        # Advisory only -- does not block.
        assert result.state.value == "validated"


class TestContentMutationInvalidatesValidation:
    def test_updating_skill_md_demotes_validated_to_draft(self):
        from agent.skill_creation import create_draft, validate_draft, preview_draft
        from agent.skill_creation.staging import update_draft_file

        ok, manifest = create_draft("mutate-skill", VALID_CONTENT)
        assert ok
        ok2, manifest2 = validate_draft(manifest.draft_id)
        assert ok2
        assert manifest2.state.value == "validated"

        err = update_draft_file(manifest.draft_id, "SKILL.md", VALID_CONTENT + "\nMore.\n")
        assert err is None

        ok3, preview = preview_draft(manifest.draft_id)
        assert ok3
        assert preview["manifest"].state.value == "draft"
        assert preview["validation_stale"] is True

    def test_rewriting_identical_content_does_not_demote(self):
        from agent.skill_creation import create_draft, validate_draft, preview_draft
        from agent.skill_creation.staging import update_draft_file

        ok, manifest = create_draft("no-change-skill", VALID_CONTENT)
        assert ok
        validate_draft(manifest.draft_id)

        err = update_draft_file(manifest.draft_id, "SKILL.md", VALID_CONTENT)
        assert err is None

        ok2, preview = preview_draft(manifest.draft_id)
        assert ok2
        assert preview["manifest"].state.value == "validated"
        assert preview["validation_stale"] is False


class TestStateMachine:
    def test_cancelled_is_terminal(self):
        from agent.skill_creation.models import DraftManifest, DraftState

        manifest = DraftManifest(
            draft_id="a" * 32, name="x", category=None, state=DraftState.DRAFT,
            content_hash="h", created_at="t", updated_at="t",
        )
        manifest.record_transition(DraftState.CANCELLED)
        with pytest.raises(ValueError):
            manifest.record_transition(DraftState.VALIDATING)

    def test_draft_cannot_skip_straight_to_validated(self):
        from agent.skill_creation.models import DraftManifest, DraftState

        manifest = DraftManifest(
            draft_id="b" * 32, name="x", category=None, state=DraftState.DRAFT,
            content_hash="h", created_at="t", updated_at="t",
        )
        with pytest.raises(ValueError):
            manifest.record_transition(DraftState.VALIDATED)

    def test_valid_transition_sequence(self):
        from agent.skill_creation.models import DraftManifest, DraftState

        manifest = DraftManifest(
            draft_id="c" * 32, name="x", category=None, state=DraftState.DRAFT,
            content_hash="h", created_at="t", updated_at="t",
        )
        manifest.record_transition(DraftState.VALIDATING)
        manifest.record_transition(DraftState.VALIDATED)
        assert manifest.state == DraftState.VALIDATED
        assert len(manifest.state_history) == 2
