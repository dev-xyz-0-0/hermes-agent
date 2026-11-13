"""Phase 7 — "Optional viewer dependencies are not imported during normal
Hermes startup" (docs/plans/demo-reference-plan/checklist.md section 11,
"Viewer tests"; implementation-plan.md's "9.10").

Runs actual fresh-interpreter subprocesses (never just reasons about import
statements) and checks `sys.modules` after importing each normal entrypoint
module, per the task's explicit guidance: "run it by invoking the actual
CLI entrypoint in a subprocess ... or by checking sys.modules after
importing hermes_cli.main/run_agent/cli in a fresh interpreter."
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
VIEWER_MODULE = "plugins.memory.holographic.viewer"


def _run_fresh_interpreter(import_statement: str) -> str:
    """Import `import_statement` in a brand-new interpreter and report
    whether the viewer module ended up in sys.modules anyway (e.g. via a
    transitive import nobody noticed)."""
    code = (
        "import sys\n"
        f"{import_statement}\n"
        f"print('VIEWER_IMPORTED=' + str({VIEWER_MODULE!r} in sys.modules))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"subprocess import failed (stdout={result.stdout!r}, stderr={result.stderr!r})"
    )
    for line in result.stdout.splitlines():
        if line.startswith("VIEWER_IMPORTED="):
            return line.split("=", 1)[1]
    raise AssertionError(f"marker line not found in subprocess output: {result.stdout!r} / {result.stderr!r}")


class TestViewerNotImportedByNormalStartup:
    def test_run_agent_does_not_import_viewer(self):
        assert _run_fresh_interpreter("import run_agent") == "False"

    def test_cli_module_does_not_import_viewer(self):
        assert _run_fresh_interpreter("import cli") == "False"

    def test_hermes_cli_main_does_not_import_viewer(self):
        assert _run_fresh_interpreter("import hermes_cli.main") == "False"

    def test_model_tools_does_not_import_viewer(self):
        assert _run_fresh_interpreter("import model_tools") == "False"

    def test_holographic_plugin_itself_does_not_import_viewer(self):
        """Importing the core plugin package (what happens whenever the
        Holographic memory provider is configured/used normally) must not
        transitively pull in the viewer module either."""
        assert _run_fresh_interpreter("import plugins.memory.holographic") == "False"

    def test_memory_setup_module_does_not_import_viewer_at_module_level(self):
        """`hermes_cli.memory_setup` defines `cmd_viewer`, but it must only
        import `plugins.memory.holographic.viewer` lazily inside that
        function body — importing the module itself must not trigger it."""
        assert _run_fresh_interpreter("import hermes_cli.memory_setup") == "False"

    def test_explicitly_importing_the_viewer_module_does_work(self):
        """Sanity check that the marker mechanism itself is correct — the
        viewer module does exist and CAN be imported when asked for."""
        assert _run_fresh_interpreter("import plugins.memory.holographic.viewer") == "True"
