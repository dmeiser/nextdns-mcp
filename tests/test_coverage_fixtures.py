"""Behavioral tests that feed the conftest fixtures into application code.

These replace fixture-introspection asserts (path exists, value is not None)
with assertions on observable behavior: each fixture drives a real code path -
config key loading, profile access gates, DoH response parsing - so a
regression in that behavior fails the test instead of the suite merely
confirming a fixture returned a value.
"""

from nextdns_mcp import config
from nextdns_mcp.tools import doh as doh_module


class _JsonResponse:
    """Minimal stand-in for an httpx.Response carrying a JSON body."""

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeDohClient:
    """Async client answering every GET with the given DNS JSON payload."""

    def __init__(self, payload):
        self._payload = payload
        self.requests = []

    async def get(self, doh_url, params=None, headers=None):
        self.requests.append({"url": doh_url, "params": params, "headers": headers})
        return _JsonResponse(dict(self._payload))


def _allow_all_access() -> config.ProfileAccessControl:
    """An open read/write snapshot, matching the suite's default gate."""
    return config.ProfileAccessControl(read_only=False, readable=frozenset(), writable=frozenset())


def test_api_key_file_fixture_loads_through_config(monkeypatch, temp_api_key_file):
    """The temp key file must be readable via the Docker-secret env path."""
    monkeypatch.setenv("NEXTDNS_API_KEY_FILE", str(temp_api_key_file))
    assert config.get_api_key() == "test_api_key_12345"

    # A missing secret file must yield None, not raise.
    monkeypatch.setenv("NEXTDNS_API_KEY_FILE", f"{temp_api_key_file}.missing")
    assert config.get_api_key() is None


def test_env_api_key_fixture_and_profiles_drive_access_gate(set_env_api_key, mock_profile_id, mock_profiles_response):
    """set_env_api_key must reach the config layer, and the profile ids from
    mock_profiles_response must be honored by the read gate."""
    assert config.get_api_key() == "test_api_key_12345"

    listed_ids = {p["id"] for p in mock_profiles_response["data"]}
    assert mock_profile_id in listed_ids

    allow_one = config.ProfileAccessControl(read_only=False, readable=frozenset({mock_profile_id}), writable=None)
    assert allow_one.can_read(mock_profile_id) is True
    other_id = next(pid for pid in listed_ids if pid != mock_profile_id)
    assert allow_one.can_read(other_id) is False


async def test_doh_response_fixture_drives_lookup(mock_profile_id, mock_doh_response, mock_nextdns_base_url):
    """A fixture DoH answer must survive doh_lookup's parsing and metadata
    mapping - these assertions fail if the status-code mapping or the answer
    shape regress."""
    fake = _FakeDohClient(mock_doh_response)
    with doh_module.doh_client_owned(doh_module.DohClientOwner(client=fake)):
        doh_url = f"{mock_nextdns_base_url}/{mock_profile_id}/dns-query"
        result = await doh_module.doh_lookup(doh_url, "google.com", "A", mock_profile_id, _allow_all_access())

    assert result["data"] == mock_doh_response
    metadata = result["_metadata"]
    assert metadata["profile_id"] == mock_profile_id
    assert metadata["query_domain"] == "google.com"
    assert metadata["query_type"] == "A"
    assert metadata["status_description"] == "NOERROR - Success"
    assert metadata["doh_endpoint"] == f"{doh_url}?name=google.com&type=A"

    # The request the tool actually sent is pinned too.
    (sent,) = fake.requests
    assert sent["url"] == doh_url
    assert sent["params"] == {"name": "google.com", "type": "A"}
    assert sent["headers"] == {"accept": "application/dns-json"}
