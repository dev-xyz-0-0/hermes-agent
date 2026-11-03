"""Persistent draft registry (Phase 2).

Drafts live under ``~/.hermes/skill-drafts/<draft-id>/`` -- a sibling of
``~/.hermes/skills/``, never inside any directory that
``agent.skill_paths.get_skill_discovery_dirs()`` can return, so drafts are
excluded from normal skill discovery by construction (no filtering needed
anywhere else).

``get_drafts_root()`` re-reads ``HERMES_HOME`` on every call rather than
caching it as a module-level constant at import time -- a module-level
constant would go stale the moment a profile switch (or a test's
``HERMES_HOME`` monkeypatch) happens after this module has already been
imported, the same staleness issue ``agent/skill_paths.py`` was written to
avoid for ``skills.create_dir`` in Phase 1.

Each draft's ``manifest.json`` is written atomically (temp file + rename, via
``tools.skill_manager_tool._atomic_write_text``) so a crash mid-write never
leaves a half-written manifest behind. A draft only "exists" once its
manifest.json has been written -- an interrupted ``create_draft`` leaves an
orphaned directory with no manifest, which every function here treats the
same as "no draft".
"""

import json
import logging
import re
import secrets
import shutil
from pathlib import Path
from typing import List, Optional, Tuple

from hermes_constants import get_hermes_home

from agent.skill_creation.models import DraftManifest

logger = logging.getLogger(__name__)

_DRAFT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
MANIFEST_FILENAME = "manifest.json"
SKILL_SUBDIR = "skill"


def get_drafts_root() -> Path:
    """The current profile's draft registry root. Never cached at import time."""
    return get_hermes_home() / "skill-drafts"


def is_valid_draft_id(draft_id: str) -> bool:
    """Format check -- the traversal/unauthorized-access guard.

    A ``draft_id`` is only ever used to build ``get_drafts_root() / draft_id``,
    so rejecting anything that isn't a bare 32-hex-char token here means a
    crafted id like ``"../../etc"`` never reaches the filesystem.
    """
    return bool(draft_id) and bool(_DRAFT_ID_RE.match(draft_id))


def _draft_dir(draft_id: str) -> Path:
    return get_drafts_root() / draft_id


def _manifest_path(draft_id: str) -> Path:
    return _draft_dir(draft_id) / MANIFEST_FILENAME


def skill_dir_for(draft_id: str) -> Path:
    """The ``skill/`` directory inside a draft where SKILL.md + support files live."""
    return _draft_dir(draft_id) / SKILL_SUBDIR


def generate_draft_id() -> str:
    """Unique, unpredictable draft id (128 bits -- matches ``_DRAFT_ID_RE``)."""
    return secrets.token_hex(16)


def allocate_draft_dir() -> Tuple[str, Path]:
    """Reserve a new, empty draft directory. Returns (draft_id, draft_dir).

    Uses ``mkdir(exist_ok=False)``, which is atomic at the OS level, so two
    concurrent callers can never be handed the same directory. Retries a
    handful of times on the astronomically unlikely id collision.
    """
    drafts_root = get_drafts_root()
    drafts_root.mkdir(parents=True, exist_ok=True)
    for _ in range(5):
        draft_id = generate_draft_id()
        draft_dir = _draft_dir(draft_id)
        try:
            draft_dir.mkdir(parents=True, exist_ok=False)
            return draft_id, draft_dir
        except FileExistsError:
            continue
    raise RuntimeError("Could not allocate a unique draft id after 5 attempts")


def save_manifest(manifest: DraftManifest) -> None:
    """Atomically persist a manifest. Caller's draft dir must already exist."""
    from tools.skill_manager_tool import _atomic_write_text

    draft_dir = _draft_dir(manifest.draft_id)
    draft_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False) + "\n"
    _atomic_write_text(_manifest_path(manifest.draft_id), payload)


def load_manifest(draft_id: str) -> Optional[DraftManifest]:
    """Load a draft's manifest. Returns None for missing/corrupt/invalid-id.

    A corrupt or unreadable manifest is treated exactly like "no such draft"
    rather than raising -- callers report "draft not found", never a 500.
    """
    if not is_valid_draft_id(draft_id):
        return None
    path = _manifest_path(draft_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        logger.warning("Corrupt draft manifest %s: %s", path, e)
        return None
    try:
        return DraftManifest.from_dict(data)
    except (KeyError, ValueError) as e:
        logger.warning("Invalid draft manifest %s: %s", path, e)
        return None


def list_drafts() -> List[DraftManifest]:
    """List all drafts in the current profile's registry (summaries only).

    Skips any draft directory without a valid, readable manifest -- orphaned
    directories from an interrupted create never show up as drafts.
    """
    drafts_root = get_drafts_root()
    if not drafts_root.is_dir():
        return []
    drafts: List[DraftManifest] = []
    for entry in sorted(drafts_root.iterdir()):
        if not entry.is_dir():
            continue
        manifest = load_manifest(entry.name)
        if manifest is not None:
            drafts.append(manifest)
    return drafts


def delete_draft(draft_id: str) -> bool:
    """Remove a draft entirely. Returns False if it didn't exist."""
    if not is_valid_draft_id(draft_id):
        return False
    draft_dir = _draft_dir(draft_id)
    if not draft_dir.exists():
        return False
    try:
        resolved = draft_dir.resolve()
        if not resolved.is_relative_to(get_drafts_root().resolve()):
            # Defense in depth -- should be unreachable given the id regex.
            logger.error("Refusing to delete draft dir outside drafts root: %s", resolved)
            return False
    except OSError:
        return False
    shutil.rmtree(draft_dir, ignore_errors=True)
    return True


def assert_drafts_root_disjoint_from_discovery() -> None:
    """Sanity check: the drafts root must never be inside a skill discovery dir.

    The drafts root isn't user-configurable in Phase 2, so this is a static
    invariant rather than a runtime validation -- it exists so a future
    change can't accidentally nest drafts inside a discoverable directory
    without a loud failure.
    """
    from agent.skill_paths import get_skill_discovery_dirs

    local_skills = get_hermes_home() / "skills"
    drafts_root = get_drafts_root()
    resolved_root = drafts_root.resolve() if drafts_root.exists() else drafts_root
    for discovery_dir in get_skill_discovery_dirs(local_skills):
        resolved_discovery = (
            discovery_dir.resolve() if discovery_dir.exists() else discovery_dir
        )
        if resolved_root == resolved_discovery or str(resolved_root).startswith(
            str(resolved_discovery) + "/"
        ):
            raise AssertionError(
                f"Drafts root {resolved_root} is nested inside a discoverable "
                f"skill directory {resolved_discovery} -- drafts would leak "
                f"into normal skill discovery."
            )
