"""Bounded preview accounting: pure text-line and byte bookkeeping, no I/O.

The download's caller receives only a bounded preview of the leading lines so
a CSV export cannot blow up the LLM context. Everything here is a pure
function over strings and integers: no ``httpx``, no ``os``, no file or
network access, so the caps can be reasoned about (and tested) without a
stream. The caps are module globals read at call time, so tests rebind them
on THIS module to exercise the boundaries.

SPDX-License-Identifier: MIT
"""

# Caps for log downloads: the CSV is streamed to a temp file (never buffered
# twice or inlined into the tool payload) and only a bounded preview is
# returned so large downloads cannot blow up the LLM context. The total
# download size itself is capped separately and configured in config.py
# (NEXTDNS_DOWNLOAD_MAX_BYTES).
DOWNLOAD_PREVIEW_MAX_LINES = 20
DOWNLOAD_PREVIEW_MAX_BYTES = 256 * 1024


def _preview_full(preview: list[str], preview_bytes: int) -> bool:
    return len(preview) >= DOWNLOAD_PREVIEW_MAX_LINES or preview_bytes >= DOWNLOAD_PREVIEW_MAX_BYTES


def _append_preview(preview: list[str], preview_bytes: int, text: str) -> int:
    """Add this chunk's leading lines to the preview, within the line and byte caps.

    Returns the new preview byte count. A preview that already hit either cap
    returns before the chunk is split into lines at all: past 20 lines or
    256 KB there is nothing any later chunk can add, and ``splitlines()`` on
    every remaining chunk of a 1 GiB CSV is work that buys nothing. A first
    line that would cross the byte cap still trips the same break the
    per-chunk guard used before the extraction.
    """
    if _preview_full(preview, preview_bytes):
        return preview_bytes
    for line in text.splitlines(keepends=True):
        if _preview_full(preview, preview_bytes):
            break
        line_bytes = len(line.encode("utf-8"))
        if preview_bytes + line_bytes > DOWNLOAD_PREVIEW_MAX_BYTES:
            break
        preview.append(line)
        preview_bytes += line_bytes
    return preview_bytes


def _previewable(carry: str, text: str) -> tuple[str, str]:
    """Split a stream piece into whole leading lines and the trailing fragment.

    A chunk boundary is not a record boundary: only whole lines reach the
    preview, and the unterminated tail rides over to the next chunk, so a CSV
    record is never previewed as two fragments (or dropped and counted).
    """
    piece = carry + text
    if piece.endswith("\n"):
        return piece, ""
    cut = piece.rfind("\n")
    if cut == -1:
        return "", piece
    return piece[: cut + 1], piece[cut + 1 :]


def _skip_dropped_record_tail(piece: str, dropping: bool) -> tuple[str, bool]:
    """Cut the leading fragment of a record already dropped from the preview.

    ``piece`` is whole lines, so a non-empty piece always ends with a newline:
    its first line is the dropped record's tail (or several lines of it), and
    only what follows the first newline is a record of its own and previewable.
    Without this, a record longer than the preview cap would re-surface in the
    preview as a fresh record every time its tail crossed a chunk boundary.
    """
    if not dropping or not piece:
        return piece, dropping
    return piece[piece.find("\n") + 1 :], False


def _flush_tail(
    preview: list[str], preview_bytes: int, carry: str, open_line: bool, dropping_record: bool
) -> tuple[int, int]:
    """The stream's final unterminated record: extra rows and the new preview size.

    ``dropping_record`` means the record still being read was already dropped
    from the preview for crossing the byte cap; its leftover tail at EOF
    belongs to that same record, so previewing it would resurrect the dropped
    record as a phantom line. The row itself still counts - it exists in the
    file whether or not the preview shows it.
    """
    if carry and not dropping_record:
        preview_bytes = _append_preview(preview, preview_bytes, carry)
    return (1 if open_line else 0), preview_bytes
