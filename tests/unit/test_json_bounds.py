"""The shared recursive bounder: one walk, caller-supplied caps (issue #614).

``errors`` bounds an upstream document before inlining it into a tool result,
and ``call_log`` projects a result before serializing its row. Both callers
use the same walk with different caps and markers; this suite pins the walk's
contract and the two specs' shapes, so a drift between a caller's numbers and
its spec is a test failure rather than a silent behavior change.
"""

import pytest

from nextdns_mcp import call_log, errors
from nextdns_mcp.json_bounds import BoundSpec, bound_json


class TestStringCap:
    def test_a_cut_string_carries_the_marker_and_the_flag(self):
        spec = BoundSpec(max_string_chars=4, string_marker="…")
        bounded, cut = bound_json("abcdefgh", spec)
        assert bounded == "abcd…"
        assert cut is True

    def test_a_string_under_the_cap_passes_through_unmarked(self):
        spec = BoundSpec(max_string_chars=4)
        bounded, cut = bound_json("abc", spec)
        assert bounded == "abc"
        assert cut is False

    def test_a_cut_string_without_a_marker_cuts_bare(self):
        spec = BoundSpec(max_string_chars=2)
        bounded, cut = bound_json("abcd", spec)
        assert bounded == "ab"
        assert cut is True


class TestContainers:
    def test_containers_are_rebuilt_with_flag_propagating_from_a_deep_string(self):
        spec = BoundSpec(max_string_chars=2)
        bounded, cut = bound_json({"a": {"b": ["x" * 5, 7]}}, spec)
        assert bounded == {"a": {"b": ["xx", 7]}}
        assert cut is True

    def test_a_scalar_passes_through_with_no_flag(self):
        spec = BoundSpec(max_string_chars=2)
        assert bound_json(42, spec) == (42, False)
        assert bound_json(None, spec) == (None, False)
        assert bound_json(True, spec) == (True, False)

    def test_the_input_document_is_not_mutated(self):
        spec = BoundSpec(max_string_chars=2)
        source = {"a": ["x" * 5, {"b": ["y" * 5]}]}
        snapshot = {"a": ["x" * 5, {"b": ["y" * 5]}]}
        bound_json(source, spec)
        assert source == snapshot


class TestListCap:
    def test_a_list_over_the_cap_keeps_leading_items_and_names_the_dropped_ones(self):
        spec = BoundSpec(max_string_chars=200, max_items=2, omitted_key="_omitted_items")
        bounded, cut = bound_json(["a", "b", "c", "d", "e"], spec)
        assert bounded == ["a", "b", {"_omitted_items": 3}]
        assert cut is True

    def test_a_list_within_the_cap_is_kept_whole_without_a_marker(self):
        spec = BoundSpec(max_string_chars=200, max_items=3, omitted_key="_omitted_items")
        bounded, cut = bound_json(["a", "b", "c"], spec)
        assert bounded == ["a", "b", "c"]
        assert cut is False

    def test_the_omission_marker_counts_what_the_cap_dropped(self):
        spec = BoundSpec(max_string_chars=200, max_items=1, omitted_key="_omitted_items")
        bounded, _ = bound_json(["a", "b", "c", "d"], spec)
        assert bounded == ["a", {"_omitted_items": 3}]


class TestKeyCap:
    def test_a_dict_over_the_key_cap_keeps_the_leading_keys_and_flags_the_drop(self):
        # Keys the cap drops are content the reader cannot see is missing, so
        # the cut is flagged exactly like a dropped list item (issue #753).
        spec = BoundSpec(max_string_chars=200, max_keys=2)
        bounded, cut = bound_json({"a": 1, "b": 2, "c": 3}, spec)
        assert bounded == {"a": 1, "b": 2}
        assert cut is True

    def test_the_key_cap_slices_lazily(self):
        consumed: list[str] = []

        class CountingDict(dict):
            def items(self):
                for item in dict.items(self):
                    consumed.append(item[0])
                    yield item

        spec = BoundSpec(max_string_chars=200, max_keys=2)
        bound_json(CountingDict({"a": 1, "b": 2, "c": 3}), spec)
        assert consumed == ["a", "b"]


class TestKeyBounding:
    def test_keys_pass_through_uncapped_when_the_spec_does_not_bound_them(self):
        spec = BoundSpec(max_string_chars=4)
        bounded, cut = bound_json({"abcdef": "x"}, spec)
        assert bounded == {"abcdef": "x"}
        assert cut is False

    def test_a_long_key_is_cut_marked_and_flagged_when_the_spec_bounds_keys(self):
        spec = BoundSpec(max_string_chars=4, string_marker="…", bound_keys=True)
        bounded, cut = bound_json({"abcdef": "v"}, spec)
        assert bounded == {"abcd…": "v"}
        assert cut is True

    def test_a_key_under_the_cap_keeps_its_text_and_value(self):
        spec = BoundSpec(max_string_chars=4, string_marker="…", bound_keys=True)
        bounded, cut = bound_json({"abc": "x"}, spec)
        assert bounded == {"abc": "x"}
        assert cut is False

    def test_cut_keys_that_collide_are_named_so_no_field_is_lost(self):
        spec = BoundSpec(max_string_chars=4, string_marker="…", bound_keys=True)
        bounded, cut = bound_json({"abcde one": 1, "abcde two": 2, "abcde three": 3}, spec)
        assert bounded == {"abcd…": 1, "abcd… [duplicate 2]": 2, "abcd… [duplicate 3]": 3}
        assert cut is True

    def test_collision_naming_stays_correct_at_scale(self):
        spec = BoundSpec(max_string_chars=4, string_marker="…", bound_keys=True)
        source = {f"abcde {index}": index for index in range(100)}
        bounded, cut = bound_json(source, spec)
        assert bounded["abcd…"] == 0
        for index in range(2, 101):
            assert bounded[f"abcd… [duplicate {index}]"] == index - 1
        assert cut is True

    def test_a_candidate_name_already_in_the_mapping_is_skipped(self):
        # ``bound_key`` never overwrites a field the mapping under construction
        # already holds, even a literal one spelled like a collision candidate:
        # the search steps past it to the next free suffix.
        from nextdns_mcp.json_bounds import bound_key

        spec = BoundSpec(max_string_chars=4, string_marker="…", bound_keys=True)
        bounded: dict = {"abcd…": "kept", "abcd… [duplicate 2]": "literal"}
        key, cut = bound_key(bounded, "abcde one", spec, {})
        assert key == "abcd… [duplicate 3]"
        assert cut is True

    def test_the_suffix_search_probes_each_candidate_at_most_once(self):
        # The collision counter never moves backwards, so naming n colliding
        # keys costs O(n) membership probes in total; restarting from
        # [duplicate 2] on every collision cost O(n^2) (issue #753).
        from nextdns_mcp.json_bounds import _distinct_key

        class CountingDict(dict):
            def __init__(self) -> None:
                super().__init__()
                self.contains_calls = 0

            def __contains__(self, key):
                self.contains_calls += 1
                return super().__contains__(key)

        bounded = CountingDict()
        bounded["k"] = None
        collisions: dict[str, int] = {}
        for _ in range(200):
            bounded[_distinct_key(bounded, "k", collisions)] = None
        assert bounded.contains_calls == 200

    def test_a_non_string_key_passes_through_without_a_flag(self):
        # ``json`` never decodes a non-string key, but a caller that walks a
        # document it did not write must not fail on a key the string cap
        # cannot be applied to.
        spec = BoundSpec(max_string_chars=4, string_marker="…", bound_keys=True)
        bounded, cut = bound_json({422: "x"}, spec)
        assert bounded == {422: "x"}
        assert cut is False


class TestDepthCap:
    def test_a_nesting_past_the_depth_cap_is_projected_to_none(self):
        spec = BoundSpec(max_string_chars=200, max_depth=2)
        bounded, _ = bound_json({"data": {"list": [["deep"]]}}, spec)
        assert bounded == {"data": {"list": [None]}}

    def test_the_depth_offset_is_honored(self):
        spec = BoundSpec(max_string_chars=200, max_depth=1)
        # Depth 1 is the first allowed level; the list at depth 2 arrives as None.
        bounded, _ = bound_json([["deep"]], spec, depth=1)
        assert bounded == [None]

    def test_a_depth_cut_is_flagged(self):
        # The None left behind is indistinguishable from a null the document
        # carried, so the cut is flagged like every other drop (issue #753).
        spec = BoundSpec(max_string_chars=200, max_depth=1)
        _, cut = bound_json([["deep"]], spec)
        assert cut is True


class TestCallerSpecs:
    def test_the_error_path_spec_matches_the_module_constants(self):
        from nextdns_mcp.errors import body as errors_body
        from nextdns_mcp.errors.body import _BODY_BOUNDS

        assert _BODY_BOUNDS.max_string_chars == errors.MAX_RESPONSE_BODY_CHARS
        assert _BODY_BOUNDS.string_marker == errors.TRUNCATION_MARKER
        assert _BODY_BOUNDS.max_items == errors_body.MAX_BODY_ITEMS
        assert _BODY_BOUNDS.omitted_key == errors_body.BODY_OMITTED_ITEMS_KEY
        assert _BODY_BOUNDS.max_keys == errors_body.MAX_BODY_KEYS
        assert _BODY_BOUNDS.max_depth == errors_body.MAX_BODY_DEPTH

    def test_the_call_log_spec_matches_the_module_constants(self):
        from nextdns_mcp.call_log import _PROJECTION_BOUNDS

        assert _PROJECTION_BOUNDS == BoundSpec(
            max_string_chars=call_log.PROJECT_MAX_STRING_CHARS,
            max_items=call_log.PROJECT_MAX_ITEMS,
            omitted_key=call_log.OMITTED_ITEMS_KEY,
            max_keys=call_log.PROJECT_MAX_KEYS,
            max_depth=call_log.PROJECT_MAX_DEPTH,
        )

    def test_the_error_path_spec_bounds_keys_and_the_call_log_one_does_not(self):
        from nextdns_mcp.call_log import _PROJECTION_BOUNDS
        from nextdns_mcp.errors.body import _BODY_BOUNDS

        # The errors caller inlines documents it did not write, so keys are
        # capped there (issue #600); the call log writes its own projection
        # and keeps keys verbatim.
        assert _BODY_BOUNDS.bound_keys is True
        assert _PROJECTION_BOUNDS.bound_keys is False

    def test_the_spec_is_frozen(self):
        from dataclasses import FrozenInstanceError

        spec = BoundSpec(max_string_chars=1)
        with pytest.raises(FrozenInstanceError):
            spec.max_items = 1  # type: ignore[misc]
