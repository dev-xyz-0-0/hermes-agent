"""Tests for agent/skill_creation/__init__.py's publish_draft (Phase 3)."""

import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.skill_creation import create_draft, validate_draft, approve_draft, publish_draft
from agent.skill_creation.models import DraftState


@pytest.fixture(autouse=True)
def _isolated_legacy_skills_dir(tmp_path, monkeypatch):
    """publish_draft falls back to tools.skill_manager_tool.SKILLS_DIR when
    skills.create_dir isn't configured (none of this file's tests configure
    it). That module-level constant is computed once per worker process at
    first import -- without this, it can resolve to whatever HERMES_HOME was
    (or wasn't) set at that moment, up to and including the real ~/.hermes/
    if no isolated test had patched it yet. Every test in this file must
    publish into a tmp_path-scoped directory instead.
    """
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("tools.skill_manager_tool.SKILLS_DIR", skills_dir)
    # tools/skills_tool.py has its own separate SKILLS_DIR module constant
    # (used by skills_list()/skill_view()) -- discoverability checks need
    # it patched too, or they'd look at yet another stale/real location.
    monkeypatch.setattr("tools.skills_tool.SKILLS_DIR", skills_dir)
    yield skills_dir


@pytest.fixture(autouse=True)
def _auto_approve(monkeypatch):
    """This file tests publish_draft's own behavior, not the human-approval
    gate (that's tests/agent/test_skill_draft_approval.py) -- stand in for
    "a human already approved" on every approve_draft call here.
    """
    monkeypatch.setattr("tools.approval.request_explicit_approval", lambda **kw: True)


VALID_CONTENT = """\
---
name: publish-test-skill
description: A test skill for the publish step.
---

Body text.
"""


def _approved(name):
    content = VALID_CONTENT.replace("publish-test-skill", name)
    ok, manifest = create_draft(name, content)
    assert ok
    ok, manifest = validate_draft(manifest.draft_id)
    assert ok
    ok, manifest = approve_draft(manifest.draft_id, ttl_seconds=900)
    assert ok
    return manifest


class TestPublishDraft:
    def test_publish_requires_awaiting_approval(self):
        ok, manifest = create_draft("not-approved-skill", VALID_CONTENT.replace(
            "publish-test-skill", "not-approved-skill"
        ))
        assert ok
        ok2, err = publish_draft(manifest.draft_id)
        assert ok2 is False
        assert "approve first" in err.lower()

    def test_publish_moves_bytes_to_destination(self):
        manifest = _approved("publish-success-skill")
        ok, result = publish_draft(manifest.draft_id)
        assert ok
        assert result.state == DraftState.PUBLISHED
        target = Path(result.publication["published_path"])
        assert target.exists()
        assert (target / "SKILL.md").read_text() == VALID_CONTENT.replace(
            "publish-test-skill", "publish-success-skill"
        )

    def test_publish_rejects_destination_collision(self):
        manifest = _approved("collision-skill")
        target = Path(manifest.destination["proposed_path"])
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text("some other skill already here")

        ok, err = publish_draft(manifest.draft_id)
        assert ok is False
        assert "already exists" in err.lower() or "overwrite" in err.lower()

        from agent.skill_creation import registry
        reloaded = registry.load_manifest(manifest.draft_id)
        assert reloaded.state == DraftState.FAILED

    def test_publish_is_idempotent_on_retry_after_manifest_crash(self):
        """If the filesystem move succeeded but the manifest update didn't
        (process crash between os.replace and save_manifest), a retry must
        not double-publish or error -- it fixes up the manifest."""
        manifest = _approved("idempotent-retry-skill")

        target = Path(manifest.destination["proposed_path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        from agent.skill_creation import registry
        skill_dir = registry.skill_dir_for(manifest.draft_id)
        import shutil
        shutil.copytree(skill_dir, target)  # simulate the "already moved" crash state

        ok, result = publish_draft(manifest.draft_id)
        assert ok
        assert result.state == DraftState.PUBLISHED
        assert result.publication["published_path"] == str(target)

    def test_publish_rejects_interrupted_copy_verification_failure(self):
        """A corrupted copy (hash mismatch) must not become a visible skill."""
        manifest = _approved("corrupt-copy-skill")

        with patch(
            "agent.skill_creation.staging.compute_content_hash",
            side_effect=["not-the-real-hash"],  # only the post-copy verification call
        ):
            ok, err = publish_draft(manifest.draft_id)
        assert ok is False

        target = Path(manifest.destination["proposed_path"])
        assert not target.exists()

    def test_published_skill_is_immediately_discoverable(self):
        manifest = _approved("discoverable-after-publish-skill")
        ok, result = publish_draft(manifest.draft_id)
        assert ok

        from tools.skills_tool import skills_list
        assert "discoverable-after-publish-skill" in skills_list()

    def test_publish_cleans_up_temp_dir_on_failure(self, _isolated_legacy_skills_dir):
        create_dir = _isolated_legacy_skills_dir
        before = set(p.name for p in create_dir.iterdir())

        manifest = _approved("cleanup-on-failure-skill")
        with patch("shutil.copytree", side_effect=OSError("disk full")):
            ok, err = publish_draft(manifest.draft_id)
        assert ok is False

        after = set(p.name for p in create_dir.iterdir())
        assert after == before  # no leftover tmp* directory from the failed attempt


class TestConcurrentPublish:
    def test_simultaneous_publish_same_draft_yields_one_winner(self):
        manifest = _approved("concurrent-publish-skill")
        results = []

        def worker():
            ok, res = publish_draft(manifest.draft_id)
            results.append(ok)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(results) == 1

        from tools.skills_tool import skills_list
        assert "concurrent-publish-skill" in skills_list()
