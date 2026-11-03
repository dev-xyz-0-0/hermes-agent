"""Isolated Git commit for a published skill (Phase 3).

Everything here runs through ``subprocess.run(["git", ...])`` -- the same
dependency-free approach ``tools/skills_hub.py``'s ``GitHubAuth._try_gh_cli``
already uses, no GitPython or other new dependency.

Two distinct index operations happen here, and they must not be confused:

1. **Building the commit** uses a *temporary* index (``GIT_INDEX_FILE``
   pointed at a fresh temp file, never the repo's real ``.git/index``):
   ``read-tree HEAD`` loads the last-committed tree, ``add`` stages only the
   one published path, ``write-tree``/``commit-tree`` build the commit
   object, and ``update-ref <branch> <new> <old>`` is a compare-and-swap ref
   update -- it fails (no-op, not a corrupting write) if ``HEAD`` moved
   concurrently. This step never reads or writes the real index and cannot
   be affected by -- or affect -- whatever the user already has staged.
2. **After** a successful commit, exactly the newly-published file paths
   are additionally synced into the *real* index (no ``GIT_INDEX_FILE``
   override) via ``update-index --add --cacheinfo``, purely so `git status`
   doesn't show a false "deleted" entry for a path that HEAD now has but
   the untouched real index doesn't know about yet. This DOES touch the
   real index -- deliberately, narrowly, and only for paths this function
   itself just committed. A pre-commit guard (``_check_target_path_clean``)
   refuses to proceed at all if the real index or working tree already has
   *any* entry for the target path, so step 2 can never clobber a
   pre-existing staged/unstaged change there. No unrelated path is ever
   touched by either step. Its own failure is logged but never flips
   ``committed``/``commit_sha``, which are already final by then.

No ``git add .``/``-A``, no ``commit --amend``, no ``--force`` push, no
interactive rebase, and no destructive reset anywhere in this module.
"""

import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_GIT_TIMEOUT = 15


def _run_git(args, cwd: Path, env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT,
    )


def _check_target_path_clean(repo_root: Path, rel_path: Path) -> Optional[str]:
    """Return an error string if *rel_path* already has a REAL INDEX entry
    (staged add/modify/delete/rename) -- None if it's clean or merely
    untracked.

    Runs against the real repo state (no ``GIT_INDEX_FILE`` override) --
    this is what makes the later real-index sync in commit_published_skill
    safe: we've already confirmed nothing is already *staged* at this exact
    path before touching it. The published path is always untracked at this
    point in normal operation -- publish_draft just moved brand-new files
    into place and nothing has `git add`ed them yet -- so "??" (untracked,
    not yet in the index at all) is expected and fine; it is NOT the
    "already staged" risk this guard exists for. Only a real index entry
    (anything in `git status --porcelain`'s first/index column other than
    space or "?") means someone already staged something else at this path,
    which the later real-index sync must not silently overwrite.
    """
    status = _run_git(["status", "--porcelain", "--", str(rel_path)], cwd=repo_root)
    if status.returncode != 0:
        return f"git status failed: {status.stderr.strip()}"
    for line in status.stdout.splitlines():
        if not line:
            continue
        index_status = line[0]
        if index_status not in (" ", "?"):
            return (
                f"Target path '{rel_path}' already has a staged change in "
                f"the repo's real index -- refusing to commit over it:\n{line}"
            )
    return None


def detect_repo_root(path: Path) -> Optional[Path]:
    """Return the Git repo root containing *path*, or None if it isn't in one."""
    try:
        result = _run_git(["rev-parse", "--show-toplevel"], cwd=path if path.is_dir() else path.parent)
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError) as e:
        logger.debug("git rev-parse failed: %s", e)
        return None
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip())


def commit_published_skill(
    repo_root: Path, published_path: Path, skill_name: str
) -> Dict[str, Any]:
    """Commit exactly *published_path* against an isolated temporary index.

    Returns a dict matching ``manifest.publication["git"]``'s shape:
    ``{attempted, committed, commit_sha, pushed, repo_root, error}``.
    Never raises -- any failure is reported in ``error`` with
    ``committed: False`` so the caller can retry later.
    """
    result: Dict[str, Any] = {
        "attempted": True,
        "committed": False,
        "commit_sha": None,
        "pushed": False,
        "repo_root": str(repo_root),
        "error": None,
    }

    try:
        rel_path = published_path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        result["error"] = f"Published path {published_path} is outside repo {repo_root}."
        return result

    dirty_error = _check_target_path_clean(repo_root, rel_path)
    if dirty_error:
        result["error"] = dirty_error
        return result

    head = _run_git(["rev-parse", "HEAD"], cwd=repo_root)
    if head.returncode != 0:
        result["error"] = f"Could not resolve HEAD: {head.stderr.strip()}"
        return result
    head_sha = head.stdout.strip()

    branch_result = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_root)
    branch = branch_result.stdout.strip() if branch_result.returncode == 0 else ""
    if not branch or branch == "HEAD":
        result["error"] = "Repo is in a detached HEAD state; refusing to commit."
        return result

    fd, tmp_index_path = tempfile.mkstemp(prefix="hermes-skill-publish-", suffix=".index")
    os.close(fd)
    os.unlink(tmp_index_path)  # git creates it fresh via read-tree
    try:
        env = {"GIT_INDEX_FILE": tmp_index_path}

        read_tree = _run_git(["read-tree", head_sha], cwd=repo_root, env=env)
        if read_tree.returncode != 0:
            result["error"] = f"git read-tree failed: {read_tree.stderr.strip()}"
            return result

        add = _run_git(["add", "--", str(rel_path)], cwd=repo_root, env=env)
        if add.returncode != 0:
            result["error"] = f"git add failed: {add.stderr.strip()}"
            return result

        write_tree = _run_git(["write-tree"], cwd=repo_root, env=env)
        if write_tree.returncode != 0:
            result["error"] = f"git write-tree failed: {write_tree.stderr.strip()}"
            return result
        tree_sha = write_tree.stdout.strip()

        commit_tree = _run_git(
            ["commit-tree", tree_sha, "-p", head_sha, "-m", f"Add skill: {skill_name}"],
            cwd=repo_root,
        )
        if commit_tree.returncode != 0:
            result["error"] = f"git commit-tree failed: {commit_tree.stderr.strip()}"
            return result
        commit_sha = commit_tree.stdout.strip()

        update_ref = _run_git(
            ["update-ref", f"refs/heads/{branch}", commit_sha, head_sha], cwd=repo_root
        )
        if update_ref.returncode != 0:
            result["error"] = (
                f"git update-ref failed (branch moved concurrently?): {update_ref.stderr.strip()}"
            )
            return result

        result["committed"] = True
        result["commit_sha"] = commit_sha
        result["branch"] = branch

        # Sync just these paths into the *real* index so `git status` doesn't
        # show a confusing phantom delete for files HEAD now has but the
        # untouched real index doesn't know about. `-r` expands rel_path
        # (a directory -- the whole published skill) into its individual
        # file blobs; each gets its own --cacheinfo entry. Every other
        # real-index entry (including any unrelated staged changes) is left
        # completely alone. Best-effort cosmetic cleanup -- a failure here
        # doesn't affect `committed`/`commit_sha`, which are already final.
        ls_tree = _run_git(["ls-tree", "-r", tree_sha, "--", str(rel_path)], cwd=repo_root)
        if ls_tree.returncode == 0:
            for line in ls_tree.stdout.splitlines():
                if not line.strip():
                    continue
                try:
                    meta, file_path = line.split("\t", 1)
                    mode, _obj_type, blob_sha = meta.split(" ")
                except ValueError:
                    continue
                _run_git(
                    ["update-index", "--add", "--cacheinfo", f"{mode},{blob_sha},{file_path}"],
                    cwd=repo_root,
                )
        return result
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError) as e:
        result["error"] = f"Git operation raised: {e}"
        return result
    finally:
        try:
            os.unlink(tmp_index_path)
        except OSError:
            pass


def push_branch(repo_root: Path, branch: str, remote: str = "origin") -> Dict[str, Any]:
    """Push *branch* to *remote*. Never uses --force."""
    try:
        result = _run_git(["push", remote, branch], cwd=repo_root)
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError) as e:
        return {"pushed": False, "error": f"git push raised: {e}"}
    if result.returncode != 0:
        return {"pushed": False, "error": f"git push failed: {result.stderr.strip()}"}
    return {"pushed": True, "error": None}
