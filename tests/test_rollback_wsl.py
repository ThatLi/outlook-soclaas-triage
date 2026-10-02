from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


def _to_wsl_path(path: Path) -> str:
    resolved = path.resolve()
    drive = resolved.drive.rstrip(":").lower()
    suffix = resolved.as_posix().split(":", 1)[1]
    return f"/mnt/{drive}{suffix}"


@pytest.mark.skipif(os.name != "nt", reason="WSL integration test runs from Windows")
def test_wsl_rollback_dry_run_archive_remove_and_restore():
    script = _to_wsl_path(Path(__file__).parents[1] / "scripts" / "rollback_wsl.sh")
    harness = _to_wsl_path(Path(__file__).parent / "fixtures" / "rollback_harness.sh")
    try:
        subprocess.run(
            ["wsl.exe", "--", "bash", harness, script],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError:
        pytest.skip("WSL is unavailable to the test process")
    except subprocess.CalledProcessError as exc:
        error = (exc.stderr or "").replace("\x00", "")
        if "Access is denied" in error or "E_ACCESSDENIED" in error:
            pytest.skip("WSL access is unavailable to the test process")
        raise

