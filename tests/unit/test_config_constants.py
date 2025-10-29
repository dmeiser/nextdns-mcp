"""Tests for configuration constants and route mappings."""
from nextdns_mcp.config import (
    DNS_STATUS_CODES, VALID_DNS_RECORD_TYPES, EXCLUDED_ROUTES,
    MCPType
)


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
        "A", "AAAA", "CNAME", "MX", "NS", "PTR",
        "SOA", "TXT", "SRV", "CAA", "DNSKEY", "DS"
    ]
    
    for record_type in required_types:
        assert record_type in VALID_DNS_RECORD_TYPES
        
        
def test_excluded_routes_contain_required_patterns():
    """Test excluded routes contain expected patterns."""
    # Extract route patterns for easy validation
    patterns = [route.pattern for route in EXCLUDED_ROUTES]
    
    # Array-based PUT endpoints
    assert r"^/profiles/\{profile_id\}/denylist$" in patterns
    assert r"^/profiles/\{profile_id\}/allowlist$" in patterns
    assert r"^/profiles/\{profile_id\}/parentalControl/services$" in patterns
    assert r"^/profiles/\{profile_id\}/parentalControl/categories$" in patterns
    assert r"^/profiles/\{profile_id\}/security/tlds$" in patterns
    assert r"^/profiles/\{profile_id\}/privacy/blocklists$" in patterns
    assert r"^/profiles/\{profile_id\}/privacy/natives$" in patterns
    
    # Unsupported endpoints
    assert r"^/profiles/\{profile_id\}/analytics/domains;series$" in patterns
    assert r"^/profiles/\{profile_id\}/logs/stream$" in patterns
    
    # Verify PUT methods for array-based endpoints
    array_patterns = [
        r"^/profiles/\{profile_id\}/denylist$",
        r"^/profiles/\{profile_id\}/allowlist$",
        r"^/profiles/\{profile_id\}/parentalControl/services$",
        r"^/profiles/\{profile_id\}/parentalControl/categories$",
        r"^/profiles/\{profile_id\}/security/tlds$",
        r"^/profiles/\{profile_id\}/privacy/blocklists$",
        r"^/profiles/\{profile_id\}/privacy/natives$"
    ]
    
    for pattern in array_patterns:
        route = next(r for r in EXCLUDED_ROUTES if r.pattern == pattern)
        assert route.methods == ["PUT"]
        assert route.mcp_type == MCPType.EXCLUDE