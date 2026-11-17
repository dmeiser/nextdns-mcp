"""Widget implementations, section dispatch, and the published dashboard tool.

This is the layer the MCP surface belongs to: one thin published wrapper
(``overviewDashboard``) over a private ``_impl``, one implementation per section, and
the table that maps a ``section`` argument to an implementation. Each implementation
reads through ``data``, renders through ``components`` and words its result through
``formatters`` - it holds no transport call and no Prefab construction of its own.

SPDX-License-Identifier: MIT
"""

from collections.abc import Awaitable
from typing import Any, Literal, Protocol, get_args

from fastmcp.tools.base import ToolResult
from prefab_ui.components import Column

from ...charts import series_budget_error
from ...coercion import OptionalProfileId
from ...errors import ErrorCode, error_payload
from ...utils import normalize_time_value
from .components import (
    _append_extra_cards,
    _assemble_overview_layout,
    _build_destinations_card,
    _build_devices_card,
    _build_domains_card,
    _build_gafam_card,
    _build_ips_card,
    _build_kpi_app,
    _build_page_app,
    _build_reasons_card,
    _build_security_card,
    _wrap_card_result,
)
from .data import (
    _build_analytics_params,
    _calc_rate,
    _calculate_status_kpis,
    _destination_label,
    _device_label,
    _domain_label,
    _fetch_card_data,
    _fetch_extra_payloads,
    _fetch_overview_payloads,
    _fetch_security_payloads,
    _gafam_rows,
    _item_queries,
    _overview_interval,
    _payload_data,
    _reason_label,
    _resolve_profile,
)
from .formatters import (
    _format_gafam_summary,
    _format_items_summary,
    _ip_summary_items,
    _overview_top_summary,
)


async def _gafam_analytics_impl(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
    view_type: Literal["auto", "pie", "bar", "table"] = "auto",
) -> dict[str, Any] | ToolResult:
    """Render the GAFAM dominance donut chart and table."""
    data, err = await _fetch_card_data(
        profile_id, "destinations", from_time=from_time, to_time=to_time, limit=limit, type="gafam"
    )
    if err:
        return err
    if data is None:
        return error_payload(ErrorCode.INTERNAL_ERROR, "Card data fetch returned no data or error")
    chart_data, _ = _gafam_rows(data)
    total = sum(r["queries"] for r in chart_data)
    summary = _format_gafam_summary(chart_data, total)
    return _wrap_card_result(_build_gafam_card(data, view_type=view_type), summary)


async def _security_analytics_impl(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
) -> dict[str, Any] | ToolResult:
    """Render the security analytics cards (Encrypted DNS and DNSSEC rates)."""
    pid, err = await _resolve_profile(profile_id)
    if err is not None:
        return err
    if pid is None:
        return error_payload(ErrorCode.INTERNAL_ERROR, "Profile resolution returned no id")
    params = _build_analytics_params(from_time=from_time, to_time=to_time)
    (encryption_payload, enc_err), (dnssec_payload, sec_err) = await _fetch_security_payloads(pid, params)
    first_err = enc_err or sec_err
    if first_err:
        return first_err

    encryption_data = _payload_data(encryption_payload)
    dnssec_data = _payload_data(dnssec_payload)
    enc_val, enc_total, enc_pct = _calc_rate(encryption_data, "encrypted")
    dnssec_val, dnssec_total, dnssec_pct = _calc_rate(dnssec_data, "validated")

    card = _build_security_card(encryption_data, dnssec_data)
    summary = (
        f"[Rendered Security Analytics: Encrypted DNS {enc_pct:.2f}% ({enc_val:,}/{enc_total:,}), "
        f"DNSSEC {dnssec_pct:.2f}% ({dnssec_val:,}/{dnssec_total:,})]"
    )
    return _wrap_card_result(card, summary)


async def _reasons_analytics_impl(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
    view_type: Literal["auto", "bar", "table", "pie"] = "auto",
) -> dict[str, Any] | ToolResult:
    """Render the reasons for blocking card."""
    data, err = await _fetch_card_data(profile_id, "reasons", from_time=from_time, to_time=to_time, limit=limit)
    if err:
        return err
    if data is None:
        return error_payload(ErrorCode.INTERNAL_ERROR, "Card data fetch returned no data or error")
    items = [(_reason_label(item), _item_queries(item)) for item in data]
    total = sum(q for _, q in items)
    summary = _format_items_summary("Reasons for Blocking", "blocklists", items, total)
    return _wrap_card_result(_build_reasons_card(data, view_type=view_type), summary)


async def _device_analytics_impl(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
    view_type: Literal["auto", "bar", "table", "pie"] = "auto",
) -> dict[str, Any] | ToolResult:
    """Render the device analytics card."""
    data, err = await _fetch_card_data(profile_id, "devices", from_time=from_time, to_time=to_time, limit=limit)
    if err:
        return err
    if data is None:
        return error_payload(ErrorCode.INTERNAL_ERROR, "Card data fetch returned no data or error")
    items = [(_device_label(item), _item_queries(item)) for item in data]
    total = sum(q for _, q in items)
    summary = _format_items_summary("Device Analytics", "devices", items, total)
    return _wrap_card_result(_build_devices_card(data, view_type=view_type), summary)


async def _ip_analytics_impl(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
) -> dict[str, Any] | ToolResult:
    """Render the IP analytics card."""
    data, err = await _fetch_card_data(profile_id, "ips", from_time=from_time, to_time=to_time, limit=limit)
    if err:
        return err
    if data is None:
        return error_payload(ErrorCode.INTERNAL_ERROR, "Card data fetch returned no data or error")
    items = _ip_summary_items(data)
    total = sum(q for _, q in items)
    summary = _format_items_summary("IP Analytics", "IP addresses", items, total)
    return _wrap_card_result(_build_ips_card(data), summary)


async def _domain_card_analytics_impl(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
    view_type: Literal["auto", "table", "bar", "pie"] = "auto",
    *,
    fetch_extra: dict[str, Any],
    title: str,
    description: str,
    summary_title: str,
    summary_noun: str = "domains",
    color: str = "#3b82f6",
    key_name: str = "domain",
) -> dict[str, Any] | ToolResult:
    """Render a domain analytics card (root/resolved/blocked)."""
    data, err = await _fetch_card_data(
        profile_id, "domains", from_time=from_time, to_time=to_time, limit=limit, **fetch_extra
    )
    if err:
        return err
    if data is None:
        return error_payload(ErrorCode.INTERNAL_ERROR, "Card data fetch returned no data or error")
    items = [(_domain_label(item, key_name), _item_queries(item)) for item in data]
    total = sum(q for _, q in items)
    summary = _format_items_summary(summary_title, summary_noun, items, total)
    card = _build_domains_card(
        title,
        description,
        data,
        key_name=key_name,
        view_type=view_type,
        color=color,
    )
    return _wrap_card_result(card, summary)


async def _country_analytics_impl(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
    view_type: Literal["auto", "table", "bar", "pie"] = "auto",
) -> dict[str, Any] | ToolResult:
    """Render the destination countries analytics card."""
    data, err = await _fetch_card_data(
        profile_id, "destinations", from_time=from_time, to_time=to_time, limit=limit, type="countries"
    )
    if err:
        return err
    if data is None:
        return error_payload(ErrorCode.INTERNAL_ERROR, "Card data fetch returned no data or error")
    items = [(_destination_label(item), _item_queries(item)) for item in data]
    total = sum(q for _, q in items)
    summary = _format_items_summary("Traffic Destinations", "countries", items, total)
    return _wrap_card_result(_build_destinations_card(data, view_type=view_type), summary)


async def _kpi_analytics_impl(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
) -> dict[str, Any] | ToolResult:
    """Render top KPI metric cards."""
    data, err = await _fetch_card_data(profile_id, "status", from_time=from_time, to_time=to_time)
    if err:
        return err
    if data is None:
        return error_payload(ErrorCode.INTERNAL_ERROR, "Card data fetch returned no data or error")
    kpis = _calculate_status_kpis(data)
    summary = (
        f"[Rendered NextDNS KPI Metrics: {kpis['total']:,} queries, {kpis['blocked']:,} blocked ({kpis['rate']:.2f}%)]"
    )
    return ToolResult(content=summary, structured_content=_build_kpi_app(kpis))


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
        profile_id: OptionalProfileId = None,
        from_time: str | int = "-30d",
        to_time: str | int = "now",
        limit: int = 10,
        view_type: Literal["auto", "bar", "table", "pie"] = "auto",
    ) -> Awaitable[dict[str, Any] | ToolResult]: ...


WidgetHandler = _WidgetHandler


async def _dispatch_kpi(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
    view_type: Literal["auto", "bar", "table", "pie"] = "auto",
) -> dict[str, Any] | ToolResult:
    return await _kpi_analytics_impl(profile_id, from_time=from_time, to_time=to_time)


async def _dispatch_domains(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
    view_type: Literal["auto", "bar", "table", "pie"] = "auto",
) -> dict[str, Any] | ToolResult:
    return await _domain_card_analytics_impl(
        profile_id,
        from_time=from_time,
        to_time=to_time,
        limit=limit,
        view_type=view_type,
        fetch_extra={"status": "default"},
        title="Resolved Domains",
        description="Domains that resolved without being blocked.",
        summary_title="Resolved Domains",
        color="#3b82f6",
    )


async def _dispatch_blocked_domains(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
    view_type: Literal["auto", "bar", "table", "pie"] = "auto",
) -> dict[str, Any] | ToolResult:
    return await _domain_card_analytics_impl(
        profile_id,
        from_time=from_time,
        to_time=to_time,
        limit=limit,
        view_type=view_type,
        fetch_extra={"status": "blocked"},
        title="Blocked Domains",
        description="Domains blocked by security, privacy, or parental controls.",
        summary_title="Blocked Domains",
        color="#ef4444",
    )


async def _dispatch_reasons(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
    view_type: Literal["auto", "bar", "table", "pie"] = "auto",
) -> dict[str, Any] | ToolResult:
    return await _reasons_analytics_impl(
        profile_id, from_time=from_time, to_time=to_time, limit=limit, view_type=view_type
    )


async def _dispatch_devices(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
    view_type: Literal["auto", "bar", "table", "pie"] = "auto",
) -> dict[str, Any] | ToolResult:
    return await _device_analytics_impl(
        profile_id, from_time=from_time, to_time=to_time, limit=limit, view_type=view_type
    )


async def _dispatch_ips(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
    view_type: Literal["auto", "bar", "table", "pie"] = "auto",
) -> dict[str, Any] | ToolResult:
    return await _ip_analytics_impl(profile_id, from_time=from_time, to_time=to_time, limit=limit)


async def _dispatch_root_domains(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
    view_type: Literal["auto", "bar", "table", "pie"] = "auto",
) -> dict[str, Any] | ToolResult:
    return await _domain_card_analytics_impl(
        profile_id,
        from_time=from_time,
        to_time=to_time,
        limit=limit,
        view_type=view_type,
        fetch_extra={"root": "true"},
        title="Root Domains",
        description="Aggregate queries for root domains and subdomains.",
        summary_title="Root Domains",
        key_name="domain",
    )


async def _dispatch_countries(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
    view_type: Literal["auto", "bar", "table", "pie"] = "auto",
) -> dict[str, Any] | ToolResult:
    return await _country_analytics_impl(
        profile_id, from_time=from_time, to_time=to_time, limit=limit, view_type=view_type
    )


async def _dispatch_gafam(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
    view_type: Literal["auto", "bar", "table", "pie"] = "auto",
) -> dict[str, Any] | ToolResult:
    return await _gafam_analytics_impl(
        profile_id, from_time=from_time, to_time=to_time, limit=limit, view_type=view_type
    )


async def _dispatch_security(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
    view_type: Literal["auto", "bar", "table", "pie"] = "auto",
) -> dict[str, Any] | ToolResult:
    return await _security_analytics_impl(profile_id, from_time=from_time, to_time=to_time)


_WIDGET_DISPATCH: dict[str, _WidgetHandler] = {
    "kpi": _dispatch_kpi,
    "domains": _dispatch_domains,
    "blocked_domains": _dispatch_blocked_domains,
    "reasons": _dispatch_reasons,
    "devices": _dispatch_devices,
    "ips": _dispatch_ips,
    "root_domains": _dispatch_root_domains,
    "countries": _dispatch_countries,
    "gafam": _dispatch_gafam,
    "security": _dispatch_security,
}


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


async def _append_extra_dashboard_sections(
    profile_id: str,
    view: Column,
    from_time: str | int,
    to_time: str | int,
    limit: int,
    domain_view: str = "table",
    view_type: str = "auto",
) -> None:
    """Fetch and append reasons, devices, IPs, root domains, GAFAM, destinations and security cards."""
    dom_params = _build_analytics_params(from_time=from_time, to_time=to_time, limit=limit)
    params = _build_analytics_params(from_time=from_time, to_time=to_time)

    payloads = await _fetch_extra_payloads(profile_id, dom_params, params)
    _append_extra_cards(view, payloads, domain_view=domain_view, view_type=view_type)


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
    s_p, ser_p, r_data, b_data, extra_payloads, fetch_err = await _fetch_overview_payloads(
        pid, from_time, to_time, limit, include_extra=include_extra
    )
    if fetch_err:
        return fetch_err

    eff_domain_view = _resolve_domain_view(domain_view, view_type)
    kpis = _calculate_status_kpis(_payload_data(s_p))
    view = _assemble_overview_layout(kpis, ser_p, r_data, b_data, domain_view=eff_domain_view)
    if include_extra and extra_payloads is not None:
        _append_extra_cards(view, extra_payloads, domain_view=eff_domain_view, view_type=view_type)

    top_summary = _overview_top_summary(r_data, b_data)
    summary = (
        f"[Rendered NextDNS Dashboard: {kpis['total']:,} queries, {kpis['blocked']:,} blocked ({kpis['rate']:.2f}%). "
        f"{top_summary}]"
    )
    return ToolResult(content=summary, structured_content=_build_page_app(view))


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


DASHBOARD_LIMIT_MIN = 1
DASHBOARD_LIMIT_MAX = 500


def _validate_dashboard_limit(limit: int) -> dict[str, Any] | None:
    """Reject out-of-range dashboard limits before any upstream request."""
    if isinstance(limit, bool) or limit < DASHBOARD_LIMIT_MIN or limit > DASHBOARD_LIMIT_MAX:
        return error_payload(
            ErrorCode.INVALID_ARGUMENT,
            f"limit must be between {DASHBOARD_LIMIT_MIN} and {DASHBOARD_LIMIT_MAX}, got {limit}",
            min_limit=DASHBOARD_LIMIT_MIN,
            max_limit=DASHBOARD_LIMIT_MAX,
            limit=limit,
        )
    return None


async def _overview_dashboard_impl(
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
    - "all": Complete dashboard with overview cards plus reasons, devices, root domains, IPs, GAFAM, and security cards.
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
    """
    if section in _WIDGET_DISPATCH:
        norm_from = normalize_time_value(from_time)
        norm_to = normalize_time_value(to_time)
        budget_err = series_budget_error(norm_from, norm_to, _overview_interval(norm_from, norm_to))
        if budget_err is not None:
            return budget_err
        # Single-widget sections are not domain-card groups: they keep view_type so
        # "auto" reaches each card's own default (#547).
        return await _WIDGET_DISPATCH[section](profile_id, norm_from, norm_to, limit, view_type)

    pid, err = await _resolve_profile(profile_id)
    if err is not None:
        return err
    if pid is None:
        return error_payload(ErrorCode.INTERNAL_ERROR, "Profile resolution returned no id")

    return await _render_overview_page(pid, from_time, to_time, limit, section, domain_view, view_type)


async def overviewDashboard(
    profile_id: OptionalProfileId = None,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int = 10,
    section: DashboardSection = "overview",
    view_type: Literal["auto", "bar", "table", "pie"] = "auto",
    domain_view: Literal["table", "bar", "pie"] = "table",
) -> dict[str, Any] | ToolResult:
    """Render interactive visual Prefab UI analytics dashboards and specialized widgets."""
    arg_err = _validate_overview_dashboard_args(section, view_type, domain_view)
    if arg_err is not None:
        return arg_err
    limit_err = _validate_dashboard_limit(limit)
    if limit_err is not None:
        return limit_err
    return await _overview_dashboard_impl(
        profile_id=profile_id,
        from_time=from_time,
        to_time=to_time,
        limit=limit,
        section=section,
        view_type=view_type,
        domain_view=domain_view,
    )


overviewDashboard.__doc__ = _overview_dashboard_impl.__doc__
