"""Per-server download retention: the registry, eviction, and temp-file removal.

Successful downloads this server lifespan has handed out are registered here.
The CSV must outlive the tool call - the caller reads the file at the returned
path after the call returns - so it cannot be removed when the call ends.
Registration is what bounds total disk use: the owning server's lifespan
removes everything in its registry at shutdown, so successful downloads no
longer accumulate on disk for the life of the container.

Ownership is PER SERVER, not per process. The registry a download registers
with is the one the current server lifespan owns (the ``download_registry``
context var, set by ``nextdns_mcp.lifespan.server_lifespan``, or the
resources of the FastMCP server serving this request); there is no
process-global fallback, so a download outside any owner fails fast (see
``MissingDownloadRegistryError``). The registries used to be one
process-global set that EVERY server's lifespan emptied, so one embedded
server deleted another live server's tracked CSVs mid-read.
Insertion-ordered, so the oldest entry is always first: that order is the
eviction order (see ``DownloadRegistry.note``).

Stdlib only; nothing here knows about HTTP.

SPDX-License-Identifier: MIT
"""

import contextlib
import contextvars
import logging
import os
import threading
from collections.abc import Iterator

from ...state import request_resources

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
# with is the one the current server lifespan owns (the ``download_registry``
# context var, set by ``nextdns_mcp.lifespan.server_lifespan``). There is no
# process-global fallback: a download handled outside any lifespan has no
# owner that could ever reclaim it, so registration fails fast with
# ``MissingDownloadRegistryError`` instead of leaking the CSV for the life of
# the process (issue #647). The registries used to be one process-global set
# that EVERY server's lifespan emptied, so one embedded server deleted another
# live server's tracked CSVs mid-read. Insertion-ordered, so the oldest entry
# is always first: that order is the eviction order (see
# ``_note_successful_download``).
class DownloadRegistry:
    """Owns the successful downloadLogs CSVs for one explicit lifetime.

    One server lifespan owns one registry; a standalone session owns one via
    :func:`downloads_owned`. Insertion-ordered, so the oldest entry is always
    first: that order is the eviction order.
    """

    def __init__(self) -> None:
        self._paths: dict[str, None] = {}
        # Registration and reclaim no longer run on the caller's event loop:
        # they run on worker threads (see the download tool and the lifespan
        # shutdown paths), so two servers' downloads - or one server's downloads
        # racing its own eviction sweep - can mutate ``_paths`` concurrently.
        # Insertion order is the eviction order, so the whole register-then-
        # evict step is held under one lock: it is a check-then-act, and a
        # mutation inside the eviction loop raises ``RuntimeError: dictionary
        # changed size during iteration`` out of a download that already
        # succeeded.
        self._lock = threading.Lock()

    def note(self, path: str, downloaded: bool) -> None:
        """Register a fully-written CSV for reclaim; failures stay unregistered.

        Registration is insertion-ordered, and past ``MAX_RETAINED_DOWNLOADS``
        the oldest CSV is evicted and unlinked, so disk use stays bounded
        across a long server lifetime even when shutdown (the other reclaim
        point) is far away or never comes (SIGKILL).
        """
        if not downloaded:
            return
        with self._lock:
            self._paths[path] = None
            while len(self._paths) > MAX_RETAINED_DOWNLOADS:
                oldest = next(iter(self._paths))
                if not _remove_downloaded_file(oldest):
                    break  # cannot evict: leave the overflow for shutdown reclaim rather than spin
                self._paths.pop(oldest, None)

    def cleanup(self) -> None:
        """Remove every registered temp file and its directory.

        Called once at the owning lifetime's end (server shutdown, or the end
        of a ``downloads_owned`` block). A downloadLogs CSV can be hundreds of
        MB; without this the file outlived the process that created it. Best
        effort and idempotent: a second call has nothing registered and does
        nothing.
        """
        with self._lock:
            for path in list(self._paths):
                if _remove_downloaded_file(path):
                    self._paths.pop(path, None)

    def forget(self, path: str) -> None:
        """Drop a registration whose download was cancelled before handoff.

        The download is still the path's owner until its payload is returned.
        If a cancellation landed while registration was running, settling it
        and removing the path here keeps the registry from claiming a CSV the
        caller is about to delete.
        """
        with self._lock:
            self._paths.pop(path, None)

    def __contains__(self, path: object) -> bool:
        with self._lock:
            return path in self._paths

    def __len__(self) -> int:
        with self._lock:
            return len(self._paths)


download_registry: contextvars.ContextVar[DownloadRegistry | None] = contextvars.ContextVar(
    "nextdns_download_registry", default=None
)

# Pre-rename private name, kept bound for the package's compatibility
# re-export and its callers; lifespan takes the public name (issue #783).
_download_registry = download_registry

# A long-lived server would otherwise keep every CSV it ever handed out until
# shutdown - and shutdown never runs at all under SIGKILL. Retaining at most
# the newest N bounds disk use between those two reclaim points; the oldest
# download is evicted (unlinked) past the cap.
MAX_RETAINED_DOWNLOADS = 8


def current_download_registry() -> DownloadRegistry:
    """The registry downloads register with now.

    Resolution order: a registry published with :func:`downloads_owned`, then
    the resources of the FastMCP server serving this request.

    Raises:
        MissingDownloadRegistryError: When no registry owner is active.
            Server code runs inside a server lifespan, which publishes one;
            standalone callers must wrap the work in ``downloads_owned()``.
            Nothing would ever reclaim a CSV registered without an owner, so
            the caller is told instead of the path joining an unowned
            registry.
    """
    owned = download_registry.get()
    if owned is None:
        resources = request_resources()
        if resources is not None:
            owned = resources.downloads
    if owned is None:
        raise MissingDownloadRegistryError(
            "No server lifespan owns this call: the downloaded CSV has no registry to be "
            "reclaimed from. Server code runs inside a server lifespan, which publishes one; "
            "standalone callers must wrap the work in `downloads_owned()`."
        )
    return owned


@contextlib.contextmanager
def downloads_owned(registry: DownloadRegistry | None = None) -> Iterator[DownloadRegistry]:
    """Publish a download registry owner for the block (a standalone session).

    Yields the registry so the caller can inspect it; cleanup stays the
    caller's explicit decision via ``registry.cleanup()``.
    """
    owner = registry if registry is not None else DownloadRegistry()
    token = download_registry.set(owner)
    try:
        yield owner
    finally:
        download_registry.reset(token)


def _remove_downloaded_file(path: str) -> bool:
    """Registry-side cleanup of a finished download; see ``_remove_temp_file``."""
    return _remove_temp_file(path)


def cleanup_successful_downloads() -> None:
    """Remove every registered temp file and its directory.

    Called once at the owning server's shutdown. A downloadLogs CSV can be
    hundreds of MB; without this the file outlived the process that created
    it. Best effort and idempotent: a second call has nothing registered and
    does nothing. Because registration is per-lifespan, this reclaims only
    the calling server's own downloads, and raising outside an owner (see
    ``current_download_registry``) is a caller bug, not a fallback.
    """
    current_download_registry().cleanup()


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
    current_download_registry().note(path, downloaded)


def _forget_download(path: str) -> None:
    """Undo a registration when the download was cancelled before handoff.

    Resolves the owner the same way registration does, so a cancelled download
    cannot remove another server's entry (issue #642).
    """
    try:
        current_download_registry().forget(path)
    except MissingDownloadRegistryError:
        pass  # no owner means there is no registration to undo
