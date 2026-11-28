"""Widget implementations, section dispatch, and the published dashboard tool.

This is the layer the MCP surface belongs to: one thin published wrapper
(``overviewDashboard``) over a private ``_impl``, one ``_CardSpec`` recipe per
single-card section over a shared fetch-word-render flow, the few sections whose
payload shape differs (kpi, gafam, security), and the table that maps a ``section``
argument to a handler. Each implementation reads through ``data``, renders through
``components`` and words its result through ``formatters`` - it holds no transport
call and no Prefab construction of its own.

SPDX-License-Identifier: MIT
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Literal, NamedTuple, Protocol, get_args

from fastmcp.tools.base import ToolResult
from prefab_ui.components import Card

from ...chart_ui import bounded_app_result
from ...charts import analytics_range_error
from ...coercion import OptionalProfileId
from ...errors import ErrorCode, error_payload
from ...utils import limit_range_error, normalize_time_value, resolve_default_profile
from .components import (
    append_extra_cards,
    assemble_overview_layout,
    build_destinations_card,
    build_devices_card,
    build_domains_card,
    build_gafam_card,
    build_ips_card,
    build_kpi_app,
    build_page_app,
    build_reasons_card,
    build_security_card,
    wrap_card_result,
)
from .data import (
    build_analytics_params,
    calc_rate,
    calculate_status_kpis,
    destination_label,
    device_label,
    domain_label,
    fetch_card_data,
    fetch_overview_payloads,
    fetch_security_payloads,
    gafam_rows,
    item_queries,
    payload_data,
    reason_label,
)
from .formatters import (
    format_items_summary,
    gafam_summary_items,
    ip_summary_items,
    overview_top_summary,
)


@dataclass(frozen=True)
class _CardSpec:
    """What a single-card section adds to the shared fetch-word-render flow.

    The endpoint, the summary wording, and the card builder are all a section
    differs in: ``_card_analytics_impl`` holds the fetch, the fail-closed guards,
    and the result wrapping once for all of them.
    """

    endpoint: str
    summary_title: str
    summary_noun: str
    items: Callable[[list[dict[str, Any]]], list[tuple[str, int]]]
    build_card: Callable[[list[dict[str, Any]], str], Card]
    fetch_extra: Mapping[str, Any] = field(default_factory=dict)


async def _card_analytics_impl(
    spec: _CardSpec,
    profile_id: str,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
    view_type: Literal["auto", "bar", "table", "pie"] = "auto",
) -> dict[str, Any] | ToolResult:
    """Fetch, word, and render the single card described by ``spec``."""
    data, err = await fetch_card_data(
        profile_id, spec.endpoint, from_time=from_time, to_time=to_time, limit=limit, **spec.fetch_extra
    )
    if err:
        return err
    if data is None:
        return error_payload(ErrorCode.INTERNAL_ERROR, "Card data fetch returned no data or error")
    items = spec.items(data)
    summary = format_items_summary(spec.summary_title, spec.summary_noun, items, sum(q for _, q in items))
    return wrap_card_result(spec.build_card(data, view_type), summary)


_RESOLVED_DOMAINS_SPEC = _CardSpec(
    endpoint="domains",
    summary_title="Resolved Domains",
    summary_noun="domains",
    items=lambda data: [(domain_label(item), item_queries(item)) for item in data],
    build_card=lambda data, view_type: build_domains_card(
        "Resolved Domains",
        "Domains that resolved without being blocked.",
        data,
        view_type=view_type,
    ),
    fetch_extra={"status": "default"},
)

_BLOCKED_DOMAINS_SPEC = _CardSpec(
    endpoint="domains",
    summary_title="Blocked Domains",
    summary_noun="domains",
    items=lambda data: [(domain_label(item), item_queries(item)) for item in data],
    build_card=lambda data, view_type: build_domains_card(
        "Blocked Domains",
        "Domains blocked by security, privacy, or parental controls.",
        data,
        view_type=view_type,
        color="#ef4444",
    ),
    fetch_extra={"status": "blocked"},
)

_ROOT_DOMAINS_SPEC = _CardSpec(
    endpoint="domains",
    summary_title="Root Domains",
    summary_noun="domains",
    items=lambda data: [(domain_label(item), item_queries(item)) for item in data],
    build_card=lambda data, view_type: build_domains_card(
        "Root Domains",
        "Aggregate queries for root domains and subdomains.",
        data,
        view_type=view_type,
    ),
    fetch_extra={"root": "true"},
)

_REASONS_SPEC = _CardSpec(
    endpoint="reasons",
    summary_title="Reasons for Blocking",
    summary_noun="blocklists",
    items=lambda data: [(reason_label(item), item_queries(item)) for item in data],
    build_card=lambda data, view_type: build_reasons_card(data, view_type=view_type),
)

_DEVICES_SPEC = _CardSpec(
    endpoint="devices",
    summary_title="Device Analytics",
    summary_noun="devices",
    items=lambda data: [(device_label(item), item_queries(item)) for item in data],
    build_card=lambda data, view_type: build_devices_card(data, view_type=view_type),
)

# IPs have no chart form: `build_ips_card` is fixed-format and ignores view_type,
# as documented for section="ips". The shared builder signature drops that argument here.
_IPS_SPEC = _CardSpec(
    endpoint="ips",
    summary_title="IP Analytics",
    summary_noun="IP addresses",
    items=ip_summary_items,
    build_card=lambda data, view_type: build_ips_card(data),
)

_COUNTRIES_SPEC = _CardSpec(
    endpoint="destinations",
    summary_title="Traffic Destinations",
    summary_noun="countries",
    items=lambda data: [(destination_label(item), item_queries(item)) for item in data],
    build_card=lambda data, view_type: build_destinations_card(data, view_type=view_type),
    fetch_extra={"type": "countries"},
)


async def _gafam_analytics_impl(
    profile_id: str,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
    view_type: Literal["auto", "pie", "bar", "table"] = "auto",
) -> dict[str, Any] | ToolResult:
    """Render the GAFAM dominance donut chart and table."""
    data, err = await fetch_card_data(
        profile_id, "destinations", from_time=from_time, to_time=to_time, limit=limit, type="gafam"
    )
    if err:
        return err
    if data is None:
        return error_payload(ErrorCode.INTERNAL_ERROR, "Card data fetch returned no data or error")
    chart_data, table_rows = gafam_rows(data)
    items = gafam_summary_items(chart_data)
    summary = format_items_summary("GAFAM Dominance", None, items, sum(q for _, q in items))
    return wrap_card_result(build_gafam_card(chart_data, table_rows, view_type=view_type), summary)


async def _security_analytics_impl(
    profile_id: str,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
    view_type: Literal["auto", "bar", "table", "pie"] = "auto",
) -> dict[str, Any] | ToolResult:
    """Render the security analytics cards (Encrypted DNS and DNSSEC rates)."""
    params = build_analytics_params(from_time=from_time, to_time=to_time)
    (encryption_payload, enc_err), (dnssec_payload, sec_err) = await fetch_security_payloads(profile_id, params)
    first_err = enc_err or sec_err
    if first_err:
        return first_err

    encryption_rate = calc_rate(payload_data(encryption_payload), "encrypted")
    dnssec_rate = calc_rate(payload_data(dnssec_payload), "validated")
    enc_val, enc_total, enc_pct = encryption_rate
    dnssec_val, dnssec_total, dnssec_pct = dnssec_rate

    summary = (
        f"[Rendered Security Analytics: Encrypted DNS {enc_pct:.2f}% ({enc_val:,}/{enc_total:,}), "
        f"DNSSEC {dnssec_pct:.2f}% ({dnssec_val:,}/{dnssec_total:,})]"
    )
    return wrap_card_result(build_security_card(encryption_rate, dnssec_rate), summary)


async def _kpi_analytics_impl(
    profile_id: str,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
    view_type: Literal["auto", "bar", "table", "pie"] = "auto",
) -> dict[str, Any] | ToolResult:
    """Render top KPI metric cards."""
    data, err = await fetch_card_data(profile_id, "status", from_time=from_time, to_time=to_time)
    if err:
        return err
    if data is None:
        return error_payload(ErrorCode.INTERNAL_ERROR, "Card data fetch returned no data or error")
    kpis = calculate_status_kpis(data)
    summary = (
        f"[Rendered NextDNS KPI Metrics: {kpis['total']:,} queries, {kpis['blocked']:,} blocked ({kpis['rate']:.2f}%)]"
    )
    return bounded_app_result(build_kpi_app(kpis), summary, "KPI cards")


DashboardSection = Literal[
    "overview",
    "all",
    "kpi",
    "domains",
    "blocked_domains",
    "reasons",
    "devices",
    "ips",
    "root_domains",
    "countries",
    "gafam",
    "security",
]

# The section table the published tool accepts, read from the same Literal so the
# E2E sweep and the actor harness cannot drift from it (#470).
DASHBOARD_SECTIONS = get_args(DashboardSection)


class _WidgetHandler(Protocol):
    def __call__(
        self,
        profile_id: str,
        from_time: str | int = "-30d",
        to_time: str | int = "now",
        limit: int = 10,
        view_type: Literal["auto", "bar", "table", "pie"] = "auto",
    ) -> Awaitable[dict[str, Any] | ToolResult]: ...


# ``functools.partial`` binds a section's recipe and leaves the handler signature the
# protocol declares, so every entry stays callable with the published arguments.
_WIDGET_DISPATCH: dict[str, _WidgetHandler] = {
    "kpi": _kpi_analytics_impl,
    "domains": partial(_card_analytics_impl, _RESOLVED_DOMAINS_SPEC),
    "blocked_domains": partial(_card_analytics_impl, _BLOCKED_DOMAINS_SPEC),
    "reasons": partial(_card_analytics_impl, _REASONS_SPEC),
    "devices": partial(_card_analytics_impl, _DEVICES_SPEC),
    "ips": partial(_card_analytics_impl, _IPS_SPEC),
    "root_domains": partial(_card_analytics_impl, _ROOT_DOMAINS_SPEC),
    "countries": partial(_card_analytics_impl, _COUNTRIES_SPEC),
    "gafam": _gafam_analytics_impl,
    "security": _security_analytics_impl,
}

# The two sections that render whole pages rather than one card; every other name in
# ``DASHBOARD_SECTIONS`` must have a widget handler.
_PAGE_SECTIONS: tuple[str, ...] = ("overview", "all")


def _check_section_parity(sections: set[str], page: set[str], widgets: set[str]) -> None:
    """Refuse to import when a section name has no handler.

    A section added to ``DashboardSection`` without an entry in ``_PAGE_SECTIONS``
    or ``_WIDGET_DISPATCH`` passes argument validation and falls through to the
    overview page, which renders the wrong dashboard without saying so.
    """
    drifted = sections ^ (page | widgets)
    if not drifted:
        return
    raise RuntimeError(f"DashboardSection and the section handlers drifted: {sorted(drifted)}")


_check_section_parity(set(DASHBOARD_SECTIONS), set(_PAGE_SECTIONS), set(_WIDGET_DISPATCH))


def _resolve_domain_view(
    domain_view: Literal["table", "bar", "pie"],
    view_type: Literal["auto", "bar", "table", "pie"],
) -> Literal["table", "bar", "pie"]:
    """Resolve active domain view format.

    Only the domain cards read this: an explicit ``view_type`` overrides the
    ``domain_view`` default, and ``"auto"`` defers to ``domain_view``.
    """
    if view_type in ("table", "bar", "pie"):
        return view_type
    return domain_view


async def _render_overview_page(
    pid: str,
    from_time: str | int,
    to_time: str | int,
    limit: int,
    section: str,
    domain_view: Literal["table", "bar", "pie"] = "table",
    view_type: Literal["auto", "bar", "table", "pie"] = "auto",
) -> dict[str, Any] | ToolResult:
    """Fetch payloads, assemble layout, and return ToolResult for overview or all.

    ``domain_view`` formats the domain cards; the non-domain cards appended by
    ``section="all"`` take ``view_type``, so its documented ``"auto"`` default is
    not coerced to that domain format (#547).
    """
    include_extra = section == "all"
    (
        status_payload,
        series_payload,
        resolved_data,
        blocked_data,
        extra_payloads,
        fetch_err,
    ) = await fetch_overview_payloads(pid, from_time, to_time, limit, include_extra=include_extra)
    if fetch_err:
        return fetch_err

    eff_domain_view = _resolve_domain_view(domain_view, view_type)
    kpis = calculate_status_kpis(payload_data(status_payload))
    view = assemble_overview_layout(kpis, series_payload, resolved_data, blocked_data, domain_view=eff_domain_view)
    if include_extra and extra_payloads is not None:
        append_extra_cards(view, extra_payloads, domain_view=eff_domain_view, view_type=view_type)

    top_summary = overview_top_summary(resolved_data, blocked_data)
    summary = (
        f"[Rendered NextDNS Dashboard: {kpis['total']:,} queries, {kpis['blocked']:,} blocked ({kpis['rate']:.2f}%). "
        f"{top_summary}]"
    )
    return bounded_app_result(build_page_app(view), summary, "dashboard page")


def _validate_dashboard_arg(name: str, value: Any, allowed_literal: Any) -> dict[str, Any] | None:
    """Return an INVALID_ARGUMENT payload when ``value`` is outside ``allowed_literal``."""
    allowed = set(get_args(allowed_literal))
    if value in allowed:
        return None
    return error_payload(
        ErrorCode.INVALID_ARGUMENT,
        f"Invalid '{name}': {value!r}; allowed values: {sorted(allowed)}",
        **{name: value},
    )


def _validate_overview_dashboard_args(section: Any, view_type: Any, domain_view: Any) -> dict[str, Any] | None:
    """Validate overview dashboard arguments; return an error payload or None."""
    for name, value, allowed in (
        ("section", section, DashboardSection),
        ("view_type", view_type, Literal["auto", "bar", "table", "pie"]),
        ("domain_view", domain_view, Literal["table", "bar", "pie"]),
    ):
        err = _validate_dashboard_arg(name, value, allowed)
        if err is not None:
            return err
    return None


class _OverviewWindow(NamedTuple):
    """The resolved profile and normalized window every section handler consumes.

    The explicit ``error`` field is always ``None`` here; it exists so the
    caller inspects one named field on the error variant instead of
    discriminating positional tuple members.
    """

    profile_id: str
    from_time: str | int
    to_time: str | int
    error: None = None


class _OverviewWindowError(NamedTuple):
    """The typed payload that stopped overview preflight."""

    error: dict[str, Any]
    profile_id: None = None
    from_time: None = None
    to_time: None = None


def _overview_preflight(
    profile_id: OptionalProfileId,
    from_time: str | int,
    to_time: str | int,
) -> _OverviewWindow | _OverviewWindowError:
    """Resolve the profile and read the window once, in one order, for every section.

    Returns a named ``_OverviewWindow`` with the window already normalized, or
    an ``_OverviewWindowError`` carrying the typed payload that stopped the
    call. Sections used to disagree here: the single widgets bounded the range
    before they touched the profile, and ``overview``/``all`` the other way
    round, so the same bad call answered a different error depending on the
    section named.
    """
    resolved = resolve_default_profile(profile_id)
    if resolved[1] is not None:
        return _OverviewWindowError(error=resolved[1])
    norm_from = normalize_time_value(from_time)
    norm_to = normalize_time_value(to_time)
    range_err = analytics_range_error(norm_from, norm_to)
    if range_err is not None:
        return _OverviewWindowError(error=range_err)
    return _OverviewWindow(profile_id=resolved[0], from_time=norm_from, to_time=norm_to)


async def _overview_dashboard_impl(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
    section: DashboardSection = "overview",
    view_type: Literal["auto", "bar", "table", "pie"] = "auto",
    domain_view: Literal["table", "bar", "pie"] = "table",
) -> dict[str, Any] | ToolResult:
    """Render one dashboard section. See ``overviewDashboard`` for the published documentation."""
    arg_err = _validate_overview_dashboard_args(section, view_type, domain_view)
    if arg_err is not None:
        return arg_err
    limit_err = limit_range_error(limit)
    if limit_err is not None:
        return limit_err

    window = _overview_preflight(profile_id, from_time, to_time)
    if isinstance(window, _OverviewWindowError):
        return window.error

    # A single widget asks for top-N rows, so its response is bounded by ``limit``;
    # only the pages that request ``status;series`` can breach the point budget,
    # and that budget is priced where the series request is built (``data``).
    # Single-widget sections are not domain-card groups: they keep view_type so
    # "auto" reaches each card's own default (#547).
    handler = _WIDGET_DISPATCH.get(section)
    if handler is not None:
        return await handler(window.profile_id, window.from_time, window.to_time, limit, view_type)

    # Only the page sections reach here: ``section`` is validated against
    # ``DashboardSection`` and the import-time guard gives every other name a handler.
    return await _render_overview_page(
        window.profile_id, window.from_time, window.to_time, limit, section, domain_view, view_type
    )


async def overviewDashboard(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
    section: DashboardSection = "overview",
    view_type: Literal["auto", "bar", "table", "pie"] = "auto",
    domain_view: Literal["table", "bar", "pie"] = "table",
) -> dict[str, Any] | ToolResult:
    """Render interactive visual Prefab UI analytics dashboards and specialized widgets.

    Displays visual cards, charts, progress indicators, and sortable tables for NextDNS analytics.
    Use this tool whenever the user asks for a dashboard, visual overview, or interactive widget
    (e.g., 'show dashboard', 'visualize top domains', 'display device widget', 'show security cards').
    For raw JSON query data without UI rendering, use the respective getAnalytics* API tools instead.

    Sections available via the `section` parameter:
    - "overview" (default): Full overview layout mirroring the NextDNS web UI (KPI cards, queries evolution chart, resolved & blocked domains).
    - "all": Complete dashboard with overview cards plus reasons, devices, root domains, IPs, GAFAM, countries, and security cards.
    - "kpi": Top KPI summary cards (Total Queries, Blocked Queries in red, Queries Blocked rate).
    - "domains": Top resolved domains card with format selection.
    - "blocked_domains": Top blocked domains card with format selection.
    - "reasons": Block reasons breakdown (security/privacy filters and blocklists).
    - "devices": Active client devices and models breakdown.
    - "ips": Client IP addresses and ISP/geo details.
    - "root_domains": Top aggregate root domains.
    - "countries": Traffic breakdown by country destination or origin.
    - "gafam": Traffic dominance share of Google, Apple, Facebook, Amazon, and Microsoft.
    - "security": Encrypted DNS (DoH/DoT) and DNSSEC validation percentage cards.

    Args:
        profile_id: Profile ID (falls back to NEXTDNS_DEFAULT_PROFILE when omitted).
        from_time: Start of time window (e.g. "-30d", "-7d", "-1d", Unix timestamp). Default "-30d".
        to_time: End of time window (default "now").
        Default window: from_time="-30d" to to_time="now" (30 days).
        limit: Maximum number of rows to display in lists and tables (default 10).
        section: Dashboard section or widget to render (default "overview").
        view_type: Visualization format: "auto" (default: table or card default), "bar", "table", or "pie".
        domain_view: Visualization format for domain cards in overview mode: "table" (default), "bar", or "pie".

    What is fetched is bounded by ``limit``; what is rendered is bounded too. A
    table shows at most its 100 busiest rows, paginated 25 to a page, and a bar or
    pie chart draws its 24 busiest entries with the rest rolled up into one
    "Other" entry (25 entries in all), so the chart's totals still account for
    every row it was sent. A time-series chart draws at most 25 series including
    that rollup. A rendered page still over the 262144-byte result ceiling is
    refused with a ``response_too_large`` payload naming its size rather than
    delivered trimmed.
    """
    return await _overview_dashboard_impl(
        profile_id=profile_id,
        from_time=from_time,
        to_time=to_time,
        limit=limit,
        section=section,
        view_type=view_type,
        domain_view=domain_view,
    )
