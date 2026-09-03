"""The cross-process scan lock must work on macOS *and* Windows.

Before this module existed the scanner imported ``fcntl`` unconditionally, so
the Windows release installed successfully and then failed to serve: the
version check only imports ``sf_housing``, while ``sf_housing.app`` pulls in
the scanner. These tests exercise the real POSIX path and drive the Windows
path through an isolated module instance, since CI runs on macOS.
"""

from __future__ import annotations

import builtins
import errno
import importlib.util
from pathlib import Path

import pytest

from sf_housing import filelock


class RecordingMsvcrt:
    """Enough of ``msvcrt`` to execute the Windows branch faithfully."""

    LK_NBLCK = 2
    LK_UNLCK = 0

    def __init__(self, error: OSError | None = None) -> None:
        self.error = error
        self.calls: list[tuple[int, int]] = []

    def locking(self, fd: int, mode: int, nbytes: int) -> None:
        self.calls.append((mode, nbytes))
        if self.error is not None and mode == self.LK_NBLCK:
            raise self.error


def load_windows_filelock(fake_msvcrt: RecordingMsvcrt):
    """Execute filelock.py as it would run on Windows, without touching the real module."""
    real_import = builtins.__import__

    def guard(name, *args, **kwargs):
        if name == "fcntl":
            raise ModuleNotFoundError("No module named 'fcntl'")
        if name == "msvcrt":
            return fake_msvcrt
        return real_import(name, *args, **kwargs)

    spec = importlib.util.spec_from_file_location(
        "sf_housing._filelock_windows_under_test", Path(filelock.__file__)
    )
    module = importlib.util.module_from_spec(spec)
    builtins.__import__ = guard
    try:
        spec.loader.exec_module(module)
    finally:
        builtins.__import__ = real_import
    return module


def test_posix_lock_is_exclusive_and_reusable_after_release(tmp_path: Path) -> None:
    lock_path = tmp_path / "scan.lock"
    first = lock_path.open("a+", encoding="utf-8")
    second = lock_path.open("a+", encoding="utf-8")
    try:
        assert filelock.try_acquire(first) is True
        assert filelock.try_acquire(second) is False, "a second holder must be refused"

        filelock.release(first)
        assert filelock.try_acquire(second) is True, "the lock must be reusable after release"
        filelock.release(second)
    finally:
        first.close()
        second.close()


def test_posix_lock_is_released_when_the_owning_handle_closes(tmp_path: Path) -> None:
    """A crashed scan must not wedge every future scan."""
    lock_path = tmp_path / "scan.lock"
    abandoned = lock_path.open("a+", encoding="utf-8")
    assert filelock.try_acquire(abandoned) is True
    abandoned.close()

    recovered = lock_path.open("a+", encoding="utf-8")
    try:
        assert filelock.try_acquire(recovered) is True
        filelock.release(recovered)
    finally:
        recovered.close()


def test_windows_branch_locks_one_byte_from_the_start_of_the_file(tmp_path: Path) -> None:
    fake = RecordingMsvcrt()
    windows = load_windows_filelock(fake)
    handle = (tmp_path / "scan.lock").open("a+", encoding="utf-8")
    try:
        handle.write("stale text that moves the append cursor")

        assert windows.try_acquire(handle) is True
        assert fake.calls == [(RecordingMsvcrt.LK_NBLCK, 1)]
        assert handle.tell() == 0, "the locked region must start at byte 0"

        windows.release(handle)
        assert fake.calls[-1] == (RecordingMsvcrt.LK_UNLCK, 1)
    finally:
        handle.close()


@pytest.mark.parametrize("code", [errno.EACCES, 36])
def test_windows_contention_reads_as_busy_not_as_a_crash(tmp_path: Path, code: int) -> None:
    fake = RecordingMsvcrt(error=OSError(code, "locked"))
    windows = load_windows_filelock(fake)
    handle = (tmp_path / "scan.lock").open("a+", encoding="utf-8")
    try:
        assert windows.try_acquire(handle) is False
    finally:
        handle.close()


def test_windows_real_errors_still_propagate(tmp_path: Path) -> None:
    """A broken lock file must never be misreported as 'a scan is already running'."""
    fake = RecordingMsvcrt(error=OSError(errno.EIO, "disk failure"))
    windows = load_windows_filelock(fake)
    handle = (tmp_path / "scan.lock").open("a+", encoding="utf-8")
    try:
        with pytest.raises(OSError):
            windows.try_acquire(handle)
    finally:
        handle.close()


def test_windows_release_tolerates_an_already_released_region(tmp_path: Path) -> None:
    class FailingUnlock(RecordingMsvcrt):
        def locking(self, fd: int, mode: int, nbytes: int) -> None:
            self.calls.append((mode, nbytes))
            if mode == self.LK_UNLCK:
                raise OSError(errno.EACCES, "not locked")

    windows = load_windows_filelock(FailingUnlock())
    handle = (tmp_path / "scan.lock").open("a+", encoding="utf-8")
    try:
        windows.release(handle)  # must not raise; the handle closes next anyway
    finally:
        handle.close()


def test_scanner_never_imports_fcntl_at_module_level() -> None:
    """Regression guard for the defect that made the Windows release unusable."""
    from sf_housing import scanner

    source = Path(scanner.__file__).read_text(encoding="utf-8")
    assert "import fcntl" not in source
    assert "from .filelock import" in source
