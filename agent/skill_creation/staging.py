"""Draft filesystem staging (Phase 2).

Writes the ``skill/`` tree inside a draft directory and reuses
``tools.skill_manager_tool``'s existing structural validators so a draft's
SKILL.md is held to exactly the same bar as a published skill's.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agent.skill_creation.models import DraftState
from agent.skill_creation import registry


def write_draft_skill(
    draft_dir: Path, name: str, content: str, category: Optional[str] = None
) -> Optional[str]:
    """Validate and write SKILL.md into ``draft_dir/skill/``.

    Returns an error message, or None on success. Reuses
    ``skill_manager_tool``'s name/category/frontmatter/size validators --
    the single source of truth for "is this a valid SKILL.md" that
    ``create`` already uses.
    """
    from tools.skill_manager_tool import (
        _validate_name,
        _validate_category,
        _validate_frontmatter,
        _validate_content_size,
        _atomic_write_text,
    )

    err = _validate_name(name)
    if err:
        return err
    err = _validate_category(category)
    if err:
        return err
    err = _validate_frontmatter(content)
    if err:
        return err
    err = _validate_content_size(content)
    if err:
        return err

    skill_dir = draft_dir / registry.SKILL_SUBDIR
    skill_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(skill_dir / "SKILL.md", content)
    return None


def compute_content_hash(skill_dir: Path) -> str:
    """Hash covering every file under ``skill/`` -- not just SKILL.md."""
    from tools.skills_guard import content_hash

    return content_hash(skill_dir)


def update_draft_file(draft_id: str, rel_path: str, content: str) -> Optional[str]:
    """Rewrite a file inside a draft's ``skill/`` tree and invalidate validation.

    Internal primitive -- not wired to a ``skill_manage`` action in Phase 2
    (the action list is exactly draft/validate/preview/discard/drafts_list).
    It exists so content mutation has a single, well-defined place that:
      1. writes the file atomically,
      2. recomputes the manifest's content_hash,
      3. demotes ``state`` from VALIDATED back to DRAFT when the hash
         actually changed, since the previous validation no longer reflects
         what's on disk.

    Returns an error message, or None on success.
    """
    from tools.skill_manager_tool import _validate_file_path, _atomic_write_text

    manifest = registry.load_manifest(draft_id)
    if manifest is None:
        return f"Draft '{draft_id}' not found."

    if rel_path != "SKILL.md":
        err = _validate_file_path(rel_path)
        if err:
            return err

    skill_dir = registry.skill_dir_for(draft_id)
    target = skill_dir / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(target, content)

    new_hash = compute_content_hash(skill_dir)
    if new_hash != manifest.content_hash:
        manifest.content_hash = new_hash
        if manifest.state in (DraftState.VALIDATED, DraftState.FAILED):
            manifest.record_transition(DraftState.DRAFT)
        else:
            manifest.updated_at = datetime.now(timezone.utc).isoformat()
        registry.save_manifest(manifest)
    return None
