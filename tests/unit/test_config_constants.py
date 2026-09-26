"""Tests for configuration constants."""

from nextdns_mcp.config import DNS_STATUS_CODES, VALID_DNS_RECORD_TYPES


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
