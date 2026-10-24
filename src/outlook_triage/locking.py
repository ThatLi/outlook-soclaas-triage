from __future__ import annotations

import os
import shutil
import time
from contextlib import contextmanager
from pathlib import Path


class AlreadyRunning(RuntimeError):
    pass


def _owner_is_dead(path: Path) -> bool:
    """Recover a bridge after Task Scheduler/power loss without waiting two hours.

    Unknown or inaccessible owners stay locked. Never use os.kill(pid, 0) on
    Windows: non-console signals there can terminate the process.
    """
    try:
        first = (path / "owner").read_text(encoding="utf-8").splitlines()[0]
        if not first.startswith("pid="):
            return False
        owner_pid = int(first[4:])
        if owner_pid <= 0:
            return False
    except (OSError, ValueError, IndexError):
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, owner_pid)
        if not handle:
            return ctypes.get_last_error() == 87  # ERROR_INVALID_PARAMETER: no such PID
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value != 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(owner_pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


@contextmanager
def process_lock(path: Path, stale_after_seconds: int = 7200, *, label: str = "synchronization",
                 recover_dead_owner: bool = False):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.mkdir()
    except FileExistsError:
        age = time.time() - path.stat().st_mtime
        if age <= stale_after_seconds and not (recover_dead_owner and _owner_is_dead(path)):
            raise AlreadyRunning(f"Another {label} is running (lock: {path})")
        shutil.rmtree(path)
        path.mkdir()
    try:
        (path / "owner").write_text(f"pid={os.getpid()}\nstarted={int(time.time())}\n", encoding="utf-8")
        yield
    finally:
        shutil.rmtree(path, ignore_errors=True)

