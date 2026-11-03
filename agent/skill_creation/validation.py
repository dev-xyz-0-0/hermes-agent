"""Draft validation pipeline (Phase 2).

Five checks -- structure, destination, security, dependencies, duplicates --
each producing a ``pass | warning | blocked | error`` verdict. The overall
verdict is the worst of the five. A draft only reaches ``validated`` on
``pass``/``warning``; ``blocked``/``error`` sends it to ``failed``.

Security is intentionally stricter here than
``skill_manager_tool._security_scan_skill()`` (used by ``create``, which
only warns on an agent-created "ask" verdict): validation fails closed --
scanner unavailable or raising is an ``error``, and any non-"allow" decision
(including "ask") is ``blocked``, never silently passed through.

No generated script is ever executed here -- every check is static
(frontmatter parsing, regex scanning, `shutil.which`, env var lookups).
"""

import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from agent.skill_creation import registry
from agent.skill_creation.models import DraftState, ValidationReport, worst_verdict

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------

def _check_structure(skill_dir: Path, name: str, category, content: str) -> Dict[str, Any]:
    from tools.skill_manager_tool import (
        _validate_name,
        _validate_category,
        _validate_frontmatter,
        _validate_content_size,
    )

    errors: List[str] = []
    for err in (
        _validate_name(name),
        _validate_category(category),
        _validate_frontmatter(content),
        _validate_content_size(content),
    ):
        if err:
            errors.append(err)

    if errors:
        return {"status": "blocked", "details": errors}
    return {"status": "pass", "details": []}


# ---------------------------------------------------------------------------
# Destination
# ---------------------------------------------------------------------------

def _check_destination(name: str, category) -> Dict[str, Any]:
    from agent.skill_paths import get_skill_create_dir, resolve_new_skill_path, validate_skill_destination
    from tools.skill_manager_tool import _find_skill, SKILLS_DIR

    create_dir = get_skill_create_dir(legacy_default=SKILLS_DIR)
    proposed = resolve_new_skill_path(name, category, legacy_default=SKILLS_DIR)

    details: List[str] = []
    status = "pass"

    dest_error = validate_skill_destination(create_dir, proposed)
    if dest_error:
        details.append(dest_error)
        status = "blocked"

    existing = _find_skill(name)
    if existing:
        details.append(f"A skill named '{name}' already exists at {existing['path']}.")
        status = "blocked"

    return {
        "status": status,
        "details": details,
        "proposed_destination": str(proposed),
    }


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------

def _check_security(skill_dir: Path) -> Dict[str, Any]:
    try:
        from tools.skills_guard import scan_skill, should_allow_install
    except ImportError as e:
        return {
            "status": "error",
            "details": [f"Security scanner unavailable: {e}. Failing closed."],
        }

    try:
        result = scan_skill(skill_dir, source="agent-created")
        allowed, reason = should_allow_install(result)
    except Exception as e:
        logger.warning("Security scan raised during draft validation: %s", e, exc_info=True)
        return {
            "status": "error",
            "details": [f"Security scanner raised an exception: {e}. Failing closed."],
        }

    findings = [
        {
            "severity": f.severity,
            "category": f.category,
            "file": f.file,
            "line": f.line,
            "description": f.description,
        }
        for f in result.findings
    ]

    # Stricter than create(): any non-"allow" decision blocks (allowed is
    # True only on "allow"; False is "block"; None is "ask" -- both are
    # treated as blocking here so high-risk findings can't slip through
    # unattended during draft validation).
    if allowed is True:
        status = "pass" if result.verdict == "safe" else "warning"
    else:
        status = "blocked"

    return {
        "status": status,
        "details": [reason],
        "verdict": result.verdict,
        "findings": findings,
    }


# ---------------------------------------------------------------------------
# Dependencies -- report only, never installs or executes anything
# ---------------------------------------------------------------------------

def _check_dependencies(frontmatter: Dict[str, Any]) -> Dict[str, Any]:
    from tools.skills_tool import _collect_prerequisite_values

    env_vars, commands = _collect_prerequisite_values(frontmatter)

    available: List[str] = []
    missing: List[str] = []
    for var in env_vars:
        if os.environ.get(var):
            available.append(f"env:{var}")
        else:
            missing.append(f"env:{var}")

    unverifiable: List[str] = []
    for cmd in commands:
        if shutil.which(cmd):
            available.append(f"command:{cmd}")
        elif not cmd or any(c in cmd for c in (" ", "/", "\\")):
            unverifiable.append(f"command:{cmd}")
        else:
            missing.append(f"command:{cmd}")

    details = []
    if missing:
        details.append(f"missing: {', '.join(missing)}")
    if unverifiable:
        details.append(f"unverifiable: {', '.join(unverifiable)}")

    # Dependencies never block validation -- report only.
    return {
        "status": "warning" if (missing or unverifiable) else "pass",
        "details": details,
        "available": available,
        "missing": missing,
        "unverifiable": unverifiable,
    }


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------

def _check_duplicates(name: str, draft_id: str, content_hash: str) -> Dict[str, Any]:
    from tools.skill_manager_tool import _find_skill

    details: List[str] = []
    status = "pass"

    existing = _find_skill(name)
    if existing:
        details.append(f"Exact name collision with published skill at {existing['path']}.")
        status = "blocked"

    for other in registry.list_drafts():
        if other.draft_id == draft_id or other.state == DraftState.CANCELLED:
            continue
        if other.content_hash == content_hash:
            details.append(f"Identical content to draft '{other.draft_id}' ({other.name}).")
            status = worst_verdict([status, "warning"])

    return {"status": status, "details": details}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def validate_draft(draft_id: str) -> Tuple[bool, Any]:
    """Run the full validation pipeline for a draft.

    Returns (True, DraftManifest) on success (manifest has been updated and
    persisted with the new state + report), or (False, error_message).
    """
    manifest = registry.load_manifest(draft_id)
    if manifest is None:
        return False, f"Draft '{draft_id}' not found."

    skill_dir = registry.skill_dir_for(draft_id)
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return False, f"Draft '{draft_id}' has no SKILL.md to validate."

    content = skill_md.read_text(encoding="utf-8")
    from agent.skill_utils import parse_frontmatter

    frontmatter, _ = parse_frontmatter(content)

    manifest.record_transition(DraftState.VALIDATING)
    registry.save_manifest(manifest)

    # Anything unexpected past this point (including a hash computation
    # failure) must still land the draft in FAILED with an "error" check --
    # never leave it stuck in VALIDATING because of an uncaught exception.
    try:
        from agent.skill_creation.staging import compute_content_hash

        current_hash = compute_content_hash(skill_dir)

        checks = {
            "structure": _check_structure(skill_dir, manifest.name, manifest.category, content),
            "destination": _check_destination(manifest.name, manifest.category),
            "security": _check_security(skill_dir),
            "dependencies": _check_dependencies(frontmatter),
            "duplicates": _check_duplicates(manifest.name, draft_id, current_hash),
        }
    except Exception as e:
        logger.error("Draft validation pipeline raised unexpectedly: %s", e, exc_info=True)
        current_hash = manifest.content_hash
        checks = {"pipeline": {"status": "error", "details": [f"Validation pipeline error: {e}"]}}

    overall = worst_verdict([c["status"] for c in checks.values()])

    report = ValidationReport(
        overall=overall,
        checks=checks,
        content_hash=current_hash,
        validated_at=_now_iso(),
    )

    manifest.content_hash = current_hash
    manifest.validation = report
    final_state = DraftState.VALIDATED if overall in ("pass", "warning") else DraftState.FAILED
    manifest.record_transition(final_state)
    registry.save_manifest(manifest)

    return True, manifest
