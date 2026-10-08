from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from outlook_triage.locking import AlreadyRunning, process_lock


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows lock semantics")


def test_process_lock_writes_owner_and_cleans_up_after_success(tmp_path: Path):
    lock = tmp_path / "state" / "sync.lock"
    with process_lock(lock):
        assert lock.is_dir()
        owner = (lock / "owner").read_text(encoding="utf-8")
        assert f"pid={os.getpid()}" in owner
        assert "started=" in owner
    assert not lock.exists()


def test_second_process_lock_is_refused_without_removing_owner(tmp_path: Path):
    lock = tmp_path / "sync.lock"
    with process_lock(lock):
        owner_before = (lock / "owner").read_text(encoding="utf-8")
        with pytest.raises(AlreadyRunning, match="Another synchronization is running"):
            with process_lock(lock):
                pytest.fail("A second lock was acquired")
        assert lock.is_dir()
        assert (lock / "owner").read_text(encoding="utf-8") == owner_before


def test_process_lock_cleans_up_after_exception(tmp_path: Path):
    lock = tmp_path / "sync.lock"
    with pytest.raises(RuntimeError, match="operation failed"):
        with process_lock(lock):
            raise RuntimeError("operation failed")
    assert not lock.exists()


def test_recent_existing_lock_is_not_treated_as_stale(tmp_path: Path):
    lock = tmp_path / "sync.lock"
    lock.mkdir()
    (lock / "owner").write_text("pid=123\nstarted=recent\n", encoding="utf-8")

    with pytest.raises(AlreadyRunning):
        with process_lock(lock, stale_after_seconds=60):
            pytest.fail("A recent lock was reclaimed")

    assert (lock / "owner").read_text(encoding="utf-8") == "pid=123\nstarted=recent\n"


def test_stale_lock_is_reclaimed_and_replaced(tmp_path: Path):
    lock = tmp_path / "sync.lock"
    lock.mkdir()
    old_owner = lock / "owner"
    old_owner.write_text("pid=123\nstarted=old\n", encoding="utf-8")
    stale_time = time.time() - 120
    os.utime(lock, (stale_time, stale_time))

    with process_lock(lock, stale_after_seconds=60):
        owner = (lock / "owner").read_text(encoding="utf-8")
        assert f"pid={os.getpid()}" in owner
        assert "started=old" not in owner

    assert not lock.exists()
