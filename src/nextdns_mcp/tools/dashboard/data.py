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
from typing import Any

from ...charts import resolve_time_point, series_budget_error
from ...coercion import OptionalProfileId
from ...errors import ErrorCode, error_payload
from ...transport import MAX_SERIES_RESPONSE_BYTES, _api_request_payload
from ...utils import normalize_time_value, resolve_profile_id


def _time_params(from_time: str | int | None, to_time: str | int | None) -> dict[str, Any]:
    """Extract normalized time window parameters."""
    p: dict[str, Any] = {}
    if from_time is not None:
        p["from"] = normalize_time_value(from_time)
    if to_time is not None:
        p["to"] = normalize_time_value(to_time)
    return p


def _build_analytics_params(
    from_time: str | int | None = "-30d",
    to_time: str | int | None = "now",
    limit: int | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build normalized query parameters for analytics endpoints."""
    params = _time_params(from_time, to_time)
    if limit is not None:
        params["limit"] = limit
    params.update({k: v for k, v in extra.items() if v is not None})
    return params


async def _fetch_analytics_payload(
    profile_id: str,
    endpoint: str,
    params: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Fetch an analytics endpoint payload through the access-controlled transport.

    Only a ``;series`` endpoint rides the capped streaming path: the ceiling
    exists to bound a large series body, while the aggregate endpoints answer
    small JSON that takes the normal request path (WORK-122).
    """
    url = f"/profiles/{profile_id}/analytics/{endpoint}"
    max_bytes = MAX_SERIES_RESPONSE_BYTES if ";series" in endpoint else None
    payload = await _api_request_payload("GET", url, params=params, max_response_bytes=max_bytes)
    if _is_error_payload(payload):
        return None, payload
    return payload, None


def _is_error_payload(payload: Any) -> bool:
    return isinstance(payload, dict) and "code" in payload and "error" in payload and "data" not in payload


def _format_share(count: int, total: int) -> str:
    """Format a query count as a percentage share of the total."""
    return f"{(count / total * 100):.1f}%" if total > 0 else "0.0%"


def _item_queries(item: dict[str, Any]) -> int:
    """Extract integer query count from an item dict."""
    return int(item.get("queries") or 0)


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
    return str(item.get(key_name) or item.get("domain") or "-")


def _reason_label(item: dict[str, Any]) -> str:
    """Extract reason or blocklist name."""
    return str(item.get("name") or item.get("id") or "Unknown")


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
    dev_id = str(item.get("id") or "-")
    return "Unidentified" if dev_id == "__UNIDENTIFIED__" else str(item.get("name") or dev_id)


def _device_label(item: dict[str, Any]) -> str:
    """Extract human-readable device name and model."""
    dev_name = _device_name(item)
    model = str(item.get("model") or "")
    if model and model != "-" and model != dev_name:
        return f"{dev_name} ({model})"
    return dev_name


def _device_row(item: dict[str, Any], total: int) -> dict[str, Any]:
    """Format one device row."""
    q = _item_queries(item)
    return {
        "device": _device_label(item),
        "model": str(item.get("model") or "-"),
        "queries": q,
        "share": _format_share(q, total),
    }


def _location_string(geo: dict[str, Any]) -> str:
    """Format city and country into a readable location string."""
    city = geo.get("city")
    country = geo.get("country")
    if city and country:
        return f"{city}, {country}"
    return str(city or country or "-")


def _ip_row(item: dict[str, Any]) -> dict[str, Any]:
    """Format one IP address row."""
    geo = item.get("geo") or {}
    network = item.get("network") or {}
    return {
        "ip": str(item.get("ip") or "-"),
        "isp": str(network.get("isp") or "-"),
        "location": _location_string(geo),
        "queries": _item_queries(item),
    }


def _single_gafam_row(item: dict[str, Any], total: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """Format single company entry for GAFAM donut chart and table."""
    name = str(item.get("company") or "others").capitalize()
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
    return str(item.get("name") or item.get("code") or "-")


def _destination_row(item: dict[str, Any]) -> dict[str, Any]:
    """Format one country destination row."""
    sample = ", ".join(item.get("domains") or [])[:60]
    return {
        "country": _destination_label(item),
        "code": str(item.get("code") or "-"),
        "queries": _item_queries(item),
        "sample_domains": sample or "-",
    }


async def _resolve_profile(profile_id: OptionalProfileId) -> tuple[str | None, dict[str, Any] | None]:
    """Resolve target profile ID or return typed error payload."""
    target_profile, val_error = resolve_profile_id(profile_id, allow_default=True)
    if val_error:
        return None, val_error
    if target_profile is None:
        return None, error_payload(ErrorCode.INTERNAL_ERROR, "Profile resolution failed")
    return target_profile, None


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
    profile_id: OptionalProfileId,
    endpoint: str,
    from_time: str | int = "-30d",
    to_time: str | int = "now",
    limit: int | None = None,
    **extra: Any,
) -> tuple[str | None, list[dict[str, Any]] | None, dict[str, Any] | None]:
    """Fetch single endpoint data list with profile validation and error classification."""
    pid, err = await _resolve_profile(profile_id)
    if err or pid is None:
        return None, None, err
    params = _build_analytics_params(from_time=from_time, to_time=to_time, limit=limit, **extra)
    payload, fetch_err = await _fetch_analytics_payload(pid, endpoint, params)
    if fetch_err:
        return pid, None, fetch_err
    data, no_data_err = _validate_card_data(payload, endpoint, pid)
    return pid, data, no_data_err


OVERVIEW_FANOUT_CONCURRENCY = 2


async def _bounded_fetch_analytics_payload(
    semaphore: asyncio.Semaphore,
    profile_id: str,
    endpoint: str,
    params: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Fetch an analytics payload while bounding concurrent upstream fan-out."""
    async with semaphore:
        return await _fetch_analytics_payload(profile_id, endpoint, params)


async def _fetch_overview_payloads(
    pid: str,
    from_time: str | int,
    to_time: str | int,
    limit: int,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any] | None]:
    """Fetch all datasets required for the main overview cards."""
    from_time = normalize_time_value(from_time)
    to_time = normalize_time_value(to_time)
    interval = _overview_interval(from_time, to_time)
    budget_err = series_budget_error(from_time, to_time, interval)
    if budget_err:
        return {}, {}, [], [], budget_err

    params = _build_analytics_params(from_time=from_time, to_time=to_time)
    dom_params = _build_analytics_params(from_time=from_time, to_time=to_time, limit=limit, status="default")
    blk_params = _build_analytics_params(from_time=from_time, to_time=to_time, limit=limit, status="blocked")
    ser_params = {"from": from_time, "to": to_time, "interval": interval}

    sem = asyncio.Semaphore(OVERVIEW_FANOUT_CONCURRENCY)
    s_task = _bounded_fetch_analytics_payload(sem, pid, "status", params)
    ser_task = _bounded_fetch_analytics_payload(sem, pid, "status;series", ser_params)
    r_task = _bounded_fetch_analytics_payload(sem, pid, "domains", dom_params)
    b_task = _bounded_fetch_analytics_payload(sem, pid, "domains", blk_params)

    (s_p, s_err), (ser_p, _), (r_p, _), (b_p, _) = await asyncio.gather(s_task, ser_task, r_task, b_task)
    if s_err:
        return {}, {}, [], [], s_err
    return (s_p or {}), (ser_p or {}), _payload_data(r_p), _payload_data(b_p), None


async def _fetch_security_payloads(
    profile_id: str, params: dict[str, Any]
) -> tuple[tuple[dict[str, Any] | None, dict[str, Any] | None], tuple[dict[str, Any] | None, dict[str, Any] | None]]:
    """Fetch the encryption and dnssec payloads for one profile in parallel."""
    return await asyncio.gather(
        _fetch_analytics_payload(profile_id, "encryption", params),
        _fetch_analytics_payload(profile_id, "dnssec", params),
    )


async def _fetch_extra_payloads(
    profile_id: str, dom_params: dict[str, Any], params: dict[str, Any]
) -> list[list[dict[str, Any]]]:
    """Fetch data lists for extra dashboard sections."""
    sem = asyncio.Semaphore(OVERVIEW_FANOUT_CONCURRENCY)
    tasks = [
        _bounded_fetch_analytics_payload(sem, profile_id, "reasons", dom_params),
        _bounded_fetch_analytics_payload(sem, profile_id, "devices", dom_params),
        _bounded_fetch_analytics_payload(sem, profile_id, "ips", dom_params),
        _bounded_fetch_analytics_payload(sem, profile_id, "domains", {**dom_params, "root": "true"}),
        _bounded_fetch_analytics_payload(sem, profile_id, "destinations", {**params, "type": "gafam"}),
        _bounded_fetch_analytics_payload(sem, profile_id, "encryption", params),
        _bounded_fetch_analytics_payload(sem, profile_id, "dnssec", params),
    ]
    res = await asyncio.gather(*tasks)
    return [_payload_data(item[0]) for item in res]
