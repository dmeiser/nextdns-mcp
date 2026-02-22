"""Tests for configuration constants and route mappings."""

from nextdns_mcp.config import DNS_STATUS_CODES, EXCLUDED_ROUTES, VALID_DNS_RECORD_TYPES


def test_dns_status_codes_contain_required_codes():
    """Test DNS status codes include essential values."""
    assert 0 in DNS_STATUS_CODES  # NOERROR
    assert 1 in DNS_STATUS_CODES  # FORMERR
    assert 2 in DNS_STATUS_CODES  # SERVFAIL
    assert 3 in DNS_STATUS_CODES  # NXDOMAIN
    assert 4 in DNS_STATUS_CODES  # NOTIMP
    assert 5 in DNS_STATUS_CODES  # REFUSED

    # Verify descriptions are present
    for code, desc in DNS_STATUS_CODES.items():
        assert isinstance(desc, str)
        assert desc.strip()  # Not empty string


def test_valid_dns_record_types_contain_required_types():
    """Test DNS record types include essential values."""
    required_types = [
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

    for record_type in required_types:
        assert record_type in VALID_DNS_RECORD_TYPES


def test_excluded_routes_contain_required_patterns():
    """Test excluded routes contain expected patterns."""
    # Extract route patterns for easy validation
    patterns = [route.pattern for route in EXCLUDED_ROUTES]

    # Truly unsupported endpoints remain excluded
    assert r"^/profiles/\{profile_id\}/analytics/domains;series$" in patterns
    assert r"^/profiles/\{profile_id\}/logs/stream$" in patterns

    # Array-based PUT endpoints are no longer excluded - FastMCP 3.x handles them natively
    assert r"^/profiles/\{profile_id\}/denylist$" not in patterns
    assert r"^/profiles/\{profile_id\}/allowlist$" not in patterns
    assert r"^/profiles/\{profile_id\}/parentalControl/services$" not in patterns
    assert r"^/profiles/\{profile_id\}/parentalControl/categories$" not in patterns
    assert r"^/profiles/\{profile_id\}/security/tlds$" not in patterns
    assert r"^/profiles/\{profile_id\}/privacy/blocklists$" not in patterns
    assert r"^/profiles/\{profile_id\}/privacy/natives$" not in patterns
