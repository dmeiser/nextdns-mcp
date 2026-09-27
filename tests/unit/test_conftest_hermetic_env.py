"""Meta-tests for the hermetic environment fixture (issue #287).

``tests/conftest.py::clean_env`` used to clear four explicitly named variables
and only ran for tests that requested it by name, so the three access-control
variables (``NEXTDNS_READ_ONLY``, ``NEXTDNS_READABLE_PROFILES``,
``NEXTDNS_WRITABLE_PROFILES``) leaked in from the developer's shell: unit tests
failed, or worse, passed while asserting the wrong code path.

These tests simulate a fully polluted shell and assert that the fixture is
autouse and clears every ``NEXTDNS_``/``MCP_``/``FASTMCP_`` variable, so a
future key added to ``config.py`` cannot silently re-open the hole.
"""

import os
from collections.abc import Iterator

import pytest

# Every env var family the server reads, plus a name that config.py does not
# know about yet: prefix-based clearing must handle a future key too.
POLLUTED_ENV = {
    "NEXTDNS_API_KEY": "leaked-key",
    "NEXTDNS_API_KEY_FILE": "/tmp/leaked-key-file",
    "NEXTDNS_DEFAULT_PROFILE": "leaked-profile",
    "NEXTDNS_HTTP_TIMEOUT": "99",
    "NEXTDNS_READ_ONLY": "true",
    "NEXTDNS_READABLE_PROFILES": "ALL",
    "NEXTDNS_WRITABLE_PROFILES": "ALL",
    "NEXTDNS_FUTURE_KEY_NOT_IN_CONFIG_YET": "1",
    "MCP_SOMETHING": "1",
    "FASTMCP_SOMETHING": "1",
}


@pytest.fixture(scope="module", autouse=True)
def polluted_shell() -> Iterator[None]:
    """Export a fully polluted environment before the function-scoped fixtures run."""
    saved = {key: os.environ.get(key) for key in POLLUTED_ENV}
    os.environ.update(POLLUTED_ENV)
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def test_clean_env_clears_polluted_environment_without_being_requested() -> None:
    """clean_env is autouse, so these vars are gone even though nothing asked for it."""
    leaked = sorted(key for key in POLLUTED_ENV if key in os.environ)
    assert leaked == [], f"ambient config leaked into the test environment: {leaked}"


def test_clean_env_leaves_unrelated_variables_alone() -> None:
    """Clearing is scoped to the three prefixes, not a blanket os.environ wipe."""
    assert "PATH" in os.environ or "HOME" in os.environ
