"""Tests for replay/probe/replay_sequence MCP tools.

Uses httpx.MockTransport (same pattern as tests/test_agent_replay.py) so
no real network calls are made.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from motim.mcp.server import build_server


async def _call(server, name: str, **kwargs):
    _, structured = await server.call_tool(name, kwargs)
    return structured


@pytest.fixture
def mock_httpx(monkeypatch):
    """Monkeypatch httpx.Client so replay sends through a MockTransport.

    Returns a list that collects (method, url, headers, body) for every
    request the test triggers, so assertions can verify what got sent.
    """
    sent: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(
            (
                request.method,
                str(request.url),
                {k: v for k, v in request.headers.items()},
                request.content or None,
            )
        )
        # Echo body length in a header so tests can spot mutations
        return httpx.Response(
            200,
            headers={
                "Content-Type": "application/json",
                "X-Echo-Len": str(len(request.content or b"")),
            },
            content=b'{"ok":true}',
        )

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", factory)
    return sent


async def test_replay_sends_request_and_records_result(server, mock_httpx):
    rows = (await _call(server, "search_exchanges", method="POST", limit=1))["result"]
    eid = rows[0]["id"]

    out = await _call(server, "replay", exchange_id=eid)
    assert out["original_id"] == eid
    assert out["replay_id"] != eid
    assert out["status"] == 200
    assert len(mock_httpx) == 1
    method, url, _, _ = mock_httpx[0]
    assert method == "POST"
    assert "api.example.com" in url


async def test_replay_with_patch_json_mutates_body(server, mock_httpx):
    rows = (await _call(server, "search_exchanges", method="POST", limit=1))["result"]
    eid = rows[0]["id"]

    out = await _call(server, "replay", exchange_id=eid, patch_json={"name": "alice"})
    assert out["status"] == 200
    _, _, _, body = mock_httpx[0]
    assert body is not None
    assert b"alice" in body
    # original 'bob' should be replaced by the merge patch
    assert b"bob" not in body


async def test_replay_with_set_headers(server, mock_httpx):
    rows = (await _call(server, "search_exchanges", method="POST", limit=1))["result"]
    eid = rows[0]["id"]

    await _call(
        server,
        "replay",
        exchange_id=eid,
        set_headers={"X-Custom": "yes"},
    )
    _, _, headers, _ = mock_httpx[0]
    # headers keys are normalized lowercase by httpx
    assert headers.get("x-custom") == "yes"


async def test_replay_with_drop_headers(server, mock_httpx):
    # Seed a GET that has Authorization; the read fixture's GET exchange does.
    rows = (await _call(server, "search_exchanges", method="GET", host="api.example.com", limit=1))[
        "result"
    ]
    eid = rows[0]["id"]

    await _call(
        server,
        "replay",
        exchange_id=eid,
        drop_headers=["Authorization"],
    )
    _, _, headers, _ = mock_httpx[0]
    assert "authorization" not in {k.lower() for k in headers}


async def test_probe_returns_baseline_plus_runs(server, mock_httpx):
    rows = (await _call(server, "search_exchanges", method="POST", limit=1))["result"]
    eid = rows[0]["id"]

    out = await _call(
        server,
        "probe",
        exchange_id=eid,
        patch_json=[{"name": "alice"}, {"role": "admin"}],
        drop_headers=["Content-Type"],
    )
    assert out["original_id"] == eid
    assert "baseline_id" in out
    runs = out["runs"]
    # 2 patch runs + 1 drop_header run = 3 runs
    assert len(runs) == 3
    kinds = [r["kind"] for r in runs]
    assert kinds.count("patch_json") == 2
    assert kinds.count("drop_header") == 1
    # 1 baseline + 3 mutation requests = 4 real sends
    assert len(mock_httpx) == 4


async def test_probe_runs_have_diffs(server, mock_httpx):
    rows = (await _call(server, "search_exchanges", method="POST", limit=1))["result"]
    eid = rows[0]["id"]

    out = await _call(server, "probe", exchange_id=eid, patch_json=[{"x": 1}])
    for run in out["runs"]:
        assert "diff" in run
        assert isinstance(run["diff"], dict)


async def test_replay_sequence_replays_in_order(server, mock_httpx):
    rows = (await _call(server, "search_exchanges", limit=10))["result"]
    ids = [r["id"] for r in rows[:2]]

    out = await _call(server, "replay_sequence", exchange_ids=ids)
    result_list = out["result"]
    assert [r["original_id"] for r in result_list] == ids
    assert len(mock_httpx) == 2


async def test_replay_sequence_empty_errors(server):
    from mcp.server.fastmcp.exceptions import ToolError

    with pytest.raises(ToolError):
        await server.call_tool("replay_sequence", {"exchange_ids": []})


async def test_replay_invalid_id_errors(server, mock_httpx):
    from mcp.server.fastmcp.exceptions import ToolError

    with pytest.raises(ToolError):
        await server.call_tool("replay", {"exchange_id": 999_999})
    # Should not have actually sent a request
    assert len(mock_httpx) == 0


async def test_build_server_with_default_db_path_does_not_open_db(tmp_path: Path):
    """Construction should not touch the DB until a tool is invoked."""
    server = build_server(db_path=str(tmp_path / "never.sqlite3"))
    assert server is not None
    # No exception during build
