"""What the server says about itself in the MCP ``initialize`` handshake.

``instructions`` is the one text every client puts in front of the model before the
first tool call, so it is contract, not decoration: it has to describe the catalog the
server actually publishes, stay true whichever size that catalog is, and never drift
into coaching a particular client or instructing a model how to behave.

SPDX-License-Identifier: MIT
"""

from nextdns_mcp.usage import ADVERTISE_CATALOG_MAX_TOOLS, server_instructions


class TestInstructionsDescribeThePublishedSurface:
    def test_one_tool_per_endpoint_and_no_operation_to_choose(self):
        """The first thing a caller needs is that the name is the whole decision."""
        text = server_instructions("off")
        assert "one tool per endpoint" in text
        assert "no operation to choose" in text

    def test_the_two_local_operations_are_named(self):
        """Charting and DoH resolution have no endpoint, so nothing else says they exist."""
        text = server_instructions("off")
        assert "plotAnalytics" in text
        assert "dohLookup" in text

    def test_failure_is_described_as_the_contract_it_is(self):
        text = server_instructions("off")
        assert "error `code`" in text
        assert "hint" in text

    def test_the_shape_of_a_call_is_described(self):
        """`profile_id` plus the endpoint's own fields is what a caller has to get right."""
        text = server_instructions("off")
        assert "profile_id" in text


class TestCatalogSizeDecidesWhatIsAdvertised:
    """Naming the catalog is a cost judgement, and the cost flips with its size."""

    def test_off_mode_names_no_search_hop(self):
        text = server_instructions("off", ["getProfile"])
        assert "search_tools" not in text

    def test_a_small_catalog_is_named_in_full(self):
        names = ["getProfile", "listProfiles", "addToDenylist"]
        text = server_instructions("bm25", names)
        for name in names:
            assert f"`{name}`" in text, f"{name} should be named where `call_tool` can use it"
        assert "search_tools" in text

    def test_a_large_catalog_points_at_search_instead_of_naming_tools(self):
        names = [f"tool{i}" for i in range(ADVERTISE_CATALOG_MAX_TOOLS + 1)]
        text = server_instructions("bm25", names)
        assert "does not advertise tool names" in text
        assert "tool0" not in text

    def test_an_unknown_catalog_size_does_not_guess(self):
        """No names means nothing to advertise, not an empty catalog to state."""
        text = server_instructions("bm25", [])
        assert "does not advertise tool names" in text

    def test_the_boundary_is_the_advertise_constant(self):
        """The threshold is a number in the code, and the behaviour changes exactly there."""
        at_limit = [f"tool{i}" for i in range(ADVERTISE_CATALOG_MAX_TOOLS)]
        over_limit = [*at_limit, "one_more"]
        assert "`tool0`" in server_instructions("bm25", at_limit)
        assert "tool0" not in server_instructions("bm25", over_limit)


class TestInstructionsStayTheServersBusiness:
    def test_no_client_or_tool_names_in_the_instructions(self):
        blob = " ".join(server_instructions(mode, ["getProfile"]) for mode in ("off", "bm25", "regex")).casefold()
        for marker in ("opencode", "claude", "code mode", "getdenylist", "listprofiles"):
            assert marker not in blob, f"instructions mention {marker!r}"

    def test_no_behaviour_instructions(self):
        """A DNS server's instructions say what it exposes, not how to behave.

        A line like "never guess a name" or "do not undo your own writes" is a
        particular caller's failure mode speaking through the product, and it does
        not belong in a protocol field every client receives.
        """
        blob = " ".join(server_instructions(mode) for mode in ("off", "bm25", "regex")).casefold()
        for marker in ("never guess", "do not", "instead of repeating", "you should", "must not"):
            assert marker not in blob, f"instructions instruct behaviour with {marker!r}"
