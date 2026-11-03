"""Skill draft lifecycle, validation, and publication (Phases 2-3).

Public entry points used by ``tools/skill_manager_tool.py``'s ``skill_manage``
dispatcher: ``create_draft``, ``validate_draft``, ``preview_draft``,
``discard_draft``, ``list_draft_summaries`` (Phase 2), plus ``approve_draft``,
``publish_draft``, ``publish_git_draft`` (Phase 3).

See ``docs/hermes-skill-creation-plan/phase-0{2,3}-*.md`` for the full specs.
Drafts are staged under ``~/.hermes/skill-drafts/`` and are never
discoverable as published skills until ``publish_draft`` moves them.
"""

import logging
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from agent.skill_creation.models import DraftManifest, DraftState

logger = logging.getLogger(__name__)


def create_draft(
    name: str, content: str, category: Optional[str] = None, creator: Optional[Dict[str, Any]] = None
) -> Tuple[bool, Any]:
    """Stage a new, unpublished draft. Returns (True, DraftManifest) or (False, error)."""
    from agent.skill_creation import registry, staging
    from agent.skill_paths import get_skill_create_dir, resolve_new_skill_path
    from tools.skill_manager_tool import SKILLS_DIR

    draft_id, draft_dir = registry.allocate_draft_dir()
    try:
        err = staging.write_draft_skill(draft_dir, name, content, category)
        if err:
            registry.delete_draft(draft_id)
            return False, err

        skill_dir = registry.skill_dir_for(draft_id)
        content_hash = staging.compute_content_hash(skill_dir)
        now = datetime.now(timezone.utc).isoformat()

        create_dir = get_skill_create_dir(legacy_default=SKILLS_DIR)
        proposed = resolve_new_skill_path(name, category, legacy_default=SKILLS_DIR)

        manifest = DraftManifest(
            draft_id=draft_id,
            name=name,
            category=category,
            state=DraftState.DRAFT,
            content_hash=content_hash,
            created_at=now,
            updated_at=now,
            creator=creator or {},
            destination={"create_dir": str(create_dir), "proposed_path": str(proposed)},
            state_history=[{"state": DraftState.DRAFT.value, "at": now}],
        )
        registry.save_manifest(manifest)
        return True, manifest
    except Exception:
        registry.delete_draft(draft_id)
        raise


def validate_draft(draft_id: str) -> Tuple[bool, Any]:
    """Run the validation pipeline against a draft. Returns (True, DraftManifest) or (False, error)."""
    from agent.skill_creation import validation

    return validation.validate_draft(draft_id)


def preview_draft(draft_id: str) -> Tuple[bool, Any]:
    """Return a draft's manifest, SKILL.md content, and whether validation is stale."""
    from agent.skill_creation import registry, staging

    manifest = registry.load_manifest(draft_id)
    if manifest is None:
        return False, f"Draft '{draft_id}' not found."

    skill_dir = registry.skill_dir_for(draft_id)
    skill_md = skill_dir / "SKILL.md"
    content = skill_md.read_text(encoding="utf-8") if skill_md.exists() else ""

    current_hash = staging.compute_content_hash(skill_dir) if skill_dir.exists() else ""
    validation_stale = bool(
        manifest.validation and manifest.validation.content_hash != current_hash
    )

    return True, {
        "manifest": manifest,
        "content": content,
        "validation_stale": validation_stale,
    }


def discard_draft(draft_id: str) -> Tuple[bool, str]:
    """Remove a draft. Returns (True, message) or (False, error)."""
    from agent.skill_creation import registry

    if not registry.is_valid_draft_id(draft_id):
        return False, f"Invalid draft id '{draft_id}'."
    if registry.load_manifest(draft_id) is None:
        return False, f"Draft '{draft_id}' not found."
    registry.delete_draft(draft_id)
    return True, f"Draft '{draft_id}' discarded."


def list_draft_summaries() -> list:
    """Summaries of every draft in the current profile's registry."""
    from agent.skill_creation import registry

    return [
        {
            "draft_id": m.draft_id,
            "name": m.name,
            "category": m.category,
            "state": m.state.value,
            "created_at": m.created_at,
            "updated_at": m.updated_at,
        }
        for m in registry.list_drafts()
    ]


# =============================================================================
# Phase 3 -- approval, atomic publication, Git
# =============================================================================


def _get_publishing_config() -> Dict[str, Any]:
    """Read ``skills.publishing`` from config.yaml (same raw-YAML pattern as
    ``agent/skill_paths.py`` -- no CLI config import, re-read on every call).
    """
    from hermes_constants import get_hermes_home
    from agent.skill_utils import yaml_load

    defaults = {"git_enabled": True, "auto_commit": False, "auto_push": False}
    config_path = get_hermes_home() / "config.yaml"
    if not config_path.exists():
        return defaults
    try:
        parsed = yaml_load(config_path.read_text(encoding="utf-8"))
    except Exception:
        return defaults
    if not isinstance(parsed, dict):
        return defaults
    skills_cfg = parsed.get("skills")
    if not isinstance(skills_cfg, dict):
        return defaults
    publishing_cfg = skills_cfg.get("publishing")
    if not isinstance(publishing_cfg, dict):
        return defaults
    merged = dict(defaults)
    merged.update({k: v for k, v in publishing_cfg.items() if k in defaults})
    return merged


def approve_draft(
    draft_id: str, approved_by: Optional[str] = None, ttl_seconds: int = 900
) -> Tuple[bool, Any]:
    """Bind an authorization to a VALIDATED draft's exact content and destination.

    Security-critical: this function is the entire authority boundary
    between "the agent asked to publish something" and "a human actually
    said yes." Calling this with any arguments the agent controls (draft_id,
    ttl_seconds) must NOT by itself grant approval -- so before writing any
    approval record, this blocks on ``tools.approval.request_explicit_approval()``,
    the same one-shot human-approval primitive MCP tools already use for
    sensitive actions. That call routes to a real human: the gateway's
    ``/approve``/``/deny`` command (resolved only by a person typing it in
    chat) or the CLI's interactive ``input()`` prompt -- never anything the
    agent's own tool-call arguments can satisfy. It fails closed (denies)
    when neither channel is available, e.g. an unattended/autonomous run --
    there is deliberately no way to approve a draft without a live human
    approval channel.

    Requires the last validation to have passed (or passed with warnings).
    The resulting approval is only valid for *ttl_seconds* and only for the
    content hash / destination captured right now -- any change to either
    invalidates it (see staging.update_draft_file and publish_draft's own
    re-check).
    """
    from agent.skill_creation import registry
    from agent.skill_creation.locking import draft_lock, DraftLockError
    from tools.approval import request_explicit_approval, get_current_session_key

    try:
        with draft_lock(draft_id):
            manifest = registry.load_manifest(draft_id)
            if manifest is None:
                return False, f"Draft '{draft_id}' not found."
            if manifest.state != DraftState.VALIDATED:
                return False, (
                    f"Draft '{draft_id}' is '{manifest.state.value}', not 'validated'. "
                    "Run validate first."
                )
            if not manifest.validation or manifest.validation.overall not in ("pass", "warning"):
                return False, f"Draft '{draft_id}' has no passing validation report to approve."

            destination = manifest.destination.get("proposed_path")
            human_approved = request_explicit_approval(
                title=f"Publish skill '{manifest.name}'?",
                description=(
                    f"The agent wants to publish skill '{manifest.name}' to "
                    f"'{destination}'. Validation: {manifest.validation.overall}. "
                    f"Content hash: {manifest.content_hash}."
                ),
                command=f"skill_manage(action='publish', draft_id='{draft_id}')",
            )
            if not human_approved:
                return False, (
                    f"Draft '{draft_id}' was not approved by a human. No interactive "
                    "approval channel responded with explicit confirmation -- the "
                    "draft remains 'validated' and can be re-approved once a user "
                    "is available to confirm."
                )

            now = datetime.now(timezone.utc)

            manifest.approval = {
                "approved_at": now.isoformat(),
                # Descriptive only (platform/session label) -- the actual
                # authority check already happened above via
                # request_explicit_approval(); this is not itself a security
                # claim, just provenance for the handoff/audit trail.
                "approved_by": approved_by or os.getenv("HERMES_PLATFORM") or os.getenv("HERMES_SESSION_PLATFORM") or "cli",
                "session_key": get_current_session_key(),
                "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
                "content_hash": manifest.content_hash,
                "destination": destination,
            }
            manifest.record_transition(DraftState.AWAITING_APPROVAL)
            registry.save_manifest(manifest)
            return True, manifest
    except DraftLockError as e:
        return False, str(e)


def publish_draft(draft_id: str) -> Tuple[bool, Any]:
    """Re-validate the approval against current reality, then atomically publish.

    Safeguard B: re-resolves skills.create_dir and re-checks for collisions
    from scratch rather than trusting manifest.destination -- a config,
    filesystem, or symlink change between approve() and publish() demotes the
    draft back to FAILED (something changed, re-validate) rather than ever
    publishing to an unintended or stale location. A plain approval expiry
    (nothing else changed) demotes to VALIDATED instead, since all that's
    needed is a fresh approve() call.
    """
    from agent.skill_creation import registry, staging, validation
    from agent.skill_creation.locking import draft_lock, destination_lock, DraftLockError
    from agent.skill_paths import get_skill_create_dir, resolve_new_skill_path
    from tools.skill_manager_tool import SKILLS_DIR

    try:
        with draft_lock(draft_id):
            manifest = registry.load_manifest(draft_id)
            if manifest is None:
                return False, f"Draft '{draft_id}' not found."

            if manifest.state == DraftState.PUBLISHED:
                return False, f"Draft '{draft_id}' is already published."
            if manifest.state != DraftState.AWAITING_APPROVAL:
                return False, (
                    f"Draft '{draft_id}' is '{manifest.state.value}', not "
                    "'awaiting_approval'. Run approve first."
                )

            approval = manifest.approval
            if not approval:
                manifest.record_transition(DraftState.FAILED)
                registry.save_manifest(manifest)
                return False, f"Draft '{draft_id}' has no approval record."

            now = datetime.now(timezone.utc)
            if now.isoformat() > approval["expires_at"]:
                manifest.record_transition(DraftState.VALIDATED)
                registry.save_manifest(manifest)
                return False, f"Approval for draft '{draft_id}' expired. Re-approve to publish."

            skill_dir = registry.skill_dir_for(draft_id)
            current_hash = staging.compute_content_hash(skill_dir)
            if current_hash != approval["content_hash"]:
                manifest.record_transition(DraftState.FAILED)
                registry.save_manifest(manifest)
                return False, (
                    f"Draft '{draft_id}' content changed after approval. Re-validate and re-approve."
                )

            create_dir = get_skill_create_dir(legacy_default=SKILLS_DIR)
            target = resolve_new_skill_path(manifest.name, manifest.category, legacy_default=SKILLS_DIR)
            if str(target) != approval["destination"]:
                manifest.record_transition(DraftState.FAILED)
                registry.save_manifest(manifest)
                return False, (
                    f"Destination for draft '{draft_id}' changed since approval "
                    f"(was '{approval['destination']}', now '{target}'). Re-validate and re-approve."
                )

            # Idempotent-retry check comes BEFORE the generic collision
            # re-check: if target already exists with exactly our approved
            # content, a previous publish attempt already moved the bytes
            # into place and only crashed before updating the manifest --
            # that must short-circuit the "a skill already exists here"
            # collision rejection below, not trip it.
            already_published = (
                target.exists() and staging.compute_content_hash(target) == approval["content_hash"]
            )

            if not already_published:
                dest_check = validation._check_destination(manifest.name, manifest.category)
                if dest_check["status"] != "pass":
                    manifest.record_transition(DraftState.FAILED)
                    registry.save_manifest(manifest)
                    return False, (
                        f"Destination for draft '{draft_id}' is no longer valid: "
                        f"{'; '.join(dest_check['details'])}"
                    )

            try:
                with destination_lock(target):
                    return _publish_locked(manifest, skill_dir, target, create_dir)
            except DraftLockError as e:
                return False, str(e)
    except DraftLockError as e:
        return False, str(e)


def _publish_locked(manifest: DraftManifest, skill_dir: Path, target: Path, create_dir: Path) -> Tuple[bool, Any]:
    """Filesystem publish step -- runs inside both draft_lock and destination_lock."""
    from agent.skill_creation import registry, staging

    approval = manifest.approval

    if target.exists():
        # Idempotent retry: a previous publish may have completed the
        # filesystem move but crashed before the manifest was updated.
        existing_hash = staging.compute_content_hash(target)
        if existing_hash == approval["content_hash"]:
            manifest.publication = {
                "published_at": datetime.now(timezone.utc).isoformat(),
                "published_path": str(target),
                "git": None,
            }
            manifest.record_transition(DraftState.PUBLISHED)
            registry.save_manifest(manifest)
            _clear_prompt_cache()
            return True, manifest
        manifest.record_transition(DraftState.FAILED)
        registry.save_manifest(manifest)
        return False, (
            f"Destination '{target}' already exists with different content. "
            "Refusing to overwrite an existing skill."
        )

    temp_dir = Path(tempfile.mkdtemp(dir=create_dir))
    try:
        shutil.copytree(skill_dir, temp_dir, dirs_exist_ok=True, symlinks=False)
        copied_hash = staging.compute_content_hash(temp_dir)
        if copied_hash != approval["content_hash"]:
            raise RuntimeError(f"Copy verification failed: {copied_hash} != {approval['content_hash']}")

        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            # TOCTOU: something else created it between our check and now.
            raise RuntimeError(f"Destination '{target}' appeared concurrently.")

        os.replace(temp_dir, target)
    except Exception as e:
        shutil.rmtree(temp_dir, ignore_errors=True)
        manifest.record_transition(DraftState.FAILED)
        registry.save_manifest(manifest)
        return False, f"Publish failed for draft '{manifest.draft_id}': {e}"

    manifest.publication = {
        "published_at": datetime.now(timezone.utc).isoformat(),
        "published_path": str(target),
        "git": None,
    }
    manifest.record_transition(DraftState.PUBLISHED)
    registry.save_manifest(manifest)
    _clear_prompt_cache()
    return True, manifest


def _clear_prompt_cache() -> None:
    try:
        from agent.prompt_builder import clear_skills_system_prompt_cache
        clear_skills_system_prompt_cache(clear_snapshot=True)
    except Exception:
        logger.debug("Could not clear skills system prompt cache after publish", exc_info=True)


def publish_git_draft(draft_id: str, push: bool = False) -> Tuple[bool, Any]:
    """Commit (and optionally push) an already-published draft's skill directory.

    Independent of and retryable separately from publish_draft -- a Git
    failure never re-triggers a filesystem re-copy; calling this again is
    the retry path. Pushing requires BOTH skills.publishing.auto_push=true
    in config AND push=True on this call -- two independent signals, per
    "do not push without distinct explicit authorization."
    """
    from agent.skill_creation import registry
    from agent.skill_creation.locking import draft_lock, DraftLockError
    from agent.skill_creation.git_integration import detect_repo_root, commit_published_skill, push_branch

    try:
        with draft_lock(draft_id):
            manifest = registry.load_manifest(draft_id)
            if manifest is None:
                return False, f"Draft '{draft_id}' not found."
            if manifest.state != DraftState.PUBLISHED or not manifest.publication:
                return False, f"Draft '{draft_id}' has not been published yet."

            published_path = Path(manifest.publication["published_path"])
            config = _get_publishing_config()

            if not config["git_enabled"]:
                manifest.publication["git"] = {"attempted": False, "reason": "skills.publishing.git_enabled is false"}
                registry.save_manifest(manifest)
                return True, manifest

            repo_root = detect_repo_root(published_path)
            if repo_root is None:
                manifest.publication["git"] = {
                    "attempted": True, "committed": False, "error": "not a git repository",
                }
                registry.save_manifest(manifest)
                return True, manifest

            if not config["auto_commit"]:
                manifest.publication["git"] = {"attempted": False, "reason": "skills.publishing.auto_commit is false"}
                registry.save_manifest(manifest)
                return True, manifest

            git_result = commit_published_skill(repo_root, published_path, manifest.name)

            if git_result.get("committed") and push and config["auto_push"]:
                push_result = push_branch(repo_root, git_result.get("branch", ""))
                git_result["pushed"] = push_result["pushed"]
                if push_result.get("error"):
                    git_result["push_error"] = push_result["error"]
            elif push and not config["auto_push"]:
                git_result["push_error"] = "push requested but skills.publishing.auto_push is false"

            manifest.publication["git"] = git_result
            manifest.updated_at = datetime.now(timezone.utc).isoformat()
            registry.save_manifest(manifest)
            return True, manifest
    except DraftLockError as e:
        return False, str(e)
