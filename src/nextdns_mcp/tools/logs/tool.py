"""The ``downloadLogs`` entrypoint: temp-file lifecycle and typed error mapping.

:func:`_download_logs_to_tempfile` is what ``spec_tools`` delegates the
``downloadLogs`` operation to (resolved on the package at call time, so tests
can replace it). It streams the CSV through :mod:`.redirects`, registers a
successful download with :mod:`.retention` for lifespan reclaim, and maps
every failure mode to the typed payload the tool returns - including removal
of the temp file and its ``mkdtemp`` parent on every exit that is not a
successful download.

SPDX-License-Identifier: MIT
"""

import asyncio
import logging
import os
import tempfile
from typing import Any

import httpx

from ... import client
from ...errors import ErrorCode, error_payload, http_error_payload
from ...transport import access_denied_payload_for
from . import redirects, retention, stream
from .guard import DownloadRedirectRefusedError

logger = logging.getLogger(__name__)


async def _download_logs_controlled(profile_id: str, path: str) -> tuple[dict[str, Any], bool]:
    """Stream the CSV to ``path``, mapping typed failures to their payloads.

    Returns a ``(payload, downloaded)`` pair; ``downloaded`` is True only when
    the whole body was written, which is what tells the caller's cleanup guard
    to keep the file. HTTP and unexpected errors are re-raised for the caller's
    handlers, in the same order the single-function version caught them.
    """
    try:
        payload = await redirects._stream_download_to_path(profile_id, path)
        return payload, True
    except DownloadRedirectRefusedError as e:
        # A redirect to a non-public or plaintext destination is refused
        # before any request is made to the target, so no foreign body is ever
        # written or returned. The message carries only scheme and host: the
        # signed part of the URL never reaches the log or the payload. The
        # temp file and its parent are removed by the caller's finally guard,
        # which covers every non-successful exit.
        logger.warning(f"Refusing log download redirect: {e}")
        return (
            error_payload(
                ErrorCode.HTTP_ERROR, "Refusing log download redirect to a non-public or non-https destination"
            ),
            False,
        )
    except client.AccessDeniedError as e:
        # Raised by the ACL layer (via stream()) before any network request;
        # kept out of the httpx.HTTPError branch so a real upstream 403 keeps
        # its existing http_error path.
        logger.warning(f"Access denied downloading logs: {client._log_safe_error(e)}")
        return access_denied_payload_for(e), False
    except stream.DownloadTooLargeError as e:
        # Raised mid-stream when the total-size cap is exceeded; the partially
        # written CSV and its parent are removed by the caller's guard.
        logger.warning(f"Download aborted, size cap exceeded: {e}")
        return error_payload(ErrorCode.DOWNLOAD_TOO_LARGE, str(e)), False
    except TimeoutError:
        # The chain deadline in redirects._follow_redirects_to_tempfile is an
        # asyncio.timeout, which raises the builtin TimeoutError - not an
        # httpx error - so without this branch it escaped to the caller's
        # generic handler and a download timeout was reported as
        # internal_error with an empty message (issue #760).
        logger.error("Timed out while downloading logs")
        return error_payload(ErrorCode.HTTP_ERROR, "Timed out while downloading logs"), False


def _download_error_details(error: Exception) -> tuple[Any, Any, Any]:
    """Best-effort (status, scheme, host) from an httpx error, without raising."""
    status = getattr(getattr(error, "response", None), "status_code", None)
    url = None
    try:
        request = getattr(error, "request", None)
    except RuntimeError:
        request = None
    if request is not None:
        try:
            url = getattr(request, "url", None)
        except Exception:  # noqa: BLE001
            url = None
    scheme = getattr(url, "scheme", None)
    host = getattr(url, "host", None)
    return status, scheme, host


def _download_error_detail(status: Any, scheme: Any, host: Any) -> str:
    """Render the parts of the failed request that exist, space-joined; skip the rest."""
    return " ".join(
        part
        for part in (
            str(status) if status is not None else "",
            f"{scheme}://{host}" if scheme else "",
        )
        if part
    )


LOG_TEMP_DIR_PREFIX = "nextdns_logs_"


async def _create_download_path() -> str:
    """Create this download's temp directory on the worker pool and return the CSV path.

    ``mkdtemp`` is a syscall that can block - on a full or cold filesystem it
    holds whatever thread calls it - so it runs on the pool, like the open,
    write and unlink that follow it. The await is shielded because the
    directory has no owner until the caller binds the path: a plain await that
    is cancelled after the worker started would discard the created directory,
    and the caller's cleanup guard has nothing to reclaim yet - exactly the
    leak the cancellation contract forbids. The shield lets the creation land,
    reclaims what it made, and re-raises the cancellation.
    """
    creating = asyncio.ensure_future(asyncio.to_thread(tempfile.mkdtemp, prefix=LOG_TEMP_DIR_PREFIX))
    try:
        temp_dir = await asyncio.shield(creating)
    except asyncio.CancelledError:
        await _reclaim_abandoned_temp_dir(creating)
        raise
    return os.path.join(temp_dir, "download.csv")


async def _reclaim_abandoned_temp_dir(creating: asyncio.Future[str]) -> None:
    """Take back a temp directory whose creating await was cancelled.

    The worker thread cannot be cancelled, so the directory either exists by
    the time the creation task finishes or never will: waiting for the task is
    what makes the reclaim exact rather than best effort.
    """
    try:
        made = await asyncio.shield(creating)
    except (asyncio.CancelledError, OSError):
        return  # the directory was never created: there is nothing to hand back
    await asyncio.to_thread(retention._unlink_temp_file, os.path.join(made, "download.csv"))


async def _register_successful_download(path: str, downloaded: bool) -> dict[str, Any] | None:
    """Register a completed download with the owning lifespan's registry.

    Returns the rejection payload when the download cannot be handed out at
    all - it succeeded outside any server lifespan, or the registry call
    itself failed - because in either case nothing could ever reclaim the CSV,
    so the caller must not hand out the path and must remove the file. None
    when the registry took ownership.
    """
    if not downloaded:
        return None
    try:
        # Registration is where past-MAX_RETAINED_DOWNLOADS eviction unlinks the
        # oldest CSV, and that unlink is a filesystem call: it runs on a worker
        # thread so the next request is not queued behind it.
        await asyncio.to_thread(retention._note_successful_download, path, True)
    except retention.MissingDownloadRegistryError as e:
        logger.error(f"Downloaded logs could not be registered for reclaim: {client._log_safe_error(e)}")
        return error_payload(ErrorCode.INTERNAL_ERROR, f"Downloaded logs could not be handed out: {e}")
    except Exception as e:  # noqa: BLE001
        # A registry that answered with anything else still did not take the
        # path, so handing the CSV out would leave it unregistered - invisible
        # to shutdown reclaim and to eviction. Reject it and let the caller's
        # cleanup guard remove it.
        logger.error(f"Downloaded logs could not be registered for reclaim: {client._log_safe_error(e)}")
        return error_payload(ErrorCode.INTERNAL_ERROR, f"Downloaded logs could not be handed out: {e}")
    return None


def _http_error_payload(e: httpx.HTTPError) -> dict[str, Any]:
    """The typed HTTP-error payload, with whatever request detail exists."""
    status, scheme, host = _download_error_details(e)
    detail = _download_error_detail(status, scheme, host)
    return http_error_payload(
        "HTTP error while downloading logs" + (f": {detail}" if detail else ""),
        e,
        fallback_code=ErrorCode.HTTP_ERROR,
    )


async def _download_logs_to_tempfile(profile_id: str) -> dict[str, Any]:
    """Stream a log CSV download to a temp file without double-buffering.

    The response body is streamed directly to disk (bounded by the OS temp
    file), a bounded preview of the first lines is kept in memory, and the
    tool payload returns only the file path, size, row count, and preview —
    never the full CSV text.

    Every exit that is not a successful download removes the temp file and its
    mkdtemp parent, including cancellation of the awaiting task, so an
    abandoned download leaves neither an empty directory nor a partial CSV
    behind.

    The authenticated client is used only for the initial request to the
    NextDNS API, and never follows redirects: if the download endpoint
    redirects (for example to object storage), the ``Location`` is fetched with
    an unauthenticated client so the ``X-Api-Key`` header is never sent to
    another host. That unauthenticated fetch only visits https destinations on
    globally routable hosts, so a hostile ``Location`` cannot turn the download
    into a probe of an internal service.
    """
    # Created off the loop, and shielded: see ``_create_download_path``.
    path = await _create_download_path()
    downloaded = False
    try:
        payload, downloaded = await _download_logs_controlled(profile_id, path)
        # The file must survive until the caller has read it; the server
        # lifespan reclaims it at shutdown (see cleanup_successful_downloads).
        # Registration requires an owning lifespan: without one the CSV could
        # never be reclaimed, so a successful download is rejected there and
        # removed like any other unsuccessful download (downloaded stays False
        # for the finally below).
        rejection = await _register_successful_download(path, downloaded)
        if rejection is not None:
            downloaded = False
            return rejection
        return payload
    except httpx.HTTPError as e:
        logger.error(f"HTTP error downloading logs: {client._log_safe_error(e)}")
        return _http_error_payload(e)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Unexpected error downloading logs: {client._log_safe_error(e)}")
        return error_payload(ErrorCode.INTERNAL_ERROR, f"Unexpected error while downloading logs: {e}")
    finally:
        # Every exit that is not a successful download — including a cancelled
        # task, which reaches neither handler — removes the temp file and its
        # mkdtemp parent, so an abandoned download leaves nothing behind.
        if not downloaded:
            await asyncio.to_thread(retention._unlink_temp_file, path)
