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
cleanup_successful_downloads``): ``__all__`` is that supported surface, and
nothing else is part of the package's contract. The internal rebinds below it
are not exports - they stay out of ``__all__`` - but they are still rebinds of
the owning module's names, the same objects and not copies, because in-repo
callers resolve them on this package (``spec_tools`` resolves the downloader
here at call time on purpose, and ``lifespan`` binds the registry context var
from here). Monkeypatching a rebinding here would not reach the owning
module's global; patch the owning submodule instead. The redirect client's
owner global lives on ``redirects`` for the same reason: only the functions
and the owner names are re-exported here, never a mutable client singleton -
the client itself is per-server state on ``ServerResources`` (issue #774).
"""

# One statement per owning module; every name is a rebind, not a copy.
# ``__all__`` below is the supported surface. The leading-underscore names are
# NOT exports - they stay out of ``__all__`` - but they must stay bound on the
# package, because ``spec_tools`` and ``lifespan`` read them there and the
# boundary suites call them through it; the ``F401`` silence marks them as
# re-exports rather than unused imports.
from ...errors import BODY_SLICE_BYTES
from .guard import (
    _PIN_FETCH_LOCK,  # noqa: F401
    ALLOWED_DOWNLOAD_HOSTS,
    DownloadRedirectRefusedError,
    _check_download_redirect_target,  # noqa: F401
    _is_public_address,  # noqa: F401
    _pinned_dns,  # noqa: F401
)
from .preview import DOWNLOAD_PREVIEW_MAX_BYTES, DOWNLOAD_PREVIEW_MAX_LINES
from .redirects import (
    _MAX_DOWNLOAD_REDIRECTS,  # noqa: F401
    RedirectClientOwner,
    _follow_redirects_to_tempfile,  # noqa: F401
    _redirect_client_owner,  # noqa: F401
    close_redirect_client,
    current_redirect_client_owner,
    redirect_client_owned,
)
from .retention import (
    MAX_RETAINED_DOWNLOADS,
    DownloadRegistry,
    MissingDownloadRegistryError,
    _download_registry,  # noqa: F401
    _note_successful_download,  # noqa: F401
    _remove_temp_file,  # noqa: F401
    cleanup_successful_downloads,
    current_download_registry,
    download_registry,
    downloads_owned,
)
from .stream import (
    DownloadTooLargeError,
    _raise_for_streamed_status,  # noqa: F401
    _write_stream_to_tempfile,  # noqa: F401
    read_body_capped,
)
from .tool import (
    _download_error_details,  # noqa: F401
    _download_logs_to_tempfile,  # noqa: F401
)

__all__ = [
    "ALLOWED_DOWNLOAD_HOSTS",
    "BODY_SLICE_BYTES",
    "DOWNLOAD_PREVIEW_MAX_BYTES",
    "DOWNLOAD_PREVIEW_MAX_LINES",
    "MAX_RETAINED_DOWNLOADS",
    "DownloadRedirectRefusedError",
    "DownloadRegistry",
    "DownloadTooLargeError",
    "MissingDownloadRegistryError",
    "RedirectClientOwner",
    "cleanup_successful_downloads",
    "close_redirect_client",
    "current_download_registry",
    "current_redirect_client_owner",
    "download_registry",
    "downloads_owned",
    "read_body_capped",
    "redirect_client_owned",
]
