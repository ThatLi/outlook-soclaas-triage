"""Fail closed on private file types; smoke-test the bundled CLI without Python."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def verify(root: Path):
    assert (root / "outlook-triage.exe").is_file(), "Missing console program"
    assert (root / "outlook-triage-background.exe").is_file(), "Missing background program"
    for path in root.rglob("*"):
        assert path.name.lower() not in {"secrets.env", ".env", "rules.yaml", "wrangler.jsonc"}, "Private configuration in bundle"
        assert path.suffix.lower() not in {".db", ".sqlite", ".sqlite3", ".log"}, "Runtime data in bundle"
    with tempfile.TemporaryDirectory() as directory:
        env = os.environ.copy()
        # No inherited live credentials or personal runtime paths during smoke checks.
        for key in list(env):
            if key.startswith(("OUTLOOK_", "TELEGRAM_", "SOCLAAS_", "EMAIL_TRIAGE_")):
                del env[key]
        env["APPDATA"] = env["LOCALAPPDATA"] = directory
        for command in (["--help"], ["setup", "--help"], ["doctor", "--help"],
                        ["verify", "--help"], ["background", "plan"]):
            result = subprocess.run([str(root.resolve() / "outlook-triage.exe"), *command],
                                    env=env, capture_output=True, timeout=60)
            assert result.returncode == 0, f"Bundled CLI failed: {command}"
        assert not list(Path(directory).iterdir()), "Help/plan unexpectedly wrote user data"


if __name__ == "__main__":
    verify(Path(sys.argv[1]))
    print("Bundle contains both launchers, passed CLI smoke checks, and contains no runtime/configuration files.")
