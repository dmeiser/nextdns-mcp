"""Analytics payload fetching, KPI math, and row shaping for the dashboard tools.

This is the "what does upstream actually return" layer: the transport calls, the
bounded fan-out the overview pages need, the rates computed over the returned
rows, and the label/row shapers that turn one API item into one table row. It
knows nothing about Prefab UI - a card builder belongs to ``components``, the
text a model reads back belongs to ``formatters``.

SPDX-License-Identifier: MIT
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Mapping
from itertools import islice
from typing import Any, NamedTuple

from ...charts import (
    build_chart_data,
    cap_chart_series,
    downsample_chart_series,
    extract_series_fields,
    resolve_time_point,
    series_budget_error,
)
from ...errors import ErrorCode, error_payload
from ...transport import MAX_SERIES_RESPONSE_BYTES, fetch_analytics_payload
from ...utils import (
    build_query_params,
    build_series_params,
    normalize_time_value,
    sanitize_label,
)

logger = logging.getLogger(__name__)

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


def build_analytics_params(
    from_time: str | int | None = "-30d",
    to_time: str | int | None = "now",
    limit: int | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build normalized query parameters for analytics endpoints."""
    return build_query_params(
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


def _share_ratio(count: int, total: int) -> float:
    """A query count as a fraction of the total, in ``0.0``-``1.0``.

    The number stays numeric on purpose: the Share cell is sorted by the client,
    and a pre-formatted ``"12.3%"`` string sorts lexicographically, which puts
    ``10.0%`` before ``2.0%``. Prefab's ``percent`` cell format takes fractional
    input, so ``0.123`` renders as ``12.3%`` and the display does not change
    (issue #800).
    """
    return (count / total) if total > 0 else 0.0


def item_queries(item: dict[str, Any]) -> int:
    """Extract integer query count from an item dict."""
    try:
        return int(item.get("queries") or 0)
    except (ValueError, TypeError, OverflowError):
        return 0


def _blocked_queries(item: dict[str, Any]) -> int:
    """Extract blocked query count for a status item."""
    return item_queries(item) if item.get("status") == "blocked" else 0


def calculate_status_kpis(data: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute summary KPIs from status totals."""
    total = sum(item_queries(item) for item in data)
    blocked = sum(_blocked_queries(item) for item in data)
    rate = (blocked / total * 100.0) if total > 0 else 0.0
    return {"total": total, "blocked": blocked, "rate": rate}


def domain_label(item: dict[str, Any], key_name: str = "domain") -> str:
    """Extract domain label."""
    return sanitize_label(item.get(key_name) or item.get("domain") or "-")


def reason_label(item: dict[str, Any]) -> str:
    """Extract reason or blocklist name."""
    return sanitize_label(item.get("name") or item.get("id") or "Unknown")


def reason_row(item: dict[str, Any], total: int) -> dict[str, Any]:
    """Format one reasons-table row."""
    q = item_queries(item)
    return {
        "name": reason_label(item),
        "queries": q,
        "share": _share_ratio(q, total),
    }


def reason_rows(data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Format the reasons table rows, with the share denominator computed once."""
    total = sum(item_queries(item) for item in data)
    return [reason_row(item, total) for item in data]


def _device_name(item: dict[str, Any]) -> str:
    """Extract a human-readable device name."""
    dev_id = sanitize_label(item.get("id") or "-")
    return "Unidentified" if dev_id == "__UNIDENTIFIED__" else sanitize_label(item.get("name") or dev_id)


def device_label(item: dict[str, Any]) -> str:
    """Extract human-readable device name and model."""
    dev_name = _device_name(item)
    model = sanitize_label(item.get("model") or "")
    if model and model != "-" and model != dev_name:
        return f"{dev_name} ({model})"
    return dev_name


def device_row(item: dict[str, Any], total: int) -> dict[str, Any]:
    """Format one device row."""
    q = item_queries(item)
    return {
        "device": device_label(item),
        "model": sanitize_label(item.get("model") or "-"),
        "queries": q,
        "share": _share_ratio(q, total),
    }


def device_rows(data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Format the devices table rows, with the share denominator computed once."""
    total = sum(item_queries(item) for item in data)
    return [device_row(item, total) for item in data]


def _location_string(geo: dict[str, Any]) -> str:
    """Format city and country into a readable location string."""
    city = geo.get("city")
    country = geo.get("country")
    if not city and not country:
        return "-"
    parts = [sanitize_label(value) for value in (city, country) if value]
    return ", ".join(parts)


def ip_row(item: dict[str, Any]) -> dict[str, Any]:
    """Format one IP address row."""
    geo = item.get("geo") or {}
    network = item.get("network") or {}
    return {
        "ip": sanitize_label(item.get("ip") or "-"),
        "isp": sanitize_label(network.get("isp") or "-"),
        "location": _location_string(geo),
        "queries": item_queries(item),
    }


def _single_gafam_row(item: dict[str, Any], total: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """Format single company entry for GAFAM donut chart and table."""
    name = sanitize_label(item.get("company") or "others").capitalize()
    q = item_queries(item)
    return {"label": name, "company": name, "queries": q}, {
        "company": name,
        "queries": q,
        "share": _share_ratio(q, total),
    }


def gafam_rows(data: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Format data rows for the GAFAM donut chart and data table."""
    total = sum(item_queries(item) for item in data)
    pairs = [_single_gafam_row(item, total) for item in data]
    table_rows: list[dict[str, Any]] = [p[1] for p in pairs]
    return [p[0] for p in pairs], table_rows


def prepare_queries_series(series_payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    """Extract a ``status;series`` payload and shape it for the queries evolution card.

    Returns the ``(chart_data, chart_series_data)`` pair the card renders from,
    or ``None`` when the payload carries nothing plottable - the card is then
    omitted from the page rather than rendered empty. The rendered rows are
    downsampled through the shared chart budget: a multi-year overview answers
    up to 1,999 intervals, and this card is one of many on the page, so
    full-resolution rows here bloat the whole ``PrefabApp`` rather than one
    chart result (issue #550). Payload-shape errors degrade to ``None``; any
    other failure is a programming error and propagates (FUNC-5 / WORK-324).
    """
    try:
        times, series_data = extract_series_fields(series_payload)
        if not times or not series_data:
            return None
        series_data = cap_chart_series(series_data)
        chart_times, chart_series_data = downsample_chart_series(times, series_data)
        chart_data = build_chart_data(chart_times, chart_series_data)
    except (ValueError, TypeError, KeyError, AttributeError):
        logger.warning("Error preparing queries series data")
        return None
    return chart_data, chart_series_data


def _matching_queries(item: dict[str, Any], flag_key: str) -> int:
    """Return queries when flag condition is satisfied."""
    return item_queries(item) if item.get(flag_key) is True else 0


def calc_rate(data: list[dict[str, Any]], flag_key: str) -> tuple[int, int, float]:
    """Calculate (verified_count, total_count, percentage) for boolean-shaped analytics."""
    total = sum(item_queries(item) for item in data)
    matching = sum(_matching_queries(item, flag_key) for item in data)
    pct = (matching / total * 100.0) if total > 0 else 0.0
    return matching, total, pct


def destination_label(item: dict[str, Any]) -> str:
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
    for domain in islice(domains, 1, None):
        domain = sanitize_label(domain)
        need = used + 2 + len(domain)
        if need > budget:
            break
        labels.append(domain)
        used = need
    return ", ".join(labels)


def destination_row(item: dict[str, Any]) -> dict[str, Any]:
    """Format one country destination row."""
    sample = _accumulate_domains(item.get("domains"), budget=60)
    return {
        "country": destination_label(item),
        "code": sanitize_label(item.get("code") or "-"),
        "queries": item_queries(item),
        "sample_domains": sample or "-",
    }


def payload_data(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
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
    data = payload_data(payload)
    if payload is None:
        return None, error_payload(ErrorCode.NO_DATA, f"No {endpoint} data available", profile_id=pid)
    return data, None


async def fetch_card_data(
    pid: str,
    endpoint: str,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int | None = None,
    **extra: Any,
) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    """Fetch single endpoint data list for an already-resolved profile, with error classification."""
    params = build_analytics_params(from_time=from_time, to_time=to_time, limit=limit, **extra)
    payload, fetch_err = await _fetch_analytics_payload(pid, endpoint, params)
    if fetch_err:
        return None, fetch_err
    data, no_data_err = _validate_card_data(payload, endpoint, pid)
    return data, no_data_err


OVERVIEW_FANOUT_CONCURRENCY = 4

# What one overview fan-out fetch answers: the payload or the typed error. Every
# fetch is built under a name, and that name is what the unpacking layer reads,
# so the payload a field gets follows the key it was fetched under and never the
# slot its answer happened to land in.
FetchResult = tuple[dict[str, Any] | None, dict[str, Any] | None]


class _FanoutStop:
    """The stop signal one fan-out shares: the first error condemns every remaining wave.

    The unpacking layer answers the overview with the first error it is handed
    and discards every payload beside it, so once any fetch has failed the
    waves still queued can only spend quota and latency on results that will
    never be read - a 5xx, a timeout or an over-large body included.
    """

    def __init__(self) -> None:
        self.error: dict[str, Any] | None = None

    def observe(self, err: dict[str, Any] | None) -> None:
        """Latch the first error; later failures keep the first one's payload."""
        if self.error is None and err is not None:
            self.error = err


async def _bounded_fetch_analytics_payload(
    semaphore: asyncio.Semaphore,
    profile_id: str,
    endpoint: str,
    params: dict[str, Any] | None,
    stop: _FanoutStop,
) -> FetchResult:
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


class PreparedExtraSections(NamedTuple):
    """Extra dashboard sections with the aggregate math already done.

    ``components.append_extra_cards`` only lays these out: the GAFAM row pairs
    and the security rates are computed here, in the data layer, so the UI
    module consumes prepared inputs (WORK-339).
    """

    reasons: list[dict[str, Any]]
    devices: list[dict[str, Any]]
    ips: list[dict[str, Any]]
    root_domains: list[dict[str, Any]]
    gafam_chart: list[dict[str, Any]]
    gafam_table: list[dict[str, Any]]
    countries: list[dict[str, Any]]
    encryption_rate: tuple[int, int, float]
    dnssec_rate: tuple[int, int, float]


def prepare_extra_sections(payloads: ExtraDashboardPayloads) -> PreparedExtraSections:
    """Compute the aggregates the extra dashboard cards render from."""
    gafam_chart, gafam_table = gafam_rows(payloads.gafam)
    return PreparedExtraSections(
        reasons=payloads.reasons,
        devices=payloads.devices,
        ips=payloads.ips,
        root_domains=payloads.root_domains,
        gafam_chart=gafam_chart,
        gafam_table=gafam_table,
        countries=payloads.countries,
        encryption_rate=calc_rate(payloads.encryption, "encrypted"),
        dnssec_rate=calc_rate(payloads.dnssec, "validated"),
    )


def _build_extra_tasks(
    sem: asyncio.Semaphore,
    stop: _FanoutStop,
    pid: str,
    from_time: str | int,
    to_time: str | int,
    limit: int,
    params: dict[str, Any],
) -> dict[str, Awaitable[FetchResult]]:
    """Build bounded fetch tasks for extra dashboard sections, keyed by payload field name.

    The keys mirror ``ExtraDashboardPayloads`` fields so results gathered in the
    single overview stage re-bind by name, never by position.
    """
    extra_dom_params = build_analytics_params(from_time=from_time, to_time=to_time, limit=limit)
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


async def _tagged_result(name: str, fetch: Awaitable[FetchResult]) -> tuple[str, FetchResult]:
    """Carry a fetch's key through ``asyncio.gather`` so its answer binds by name.

    ``gather`` hands results back in argument order, which is the one positional
    coupling the fan-out must not depend on: the payload a field gets has to
    follow the key the fetch was built under, not the slot its answer happened
    to land in.
    """
    return name, await fetch


def _extract_extra_payloads(
    results: Mapping[str, FetchResult],
    include_extra: bool,
) -> tuple[ExtraDashboardPayloads | None, dict[str, Any] | None]:
    """Bind the extra-section results gathered in the overview stage to payload fields by name."""
    if not include_extra:
        return None, None
    err = _first_error(*(results[name][1] for name in ExtraDashboardPayloads._fields))
    if err:
        return None, err
    return (
        ExtraDashboardPayloads(
            reasons=payload_data(results["reasons"][0]),
            devices=payload_data(results["devices"][0]),
            ips=payload_data(results["ips"][0]),
            root_domains=payload_data(results["root_domains"][0]),
            gafam=payload_data(results["gafam"][0]),
            countries=payload_data(results["countries"][0]),
            encryption=payload_data(results["encryption"][0]),
            dnssec=payload_data(results["dnssec"][0]),
        ),
        None,
    )


def _unpack_overview_results(
    results: Mapping[str, FetchResult],
    include_extra: bool,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
    ExtraDashboardPayloads | None,
    dict[str, Any] | None,
]:
    """Unpack status, series, domain payloads and any extra dashboard rows.

    Every payload is read by the key it was fetched under; nothing here depends
    on how many fetches ran or in what order they were issued.
    """
    status_payload, status_err = results["status"]
    series_payload, series_err = results["series"]
    resolved_payload, resolved_err = results["resolved_domains"]
    blocked_payload, blocked_err = results["blocked_domains"]
    err = _first_error(status_err, series_err, resolved_err, blocked_err)
    if err:
        return {}, {}, [], [], None, err
    extra_payloads, extra_err = _extract_extra_payloads(results, include_extra)
    if extra_err:
        return {}, {}, [], [], None, extra_err
    return (
        (status_payload or {}),
        (series_payload or {}),
        payload_data(resolved_payload),
        payload_data(blocked_payload),
        extra_payloads,
        None,
    )


async def fetch_overview_payloads(
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

    params = build_analytics_params(from_time=from_time, to_time=to_time)
    dom_params = build_analytics_params(from_time=from_time, to_time=to_time, limit=limit, status="default")
    blk_params = build_analytics_params(from_time=from_time, to_time=to_time, limit=limit, status="blocked")
    ser_params = build_series_params(from_time=from_time, to_time=to_time, interval=interval)

    sem = asyncio.Semaphore(OVERVIEW_FANOUT_CONCURRENCY)
    stop = _FanoutStop()
    fetchers: dict[str, Awaitable[FetchResult]] = {
        "status": _bounded_fetch_analytics_payload(sem, pid, "status", params, stop),
        "series": _bounded_fetch_analytics_payload(sem, pid, "status;series", ser_params, stop),
        "resolved_domains": _bounded_fetch_analytics_payload(sem, pid, "domains", dom_params, stop),
        "blocked_domains": _bounded_fetch_analytics_payload(sem, pid, "domains", blk_params, stop),
    }
    if include_extra:
        fetchers.update(_build_extra_tasks(sem, stop, pid, from_time, to_time, limit, params))
    results = dict(await asyncio.gather(*(_tagged_result(name, fetch) for name, fetch in fetchers.items())))
    return _unpack_overview_results(results, include_extra)


async def fetch_security_payloads(
    profile_id: str, params: dict[str, Any]
) -> tuple[tuple[dict[str, Any] | None, dict[str, Any] | None], tuple[dict[str, Any] | None, dict[str, Any] | None]]:
    """Fetch the encryption and dnssec payloads for one profile in parallel."""
    return await asyncio.gather(
        _fetch_analytics_payload(profile_id, "encryption", params),
        _fetch_analytics_payload(profile_id, "dnssec", params),
    )
