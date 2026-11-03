"""Tests for agent/skill_creation/git_integration.py and publish_git_draft (Phase 3)."""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.skill_creation import git_integration


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=path, check=True)
    (path / "README.md").write_text("hi\n")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    return path


def _status(path: Path) -> str:
    return subprocess.run(
        ["git", "status", "--porcelain"], cwd=path, capture_output=True, text=True
    ).stdout


class TestDetectRepoRoot:
    def test_not_a_repo(self, tmp_path):
        assert git_integration.detect_repo_root(tmp_path) is None

    def test_detects_repo(self, tmp_path):
        repo = _init_repo(tmp_path)
        assert git_integration.detect_repo_root(repo) == repo

    def test_detects_repo_from_subdirectory(self, tmp_path):
        repo = _init_repo(tmp_path)
        sub = repo / "skills" / "crypto"
        sub.mkdir(parents=True)
        assert git_integration.detect_repo_root(sub) == repo


class TestCommitPublishedSkill:
    def test_commits_exact_path_only(self, tmp_path):
        repo = _init_repo(tmp_path)
        skill_dir = repo / "skills" / "test-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: test-skill\ndescription: d\n---\n\nBody.\n")

        result = git_integration.commit_published_skill(repo, skill_dir, "test-skill")
        assert result["committed"] is True
        assert result["commit_sha"]

        show = subprocess.run(
            ["git", "show", "--name-only", "--pretty=", result["commit_sha"]],
            cwd=repo, capture_output=True, text=True,
        )
        assert show.stdout.strip() == "skills/test-skill/SKILL.md"

    def test_refuses_when_target_path_already_staged(self, tmp_path):
        """If the target path somehow already has a staged entry in the
        REAL index (not just untracked-on-disk, which is the normal case
        right after publish_draft moves brand-new files into place), the
        pre-commit guard must refuse rather than let the later real-index
        sync silently overwrite that staged entry."""
        repo = _init_repo(tmp_path)
        skill_dir = repo / "skills" / "test-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("someone else's staged content")
        subprocess.run(["git", "add", "skills/test-skill/SKILL.md"], cwd=repo, check=True)

        # Now overwrite on disk with what we actually want to publish --
        # the real index still has the OLD staged blob for this path.
        (skill_dir / "SKILL.md").write_text("---\nname: test-skill\ndescription: d\n---\n\nBody.\n")

        result = git_integration.commit_published_skill(repo, skill_dir, "test-skill")
        assert result["committed"] is False
        assert "staged" in result["error"].lower()

        # Nothing committed, and the pre-existing staged entry is untouched.
        log = subprocess.run(["git", "log", "--oneline"], cwd=repo, capture_output=True, text=True)
        assert len(log.stdout.strip().splitlines()) == 1  # still just the initial commit
        status = subprocess.run(
            ["git", "status", "--porcelain", "--", "skills/test-skill/SKILL.md"],
            cwd=repo, capture_output=True, text=True,
        )
        assert status.stdout.startswith("AM") or status.stdout.startswith("A ")

    def test_untracked_target_path_is_not_treated_as_dirty(self, tmp_path):
        """The normal case: publish_draft just moved brand-new files into
        place, so the target path is untracked ('??') -- not yet staged by
        anyone. That must NOT trip the pre-existing-staged-change guard."""
        repo = _init_repo(tmp_path)
        skill_dir = repo / "skills" / "test-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: test-skill\ndescription: d\n---\n\nBody.\n")

        result = git_integration.commit_published_skill(repo, skill_dir, "test-skill")
        assert result["committed"] is True

    def test_never_touches_unrelated_staged_changes(self, tmp_path):
        """A file staged in the REAL index before publish must stay staged,
        untouched, and NOT appear in the new commit."""
        repo = _init_repo(tmp_path)
        (repo / "dirty.txt").write_text("unrelated local work\n")
        subprocess.run(["git", "add", "dirty.txt"], cwd=repo, check=True)

        skill_dir = repo / "skills" / "test-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: test-skill\ndescription: d\n---\n\nBody.\n")

        result = git_integration.commit_published_skill(repo, skill_dir, "test-skill")
        assert result["committed"] is True

        show = subprocess.run(
            ["git", "show", "--name-only", "--pretty=", result["commit_sha"]],
            cwd=repo, capture_output=True, text=True,
        )
        assert "dirty.txt" not in show.stdout

        status = _status(repo)
        assert "A  dirty.txt" in status  # still staged, exactly as the caller left it
        assert "skills/" not in status  # the published skill is now clean, not phantom-deleted

    def test_never_uses_git_add_dot(self, tmp_path):
        """The isolated-index add must always pass an exact path, never '.' or '-A'."""
        repo = _init_repo(tmp_path)
        skill_dir = repo / "skills" / "test-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("x")

        calls = []
        orig = git_integration._run_git

        def spy(args, cwd, env=None):
            calls.append(list(args))
            return orig(args, cwd, env)

        with patch.object(git_integration, "_run_git", side_effect=spy):
            git_integration.commit_published_skill(repo, skill_dir, "test-skill")

        add_calls = [c for c in calls if c and c[0] == "add"]
        assert add_calls, "expected at least one git add call"
        for call in add_calls:
            assert "." not in call
            assert "-A" not in call
            assert "--all" not in call

    def test_detached_head_refuses_to_commit(self, tmp_path):
        repo = _init_repo(tmp_path)
        head_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
        ).stdout.strip()
        subprocess.run(["git", "checkout", "-q", head_sha], cwd=repo, check=True)

        skill_dir = repo / "skills" / "test-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("x")

        result = git_integration.commit_published_skill(repo, skill_dir, "test-skill")
        assert result["committed"] is False
        assert "detached" in result["error"].lower()

    def test_path_outside_repo_rejected(self, tmp_path):
        repo = _init_repo(tmp_path)
        outside = tmp_path.parent / "not-in-repo"
        outside.mkdir(exist_ok=True)
        result = git_integration.commit_published_skill(repo, outside, "x")
        assert result["committed"] is False
        assert "outside" in result["error"].lower()

    def test_concurrent_head_move_cas_failure_reported_truthfully(self, tmp_path):
        """If HEAD moves between our read and our update-ref (simulated by
        forcing the ref update to target a stale old-value), the CAS fails
        and the result says so -- no corrupting write happens."""
        repo = _init_repo(tmp_path)
        skill_dir = repo / "skills" / "test-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("x")

        real_run = git_integration._run_git

        def fake_run(args, cwd, env=None):
            if args[:1] == ["update-ref"]:
                # Pretend the branch moved concurrently: use a bogus old-sha.
                bogus_args = list(args)
                bogus_args[-1] = "0" * 40
                return real_run(bogus_args, cwd, env)
            return real_run(args, cwd, env)

        with patch.object(git_integration, "_run_git", side_effect=fake_run):
            result = git_integration.commit_published_skill(repo, skill_dir, "test-skill")

        assert result["committed"] is False
        assert "update-ref" in result["error"].lower() or "moved" in result["error"].lower()

        # HEAD must be unchanged -- the CAS failure didn't corrupt anything.
        log = subprocess.run(["git", "log", "--oneline"], cwd=repo, capture_output=True, text=True)
        assert len(log.stdout.strip().splitlines()) == 1


class TestPushBranch:
    def test_push_never_uses_force(self, tmp_path):
        repo = _init_repo(tmp_path)
        calls = []
        orig = git_integration._run_git

        def spy(args, cwd, env=None):
            calls.append(list(args))
            return orig(args, cwd, env)

        with patch.object(git_integration, "_run_git", side_effect=spy):
            git_integration.push_branch(repo, "master", remote="nonexistent-remote")

        for call in calls:
            assert "--force" not in call
            assert "-f" not in call

    def test_push_failure_reported_not_raised(self, tmp_path):
        repo = _init_repo(tmp_path)
        result = git_integration.push_branch(repo, "master", remote="nonexistent-remote")
        assert result["pushed"] is False
        assert result["error"]


class TestPublishGitDraftIntegration:
    @pytest.fixture(autouse=True)
    def _auto_approve(self, monkeypatch):
        """These tests exercise publish_git_draft, not the human-approval
        gate -- stand in for "a human already approved" on every
        approve_draft call in this class."""
        monkeypatch.setattr("tools.approval.request_explicit_approval", lambda **kw: True)

    def test_retry_after_failure_does_not_republish_filesystem(self, tmp_path, monkeypatch):
        """publish_git failing once, then succeeding on retry, must never
        re-trigger a filesystem copy -- the published directory's mtime (and
        its one-time existence) must be untouched by a retry."""
        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        (home / "skills").mkdir()

        repo = _init_repo(tmp_path / "repo")
        create_dir = repo / "skills"
        create_dir.mkdir()
        (home / "config.yaml").write_text(f"skills:\n  create_dir: {create_dir}\n  publishing:\n    auto_commit: true\n")

        from agent.skill_creation import create_draft, validate_draft, approve_draft, publish_draft, publish_git_draft

        content = "---\nname: retry-git-skill\ndescription: d\n---\n\nBody.\n"
        ok, m = create_draft("retry-git-skill", content)
        ok, m = validate_draft(m.draft_id)
        ok, m = approve_draft(m.draft_id, ttl_seconds=900)
        ok, m = publish_draft(m.draft_id)
        assert ok
        published_path = Path(m.publication["published_path"])
        mtime_before = published_path.stat().st_mtime

        with patch(
            "agent.skill_creation.git_integration.commit_published_skill",
            side_effect=RuntimeError("network blip"),
        ):
            with pytest.raises(RuntimeError):
                publish_git_draft(m.draft_id)

        mtime_after_failure = published_path.stat().st_mtime
        assert mtime_after_failure == mtime_before

        ok, m2 = publish_git_draft(m.draft_id)
        assert ok
        assert m2.publication["git"]["committed"] is True
        assert published_path.stat().st_mtime == mtime_before  # never re-copied

    def test_git_disabled_is_a_clean_noop(self, tmp_path, monkeypatch):
        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        (home / "skills").mkdir()

        repo = _init_repo(tmp_path / "repo")
        create_dir = repo / "skills"
        create_dir.mkdir()
        (home / "config.yaml").write_text(
            f"skills:\n  create_dir: {create_dir}\n  publishing:\n    git_enabled: false\n"
        )

        from agent.skill_creation import create_draft, validate_draft, approve_draft, publish_draft, publish_git_draft

        content = "---\nname: git-disabled-skill\ndescription: d\n---\n\nBody.\n"
        ok, m = create_draft("git-disabled-skill", content)
        ok, m = validate_draft(m.draft_id)
        ok, m = approve_draft(m.draft_id, ttl_seconds=900)
        ok, m = publish_draft(m.draft_id)
        assert ok

        ok, m2 = publish_git_draft(m.draft_id)
        assert ok
        assert m2.publication["git"]["attempted"] is False

    def test_push_requires_both_config_and_call_flag(self, tmp_path, monkeypatch):
        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        (home / "skills").mkdir()

        repo = _init_repo(tmp_path / "repo")
        create_dir = repo / "skills"
        create_dir.mkdir()
        (home / "config.yaml").write_text(
            f"skills:\n  create_dir: {create_dir}\n  publishing:\n    auto_commit: true\n    auto_push: false\n"
        )

        from agent.skill_creation import create_draft, validate_draft, approve_draft, publish_draft, publish_git_draft

        content = "---\nname: push-gated-skill\ndescription: d\n---\n\nBody.\n"
        ok, m = create_draft("push-gated-skill", content)
        ok, m = validate_draft(m.draft_id)
        ok, m = approve_draft(m.draft_id, ttl_seconds=900)
        ok, m = publish_draft(m.draft_id)
        assert ok

        with patch("agent.skill_creation.git_integration.push_branch") as mock_push:
            ok, m2 = publish_git_draft(m.draft_id, push=True)  # call says yes, config says no
        assert ok
        mock_push.assert_not_called()
        assert "auto_push is false" in m2.publication["git"]["push_error"]
