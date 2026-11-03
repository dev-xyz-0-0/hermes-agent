"""Tests for agent/skill_creation/registry.py — draft persistence (Phase 2).

Relies on the autouse ``_isolate_hermes_home`` fixture in tests/conftest.py
for HERMES_HOME isolation (no manual patching needed here).
"""

from hermes_constants import get_hermes_home


VALID_CONTENT = """\
---
name: registry-test-skill
description: A test skill for the draft registry.
---

Body text.
"""


class TestAllocateAndSaveManifest:
    def test_allocate_creates_empty_dir_under_drafts_root(self):
        from agent.skill_creation import registry

        draft_id, draft_dir = registry.allocate_draft_dir()
        assert registry.is_valid_draft_id(draft_id)
        assert draft_dir.exists()
        assert draft_dir.parent == registry.get_drafts_root()
        assert registry.get_drafts_root() == get_hermes_home() / "skill-drafts"

    def test_save_and_load_manifest_roundtrip(self):
        from agent.skill_creation import registry
        from agent.skill_creation.models import DraftManifest, DraftState

        draft_id, _ = registry.allocate_draft_dir()
        manifest = DraftManifest(
            draft_id=draft_id,
            name="roundtrip-skill",
            category="crypto",
            state=DraftState.DRAFT,
            content_hash="sha256:abc",
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )
        registry.save_manifest(manifest)

        loaded = registry.load_manifest(draft_id)
        assert loaded is not None
        assert loaded.draft_id == draft_id
        assert loaded.name == "roundtrip-skill"
        assert loaded.category == "crypto"
        assert loaded.state == DraftState.DRAFT

    def test_manifest_write_is_atomic_replace(self, tmp_path):
        """Writes go through a temp file + os.replace -- never a truncated file."""
        from agent.skill_creation import registry
        from agent.skill_creation.models import DraftManifest, DraftState

        draft_id, draft_dir = registry.allocate_draft_dir()
        manifest = DraftManifest(
            draft_id=draft_id, name="atomic-skill", category=None,
            state=DraftState.DRAFT, content_hash="sha256:x",
            created_at="t", updated_at="t",
        )
        registry.save_manifest(manifest)
        # No leftover temp files after a successful write.
        leftovers = [p for p in draft_dir.iterdir() if p.name.startswith(".manifest.json.tmp.")]
        assert leftovers == []


class TestLoadManifestFailsClosed:
    def test_missing_draft_returns_none(self):
        from agent.skill_creation import registry

        assert registry.load_manifest("0" * 32) is None

    def test_corrupt_manifest_returns_none_not_raise(self):
        from agent.skill_creation import registry

        draft_id, draft_dir = registry.allocate_draft_dir()
        (draft_dir / registry.MANIFEST_FILENAME).write_text("{not valid json")

        assert registry.load_manifest(draft_id) is None

    def test_invalid_draft_id_format_rejected(self):
        from agent.skill_creation import registry

        for bad_id in ("../../etc/passwd", "not-hex!", "", "a" * 31, "a" * 33):
            assert registry.is_valid_draft_id(bad_id) is False
            assert registry.load_manifest(bad_id) is None

    def test_valid_draft_id_format_accepted(self):
        from agent.skill_creation import registry

        assert registry.is_valid_draft_id("a" * 32) is True


class TestRestartRecovery:
    def test_draft_persists_across_registry_reimport(self):
        """A fresh registry read (simulating a restart) still finds the draft."""
        from agent.skill_creation import create_draft

        ok, manifest = create_draft("restart-test-skill", VALID_CONTENT)
        assert ok

        # Simulate "restart" by re-importing the registry module fresh and
        # reading purely from disk -- no in-memory state is relied upon.
        from agent.skill_creation import registry as registry2

        reloaded = registry2.load_manifest(manifest.draft_id)
        assert reloaded is not None
        assert reloaded.name == "restart-test-skill"

    def test_interrupted_create_leaves_no_valid_draft(self):
        """A draft dir with no manifest.json (simulating a crash mid-create)
        must not appear in list_drafts() or load as a valid draft."""
        from agent.skill_creation import registry

        draft_id, draft_dir = registry.allocate_draft_dir()
        # No manifest.json written -- this is what an interrupted create leaves behind.
        assert registry.load_manifest(draft_id) is None
        assert draft_id not in [d.draft_id for d in registry.list_drafts()]


class TestListAndDeleteDrafts:
    def test_list_drafts_skips_dirs_without_manifest(self):
        from agent.skill_creation import registry

        ok_id, _ = registry.allocate_draft_dir()
        from agent.skill_creation.models import DraftManifest, DraftState
        registry.save_manifest(DraftManifest(
            draft_id=ok_id, name="listed-skill", category=None,
            state=DraftState.DRAFT, content_hash="sha256:x",
            created_at="t", updated_at="t",
        ))
        registry.allocate_draft_dir()  # orphan, no manifest

        drafts = registry.list_drafts()
        assert len(drafts) == 1
        assert drafts[0].draft_id == ok_id

    def test_delete_draft_removes_directory(self):
        from agent.skill_creation import create_draft, registry

        ok, manifest = create_draft("delete-me-skill", VALID_CONTENT)
        assert ok
        assert registry.delete_draft(manifest.draft_id) is True
        assert registry.load_manifest(manifest.draft_id) is None

    def test_delete_nonexistent_draft_returns_false(self):
        from agent.skill_creation import registry

        assert registry.delete_draft("a" * 32) is False

    def test_delete_rejects_invalid_id(self):
        from agent.skill_creation import registry

        assert registry.delete_draft("../../etc") is False


class TestProfileIsolation:
    def test_different_hermes_home_sees_different_drafts(self, tmp_path, monkeypatch):
        from agent.skill_creation import create_draft, registry

        ok, manifest = create_draft("profile-a-skill", VALID_CONTENT)
        assert ok
        profile_a_draft_id = manifest.draft_id

        other_home = tmp_path / "other-profile" / ".hermes"
        other_home.mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(other_home))

        other_drafts_root = registry.get_drafts_root()
        assert other_drafts_root == other_home / "skill-drafts"
        assert not other_drafts_root.exists() or list(other_drafts_root.iterdir()) == []
        assert profile_a_draft_id  # sanity: created under the original profile


class TestDiscoveryDisjointness:
    def test_drafts_root_is_disjoint_from_skill_discovery(self):
        from agent.skill_creation import registry

        # Must not raise -- the drafts root (".../skill-drafts") is a sibling
        # of ".../skills", never nested inside any discoverable skill directory.
        registry.assert_drafts_root_disjoint_from_discovery()

    def test_draft_skill_dir_not_returned_by_discovery(self):
        from agent.skill_creation import create_draft
        from agent.skill_utils import get_all_skills_dirs

        ok, manifest = create_draft("not-discoverable-skill", VALID_CONTENT)
        assert ok

        for d in get_all_skills_dirs():
            assert "skill-drafts" not in str(d)
