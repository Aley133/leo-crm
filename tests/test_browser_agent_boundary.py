from __future__ import annotations

import os
import subprocess
import sys


def test_local_browser_agent_import_does_not_require_database_url() -> None:
    env = os.environ.copy()
    env.pop("DATABASE_URL", None)
    result = subprocess.run(
        [sys.executable, "-c", "import tools.browser_agent; print('ok')"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
