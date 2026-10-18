import shutil
import subprocess
from pathlib import Path

import pytest


def test_browser_session_switch_ignores_stale_responses() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js and frontend npm ci are needed for the production runtime adapter regression")

    script = Path(__file__).with_name("ui_session_race.test.cjs")
    result = subprocess.run(
        [node, "--test", str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
