"""Tests for configuration constants."""

import subprocess
import sys

import pytest

from nextdns_mcp import constants
from nextdns_mcp.config import DNS_STATUS_CODES, VALID_DNS_RECORD_TYPES
from nextdns_mcp.tools import doh as doh_module

_BASELINE_RECORD_TYPES = [
    "A",
    "AAAA",
    "CNAME",
    "MX",
    "NS",
    "PTR",
    "SOA",
    "TXT",
    "SRV",
    "CAA",
    "DNSKEY",
    "DS",
]


def _run_probe(source: str) -> None:
    """Run ``source`` in a clean interpreter, failing the test on any error."""
    result = subprocess.run([sys.executable, "-c", source], check=False, capture_output=True, text=True)
    assert result.returncode == 0, f"probe failed:\n{result.stderr}"


def test_dns_status_codes_contain_required_codes():
    """Test DNS status codes include essential values."""
    assert 0 in DNS_STATUS_CODES  # NOERROR
    assert 1 in DNS_STATUS_CODES  # FORMERR
    assert 2 in DNS_STATUS_CODES  # SERVFAIL
    assert 3 in DNS_STATUS_CODES  # NXDOMAIN
    assert 4 in DNS_STATUS_CODES  # NOTIMP
    assert 5 in DNS_STATUS_CODES  # REFUSED

    # Verify descriptions are present
    for desc in DNS_STATUS_CODES.values():
        assert isinstance(desc, str)
        assert desc.strip()  # Not empty string


def test_valid_dns_record_types_contain_required_types():
    """Test DNS record types include essential values."""
    for record_type in _BASELINE_RECORD_TYPES:
        assert record_type in VALID_DNS_RECORD_TYPES


def test_constants_are_defined_once_and_shared_by_both_exporters():
    """``constants`` owns both definitions; ``config`` and ``tools.doh`` only re-export them."""
    assert constants.DNS_STATUS_CODES is DNS_STATUS_CODES
    assert constants.VALID_DNS_RECORD_TYPES is VALID_DNS_RECORD_TYPES
    assert constants.DNS_STATUS_CODES is doh_module.DNS_STATUS_CODES
    assert constants.VALID_DNS_RECORD_TYPES is doh_module.VALID_DNS_RECORD_TYPES


def test_config_answers_both_names_without_importing_the_tool_layer():
    """Issue #622: ``config`` must not reach into ``tools.doh`` to answer these names.

    ``config.__getattr__`` used to answer both names with a deferred
    ``from .tools.doh import ...``, so a base-layer module depended on a tool
    module. Reading both attributes off ``config`` in a clean interpreter must
    leave every ``nextdns_mcp.tools`` module unimported.
    """
    _run_probe(
        "import sys\n"
        "import nextdns_mcp.config as config\n"
        "config.DNS_STATUS_CODES\n"
        "config.VALID_DNS_RECORD_TYPES\n"
        "tools = sorted(m for m in sys.modules if m.startswith('nextdns_mcp.tools'))\n"
        "assert not tools, tools\n"
    )


@pytest.mark.parametrize(
    ("first", "second"),
    [("nextdns_mcp.config", "nextdns_mcp.tools.doh"), ("nextdns_mcp.tools.doh", "nextdns_mcp.config")],
)
def test_both_import_orders_keep_both_names_at_their_existing_paths(first: str, second: str) -> None:
    """Both names stay importable from ``config`` and from ``tools.doh``, unchanged."""
    _run_probe(
        f"import {first}\n"
        f"import {second}\n"
        "from nextdns_mcp.config import DNS_STATUS_CODES, VALID_DNS_RECORD_TYPES\n"
        "from nextdns_mcp.tools.doh import DNS_STATUS_CODES as doh_codes, VALID_DNS_RECORD_TYPES as doh_types\n"
        "from nextdns_mcp.constants import DNS_STATUS_CODES as base_codes, VALID_DNS_RECORD_TYPES as base_types\n"
        "assert DNS_STATUS_CODES is doh_codes is base_codes\n"
        "assert VALID_DNS_RECORD_TYPES is doh_types is base_types\n"
        "assert isinstance(DNS_STATUS_CODES, dict) and isinstance(VALID_DNS_RECORD_TYPES, list)\n"
        "assert list(DNS_STATUS_CODES) == [0, 1, 2, 3, 4, 5]\n"
        f"assert VALID_DNS_RECORD_TYPES == {_BASELINE_RECORD_TYPES!r}\n"
    )
