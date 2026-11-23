"""Meta-test: conftest's ``restore_lazy_singletons`` fixture restores module-global state.

The suite sets and clears the probe-transport singleton through
``nextdns_mcp.client``'s ``*_raw_probe_client_singleton_for_tests`` seam. A seam
call, unlike ``monkeypatch.setattr`` on the private name, is not undone at
teardown, so restoration has to come from the autouse fixture. This test pins
the mechanism the same way ``test_conftest_hermetic_env.py`` pins
``restore_nextdns_modules``: one test installs an unmistakable double through
the seam, the next one asserts the double is gone. A failure here means a test
can leave its private state for whichever test runs next.

On the pre-fix head, where the setup sites poked the module dict directly
through monkeypatch (its own auto-restore), the first test still cleared the
slot, and the second one observed the double — the same failure.
"""

from nextdns_mcp import client as client_module

_TRANSPORT_SENTINEL = object()


def test_installs_sentinel_in_raw_probe_slot() -> None:
    """Install a double through the seam, as a setup test would."""
    assert client_module._get_raw_probe_client_singleton_for_tests() is None
    client_module._set_raw_probe_client_singleton_for_tests(_TRANSPORT_SENTINEL)
    assert client_module._get_raw_probe_client_singleton_for_tests() is _TRANSPORT_SENTINEL


def test_raw_probe_slot_is_restored_after_the_previous_test() -> None:
    """The autouse fixture must have written the pre-test value back."""
    assert client_module._get_raw_probe_client_singleton_for_tests() is None


def test_setter_accepts_none_to_request_lazy_creation() -> None:
    """Clearing the slot through the seam is the supported way to force construction."""
    client_module._set_raw_probe_client_singleton_for_tests(_TRANSPORT_SENTINEL)
    client_module._set_raw_probe_client_singleton_for_tests(None)
    assert client_module._get_raw_probe_client_singleton_for_tests() is None
