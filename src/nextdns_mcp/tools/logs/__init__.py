"""The guarded log-download path behind the ``downloadLogs`` tool.

SPDX-License-Identifier: MIT

The endpoint answers with a redirect to a signed URL, so the download is not a
plain API call the generated tool can make. The path is split by
responsibility, one concern per module:

- ``retention`` - the per-server download registry and its eviction past
  ``MAX_RETAINED_DOWNLOADS``, shutdown reclaim, and temp-file removal.
- ``guard`` - SSRF vetting of the redirect chain (https-only, globally
  routable hosts, fail-closed resolution) and the pinned resolver that makes
  the dialled address the vetted address (issue #266).
- ``preview`` - the pure text accounting for the bounded preview (line and
  byte caps); no httpx, no os, no I/O.
- ``stream`` - streaming the response body to disk under the total-size cap,
  with typed mid-stream failures.
- ``redirects`` - the unauthenticated redirect-chain fetch, so the API key
  never crosses to a redirect target; one persistent bounded client serves
  every chain for keep-alive reuse.
- ``tool`` - the ``downloadLogs`` entrypoint: temp-file lifecycle, typed error
  mapping, and the payload the tool returns.

Import the surface from here (``from nextdns_mcp.tools.logs import
cleanup_successful_downloads``). The names below are rebinds of the owning
module's names - the same objects, not copies - because callers inside and
outside the repo resolve them on this package (``spec_tools`` resolves the
downloader here at call time on purpose, and ``openapi`` shares the registry
context var). Monkeypatching a rebinding here would not reach the owning
module's global; patch the owning submodule instead. The redirect client's
owner global lives on ``redirects`` for the same reason: only the functions
and the owner names are re-exported here, never a mutable client singleton -
the client itself is per-server state on ``ServerResources`` (issue #774).
"""

import contextvars
import os
import tempfile

import httpx

from ...errors import BODY_SLICE_BYTES
from .guard import (
    _PIN_FETCH_LOCK,
    ALLOWED_DOWNLOAD_HOSTS,
    DownloadRedirectRefusedError,
    _check_download_redirect_target,
    _is_public_address,
    _pinned_dns,
)
from .preview import DOWNLOAD_PREVIEW_MAX_BYTES, DOWNLOAD_PREVIEW_MAX_LINES
from .redirects import (
    _MAX_DOWNLOAD_REDIRECTS,
    RedirectClientOwner,
    _follow_redirects_to_tempfile,
    _get_redirect_client,
    _redirect_client_owner,
    close_redirect_client,
    current_redirect_client_owner,
    redirect_client_owned,
)
from .retention import (
    MAX_RETAINED_DOWNLOADS,
    DownloadRegistry,
    MissingDownloadRegistryError,
    _download_registry,
    _note_successful_download,
    _remove_downloaded_file,
    _unlink_temp_file,
    cleanup_successful_downloads,
    current_download_registry,
    download_registry,
    downloads_owned,
)
from .stream import DownloadTooLargeError, _raise_for_streamed_status, _write_stream_to_tempfile, read_body_capped
from .tool import _download_error_details, _download_logs_to_tempfile

__all__ = [
    "ALLOWED_DOWNLOAD_HOSTS",
    "BODY_SLICE_BYTES",
    "DOWNLOAD_PREVIEW_MAX_BYTES",
    "DOWNLOAD_PREVIEW_MAX_LINES",
    "MAX_RETAINED_DOWNLOADS",
    "_MAX_DOWNLOAD_REDIRECTS",
    "_PIN_FETCH_LOCK",
    "DownloadRedirectRefusedError",
    "DownloadRegistry",
    "DownloadTooLargeError",
    "MissingDownloadRegistryError",
    "RedirectClientOwner",
    "_check_download_redirect_target",
    "_download_error_details",
    "_download_logs_to_tempfile",
    "_download_registry",
    "_follow_redirects_to_tempfile",
    "_get_redirect_client",
    "_is_public_address",
    "_note_successful_download",
    "_pinned_dns",
    "_raise_for_streamed_status",
    "_redirect_client_owner",
    "_remove_downloaded_file",
    "_unlink_temp_file",
    "_write_stream_to_tempfile",
    "cleanup_successful_downloads",
    "close_redirect_client",
    "contextvars",
    "current_download_registry",
    "current_redirect_client_owner",
    "download_registry",
    "downloads_owned",
    "httpx",
    "os",
    "read_body_capped",
    "redirect_client_owned",
    "tempfile",
]
