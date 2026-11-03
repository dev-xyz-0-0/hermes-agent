"""Data model for the skill draft lifecycle (Phase 2).

Dependency-light by design (same constraint as ``agent/skill_utils.py`` and
``agent/skill_paths.py``) — only stdlib imports, safe to import at module
level from hot paths.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class DraftState(str, Enum):
    """Lifecycle states for a skill draft.

    ``AWAITING_APPROVAL``, ``REJECTED``, and ``PUBLISHED`` are defined here
    for forward compatibility with Phase 3 (approval + publication) but are
    never set by Phase 2's own actions (``draft``, ``validate``, ``preview``,
    ``discard``, ``drafts_list``).
    """

    DRAFT = "draft"
    VALIDATING = "validating"
    VALIDATED = "validated"
    AWAITING_APPROVAL = "awaiting_approval"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PUBLISHED = "published"


# States Phase 2's own transitions may move a draft into or out of.
# (AWAITING_APPROVAL / REJECTED / PUBLISHED are Phase 3's responsibility.)
_VALID_TRANSITIONS: Dict[DraftState, set] = {
    DraftState.DRAFT: {DraftState.VALIDATING, DraftState.CANCELLED},
    DraftState.VALIDATING: {DraftState.VALIDATED, DraftState.FAILED, DraftState.CANCELLED},
    DraftState.VALIDATED: {DraftState.VALIDATING, DraftState.DRAFT, DraftState.CANCELLED},
    DraftState.FAILED: {DraftState.VALIDATING, DraftState.DRAFT, DraftState.CANCELLED},
    DraftState.CANCELLED: set(),
}


def is_valid_transition(current: DraftState, target: DraftState) -> bool:
    """Whether *current* -> *target* is a transition Phase 2 is allowed to make."""
    return target in _VALID_TRANSITIONS.get(current, set())


# Check verdicts, worst-to-best.
_VERDICT_PRIORITY = {"error": 3, "blocked": 2, "warning": 1, "pass": 0}


def worst_verdict(verdicts: List[str]) -> str:
    """Return the worst of several check verdicts (error > blocked > warning > pass)."""
    if not verdicts:
        return "pass"
    return max(verdicts, key=lambda v: _VERDICT_PRIORITY.get(v, 3))


@dataclass
class ValidationReport:
    """Machine-readable validation result, persisted into the manifest."""

    overall: str  # "pass" | "warning" | "blocked" | "error"
    checks: Dict[str, Dict[str, Any]]  # name -> {"status": ..., "details": [...]}
    content_hash: str  # hash of skill/ at the moment this report was produced
    validated_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "overall": self.overall,
            "checks": self.checks,
            "content_hash": self.content_hash,
            "validated_at": self.validated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ValidationReport":
        return cls(
            overall=data.get("overall", "error"),
            checks=data.get("checks", {}),
            content_hash=data.get("content_hash", ""),
            validated_at=data.get("validated_at", ""),
        )


@dataclass
class DraftManifest:
    """Persisted record for one skill draft (``<draft_id>/manifest.json``)."""

    draft_id: str
    name: str
    category: Optional[str]
    state: DraftState
    content_hash: str
    created_at: str
    updated_at: str
    creator: Dict[str, Any] = field(default_factory=dict)
    destination: Dict[str, Any] = field(default_factory=dict)
    state_history: List[Dict[str, str]] = field(default_factory=list)
    validation: Optional[ValidationReport] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "name": self.name,
            "category": self.category,
            "state": self.state.value,
            "content_hash": self.content_hash,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "creator": self.creator,
            "destination": self.destination,
            "state_history": self.state_history,
            "validation": self.validation.to_dict() if self.validation else None,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DraftManifest":
        validation = data.get("validation")
        return cls(
            draft_id=data["draft_id"],
            name=data["name"],
            category=data.get("category"),
            state=DraftState(data.get("state", "draft")),
            content_hash=data.get("content_hash", ""),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            creator=data.get("creator", {}),
            destination=data.get("destination", {}),
            state_history=data.get("state_history", []),
            validation=ValidationReport.from_dict(validation) if validation else None,
        )

    def record_transition(self, new_state: DraftState, now: Optional[str] = None) -> None:
        """Move to *new_state*, enforcing the lifecycle state machine.

        Raises ValueError on an illegal transition (e.g. CANCELLED -> anything,
        or skipping straight from DRAFT to VALIDATED without validating).
        """
        if not is_valid_transition(self.state, new_state):
            raise ValueError(
                f"Invalid draft state transition: {self.state.value} -> {new_state.value}"
            )
        now = now or _now_iso()
        self.state_history.append({"state": new_state.value, "at": now})
        self.state = new_state
        self.updated_at = now


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
