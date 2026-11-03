"""Tests for the skill_manage draft/validate/preview/discard/drafts_list
actions (Phase 2) -- the tool-layer integration on top of agent/skill_creation/.
"""

import json

import pytest

from tools.skill_manager_tool import skill_manage


def _draft(**kw):
    kw.setdefault("name", "action-test-skill")
    kw.setdefault(
        "content",
        f"---\nname: {kw['name']}\ndescription: A test skill for the draft tool actions.\n---\n\nBody text.\n",
    )
    return json.loads(skill_manage(action="draft", **kw))


class TestDraftAction:
    def test_draft_creates_unpublished_draft(self):
        result = _draft()
        assert result["success"] is True
        assert result["state"] == "draft"
        assert "draft_id" in result

    def test_draft_requires_content(self):
        raw = skill_manage(action="draft", name="x")
        result = json.loads(raw)
        assert result["success"] is False
        assert "content" in result["error"].lower()

    def test_draft_with_category(self):
        result = _draft(category="crypto")
        assert result["success"] is True
        assert result["category"] == "crypto"
        assert "crypto" in result["destination"]["proposed_path"]

    def test_draft_does_not_clear_prompt_cache(self):
        """Draft actions must not trigger clear_skills_system_prompt_cache --
        drafts never touch published skills or the system prompt index."""
        from unittest.mock import patch

        with patch("agent.prompt_builder.clear_skills_system_prompt_cache") as mock_clear:
            _draft(name="no-cache-clear-skill")
        mock_clear.assert_not_called()


class TestValidateAction:
    def test_validate_requires_draft_id(self):
        raw = skill_manage(action="validate")
        result = json.loads(raw)
        assert result["success"] is False
        assert "draft_id" in result["error"].lower()

    def test_validate_unknown_draft_id(self):
        raw = skill_manage(action="validate", draft_id="a" * 32)
        result = json.loads(raw)
        assert result["success"] is False

    def test_validate_runs_pipeline_and_persists_report(self):
        draft = _draft(name="validate-flow-skill")
        raw = skill_manage(action="validate", draft_id=draft["draft_id"])
        result = json.loads(raw)
        assert result["success"] is True
        assert result["state"] == "validated"
        assert result["report"]["overall"] == "pass"
        assert set(result["report"]["checks"]) == {
            "structure", "destination", "security", "dependencies", "duplicates",
        }


class TestPreviewAction:
    def test_preview_unknown_draft(self):
        raw = skill_manage(action="preview", draft_id="b" * 32)
        result = json.loads(raw)
        assert result["success"] is False

    def test_preview_shows_content_and_report(self):
        draft = _draft(name="preview-flow-skill")
        skill_manage(action="validate", draft_id=draft["draft_id"])

        raw = skill_manage(action="preview", draft_id=draft["draft_id"])
        result = json.loads(raw)
        assert result["success"] is True
        assert "Body text." in result["content"]
        assert result["report"]["overall"] == "pass"
        assert result["validation_stale"] is False

    def test_preview_before_validate_has_no_report(self):
        draft = _draft(name="preview-before-validate-skill")
        raw = skill_manage(action="preview", draft_id=draft["draft_id"])
        result = json.loads(raw)
        assert result["success"] is True
        assert result["report"] is None


class TestDiscardAction:
    def test_discard_requires_draft_id(self):
        raw = skill_manage(action="discard")
        result = json.loads(raw)
        assert result["success"] is False

    def test_discard_removes_draft(self):
        draft = _draft(name="discard-flow-skill")
        raw = skill_manage(action="discard", draft_id=draft["draft_id"])
        result = json.loads(raw)
        assert result["success"] is True

        raw2 = skill_manage(action="preview", draft_id=draft["draft_id"])
        result2 = json.loads(raw2)
        assert result2["success"] is False

    def test_discard_rejects_path_traversal_draft_id(self):
        raw = skill_manage(action="discard", draft_id="../../etc/passwd")
        result = json.loads(raw)
        assert result["success"] is False


class TestDraftsListAction:
    def test_drafts_list_empty_initially(self):
        raw = skill_manage(action="drafts_list")
        result = json.loads(raw)
        assert result["success"] is True
        assert result["drafts"] == []

    def test_drafts_list_shows_created_drafts(self):
        d1 = _draft(name="list-skill-one")
        d2 = _draft(name="list-skill-two")

        raw = skill_manage(action="drafts_list")
        result = json.loads(raw)
        ids = {d["draft_id"] for d in result["drafts"]}
        assert d1["draft_id"] in ids
        assert d2["draft_id"] in ids

    def test_drafts_never_appear_in_skills_list(self):
        from tools.skills_tool import skills_list

        _draft(name="never-discoverable-skill")
        raw = skills_list()
        assert "never-discoverable-skill" not in raw

    def test_drafts_never_appear_in_scan_skill_commands(self):
        from agent.skill_commands import scan_skill_commands

        _draft(name="never-a-slash-command-skill")
        commands = scan_skill_commands()
        assert "/never-a-slash-command-skill" not in commands


class TestUnknownAction:
    def test_unknown_action_lists_draft_actions_too(self):
        raw = skill_manage(action="explode", name="x")
        result = json.loads(raw)
        assert result["success"] is False
        assert "draft" in result["error"]
        assert "drafts_list" in result["error"]


# ---------------------------------------------------------------------------
# Phase 3 -- approve / publish / publish_git
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_create_dir(tmp_path, monkeypatch):
    """Configure skills.create_dir explicitly so publish actions never fall
    back to the (process-wide, potentially stale) legacy SKILLS_DIR module
    constants -- the real production mechanism, and avoids the exact
    cross-test pollution risk documented in test_skill_draft_publication.py.
    """
    from hermes_constants import get_hermes_home

    create_dir = tmp_path / "skills-pack" / "skills"
    create_dir.mkdir(parents=True)
    config_path = get_hermes_home() / "config.yaml"
    config_path.write_text(f"skills:\n  create_dir: {create_dir}\n")
    return create_dir


def _approved(isolated_create_dir, name, **kw):
    """These tool-layer tests exercise publish/publish_git, not the
    human-approval gate itself (that's tests/agent/test_skill_draft_approval.py
    and TestApproveAction.test_approve_requires_human_confirmation below) --
    stand in for "a human already approved" for this one call.
    """
    from unittest.mock import patch

    draft = _draft(name=name, **kw)
    skill_manage(action="validate", draft_id=draft["draft_id"])
    with patch("tools.approval.request_explicit_approval", return_value=True):
        approval = json.loads(skill_manage(action="approve", draft_id=draft["draft_id"], ttl_seconds=900))
    assert approval["success"] is True
    return draft["draft_id"]


class TestApproveAction:
    def test_approve_requires_draft_id(self):
        raw = skill_manage(action="approve")
        result = json.loads(raw)
        assert result["success"] is False

    def test_approve_fails_closed_without_human_channel(self, isolated_create_dir, monkeypatch):
        """P0: calling skill_manage(action='approve') with nothing but the
        tool's own arguments -- no CLI TTY, no gateway session -- must not
        grant approval. The agent's tool call alone is not authorization."""
        monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
        monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)

        draft = _draft(name="tool-layer-self-approve-skill")
        skill_manage(action="validate", draft_id=draft["draft_id"])

        raw = skill_manage(action="approve", draft_id=draft["draft_id"])
        result = json.loads(raw)
        assert result["success"] is False
        assert "not approved by a human" in result["error"].lower()

        preview = json.loads(skill_manage(action="preview", draft_id=draft["draft_id"]))
        assert preview["state"] == "validated"  # never moved to awaiting_approval

    def test_approve_moves_to_awaiting_approval(self, isolated_create_dir):
        draft_id = _approved(isolated_create_dir, "approve-action-skill")
        raw = skill_manage(action="preview", draft_id=draft_id)
        result = json.loads(raw)
        assert result["state"] == "awaiting_approval"
        assert result["success"] is True

    def test_approve_does_not_clear_prompt_cache(self, isolated_create_dir):
        from unittest.mock import patch

        with patch("agent.prompt_builder.clear_skills_system_prompt_cache") as mock_clear:
            _approved(isolated_create_dir, "approve-no-cache-clear-skill")
        mock_clear.assert_not_called()


class TestPublishAction:
    def test_publish_requires_draft_id(self):
        raw = skill_manage(action="publish")
        result = json.loads(raw)
        assert result["success"] is False

    def test_publish_moves_bytes_and_is_discoverable(self, isolated_create_dir):
        draft_id = _approved(isolated_create_dir, "publish-action-skill")
        raw = skill_manage(action="publish", draft_id=draft_id)
        result = json.loads(raw)
        assert result["success"] is True
        assert result["state"] == "published"

        from tools.skills_tool import skills_list
        assert "publish-action-skill" in skills_list()

    def test_publish_clears_prompt_cache(self, isolated_create_dir):
        from unittest.mock import patch

        draft_id = _approved(isolated_create_dir, "publish-cache-clear-skill")
        with patch("agent.prompt_builder.clear_skills_system_prompt_cache") as mock_clear:
            skill_manage(action="publish", draft_id=draft_id)
        mock_clear.assert_called_once()

    def test_discard_refuses_once_published(self, isolated_create_dir):
        draft_id = _approved(isolated_create_dir, "discard-after-publish-skill")
        skill_manage(action="publish", draft_id=draft_id)

        raw = skill_manage(action="discard", draft_id=draft_id)
        result = json.loads(raw)
        assert result["success"] is False
        assert "delete" in result["error"].lower()


class TestPublishGitAction:
    def test_publish_git_requires_draft_id(self):
        raw = skill_manage(action="publish_git")
        result = json.loads(raw)
        assert result["success"] is False

    def test_publish_git_on_non_repo_is_clean_noop(self, isolated_create_dir):
        draft_id = _approved(isolated_create_dir, "publish-git-non-repo-skill")
        skill_manage(action="publish", draft_id=draft_id)

        raw = skill_manage(action="publish_git", draft_id=draft_id)
        result = json.loads(raw)
        assert result["success"] is True
        assert result["git"]["committed"] is False

    def test_publish_git_before_publish_fails(self, isolated_create_dir):
        draft_id = _approved(isolated_create_dir, "publish-git-too-early-skill")
        raw = skill_manage(action="publish_git", draft_id=draft_id)
        result = json.loads(raw)
        assert result["success"] is False
