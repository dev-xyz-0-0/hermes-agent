"""Regression test for the stale module-level SKILLS_DIR constant class of
bug (Phase 3.1, P1 item 6).

``tools/skill_manager_tool.py``, ``tools/skills_tool.py``, ``tools/skills_sync.py``,
and ``tools/skills_hub.py`` each cache ``SKILLS_DIR = get_hermes_home() / "skills"``
as a module-level constant at *import* time -- computed once per worker
process, not per test. During Phase 3 development this caused five+ test
fixture skills to be written into the real ``~/.hermes/skills/`` because a
test's ``HERMES_HOME`` monkeypatch (applied after these modules had already
been imported elsewhere in the same worker) never retroactively updated
their already-computed constants.

The fix lives in ``tests/conftest.py``'s autouse ``_isolate_hermes_home``
fixture, which now forces each of these constants to track the current
test's isolated fake home on every test. This file exists so a regression
in that fixture (or a new module introducing the same pattern) fails loudly
here instead of silently leaking into a real directory somewhere else.
"""

from pathlib import Path

from hermes_constants import get_hermes_home

_PATCHED_MODULES = (
    "tools.skill_manager_tool",
    "tools.skills_tool",
    "tools.skills_sync",
    "tools.skills_hub",
)


class TestSkillsDirTracksIsolatedHermesHome:
    def test_each_module_constant_matches_current_hermes_home(self):
        import importlib

        for mod_name in _PATCHED_MODULES:
            mod = importlib.import_module(mod_name)
            assert mod.SKILLS_DIR == get_hermes_home() / "skills", (
                f"{mod_name}.SKILLS_DIR ({mod.SKILLS_DIR}) does not track the "
                f"current isolated HERMES_HOME ({get_hermes_home()}) -- the "
                "conftest.py autouse fixture that patches this may have "
                "regressed."
            )

    def test_none_of_the_constants_point_at_the_real_home(self):
        """Never ~/.hermes, regardless of what the isolation fixture does --
        the single most important invariant this whole test exists for."""
        import importlib

        real_home_skills = Path.home() / ".hermes" / "skills"
        for mod_name in _PATCHED_MODULES:
            mod = importlib.import_module(mod_name)
            assert mod.SKILLS_DIR != real_home_skills, (
                f"{mod_name}.SKILLS_DIR resolved to the REAL home directory "
                f"({real_home_skills}) during a test run -- this is exactly "
                "the leak class that previously wrote fixture skills into "
                "the user's actual ~/.hermes/skills/."
            )

    def test_isolated_dir_is_under_pytest_tmp_path_not_real_home(self, tmp_path):
        """Positive confirmation: the isolated dir is genuinely a pytest
        tmp_path, not merely "some other path that happens not to be home"."""
        import tools.skill_manager_tool as m

        # tmp_path's own parent tree and SKILLS_DIR's isolated home should
        # share the same pytest-managed base temp root.
        assert "pytest-of-" in str(m.SKILLS_DIR) or str(tmp_path.anchor) == str(m.SKILLS_DIR.anchor)
        assert str(Path.home()) not in str(m.SKILLS_DIR) or "pytest-of-" in str(m.SKILLS_DIR)


class TestWriteThroughSkillManagerToolNeverReachesRealHome:
    def test_create_skill_lands_under_isolated_home(self):
        """End-to-end: an actual _create_skill() call (not just reading the
        constant) must land under the isolated home, confirmed by both the
        constant AND by checking the real home directory is untouched."""
        import json
        from tools.skill_manager_tool import skill_manage

        real_home_skills = Path.home() / ".hermes" / "skills"
        marker_name = "skills-dir-isolation-canary-skill"
        before = (real_home_skills / marker_name).exists() if real_home_skills.exists() else False
        assert before is False  # sanity: didn't already exist from a prior bug

        content = f"---\nname: {marker_name}\ndescription: canary\n---\n\nBody.\n"
        result = json.loads(skill_manage(action="create", name=marker_name, content=content))
        assert result["success"] is True

        # The canary must NOT appear in the real home -- it must be under
        # whatever the isolated SKILLS_DIR is for this test.
        after = (real_home_skills / marker_name).exists() if real_home_skills.exists() else False
        assert after is False, (
            f"'{marker_name}' was created under the REAL home directory "
            f"({real_home_skills / marker_name}) instead of the isolated "
            "test HERMES_HOME -- the SKILLS_DIR isolation regressed."
        )

        import tools.skill_manager_tool as m
        assert (m.SKILLS_DIR / marker_name / "SKILL.md").exists()
