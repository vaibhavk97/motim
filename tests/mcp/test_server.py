"""End-to-end tests for the MOTIM MCP server.

These use FastMCP's in-memory ``call_tool`` (no subprocess) and a seeded
SQLite DB. Each ``call_tool`` returns a ``(content_blocks, structured)``
tuple; we assert on ``structured`` since it's the typed payload the
agent actually consumes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from motim.mcp.server import (
    MUTATION_TOOLS,
    READ_ONLY_TOOLS,
    TOOL_NAMES,
    build_server,
)


async def _call(server, name: str, **kwargs):
    """Invoke an MCP tool and return its structured result."""
    _, structured = await server.call_tool(name, kwargs)
    return structured


async def test_list_tools_returns_full_set(server):
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert names == set(TOOL_NAMES)
    assert len(tools) == len(TOOL_NAMES)


async def test_read_tools_marked_read_only(server):
    tools = {t.name: t for t in await server.list_tools()}
    for name in READ_ONLY_TOOLS:
        ann = tools[name].annotations
        assert ann is not None, f"{name} has no annotations"
        assert ann.readOnlyHint is True, f"{name} should be readOnlyHint=True"
        assert ann.destructiveHint is False, f"{name} should be destructiveHint=False"


async def test_mutation_tools_marked_destructive(server):
    tools = {t.name: t for t in await server.list_tools()}
    for name in MUTATION_TOOLS:
        ann = tools[name].annotations
        assert ann is not None, f"{name} has no annotations"
        assert ann.readOnlyHint is False, f"{name} should be readOnlyHint=False"
        assert ann.destructiveHint is True, f"{name} should be destructiveHint=True"


async def test_proxy_management_tools_not_exposed(server):
    """We intentionally don't expose proxy start/stop or rebuild_index over MCP."""
    names = {t.name for t in await server.list_tools()}
    forbidden = {"proxy_start", "proxy_stop", "start", "stop", "rebuild_index"}
    assert names.isdisjoint(forbidden)


async def test_search_returns_seeded_exchanges(server):
    result = await _call(server, "search_exchanges", limit=100)
    rows = result["result"]
    assert isinstance(rows, list)
    assert len(rows) == 3
    methods = {r["method"] for r in rows}
    assert methods == {"GET", "POST"}


async def test_search_filters_by_host(server):
    result = await _call(server, "search_exchanges", host="api.example.com", limit=100)
    rows = result["result"]
    assert len(rows) == 2
    assert all(r["host"] == "api.example.com" for r in rows)


async def test_search_filters_by_status(server):
    result = await _call(server, "search_exchanges", status=403, limit=100)
    rows = result["result"]
    assert len(rows) == 1
    assert rows[0]["status"] == 403
    assert rows[0]["method"] == "POST"


async def test_search_filters_compose(server):
    result = await _call(
        server,
        "search_exchanges",
        host="api.example.com",
        method="get",
        status=200,
        limit=100,
    )
    rows = result["result"]
    assert len(rows) == 1
    assert rows[0]["method"] == "GET"


async def test_search_pagination(server):
    page1 = (await _call(server, "search_exchanges", limit=2, offset=0))["result"]
    page2 = (await _call(server, "search_exchanges", limit=2, offset=2))["result"]
    assert len(page1) == 2
    assert len(page2) == 1
    ids_seen = {r["id"] for r in page1} | {r["id"] for r in page2}
    assert len(ids_seen) == 3


async def test_show_exchange_returns_request_and_response(server):
    rows = (await _call(server, "search_exchanges", method="POST", limit=10))["result"]
    eid = rows[0]["id"]

    out = await _call(server, "show_exchange", exchange_id=eid)
    assert out["id"] == eid
    assert out["request"]["method"] == "POST"
    assert out["response"]["status"] == 403
    # Body was decoded as text (JSON content-type)
    assert "text" in out["request"]["body"]
    assert "bob" in out["request"]["body"]["text"]
    assert "forbidden" in out["response"]["body"]["text"]


async def test_show_exchange_include_body_false(server):
    rows = (await _call(server, "search_exchanges", limit=1))["result"]
    eid = rows[0]["id"]
    out = await _call(server, "show_exchange", exchange_id=eid, include_body=False)
    assert "body" not in out["request"]
    assert "body" not in out["response"]


async def test_show_exchange_body_truncation(server):
    rows = (await _call(server, "search_exchanges", method="POST", limit=1))["result"]
    eid = rows[0]["id"]
    out = await _call(server, "show_exchange", exchange_id=eid, max_body_bytes=5)
    body = out["response"]["body"]
    assert body["truncated"] is True
    assert len(body["text"]) == 5


async def test_show_exchange_invalid_id_errors(server):
    """Invalid id should propagate as ToolError (translated to isError at the wire)."""
    from mcp.server.fastmcp.exceptions import ToolError

    with pytest.raises(ToolError):
        await server.call_tool("show_exchange", {"exchange_id": 999_999})


async def test_cat_exchange_response_body(server):
    rows = (await _call(server, "search_exchanges", method="POST", limit=1))["result"]
    eid = rows[0]["id"]
    out = await _call(server, "cat_exchange", exchange_id=eid, side="response")
    assert out["side"] == "response"
    assert "forbidden" in out["body"]["text"]


async def test_cat_exchange_request_body(server):
    rows = (await _call(server, "search_exchanges", method="POST", limit=1))["result"]
    eid = rows[0]["id"]
    out = await _call(server, "cat_exchange", exchange_id=eid, side="request")
    assert out["side"] == "request"
    assert "bob" in out["body"]["text"]


async def test_cat_exchange_invalid_side_errors(server):
    from mcp.server.fastmcp.exceptions import ToolError

    with pytest.raises(ToolError):
        await server.call_tool("cat_exchange", {"exchange_id": 1, "side": "neither"})


async def test_list_endpoints_returns_templates(server):
    rows = (await _call(server, "list_endpoints"))["result"]
    assert isinstance(rows, list)
    # Three exchanges → at least 2 distinct templates (GET /v1/users, POST /v1/users, GET /v1/items)
    templates = {(r["method"], r["path_template"]) for r in rows}
    assert ("GET", "/v1/users") in templates
    assert ("POST", "/v1/users") in templates
    assert ("GET", "/v1/items") in templates


async def test_list_services(server):
    rows = (await _call(server, "list_services"))["result"]
    keys = {r["service_key"] for r in rows}
    assert "api_example_com" in keys
    assert "api_other_com" in keys


async def test_diff_exchanges(server):
    rows = (await _call(server, "search_exchanges", host="api.example.com", limit=10))["result"]
    a_id = rows[0]["id"]
    b_id = rows[1]["id"]
    d = await _call(server, "diff_exchanges", a_id=a_id, b_id=b_id)
    assert isinstance(d, dict)
    # Statuses differ (200 vs 403) so a status diff must be reported somewhere.
    flat = repr(d)
    assert "200" in flat and "403" in flat


async def test_around_returns_window(server):
    rows = (await _call(server, "search_exchanges", host="api.example.com", limit=10))["result"]
    seed = rows[0]["id"]
    out = (await _call(server, "around", exchange_id=seed, window_seconds=60.0))["result"]
    assert isinstance(out, list)
    assert len(out) >= 1


async def test_proxy_status_reports_db_state(server, seeded_db_path: Path):
    out = await _call(server, "proxy_status")
    assert out["db_exists"] is True
    assert out["db_path"] == str(seeded_db_path)
    assert out["exchange_count"] >= 3


async def test_proxy_status_with_missing_db(tmp_path: Path):
    missing = tmp_path / "does-not-exist.sqlite3"
    server = build_server(db_path=str(missing))
    _, structured = await server.call_tool("proxy_status", {})
    assert structured["db_exists"] is False
    assert structured["exchange_count"] == 0


async def test_concurrent_read_calls_are_safe(server):
    """Multiple concurrent read calls on the same DB shouldn't deadlock or corrupt."""
    import asyncio

    results = await asyncio.gather(
        _call(server, "search_exchanges", limit=10),
        _call(server, "list_endpoints"),
        _call(server, "list_services"),
        _call(server, "search_exchanges", host="api.example.com"),
        _call(server, "search_exchanges", status=200),
    )
    assert len(results) == 5
    assert all(r is not None for r in results)


async def test_tool_descriptions_present(server):
    """Every tool should have a non-empty description (agents rely on these)."""
    tools = await server.list_tools()
    for t in tools:
        assert t.description, f"{t.name} missing description"
        assert len(t.description) > 20, f"{t.name} description too short"


async def test_tool_input_schemas_present(server):
    """Every tool should publish a JSON Schema for inputs."""
    tools = await server.list_tools()
    for t in tools:
        assert t.inputSchema is not None, f"{t.name} missing inputSchema"
        assert t.inputSchema.get("type") == "object", f"{t.name} schema not object"


@pytest.mark.parametrize("name", sorted(TOOL_NAMES))
def test_tool_name_in_canonical_list(name: str):
    """Sanity check: the canonical list and registration stay in sync."""
    assert name in (READ_ONLY_TOOLS | MUTATION_TOOLS)
