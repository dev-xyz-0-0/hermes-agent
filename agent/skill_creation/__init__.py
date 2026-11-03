"""Skill draft lifecycle and validation (Phase 2).

Public entry points used by ``tools/skill_manager_tool.py``'s ``skill_manage``
dispatcher: ``create_draft``, ``validate_draft``, ``preview_draft``,
``discard_draft``, ``list_draft_summaries``.

See ``docs/hermes-skill-creation-plan/phase-02-drafts-validation.md`` for the
full spec. Drafts are staged under ``~/.hermes/skill-drafts/`` and are never
discoverable as published skills -- publication is Phase 3's job, not this
module's.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from agent.skill_creation.models import DraftManifest, DraftState


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
