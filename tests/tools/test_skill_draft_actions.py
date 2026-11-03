"""Tests for the skill_manage draft/validate/preview/discard/drafts_list
actions (Phase 2) -- the tool-layer integration on top of agent/skill_creation/.
"""

import json

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
