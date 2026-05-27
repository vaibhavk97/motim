"""Shared fixtures for MCP server tests.

Each test gets:
- a fresh SQLite exchange DB in a tmp path
- a small set of seeded exchanges (so search/show/diff/etc. have data)
- a FastMCP server bound to that DB

The DB path is plumbed through via ``build_server(db_path=...)`` and the
underlying tool helpers accept ``db_path`` directly, so we don't have to
monkeypatch ``get_config`` for tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from motim.exchange_db import ExchangeDB, HeaderField


@pytest.fixture
def seeded_db_path(tmp_path: Path) -> Path:
    """Create an ExchangeDB at a tmp path and seed it with sample exchanges.

    Returns the DB path. The DB is closed before the fixture yields so
    tools opening it in tests don't fight an open writer.
    """
    db_path = tmp_path / "motim.sqlite3"
    db = ExchangeDB(db_path)
    try:
        # Exchange 1: GET /v1/users 200 on api.example.com
        db.put_exchange(
            scheme="https",
            host="api.example.com",
            port=443,
            method="GET",
            path="/v1/users",
            query=None,
            url="https://api.example.com/v1/users",
            status=200,
            req_headers=[
                HeaderField("Authorization", "Bearer abc"),
                HeaderField("Accept", "application/json"),
            ],
            resp_headers=[HeaderField("Content-Type", "application/json")],
            req_body=None,
            resp_body=b'{"users":[{"id":1}]}',
            resp_content_type="application/json",
        )
        # Exchange 2: POST /v1/users 403 (auth failure) on api.example.com
        db.put_exchange(
            scheme="https",
            host="api.example.com",
            port=443,
            method="POST",
            path="/v1/users",
            query=None,
            url="https://api.example.com/v1/users",
            status=403,
            req_headers=[HeaderField("Content-Type", "application/json")],
            resp_headers=[HeaderField("Content-Type", "application/json")],
            req_body=b'{"name":"bob"}',
            resp_body=b'{"error":"forbidden"}',
            req_content_type="application/json",
            resp_content_type="application/json",
        )
        # Exchange 3: GET /v1/items 200 on api.other.com
        db.put_exchange(
            scheme="https",
            host="api.other.com",
            port=443,
            method="GET",
            path="/v1/items",
            query=None,
            url="https://api.other.com/v1/items",
            status=200,
            req_headers=[],
            resp_headers=[HeaderField("Content-Type", "application/json")],
            req_body=None,
            resp_body=b'{"items":[]}',
            resp_content_type="application/json",
        )
    finally:
        db.close()
    return db_path


@pytest.fixture
def server(seeded_db_path: Path):
    """Build a FastMCP server bound to the seeded DB."""
    from motim.mcp.server import build_server

    return build_server(db_path=str(seeded_db_path))
