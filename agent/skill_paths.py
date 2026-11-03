"""Central resolver for where new skills are created and discovered from.

Companion to ``agent/skill_utils.py`` — same import-safety constraints (no
tool registry, no CLI config, no heavy deps at module level) since this is
imported from hot paths (tool dispatch, system prompt building).

``skills.create_dir`` in config.yaml lets users redirect where
``skill_manage(action="create")`` writes new skills (e.g. a shared,
version-controlled skills pack) while legacy installs keep using
``~/.hermes/skills/``. Every function here re-reads config.yaml on each call
instead of caching at import time, so a config edit takes effect on the next
tool call without a restart.
"""

import logging
import os
from pathlib import Path
from typing import List, Optional

from hermes_constants import get_hermes_home
from agent.skill_utils import yaml_load, get_external_skills_dirs

logger = logging.getLogger(__name__)


def _read_skills_config() -> dict:
    config_path = get_hermes_home() / "config.yaml"
    if not config_path.exists():
        return {}
    try:
        parsed = yaml_load(config_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug("Could not read skills config %s: %s", config_path, e)
        return {}
    if not isinstance(parsed, dict):
        return {}
    skills_cfg = parsed.get("skills")
    return skills_cfg if isinstance(skills_cfg, dict) else {}


_VALID_CREATION_POLICIES = frozenset({"legacy", "approval_required"})


def get_skill_creation_policy() -> str:
    """Resolve ``skills.creation_policy``. Returns ``"legacy"`` (today's
    unrestricted behavior -- preserved as the default so existing workflows
    are unaffected) or ``"approval_required"``.

    Unrecognized values fall back to ``"legacy"`` with a warning rather than
    raising or silently enforcing a policy nobody asked for -- a typo in
    config.yaml must not accidentally lock an operator out of their own
    skills, and must not accidentally *relax* enforcement either, so this
    only ever falls back to the permissive default, never escalates.
    """
    raw = _read_skills_config().get("creation_policy")
    if raw is None:
        return "legacy"
    if not isinstance(raw, str) or raw.strip() not in _VALID_CREATION_POLICIES:
        logger.warning(
            "skills.creation_policy must be one of %s; got %r. Using 'legacy'.",
            sorted(_VALID_CREATION_POLICIES), raw,
        )
        return "legacy"
    return raw.strip()


def get_skill_create_dir(legacy_default: Optional[Path] = None) -> Path:
    """Resolve ``skills.create_dir`` to an absolute, normalized path.

    Falls back to *legacy_default* (or ``~/.hermes/skills/`` when not given)
    when unset, blank, non-string, or malformed (relative, degenerate).
    Never raises — a bad config value must not block skill creation.

    ``legacy_default`` lets callers pass their own module-level ``SKILLS_DIR``
    constant so test monkeypatching of that constant keeps working even
    though this function otherwise re-reads config.yaml fresh on every call.
    """
    legacy = legacy_default if legacy_default is not None else get_hermes_home() / "skills"

    skills_cfg = _read_skills_config()
    raw = skills_cfg.get("create_dir")
    if raw is None:
        return legacy
    if not isinstance(raw, str):
        logger.warning("skills.create_dir must be a string path; got %r. Using default.", raw)
        return legacy

    raw = raw.strip()
    if not raw:
        return legacy

    expanded = os.path.expanduser(os.path.expandvars(raw))
    candidate = Path(expanded)
    if not candidate.is_absolute():
        logger.warning(
            "skills.create_dir must be an absolute path (got %r). Using default.", raw
        )
        return legacy

    try:
        resolved = candidate.resolve()
    except OSError as e:
        logger.warning("skills.create_dir %r could not be resolved (%s). Using default.", raw, e)
        return legacy

    if str(resolved) == resolved.anchor:
        # Degenerate value such as "/" — refuse to use the filesystem root.
        logger.warning("skills.create_dir resolves to the filesystem root (%r). Using default.", raw)
        return legacy

    return resolved


def resolve_new_skill_path(
    name: str, category: Optional[str] = None, legacy_default: Optional[Path] = None
) -> Path:
    """Build the directory path for a new skill under the configured create_dir."""
    base = get_skill_create_dir(legacy_default)
    if category:
        return base / category / name
    return base / name


def validate_skill_destination(base_dir: Path, target_dir: Path) -> Optional[str]:
    """Canonical containment check: *target_dir* must resolve under *base_dir*.

    Defense-in-depth against a symlinked ``create_dir`` (or a symlink placed
    inside it) escaping its intended root. Returns an error message, or None
    if the destination is safe. Tolerant of paths that don't exist yet —
    resolves as far as possible and checks the nearest existing ancestor.
    """
    try:
        resolved_base = base_dir.resolve()
    except OSError as e:
        return f"Could not resolve base directory {base_dir}: {e}"

    # target_dir itself usually doesn't exist yet (it's about to be created).
    # Resolve its existing ancestors to catch a symlink anywhere in the chain,
    # then rejoin the not-yet-created tail.
    ancestor = target_dir
    tail: List[str] = []
    while not ancestor.exists() and ancestor != ancestor.parent:
        tail.append(ancestor.name)
        ancestor = ancestor.parent
    try:
        resolved_ancestor = ancestor.resolve()
    except OSError as e:
        return f"Could not resolve destination {target_dir}: {e}"
    resolved_target = resolved_ancestor.joinpath(*reversed(tail)) if tail else resolved_ancestor

    try:
        resolved_target.relative_to(resolved_base)
    except ValueError:
        return f"Destination '{target_dir}' escapes the skill creation root '{base_dir}'."
    return None


def get_skill_discovery_dirs(legacy_default: Path) -> List[Path]:
    """Return discovery dirs: [legacy_default, create_dir (if distinct), *external_dirs].

    Deduplicated by resolved path, first-seen order wins. ``legacy_default``
    is caller-supplied (rather than always ``get_hermes_home()/"skills"``) so
    each call site can pass its own module-level ``SKILLS_DIR`` constant and
    keep working under test monkeypatching of that constant.
    """
    dirs: List[Path] = [legacy_default]
    seen = {legacy_default.resolve()} if legacy_default.exists() else {legacy_default}

    try:
        create_dir = get_skill_create_dir(legacy_default)
        create_key = create_dir.resolve() if create_dir.exists() else create_dir
        if create_key not in seen:
            dirs.append(create_dir)
            seen.add(create_key)
    except Exception:
        logger.debug("Could not resolve skills.create_dir for discovery", exc_info=True)

    for ext_dir in get_external_skills_dirs():
        ext_key = ext_dir.resolve() if ext_dir.exists() else ext_dir
        if ext_key not in seen:
            dirs.append(ext_dir)
            seen.add(ext_key)

    return dirs
