"""Per-server download retention: the registry, eviction, and temp-file removal.

Successful downloads this server lifespan has handed out are registered here.
The CSV must outlive the tool call - the caller reads the file at the returned
path after the call returns - so it cannot be removed when the call ends.
Registration is what bounds total disk use: the owning server's lifespan
removes everything in its registry at shutdown, so successful downloads no
longer accumulate on disk for the life of the container.

Ownership is PER SERVER, not per process. The registry a download registers
with is the one the current server lifespan owns (the ``_download_registry``
context var, set by ``nextdns_mcp.openapi._server_lifespan``); there is no
process-global fallback, so a download outside any lifespan fails fast (see
``MissingDownloadRegistryError``). Stdlib only; nothing here knows about HTTP. The registries used to be one process-global set that EVERY server's
lifespan emptied, so one embedded server deleted another live server's
tracked CSVs mid-read. Insertion-ordered, so the oldest entry is always
first: that order is the eviction order (see ``_note_successful_download``).

Stdlib only; nothing here knows about HTTP.

SPDX-License-Identifier: MIT
"""

import contextvars
import logging
import os

logger = logging.getLogger(__name__)


def _remove_temp_file(path: str) -> bool:
    """Best-effort removal of a download temp file AND its mkdtemp parent.

    Returns True when the file is gone (or never existed) and False when the
    unlink failed. A failed unlink is logged, never raised, so it cannot mask
    the download's own error; the caller that tracks the path (the download
    registry) keeps a False path registered so a later cleanup can retry.
    Parent-directory removal is attempted on every exit and its errors are
    tolerated: another download may be writing in the same parent, and that is
    not worth replacing the download's own error with an unrelated one about a
    directory.
    """
    try:
        os.unlink(path)
    except FileNotFoundError:
        # Nothing was ever written (e.g. the ACL refused before the file was
        # opened), so there is nothing to remove and nothing to report.
        removed = True
    except OSError:
        logger.warning(f"Failed to remove temp log file: {path}")
        removed = False
    else:
        removed = True
    try:
        os.rmdir(os.path.dirname(path))  # the mkdtemp() parent is empty once the CSV is gone
    except OSError:
        pass
    return removed


def _unlink_temp_file(path: str) -> None:
    """Fire-and-forget cleanup of a failed download's temp file; ignores the outcome."""
    _remove_temp_file(path)


class MissingDownloadRegistryError(RuntimeError):
    """A download has no owning server lifespan to register with.

    Registration is what bounds the CSV's lifetime: the owning lifespan
    reclaims it at shutdown and evicts past ``MAX_RETAINED_DOWNLOADS``. Without
    an owner neither reclaim point exists, so the download is refused rather
    than left on disk for the life of the process.
    """


# Successful downloads this server lifespan has handed out. The CSV must
# outlive the tool call - the caller reads the file at the returned path after
# the call returns - so it cannot be removed when the call ends. Registration
# is what bounds total disk use: the owning server's lifespan removes
# everything in its registry at shutdown, so successful downloads no longer
# accumulate on disk for the life of the container.
#
# Ownership is PER SERVER, not per process. The registry a download registers
# with is the one the current server lifespan owns (the ``_download_registry``
# context var, set by ``nextdns_mcp.openapi._server_lifespan``). There is no
# process-global fallback: a download handled outside any lifespan has no
# owner that could ever reclaim it, so registration fails fast with
# ``MissingDownloadRegistryError`` instead of leaking the CSV for the life of
# the process (issue #647). The registries used to be one process-global set
# that EVERY server's lifespan emptied, so one embedded server deleted another
# live server's tracked CSVs mid-read. Insertion-ordered, so the oldest entry
# is always first: that order is the eviction order (see
# ``_note_successful_download``).
_download_registry: contextvars.ContextVar[dict[str, None] | None] = contextvars.ContextVar(
    "nextdns_download_registry", default=None
)

# A long-lived server would otherwise keep every CSV it ever handed out until
# shutdown - and shutdown never runs at all under SIGKILL. Retaining at most
# the newest N bounds disk use between those two reclaim points; the oldest
# download is evicted (unlinked) past the cap.
MAX_RETAINED_DOWNLOADS = 8


def _current_registry() -> dict[str, None]:
    """The registry downloads register with now: the owning lifespan's.

    Raises:
        MissingDownloadRegistryError: when no server lifespan owns this
            context. Nothing would ever reclaim a CSV registered there, so the
            caller is told instead of the path joining an unowned registry.
    """
    owned = _download_registry.get()
    if owned is None:
        raise MissingDownloadRegistryError(
            "No server lifespan owns this call, so the downloaded CSV has no registry to be reclaimed from."
        )
    return owned


def _remove_downloaded_file(path: str) -> bool:
    """Registry-side cleanup of a finished download; see ``_remove_temp_file``."""
    return _remove_temp_file(path)


def cleanup_successful_downloads() -> None:
    """Remove every registered temp file and its directory.

    Called once at the owning server's shutdown. A downloadLogs CSV can be
    hundreds of MB; without this the file outlived the process that created
    it. Best effort and idempotent: a second call has nothing registered and
    does nothing. Because registration is per-lifespan, this reclaims only
    the calling server's own downloads, and raising outside a lifespan (see
    ``_current_registry``) is a caller bug, not a fallback.
    """
    registry = _current_registry()
    for path in list(registry):
        if _remove_downloaded_file(path):
            registry.pop(path, None)


def _note_successful_download(path: str, downloaded: bool) -> None:
    """Register a fully-written CSV for lifespan reclaim; failures stay unregistered.

    Registration is insertion-ordered, and past ``MAX_RETAINED_DOWNLOADS`` the
    oldest CSV is evicted and unlinked, so disk use stays bounded across a
    long server lifetime even when shutdown (the other reclaim point) is far
    away or never comes (SIGKILL).

    Raises:
        MissingDownloadRegistryError: when the download succeeded outside any
            server lifespan. The path is NOT recorded and the caller must
            remove the file - nothing would ever reclaim it.
    """
    if not downloaded:
        return
    registry = _current_registry()
    registry[path] = None
    while len(registry) > MAX_RETAINED_DOWNLOADS:
        oldest = next(iter(registry))
        if not _remove_downloaded_file(oldest):
            break  # cannot evict: leave the overflow for shutdown reclaim rather than spin
        registry.pop(oldest, None)
