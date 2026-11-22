from nextdns_mcp import utils as utils_module


def test_is_float_shaped_malformed_numerics():
    for value in ("3-", "1-2", "3.14-", "--3", "-"):
        assert utils_module.is_float_shaped(value) is False, value

    for value in ("-3", "-3.14", "0", "3.14", "3.", "-.5"):
        assert utils_module.is_float_shaped(value) is True, value


def test_truncate_at_boundary_leaves_text_at_or_below_limit_untouched():
    assert utils_module._truncate_at_boundary("short", 100) == "short"
    assert utils_module._truncate_at_boundary("a" * 100, 100) == "a" * 100


def test_truncate_at_boundary_prefers_an_early_newline():
    text = "x" * 30 + "\n" + "y" * 30
    out = utils_module._truncate_at_boundary(text, 40)
    assert out == "x" * 30
    assert len(out) <= 40


def test_truncate_at_boundary_newline_before_halfway_falls_back_to_hard_cut():
    text = "\n" + "y" * 50
    out = utils_module._truncate_at_boundary(text, 40)
    assert out == text[:40].rstrip()


def test_truncate_at_boundary_sentence_boundary_keeps_the_period():
    text = "a" * 25 + ". " + "b" * 100
    out = utils_module._truncate_at_boundary(text, 40, sentence=True)
    assert out == "a" * 25 + "."
    # Without sentence=True the ". " break is not considered.
    out_plain = utils_module._truncate_at_boundary(text, 40)
    assert out_plain == text[:40]


def test_truncate_at_boundary_no_boundary_hard_cuts_at_the_budget():
    out = utils_module._truncate_at_boundary("a" * 2000, 100)
    assert len(out) == 100
