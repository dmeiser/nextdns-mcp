from nextdns_mcp import utils as utils_module


def test_is_float_shaped_malformed_numerics():
    for value in ("3-", "1-2", "3.14-", "--3", "-", "notfloat", "\u00b2", "1.\u00b2", ""):
        assert utils_module.is_float_shaped(value) is False, value

    for value in ("-3", "-3.14", "0", "3.14", "3.", "-.5", "1.23", "-1.23", "3"):
        assert utils_module.is_float_shaped(value) is True, value


def test_is_integer_shaped_separates_integers_from_every_other_shape():
    """The shape test behind the coercion guard (issue #273).

    ``0012`` is integer-shaped and must stay that way: it is why the schema-aware
    layer is the only thing standing between a leading-zero identifier and ``int``.
    """
    for value in ("123", "-5", "0012"):
        assert utils_module.is_integer_shaped(value) is True, value

    for value in ("1.2", "\u00b2", "-\u00b2"):
        assert utils_module.is_integer_shaped(value) is False, value


def test_truncate_at_boundary_leaves_text_at_or_below_limit_untouched():
    assert utils_module._truncate_at_boundary("short", 100) == "short"
    assert utils_module._truncate_at_boundary("a" * 100, 100) == "a" * 100


def test_truncate_at_boundary_prefers_a_boundary_after_halfway():
    text = "x" * 30 + "\n" + "y" * 30
    out = utils_module._truncate_at_boundary(text, 40)
    assert out == "x" * 30
    assert len(out) <= 40


def test_truncate_at_boundary_one_before_halfway_falls_back_to_hard_cut():
    """A boundary at ``limit // 2 - 1`` is too early: it would discard over half the budget."""
    text = "a" * 19 + "\n" + "c" * 40
    out = utils_module._truncate_at_boundary(text, 40)
    assert out == text[:40].rstrip()


def test_truncate_at_boundary_at_halfway_qualifies():
    """A boundary exactly at ``limit // 2`` is the first qualifying position (the guard is ``<``)."""
    text = "a" * 20 + "\n" + "c" * 40
    out = utils_module._truncate_at_boundary(text, 40)
    assert out == "a" * 20


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
