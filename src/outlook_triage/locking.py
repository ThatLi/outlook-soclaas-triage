from __future__ import annotations

import os
import shutil
import time
from contextlib import contextmanager
from pathlib import Path


class AlreadyRunning(RuntimeError):
    pass


@contextmanager
def process_lock(path: Path, stale_after_seconds: int = 7200):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.mkdir()
    except FileExistsError:
        age = time.time() - path.stat().st_mtime
        if age <= stale_after_seconds:
            raise AlreadyRunning(f"Another synchronization is running (lock: {path})")
        shutil.rmtree(path)
        path.mkdir()
    try:
        (path / "owner").write_text(f"pid={os.getpid()}\nstarted={int(time.time())}\n", encoding="utf-8")
        yield
    finally:
        shutil.rmtree(path, ignore_errors=True)

