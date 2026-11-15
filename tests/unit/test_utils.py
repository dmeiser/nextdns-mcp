from nextdns_mcp import utils as utils_module


def test_is_float_shaped_malformed_numerics():
    for value in ("3-", "1-2", "3.14-", "--3", "-"):
        assert utils_module.is_float_shaped(value) is False, value

    for value in ("-3", "-3.14", "0", "3.14", "3.", "-.5"):
        assert utils_module.is_float_shaped(value) is True, value
