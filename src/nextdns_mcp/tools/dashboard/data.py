"""Analytics payload fetching, KPI math, and row shaping for the dashboard tools.

This is the "what does upstream actually return" layer: the transport calls, the
bounded fan-out the overview pages need, the rates computed over the returned
rows, and the label/row shapers that turn one API item into one table row. It
knows nothing about Prefab UI - a card builder belongs to ``components``, the
text a model reads back belongs to ``formatters``.

SPDX-License-Identifier: MIT
"""

import asyncio
import time
from typing import Any, NamedTuple

from ...charts import resolve_time_point, series_budget_error
from ...errors import ErrorCode, error_payload
from ...transport import MAX_SERIES_RESPONSE_BYTES, fetch_analytics_payload
from ...utils import (
    _build_query_params,
    _build_series_params,
    normalize_time_value,
    sanitize_label,
)

# Byte ceiling on the bounded (non-``;series``) analytics endpoints this
# dashboard fetches. A ``;series`` answer is capped at MAX_SERIES_RESPONSE_BYTES
# at every door; the eleven non-series endpoints take a ``limit`` (<= 500 rows),
# and the widest honest answer - 500 destination rows carrying domain samples -
# fits well inside this ceiling, so the cap only ever fires on a pathological
# upstream document. overviewDashboard was the analytics door that capped
# nothing: with ``section="all"`` it fans out to 12 endpoints, and an uncapped
# aggregate went straight into a rendered PrefabApp (issue #550, follow-up to
# #424).
MAX_DASHBOARD_RESPONSE_BYTES = 512_000


def _build_analytics_params(
    from_time: str | int | None = "-30d",
    to_time: str | int | None = "now",
    limit: int | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build normalized query parameters for analytics endpoints."""
    return _build_query_params(
        **{
            "from": normalize_time_value(from_time),
            "to": normalize_time_value(to_time),
            "limit": limit,
            **extra,
        }
    )


async def _fetch_analytics_payload(
    profile_id: str,
    endpoint: str,
    params: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Fetch an analytics endpoint payload through the access-controlled transport.

    The fetch and the typed-error classification are ``transport.fetch_analytics_payload``
    - shared with ``plots._fetch_series_payload`` so the error contract cannot be
    copied twice and drift. What stays here is the decision only a dashboard
    endpoint can make: a ``;series`` response is capped at
    ``MAX_SERIES_RESPONSE_BYTES``, and every other analytics endpoint at
    ``MAX_DASHBOARD_RESPONSE_BYTES`` - an uncapped fetch is an unbounded page.
    """
    url = f"/profiles/{profile_id}/analytics/{endpoint}"
    max_bytes = MAX_SERIES_RESPONSE_BYTES if ";series" in endpoint else MAX_DASHBOARD_RESPONSE_BYTES
    return await fetch_analytics_payload(url, params, max_response_bytes=max_bytes)


def _format_share(count: int, total: int) -> str:
    """Format a query count as a percentage share of the total."""
    return f"{(count / total * 100):.1f}%" if total > 0 else "0.0%"


def _item_queries(item: dict[str, Any]) -> int:
    """Extract integer query count from an item dict."""
    try:
        return int(item.get("queries") or 0)
    except (ValueError, TypeError, OverflowError):
        return 0


def _blocked_queries(item: dict[str, Any]) -> int:
    """Extract blocked query count for a status item."""
    return _item_queries(item) if item.get("status") == "blocked" else 0


def _calculate_status_kpis(data: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute summary KPIs from status totals."""
    total = sum(_item_queries(item) for item in data)
    blocked = sum(_blocked_queries(item) for item in data)
    rate = (blocked / total * 100.0) if total > 0 else 0.0
    return {"total": total, "blocked": blocked, "rate": rate}


def _domain_label(item: dict[str, Any], key_name: str = "domain") -> str:
    """Extract domain label."""
    return sanitize_label(item.get(key_name) or item.get("domain") or "-")


def _reason_label(item: dict[str, Any]) -> str:
    """Extract reason or blocklist name."""
    return sanitize_label(item.get("name") or item.get("id") or "Unknown")


def _reason_row(item: dict[str, Any], total: int) -> dict[str, Any]:
    """Format one reasons-table row."""
    q = _item_queries(item)
    return {
        "name": _reason_label(item),
        "queries": q,
        "share": _format_share(q, total),
    }


def _device_name(item: dict[str, Any]) -> str:
    """Extract a human-readable device name."""
    dev_id = sanitize_label(item.get("id") or "-")
    return "Unidentified" if dev_id == "__UNIDENTIFIED__" else sanitize_label(item.get("name") or dev_id)


def _device_label(item: dict[str, Any]) -> str:
    """Extract human-readable device name and model."""
    dev_name = _device_name(item)
    model = sanitize_label(item.get("model") or "")
    if model and model != "-" and model != dev_name:
        return f"{dev_name} ({model})"
    return dev_name


def _device_row(item: dict[str, Any], total: int) -> dict[str, Any]:
    """Format one device row."""
    q = _item_queries(item)
    return {
        "device": _device_label(item),
        "model": sanitize_label(item.get("model") or "-"),
        "queries": q,
        "share": _format_share(q, total),
    }


def _location_string(geo: dict[str, Any]) -> str:
    """Format city and country into a readable location string."""
    city = geo.get("city")
    country = geo.get("country")
    if not city and not country:
        return "-"
    parts = [sanitize_label(value) for value in (city, country) if value]
    return ", ".join(parts)


def _ip_row(item: dict[str, Any]) -> dict[str, Any]:
    """Format one IP address row."""
    geo = item.get("geo") or {}
    network = item.get("network") or {}
    return {
        "ip": sanitize_label(item.get("ip") or "-"),
        "isp": sanitize_label(network.get("isp") or "-"),
        "location": _location_string(geo),
        "queries": _item_queries(item),
    }


def _single_gafam_row(item: dict[str, Any], total: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """Format single company entry for GAFAM donut chart and table."""
    name = sanitize_label(item.get("company") or "others").capitalize()
    q = _item_queries(item)
    return {"label": name, "company": name, "queries": q}, {
        "company": name,
        "queries": q,
        "share": _format_share(q, total),
    }


def _gafam_rows(data: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Format data rows for the GAFAM donut chart and data table."""
    total = sum(_item_queries(item) for item in data)
    pairs = [_single_gafam_row(item, total) for item in data]
    table_rows: list[dict[str, Any]] = [p[1] for p in pairs]
    return [p[0] for p in pairs], table_rows


def _matching_queries(item: dict[str, Any], flag_key: str) -> int:
    """Return queries when flag condition is satisfied."""
    return _item_queries(item) if item.get(flag_key) is True else 0


def _calc_rate(data: list[dict[str, Any]], flag_key: str) -> tuple[int, int, float]:
    """Calculate (verified_count, total_count, percentage) for boolean-shaped analytics."""
    total = sum(_item_queries(item) for item in data)
    matching = sum(_matching_queries(item, flag_key) for item in data)
    pct = (matching / total * 100.0) if total > 0 else 0.0
    return matching, total, pct


def _destination_label(item: dict[str, Any]) -> str:
    """Format country destination name."""
    return sanitize_label(item.get("name") or item.get("code") or "-")


def _accumulate_domains(domains: list[str] | None, budget: int = 60) -> str:
    """Accumulate domain labels until the display budget is reached.

    Returns a comma-separated string of domains whose total length with separators
    does not exceed *budget*. If the first domain alone exceeds the budget, return a
    truncated prefix of that domain. The function stops reading further domains once the
    budget would be exceeded, avoiding a full join of the input list.
    """
    if not domains:
        return ""
    labels: list[str] = []
    first = sanitize_label(domains[0])
    if len(first) > budget:
        return first[:budget]
    labels.append(first)
    used = len(first)
    for domain in domains[1:]:
        domain = sanitize_label(domain)
        need = used + 2 + len(domain)
        if need > budget:
            break
        labels.append(domain)
        used = need
    return ", ".join(labels)


def _destination_row(item: dict[str, Any]) -> dict[str, Any]:
    """Format one country destination row."""
    sample = _accumulate_domains(item.get("domains"), budget=60)
    return {
        "country": _destination_label(item),
        "code": sanitize_label(item.get("code") or "-"),
        "queries": _item_queries(item),
        "sample_domains": sample or "-",
    }


def _payload_data(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Extract list data rows from an API payload dictionary."""
    return (payload or {}).get("data") or []


def _overview_interval(from_time: str | int, to_time: str | int) -> int:
    """Pick interval based on span: 3600 for <= 2 days, 86400 for longer."""
    now = time.time()
    start = resolve_time_point(from_time, now)
    end = resolve_time_point(to_time, now)
    if start is not None and end is not None:
        span = end - start
        if 0 < span <= 172800:
            return 3600
    return 86400


def _validate_card_data(
    payload: dict[str, Any] | None, endpoint: str, pid: str
) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    """Extract list data from payload or return typed no-data error."""
    data = _payload_data(payload)
    if payload is None:
        return None, error_payload(ErrorCode.NO_DATA, f"No {endpoint} data available", profile_id=pid)
    return data, None


async def _fetch_card_data(
    pid: str,
    endpoint: str,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int | None = None,
    **extra: Any,
) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    """Fetch single endpoint data list for an already-resolved profile, with error classification."""
    params = _build_analytics_params(from_time=from_time, to_time=to_time, limit=limit, **extra)
    payload, fetch_err = await _fetch_analytics_payload(pid, endpoint, params)
    if fetch_err:
        return None, fetch_err
    data, no_data_err = _validate_card_data(payload, endpoint, pid)
    return data, no_data_err


OVERVIEW_FANOUT_CONCURRENCY = 4

# Upstream answers that reject the request itself rather than this one endpoint.
# Every queued wave of the fan-out sends the same profile id on the same API key,
# so a wave that has not started yet can only repeat the failure: the 429 spends
# quota that was already refused, the 401/403 re-proves a credential that just
# failed, the 404 names a profile no endpoint of this profile can resolve.
FANOUT_STOP_STATUS_CODES = frozenset({401, 403, 404, 429})


class _FanoutStop:
    """The stop signal one fan-out shares: the first error that condemns every remaining wave.

    A per-request 5xx or an over-large series body says nothing about the other
    endpoints, so only ``FANOUT_STOP_STATUS_CODES`` latch here.
    """

    def __init__(self) -> None:
        self.error: dict[str, Any] | None = None

    def observe(self, err: dict[str, Any] | None) -> None:
        """Latch the first request-fatal error; later failures keep the first one's payload."""
        if self.error is not None or err is None:
            return
        if err.get("status_code") in FANOUT_STOP_STATUS_CODES:
            self.error = err


async def _bounded_fetch_analytics_payload(
    semaphore: asyncio.Semaphore,
    profile_id: str,
    endpoint: str,
    params: dict[str, Any] | None,
    stop: _FanoutStop,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Fetch an analytics payload while bounding concurrent upstream fan-out.

    The check runs once the slot is won, not before it: a queued wave is woken the
    moment an in-flight request releases the semaphore, and that is where it learns
    the fan-out has already failed. The answer handed back is the originating error
    payload, so the unpacking layer reports the same typed failure it would have
    reported had the wave run.
    """
    async with semaphore:
        if stop.error is not None:
            return None, stop.error
        payload, err = await _fetch_analytics_payload(profile_id, endpoint, params)
    stop.observe(err)
    return payload, err


def _first_error(*errs: Any) -> dict[str, Any] | None:
    """Return the first non-None error tuple from a gather result."""
    for err in errs:
        if err:
            return err
    return None


class ExtraDashboardPayloads(NamedTuple):
    """Named data lists for extra dashboard sections."""

    reasons: list[dict[str, Any]]
    devices: list[dict[str, Any]]
    ips: list[dict[str, Any]]
    root_domains: list[dict[str, Any]]
    gafam: list[dict[str, Any]]
    countries: list[dict[str, Any]]
    encryption: list[dict[str, Any]]
    dnssec: list[dict[str, Any]]


def _build_extra_tasks(
    sem: asyncio.Semaphore,
    stop: _FanoutStop,
    pid: str,
    from_time: str | int,
    to_time: str | int,
    limit: int,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Build bounded fetch tasks for extra dashboard sections, keyed by payload field name.

    The keys mirror ``ExtraDashboardPayloads`` fields so results gathered in the
    single overview stage re-bind by name, never by position.
    """
    extra_dom_params = _build_analytics_params(from_time=from_time, to_time=to_time, limit=limit)
    return {
        "reasons": _bounded_fetch_analytics_payload(sem, pid, "reasons", extra_dom_params, stop),
        "devices": _bounded_fetch_analytics_payload(sem, pid, "devices", extra_dom_params, stop),
        "ips": _bounded_fetch_analytics_payload(sem, pid, "ips", extra_dom_params, stop),
        "root_domains": _bounded_fetch_analytics_payload(
            sem, pid, "domains", {**extra_dom_params, "root": "true"}, stop
        ),
        "gafam": _bounded_fetch_analytics_payload(
            sem, pid, "destinations", {**extra_dom_params, "type": "gafam"}, stop
        ),
        "countries": _bounded_fetch_analytics_payload(
            sem, pid, "destinations", {**extra_dom_params, "type": "countries"}, stop
        ),
        "encryption": _bounded_fetch_analytics_payload(sem, pid, "encryption", params, stop),
        "dnssec": _bounded_fetch_analytics_payload(sem, pid, "dnssec", params, stop),
    }


def _extract_extra_payloads(
    results: list[Any],
    extra_names: list[str],
    include_extra: bool,
) -> tuple[ExtraDashboardPayloads | None, dict[str, Any] | None]:
    """Bind the extra-section results gathered in the overview stage to payload fields by name."""
    if not include_extra:
        return None, None
    extra = dict(zip(extra_names, results[4:]))
    err = _first_error(*(v[1] for v in extra.values()))
    if err:
        return None, err
    return ExtraDashboardPayloads(
        reasons=_payload_data(extra["reasons"][0]),
        devices=_payload_data(extra["devices"][0]),
        ips=_payload_data(extra["ips"][0]),
        root_domains=_payload_data(extra["root_domains"][0]),
        gafam=_payload_data(extra["gafam"][0]),
        countries=_payload_data(extra["countries"][0]),
        encryption=_payload_data(extra["encryption"][0]),
        dnssec=_payload_data(extra["dnssec"][0]),
    ), None


def _unpack_overview_results(
    results: list[Any],
    extra_names: list[str],
    include_extra: bool,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
    ExtraDashboardPayloads | None,
    dict[str, Any] | None,
]:
    """Unpack status, series, domain payloads and any extra dashboard rows."""
    (
        (status_payload, status_err),
        (series_payload, series_err),
        (resolved_payload, resolved_err),
        (
            blocked_payload,
            blocked_err,
        ),
    ) = results[:4]
    err = _first_error(status_err, series_err, resolved_err, blocked_err)
    if err:
        return {}, {}, [], [], None, err
    extra_payloads, extra_err = _extract_extra_payloads(results, extra_names, include_extra)
    if extra_err:
        return {}, {}, [], [], None, extra_err
    return (
        (status_payload or {}),
        (series_payload or {}),
        _payload_data(resolved_payload),
        _payload_data(blocked_payload),
        extra_payloads,
        None,
    )


async def _fetch_overview_payloads(
    pid: str,
    from_time: str | int,
    to_time: str | int,
    limit: int,
    include_extra: bool = False,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
    ExtraDashboardPayloads | None,
    dict[str, Any] | None,
]:
    """Fetch all datasets required for main overview cards (and extra sections if include_extra).

    The window arrives already normalized from the dashboard preflight, which is
    the one copy of that rewrite: the interval and the ``;series`` point budget
    below are priced from the same window the preflight read.
    """
    interval = _overview_interval(from_time, to_time)
    budget_err = series_budget_error(from_time, to_time, interval)
    if budget_err:
        return {}, {}, [], [], None, budget_err

    params = _build_analytics_params(from_time=from_time, to_time=to_time)
    dom_params = _build_analytics_params(from_time=from_time, to_time=to_time, limit=limit, status="default")
    blk_params = _build_analytics_params(from_time=from_time, to_time=to_time, limit=limit, status="blocked")
    ser_params = _build_series_params(from_time=from_time, to_time=to_time, interval=interval)

    sem = asyncio.Semaphore(OVERVIEW_FANOUT_CONCURRENCY)
    stop = _FanoutStop()
    extra_fetchers = _build_extra_tasks(sem, stop, pid, from_time, to_time, limit, params) if include_extra else {}
    results = await asyncio.gather(
        _bounded_fetch_analytics_payload(sem, pid, "status", params, stop),
        _bounded_fetch_analytics_payload(sem, pid, "status;series", ser_params, stop),
        _bounded_fetch_analytics_payload(sem, pid, "domains", dom_params, stop),
        _bounded_fetch_analytics_payload(sem, pid, "domains", blk_params, stop),
        *extra_fetchers.values(),
    )
    return _unpack_overview_results(results, list(extra_fetchers), include_extra)


async def _fetch_security_payloads(
    profile_id: str, params: dict[str, Any]
) -> tuple[tuple[dict[str, Any] | None, dict[str, Any] | None], tuple[dict[str, Any] | None, dict[str, Any] | None]]:
    """Fetch the encryption and dnssec payloads for one profile in parallel."""
    return await asyncio.gather(
        _fetch_analytics_payload(profile_id, "encryption", params),
        _fetch_analytics_payload(profile_id, "dnssec", params),
    )
