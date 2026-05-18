"""Tests for cronjob no_agent mode — Python script-driven jobs that skip the LLM.

Covers:

* create_job(no_agent=True) shape, validation, and serialization.
* cronjob(action='create', no_agent=True) tool-level validation.
* cronjob(action='update') flipping no_agent on/off.
* scheduler.run_job short-circuit path: success/silent/failure.
* Python script support in _run_job_script (.py runs via sys.executable).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def hermes_env(tmp_path, monkeypatch):
    """Isolate HERMES_HOME for each test so jobs/scripts don't leak."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "scripts").mkdir()
    (home / "cron").mkdir()

    monkeypatch.setenv("HERMES_HOME", str(home))

    import importlib
    import hermes_constants

    importlib.reload(hermes_constants)

    import cron.jobs
    import cron.scheduler

    importlib.reload(cron.jobs)
    importlib.reload(cron.scheduler)

    return home


# ---------------------------------------------------------------------------
# create_job / update_job: data-layer semantics
# ---------------------------------------------------------------------------


def test_create_job_no_agent_requires_script(hermes_env):
    from cron.jobs import create_job

    with pytest.raises(ValueError, match="no_agent=True requires a script"):
        create_job(prompt=None, schedule="every 5m", no_agent=True)


def test_create_job_no_agent_stores_field(hermes_env):
    from cron.jobs import create_job

    script_path = hermes_env / "scripts" / "watchdog.py"
    script_path.write_text("print('hi')\n")

    job = create_job(
        prompt=None,
        schedule="every 5m",
        script="watchdog.py",
        no_agent=True,
        deliver="local",
    )

    assert job["no_agent"] is True
    assert job["script"] == "watchdog.py"
    assert job["prompt"] in (None, "")


def test_create_job_default_is_not_no_agent(hermes_env):
    from cron.jobs import create_job

    job = create_job(prompt="say hi", schedule="every 5m", deliver="local")
    assert job.get("no_agent") is False


def test_update_job_roundtrips_no_agent_flag(hermes_env):
    from cron.jobs import create_job, update_job, get_job

    script_path = hermes_env / "scripts" / "watchdog.py"
    script_path.write_text("print('hi')\n")

    job = create_job(
        prompt=None,
        schedule="every 5m",
        script="watchdog.py",
        no_agent=True,
        deliver="local",
    )

    update_job(job["id"], {"no_agent": False})
    reloaded = get_job(job["id"])
    assert reloaded["no_agent"] is False

    update_job(job["id"], {"no_agent": True})
    reloaded = get_job(job["id"])
    assert reloaded["no_agent"] is True


# ---------------------------------------------------------------------------
# cronjob tool: API-layer validation
# ---------------------------------------------------------------------------


def test_cronjob_tool_create_no_agent_without_script_errors(hermes_env):
    from tools.cronjob_tools import cronjob

    result = json.loads(
        cronjob(action="create", schedule="every 5m", no_agent=True, deliver="local")
    )

    assert result.get("success") is False
    assert "no_agent=True requires a script" in result.get("error", "")


def test_cronjob_tool_create_no_agent_with_python_script_succeeds(hermes_env):
    from tools.cronjob_tools import cronjob

    script_path = hermes_env / "scripts" / "alert.py"
    script_path.write_text("print('alert')\n")

    result = json.loads(
        cronjob(
            action="create",
            schedule="every 5m",
            script="alert.py",
            no_agent=True,
            deliver="local",
        )
    )

    assert result.get("success") is True
    assert result["job"]["no_agent"] is True
    assert result["job"]["script"] == "alert.py"


def test_cronjob_tool_update_toggles_no_agent_for_python_script(hermes_env):
    from tools.cronjob_tools import cronjob

    script_path = hermes_env / "scripts" / "watchdog.py"
    script_path.write_text("print('hi')\n")

    created = json.loads(
        cronjob(
            action="create",
            schedule="every 5m",
            script="watchdog.py",
            no_agent=True,
            deliver="local",
        )
    )
    job_id = created["job_id"]

    off = json.loads(
        cronjob(action="update", job_id=job_id, no_agent=False, prompt="run")
    )
    assert off["success"] is True
    assert off["job"].get("no_agent") in (False, None)

    on = json.loads(cronjob(action="update", job_id=job_id, no_agent=True))
    assert on["success"] is True
    assert on["job"]["no_agent"] is True


def test_cronjob_tool_update_no_agent_without_script_errors(hermes_env):
    """Flipping no_agent=True on a job that has no script must fail."""
    from tools.cronjob_tools import cronjob

    created = json.loads(
        cronjob(
            action="create",
            schedule="every 5m",
            prompt="do a thing",
            deliver="local",
        )
    )
    job_id = created["job_id"]

    result = json.loads(cronjob(action="update", job_id=job_id, no_agent=True))

    assert result.get("success") is False
    assert "without a script" in result.get("error", "")


def test_cronjob_tool_create_does_not_require_prompt_when_no_agent_python_script(
    hermes_env,
):
    """The 'prompt or skill required' rule is relaxed for no_agent jobs."""
    from tools.cronjob_tools import cronjob

    script_path = hermes_env / "scripts" / "watchdog.py"
    script_path.write_text("print('hi')\n")

    result = json.loads(
        cronjob(
            action="create",
            schedule="every 5m",
            script="watchdog.py",
            no_agent=True,
            deliver="local",
        )
    )

    assert result.get("success") is True


# ---------------------------------------------------------------------------
# scheduler.run_job: short-circuit behavior
# ---------------------------------------------------------------------------


def test_run_job_no_agent_python_output_is_delivered(hermes_env):
    from cron.jobs import create_job
    from cron.scheduler import run_job

    script_path = hermes_env / "scripts" / "alert.py"
    script_path.write_text("print('PYTHON_NO_AGENT_OK')\n")

    job = create_job(
        prompt=None,
        schedule="every 5m",
        script="alert.py",
        no_agent=True,
        deliver="local",
    )

    success, doc, final_response, error = run_job(job)

    assert success is True
    assert error is None
    assert "PYTHON_NO_AGENT_OK" in final_response


def test_run_job_no_agent_python_empty_output_is_silent(hermes_env):
    """Empty stdout → SILENT_MARKER, which suppresses delivery downstream."""
    from cron.jobs import create_job
    from cron.scheduler import run_job, SILENT_MARKER

    script_path = hermes_env / "scripts" / "quiet.py"
    script_path.write_text("# intentionally no output\n")

    job = create_job(
        prompt=None,
        schedule="every 5m",
        script="quiet.py",
        no_agent=True,
        deliver="local",
    )

    success, doc, final_response, error = run_job(job)

    assert success is True
    assert error is None
    assert final_response == SILENT_MARKER


def test_run_job_no_agent_python_script_failure_delivers_error(hermes_env):
    """Non-zero exit → success=False, error alert is the delivered message."""
    from cron.jobs import create_job
    from cron.scheduler import run_job

    script_path = hermes_env / "scripts" / "broken.py"
    script_path.write_text(
        "import sys\n"
        "print('oops', file=sys.stderr)\n"
        "sys.exit(3)\n"
    )

    job = create_job(
        prompt=None,
        schedule="every 5m",
        script="broken.py",
        no_agent=True,
        deliver="local",
    )

    success, doc, final_response, error = run_job(job)

    assert success is False
    assert error is not None
    assert "oops" in final_response or "exited with code 3" in final_response
    assert "Cron watchdog" in final_response


def test_run_job_no_agent_python_never_invokes_aiagent(hermes_env):
    """no_agent jobs must NOT import/construct the AIAgent."""
    from cron.jobs import create_job

    script_path = hermes_env / "scripts" / "alert.py"
    script_path.write_text("print('alert')\n")

    job = create_job(
        prompt=None,
        schedule="every 5m",
        script="alert.py",
        no_agent=True,
        deliver="local",
    )

    with patch("run_agent.AIAgent") as ai_mock:
        from cron.scheduler import run_job

        run_job(job)

    ai_mock.assert_not_called()


# ---------------------------------------------------------------------------
# _run_job_script: Python-script support
# ---------------------------------------------------------------------------


def test_run_job_script_python_runs_via_python(hermes_env):
    """Regression: .py files must run via sys.executable."""
    from cron.scheduler import _run_job_script

    script_path = hermes_env / "scripts" / "py.py"
    script_path.write_text(
        "import sys\n"
        "print(f'python {sys.version_info.major}')\n"
    )

    ok, output = _run_job_script("py.py")

    assert ok is True
    assert output.startswith("python ")


def test_run_job_script_python_can_write_side_effect_file(hermes_env):
    """Python cron script should execute normally and allow expected side effects."""
    from cron.scheduler import _run_job_script

    marker = hermes_env / "scripts" / "marker.txt"
    script_path = hermes_env / "scripts" / "writer.py"
    script_path.write_text(
        "from pathlib import Path\n"
        "Path('marker.txt').write_text('PYTHON_CRON_SIDE_EFFECT_OK')\n"
        "print('done')\n"
    )

    ok, output = _run_job_script("writer.py")

    assert ok is True
    assert "done" in output
    assert marker.read_text() == "PYTHON_CRON_SIDE_EFFECT_OK"


def test_run_job_script_path_traversal_still_blocked(hermes_env):
    """Security regression: Python-script support must NOT loosen containment."""
    from cron.scheduler import _run_job_script

    ok, output = _run_job_script("/etc/passwd")

    assert ok is False
    assert "Blocked" in output or "outside" in output