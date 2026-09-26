"""Unit tests for the /health HTTP endpoint mandated by AGENT.md.

AGENT.md requires a minimal health endpoint at ``/health`` that returns ``200 OK``
with the JSON body ``{"status": "ok"}``. The route is registered on the FastMCP
server via ``custom_route`` and served by the streamable-HTTP app; it is a
constant response that never touches the NextDNS API.
"""

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse

from nextdns_mcp.openapi import _register_health_endpoint, create_mcp_server
from nextdns_mcp.server import get_mcp_server


def _health_response_body(response: httpx.Response) -> dict:
    """Parse the JSON body of a response."""
    return response.json()


def test_create_mcp_server_registers_health_route(mock_api_key, monkeypatch):
    """create_mcp_server() attaches a GET /health route to the FastMCP server."""
    monkeypatch.setenv("NEXTDNS_API_KEY", mock_api_key)

    server = create_mcp_server()

    health_routes = [route for route in server._additional_http_routes if getattr(route, "path", None) == "/health"]
    assert health_routes, "Expected a /health route to be registered"
    route = health_routes[0]
    assert "GET" in (route.methods or set())


async def test_health_route_is_a_get_endpoint_that_returns_ok():
    """The /health handler is a GET endpoint returning 200 with {"status": "ok"}."""
    server = create_mcp_server()
    route = next(r for r in server._additional_http_routes if r.path == "/health")
    handler = route.endpoint

    request = Request(scope={"type": "http", "method": "GET", "path": "/health"})
    response = await handler(request)
    assert isinstance(response, JSONResponse)
    body = _health_response_body(httpx.Response(200, content=response.body))
    assert body == {"status": "ok"}


def test_register_health_endpoint_adds_route_on_each_invocation():
    """_register_health_endpoint adds one /health route per invocation: no de-duplication.

    Each create_mcp_server() call registers one route; calling
    _register_health_endpoint again on the same server adds a second route.
    """
    server = create_mcp_server()
    count_before = len(server._additional_http_routes)

    _register_health_endpoint(server)

    assert len(server._additional_http_routes) == count_before + 1
    health_routes = [r for r in server._additional_http_routes if r.path == "/health"]
    assert len(health_routes) == 2


def test_production_server_exposes_health_route():
    """The production server instance built by server.py carries the /health route."""
    server = get_mcp_server()
    health_routes = [r for r in server._additional_http_routes if r.path == "/health"]
    assert health_routes, "Production server should expose a /health route"


async def test_health_served_by_http_app():
    """GET /health on the streamable-HTTP app returns 200 with {"status": "ok"}."""
    server = get_mcp_server()
    app = server.http_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["content-type"] == "application/json"
