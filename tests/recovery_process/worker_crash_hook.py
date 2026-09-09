"""Test-only subprocess crash hook; never imported by production composition."""

from __future__ import annotations

import os
import sys

CRASH_EXIT_CODE = 97


def trigger_crash(point: str) -> None:
    """Terminate only an opted-in worker immediately at the requested crash point."""

    target = os.environ.get("FORGE_TEST_CRASH_POINT")
    if target == point:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(CRASH_EXIT_CODE)


def crash_after_file_write() -> None:
    """Terminate only an opted-in worker immediately after the write boundary."""

    if (
        os.environ.get("FORGE_TEST_CRASH_AFTER_FILE_WRITE") == "1"
        or os.environ.get("FORGE_TEST_CRASH_POINT") == "file_write"
    ):
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(CRASH_EXIT_CODE)

