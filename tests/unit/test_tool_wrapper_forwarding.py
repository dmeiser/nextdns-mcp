"""Tests that every public MCP tool wrapper forwards to its ``_impl`` by keyword.

Issue #280: the wrappers declared their full parameter list a second time and
passed the arguments positionally, so a parameter added or reordered in the
``_impl`` without a matching wrapper edit would bind silently to the wrong
parameter or fall back to its default -- no type error, no test failure.

These tests enforce two invariants for every wrapper/``_impl`` pair:

* the public wrapper and its ``_impl`` agree on parameter names and defaults
  (``inspect.signature``), and
* the wrapper forwards every parameter as a keyword argument, so the mapping
  is checked by the interpreter at call time instead of by hand.
"""

import inspect
from typing import Any

import pytest

from nextdns_mcp.tools import analytics, doh, lists, logs, plots, profiles, rewrites, settings

# (module, public wrapper, private impl) for every tool using the _impl indirection.
WRAPPER_PAIRS: list[tuple[Any, str, str]] = [
    (analytics, "queryAnalytics", "_query_analytics_impl"),
    (plots, "plotAnalytics", "_plot_analytics_series_impl"),
    (profiles, "manageProfiles", "_manage_profiles_impl"),
    (settings, "manageSettings", "_manage_settings_impl"),
    (lists, "manageLists", "_manage_lists_impl"),
    (rewrites, "manageRewrites", "_manage_rewrites_impl"),
    (logs, "manageLogs", "_manage_logs_impl"),
    (doh, "dohLookup", "_dohLookup_impl"),
]

PAIR_IDS = [f"{module.__name__.rsplit('.', 1)[-1]}.{public}" for module, public, _ in WRAPPER_PAIRS]


def _pair(index: int) -> tuple[Any, str, str]:
    return WRAPPER_PAIRS[index]


@pytest.mark.parametrize("pair", WRAPPER_PAIRS, ids=PAIR_IDS)
def test_wrapper_signature_matches_impl(pair):
    """The wrapper must re-declare exactly the parameters the impl accepts."""
    module, public, impl = pair
    wrapper_params = inspect.signature(getattr(module, public)).parameters
    impl_params = inspect.signature(getattr(module, impl)).parameters

    assert list(wrapper_params) == list(impl_params)
    for name, impl_param in impl_params.items():
        assert wrapper_params[name].default == impl_param.default
        assert wrapper_params[name].kind == impl_param.kind


@pytest.mark.parametrize("index", range(len(WRAPPER_PAIRS)), ids=PAIR_IDS)
@pytest.mark.asyncio
async def test_wrapper_forwards_all_parameters_by_keyword(index, monkeypatch):
    """Every parameter must reach the impl as a keyword, bound to the same name."""
    module, public, impl = _pair(index)
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def recording_impl(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append((args, kwargs))
        return {"ok": True}

    monkeypatch.setattr(module, impl, recording_impl)

    impl_params = inspect.signature(getattr(module, public)).parameters
    # One distinct non-default sentinel per parameter so a mis-bound or
    # positionally-shifted argument shows up as an unexpected value.
    kwargs = {name: f"sentinel-{name}" for name in impl_params}

    result = await getattr(module, public)(**kwargs)

    assert result == {"ok": True}
    assert len(calls) == 1
    args, forwarded = calls[0]
    assert args == (), f"{public} forwards {args} positionally"
    assert forwarded == kwargs
