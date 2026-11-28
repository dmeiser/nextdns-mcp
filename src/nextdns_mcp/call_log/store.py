"""Where the call log lives and how one row reaches or leaves it.

SPDX-License-Identifier: MIT

Owns the log file: the configured path, the append that writes one JSON
object per line, and the shared reader verifying against the log. The rows
carry account data, so the file is created ``0600`` and refuses to append
when it cannot guarantee that mode.
"""

import errno
import json
import logging
import os
import stat
import threading
from typing import Any

logger = logging.getLogger(__name__)

CALL_LOG_ENV = "NEXTDNS_CALL_LOG"

# One server process may serve concurrent calls; keep records whole.
_write_lock = threading.Lock()


def call_log_path() -> str:
    """The configured log path, or "" when call logging is off."""
    return os.environ.get(CALL_LOG_ENV, "").strip()


def _parse_log_line(line: str) -> Any:
    """The JSON object on one log line, or None when the line is blank or malformed."""
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def _checked_fd(path: str, flags: int) -> int:
    """An fd on ``path`` (created ``0600``), refusing symlinks and non-regular files.

    ``O_NOFOLLOW`` fails on a symlink (ELOOP), ``O_NONBLOCK`` refuses a FIFO
    with no reader before the open can hang, and ``fstat`` refuses anything
    not regular: no read, append or ``fchmod`` lands on a file the path names.
    """
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "call log is not a regular file", path)
    except OSError:
        os.close(fd)
        raise
    return fd


def read_call_log_rows(path: str, offset: int = 0) -> list[dict[str, Any]]:
    """The dict rows of a call-log file from ``offset`` on, skipping bad lines.

    The shared reader for anything verifying against the log (the server's own
    ``get_call_log`` and any harness reading it back): one place decides what a
    row is - a JSON object on its own line - so a malformed or partial trailing
    line is skipped identically everywhere. ``offset`` supports reading only
    what a run appended after capturing the file's previous end.
    """
    rows: list[dict[str, Any]] = []
    with os.fdopen(_checked_fd(path, os.O_RDONLY), "r", encoding="utf-8") as fh:
        fh.seek(offset)
        for line in fh:
            row = _parse_log_line(line)
            if isinstance(row, dict):
                rows.append(row)
    return rows


def get_call_log(path: str | None = None) -> list[dict[str, Any]]:
    """Read back the recorded calls (empty when unset, absent or malformed)."""
    target = call_log_path() if path is None else path
    if not target or not os.path.exists(target):
        return []
    try:
        return read_call_log_rows(target)
    except OSError as exc:
        logger.warning("call log read failed: %s", exc)
        return []


def _append_row(path: str, row: dict[str, Any]) -> None:
    """Write one row to the log. Runs on a worker thread (``_record`` offloads here)."""
    line = json.dumps(row, sort_keys=True) + "\n"
    try:
        with _write_lock, _open_log_file(path) as fh:
            fh.write(line)
    except OSError as exc:
        logger.warning("call log write failed: %s", exc)


def _open_log_file(path: str) -> Any:
    """Open the log for append, creating it ``0600`` and refusing less.

    The rows carry profile ids, entry names and bounded result content -
    account data - so the file must not be group- or world-readable from the
    moment it exists. ``open(..., "a")`` applies the process umask (often
    ``0022``) at creation; ``os.open`` with an explicit mode does not.

    Fail closed: the tightening ``fchmod`` covers a log that already exists
    (a reused path, or an older run's file made under a permissive umask).
    If it fails - an alien-owned file, a filesystem without chmod - this code
    cannot know who can read the file, so it does not append to it at all;
    ``_append_row``'s except path drops the row and warns, which is the
    correct cost for a log that cannot be made private. ``_checked_fd``
    refuses a symlink or non-regular file before the ``fchmod`` runs.
    """
    fd = _checked_fd(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    try:
        os.fchmod(fd, 0o600)
    except OSError:
        os.close(fd)
        raise
    return os.fdopen(fd, "a", encoding="utf-8")
