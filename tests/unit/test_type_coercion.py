"""Unit tests for type coercion functions in server.py."""

import pytest

from nextdns_mcp.server import _coerce_string_to_bool, _coerce_string_to_number, coerce_json_types


class TestCoerceStringToBool:
    """Test _coerce_string_to_bool helper function."""

    def test_lowercase_true(self):
        """Test that 'true' returns True."""
        assert _coerce_string_to_bool("true") is True

    def test_lowercase_false(self):
        """Test that 'false' returns False."""
        assert _coerce_string_to_bool("false") is False

    def test_uppercase_true(self):
        """Test that 'TRUE' returns True."""
        assert _coerce_string_to_bool("TRUE") is True

    def test_uppercase_false(self):
        """Test that 'FALSE' returns False."""
        assert _coerce_string_to_bool("FALSE") is False

    def test_mixedcase_true(self):
        """Test that 'TrUe' returns True."""
        assert _coerce_string_to_bool("TrUe") is True

    def test_non_boolean_string(self):
        """Test that non-boolean strings return None."""
        assert _coerce_string_to_bool("yes") is None
        assert _coerce_string_to_bool("no") is None
        assert _coerce_string_to_bool("1") is None
        assert _coerce_string_to_bool("0") is None
        assert _coerce_string_to_bool("") is None
        assert _coerce_string_to_bool("random") is None


class TestCoerceStringToNumber:
    """Test _coerce_string_to_number helper function."""

    def test_positive_integer(self):
        """Test that positive integer strings are coerced to int."""
        assert _coerce_string_to_number("42") == 42
        assert _coerce_string_to_number("0") == 0
        assert _coerce_string_to_number("999") == 999

    def test_negative_integer(self):
        """Test that negative integer strings are coerced to int."""
        assert _coerce_string_to_number("-42") == -42
        assert _coerce_string_to_number("-1") == -1
        assert _coerce_string_to_number("-999") == -999

    def test_positive_float(self):
        """Test that positive float strings are coerced to float."""
        assert _coerce_string_to_number("3.14") == 3.14
        assert _coerce_string_to_number("0.5") == 0.5
        assert _coerce_string_to_number("99.99") == 99.99

    def test_negative_float(self):
        """Test that negative float strings are coerced to float."""
        assert _coerce_string_to_number("-3.14") == -3.14
        assert _coerce_string_to_number("-0.5") == -0.5
        assert _coerce_string_to_number("-99.99") == -99.99

    def test_non_numeric_string(self):
        """Test that non-numeric strings return None."""
        assert _coerce_string_to_number("abc") is None
        assert _coerce_string_to_number("12.34.56") is None
        assert _coerce_string_to_number("true") is None
        assert _coerce_string_to_number("") is None
        assert _coerce_string_to_number("3.14.15") is None

    def test_edge_cases(self):
        """Test edge cases like multiple dots or mixed characters."""
        assert _coerce_string_to_number("--5") is None
        assert _coerce_string_to_number("5-") is None
        assert _coerce_string_to_number(".5") == 0.5  # Leading dot is valid float


class TestCoerceJsonTypes:
    """Test coerce_json_types function."""

    def test_dict_with_string_boolean(self):
        """Test that dict with string booleans are coerced."""
        data = {"enabled": "true", "disabled": "false"}
        result = coerce_json_types(data)
        assert result == {"enabled": True, "disabled": False}

    def test_dict_with_string_numbers(self):
        """Test that dict with string numbers are coerced."""
        data = {"count": "42", "price": "3.14", "negative": "-5"}
        result = coerce_json_types(data)
        assert result == {"count": 42, "price": 3.14, "negative": -5}

    def test_dict_with_mixed_types(self):
        """Test that dict with mixed types are coerced correctly."""
        data = {
            "enabled": "true",
            "count": "10",
            "price": "9.99",
            "name": "test",
            "negative": "-42",
        }
        result = coerce_json_types(data)
        assert result == {
            "enabled": True,
            "count": 10,
            "price": 9.99,
            "name": "test",
            "negative": -42,
        }

    def test_list_with_string_types(self):
        """Test that list with string types are coerced."""
        data = ["true", "42", "3.14", "text", "false", "-5"]
        result = coerce_json_types(data)
        assert result == [True, 42, 3.14, "text", False, -5]

    def test_nested_dict(self):
        """Test that nested dicts are coerced recursively."""
        data = {
            "outer": {
                "inner": {"enabled": "true", "count": "5"},
                "value": "false",
            }
        }
        result = coerce_json_types(data)
        assert result == {"outer": {"inner": {"enabled": True, "count": 5}, "value": False}}

    def test_nested_list(self):
        """Test that nested lists are coerced recursively."""
        data = [["true", "1"], ["false", "2.5"]]
        result = coerce_json_types(data)
        assert result == [[True, 1], [False, 2.5]]

    def test_primitive_boolean_string(self):
        """Test that primitive boolean strings are coerced."""
        assert coerce_json_types("true") is True
        assert coerce_json_types("false") is False

    def test_primitive_number_string(self):
        """Test that primitive number strings are coerced."""
        assert coerce_json_types("42") == 42
        assert coerce_json_types("3.14") == 3.14
        assert coerce_json_types("-5") == -5

    def test_primitive_non_coercible_string(self):
        """Test that non-coercible strings remain strings."""
        assert coerce_json_types("hello") == "hello"
        assert coerce_json_types("") == ""
        assert coerce_json_types("not-a-bool") == "not-a-bool"

    def test_non_string_primitives(self):
        """Test that non-string primitives pass through unchanged."""
        assert coerce_json_types(42) == 42
        assert coerce_json_types(3.14) == 3.14
        assert coerce_json_types(True) is True
        assert coerce_json_types(False) is False
        assert coerce_json_types(None) is None

    def test_empty_dict(self):
        """Test that empty dict is handled correctly."""
        assert coerce_json_types({}) == {}

    def test_empty_list(self):
        """Test that empty list is handled correctly."""
        assert coerce_json_types([]) == []
