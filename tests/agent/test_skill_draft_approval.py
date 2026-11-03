"""Tests for agent/skill_creation/__init__.py's approve_draft (Phase 3 + 3.1).

approve_draft is the entire authority boundary between "the agent asked to
publish something" and "a human actually said yes" -- it must block on
tools.approval.request_explicit_approval() before writing any approval
record. Most tests here mock that call explicitly (either approved or
denied) rather than exercising a real gateway/CLI channel end-to-end --
that plumbing is covered by tests/tools/test_approval_mcp.py already, and
is exercised once here for real in TestAgentCannotSelfApprove.
"""

import time
from contextlib import contextmanager
from unittest.mock import patch

from agent.skill_creation import create_draft, validate_draft, approve_draft
from agent.skill_creation.models import DraftState

VALID_CONTENT = """\
---
name: approval-test-skill
description: A test skill for the approval step.
---

Body text.
"""


@contextmanager
def _human_approval(approved: bool):
    """Mock the one-shot human-approval gate approve_draft calls through."""
    with patch("tools.approval.request_explicit_approval", return_value=approved):
        yield


def _make_validated_draft(name="approval-test-skill"):
    content = VALID_CONTENT.replace("approval-test-skill", name)
    ok, manifest = create_draft(name, content)
    assert ok
    ok2, manifest2 = validate_draft(manifest.draft_id)
    assert ok2
    return manifest2


class TestApproveDraft:
    def test_approve_requires_validated_state(self):
        ok, manifest = create_draft("not-validated-skill", VALID_CONTENT.replace(
            "approval-test-skill", "not-validated-skill"
        ))
        assert ok
        with _human_approval(True):
            ok2, err = approve_draft(manifest.draft_id)
        assert ok2 is False
        assert "validated" in err.lower()

    def test_approve_moves_to_awaiting_approval(self):
        manifest = _make_validated_draft("approve-success-skill")
        with _human_approval(True):
            ok, result = approve_draft(manifest.draft_id, ttl_seconds=900)
        assert ok
        assert result.state == DraftState.AWAITING_APPROVAL
        assert result.approval is not None

    def test_approval_binds_content_hash_and_destination(self):
        manifest = _make_validated_draft("approve-binding-skill")
        with _human_approval(True):
            ok, result = approve_draft(manifest.draft_id)
        assert ok
        assert result.approval["content_hash"] == manifest.content_hash
        assert result.approval["destination"] == manifest.destination["proposed_path"]

    def test_approval_has_expiry(self):
        manifest = _make_validated_draft("approve-expiry-skill")
        with _human_approval(True):
            ok, result = approve_draft(manifest.draft_id, ttl_seconds=5)
        assert ok
        assert result.approval["expires_at"] > result.approval["approved_at"]

    def test_approve_unknown_draft(self):
        with _human_approval(True):
            ok, err = approve_draft("a" * 32)
        assert ok is False
        assert "not found" in err.lower()

    def test_approve_records_approver(self):
        manifest = _make_validated_draft("approve-approver-skill")
        with _human_approval(True):
            ok, result = approve_draft(manifest.draft_id, approved_by="alice")
        assert ok
        assert result.approval["approved_by"] == "alice"

    def test_approve_records_session_key(self):
        manifest = _make_validated_draft("approve-session-key-skill")
        with _human_approval(True):
            ok, result = approve_draft(manifest.draft_id)
        assert ok
        assert "session_key" in result.approval

    def test_approve_rejects_failed_draft(self):
        ok, manifest = create_draft("blocked-draft-skill", VALID_CONTENT.replace(
            "approval-test-skill", "blocked-draft-skill"
        ))
        assert ok
        with patch(
            "agent.skill_creation.validation._check_security",
            return_value={"status": "blocked", "details": ["dangerous"]},
        ):
            ok2, manifest2 = validate_draft(manifest.draft_id)
        assert ok2
        assert manifest2.state == DraftState.FAILED

        with _human_approval(True):
            ok3, err = approve_draft(manifest.draft_id)
        assert ok3 is False
        assert "validated" in err.lower()

    def test_approve_requires_passing_validation(self):
        """A draft whose validation.overall is blocked/error can't be approved
        even if somehow its state were VALIDATED (defense in depth)."""
        from agent.skill_creation import registry
        from agent.skill_creation.models import ValidationReport

        manifest = _make_validated_draft("approve-bad-report-skill")
        # Force a blocked report onto an otherwise-validated manifest to
        # simulate a data inconsistency and confirm approve() still refuses.
        manifest.validation = ValidationReport(
            overall="blocked", checks={}, content_hash=manifest.content_hash, validated_at="t",
        )
        registry.save_manifest(manifest)

        with _human_approval(True):
            ok, err = approve_draft(manifest.draft_id)
        assert ok is False
        assert "passing validation" in err.lower()


class TestAgentCannotSelfApprove:
    """P0 security requirement: a tool call alone must never constitute
    approval. These exercise the REAL tools.approval machinery (not a mock)
    to prove the agent has no path to self-approval.
    """

    def test_no_interactive_channel_fails_closed(self, monkeypatch):
        """The exact "agent calls the tool, nobody else involved" scenario:
        no CLI TTY, no gateway session registered. Must deny, not silently
        succeed just because the tool was invoked with valid-looking args."""
        monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
        monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
        monkeypatch.setenv("HERMES_SESSION_KEY", "self-approve-attempt")

        manifest = _make_validated_draft("self-approve-no-channel-skill")
        ok, err = approve_draft(manifest.draft_id, ttl_seconds=900)

        assert ok is False
        assert "not approved by a human" in err.lower()

        from agent.skill_creation import registry
        reloaded = registry.load_manifest(manifest.draft_id)
        assert reloaded.state == DraftState.VALIDATED  # never moved to awaiting_approval
        assert reloaded.approval is None

    def test_gateway_deny_is_honored_not_overridden(self, monkeypatch):
        """A human explicitly denying via /deny must be honored -- the agent
        cannot retry its way past a real denial by calling the tool again
        with the same arguments (each call opens a fresh approval request)."""
        from tools import approval

        session_key = "self-approve-deny-test"
        approval.register_gateway_notify(session_key, lambda data: None)
        monkeypatch.setenv("HERMES_SESSION_KEY", session_key)

        import threading
        manifest = _make_validated_draft("self-approve-deny-skill")
        result = {}

        def worker():
            result["ok"], result["res"] = approve_draft(manifest.draft_id)

        thread = threading.Thread(target=worker)
        thread.start()
        deadline = time.time() + 2
        while time.time() < deadline and approval.pending_approval_count(session_key) == 0:
            time.sleep(0.01)
        assert approval.pending_approval_count(session_key) > 0

        approval.resolve_gateway_approval(session_key, "deny")
        thread.join(timeout=2)

        assert result["ok"] is False
        from agent.skill_creation import registry
        reloaded = registry.load_manifest(manifest.draft_id)
        assert reloaded.state == DraftState.VALIDATED

        approval.unregister_gateway_notify(session_key)
        approval.clear_session(session_key)

    def test_gateway_approve_once_succeeds(self, monkeypatch):
        """Sanity check for the positive path: a real human resolving
        /approve once through the gateway channel does grant approval."""
        from tools import approval

        session_key = "self-approve-approve-test"
        approval.register_gateway_notify(session_key, lambda data: None)
        monkeypatch.setenv("HERMES_SESSION_KEY", session_key)

        import threading
        manifest = _make_validated_draft("human-approves-for-real-skill")
        result = {}

        def worker():
            result["ok"], result["res"] = approve_draft(manifest.draft_id)

        thread = threading.Thread(target=worker)
        thread.start()
        deadline = time.time() + 2
        while time.time() < deadline and approval.pending_approval_count(session_key) == 0:
            time.sleep(0.01)
        assert approval.pending_approval_count(session_key) > 0

        approval.resolve_gateway_approval(session_key, "once")
        thread.join(timeout=2)

        assert result["ok"] is True
        assert result["res"].state == DraftState.AWAITING_APPROVAL

        approval.unregister_gateway_notify(session_key)
        approval.clear_session(session_key)

    def test_gateway_session_choice_does_not_grant_draft_approval(self, monkeypatch):
        """request_explicit_approval treats 'session'/'always' as non-grants
        for generic actions (only 'once' counts) -- confirm that still holds
        for draft approval specifically, so a broad session-level dangerous-
        command allowlist can't be repurposed to rubber-stamp publication."""
        from tools import approval

        session_key = "self-approve-session-choice-test"
        approval.register_gateway_notify(session_key, lambda data: None)
        monkeypatch.setenv("HERMES_SESSION_KEY", session_key)

        import threading
        manifest = _make_validated_draft("session-choice-skill")
        result = {}

        def worker():
            result["ok"], result["res"] = approve_draft(manifest.draft_id)

        thread = threading.Thread(target=worker)
        thread.start()
        deadline = time.time() + 2
        while time.time() < deadline and approval.pending_approval_count(session_key) == 0:
            time.sleep(0.01)

        approval.resolve_gateway_approval(session_key, "session")
        thread.join(timeout=2)

        assert result["ok"] is False

        approval.unregister_gateway_notify(session_key)
        approval.clear_session(session_key)


class TestApprovalExpiryIsNeverCached:
    def test_expiry_check_reads_fresh_from_disk(self):
        """'Gateway restart does not resurrect expired approval' -- expiry is
        a timestamp on disk, re-read and re-checked on every call, never
        cached in memory across a simulated process restart (reloading the
        manifest fresh from the registry)."""
        from agent.skill_creation import registry, publish_draft

        manifest = _make_validated_draft("approve-restart-skill")
        with _human_approval(True):
            ok, approved = approve_draft(manifest.draft_id, ttl_seconds=0)
        assert ok
        time.sleep(0.01)

        # Simulate "restart": drop all in-memory references, reload fresh.
        del approved
        reloaded = registry.load_manifest(manifest.draft_id)
        assert reloaded is not None
        assert reloaded.state == DraftState.AWAITING_APPROVAL  # not yet re-checked

        ok2, err = publish_draft(manifest.draft_id)
        assert ok2 is False
        assert "expired" in err.lower()

        final = registry.load_manifest(manifest.draft_id)
        assert final.state == DraftState.VALIDATED  # demoted, ready to re-approve
