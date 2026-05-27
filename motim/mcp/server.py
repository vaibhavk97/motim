"""MOTIM MCP server.

Exposes motim's search, inspect, and replay primitives as MCP tools so any
MCP-compatible agent (Claude Desktop, Cursor, Cline, Continue, Zed, etc.)
can drive motim without shelling out to the CLI.

Default transport is stdio. Read tools are marked ``readOnlyHint=True`` so
clients can auto-approve them; replay/probe/replay_sequence are mutations
and require explicit consent in clients that surface the destructive hint.

Usage:
    from motim.mcp.server import build_server
    server = build_server()
    server.run("stdio")
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import tools as _tools

SERVER_NAME = "motim"
SERVER_INSTRUCTIONS = (
    "motim captures browser HTTP(S) traffic into a SQLite DB. Use these "
    "tools to search captured exchanges, inspect bodies/headers, replay "
    "authenticated requests, diff responses, and probe endpoints for "
    "behavioral differences. Read tools are safe and free of side effects; "
    "replay/probe/replay_sequence send real network requests and write the "
    "resulting exchange back into the DB."
)

_READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
_MUTATION = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=True,
)


def build_server(*, db_path: str | None = None) -> FastMCP:
    """Construct a configured FastMCP server.

    ``db_path`` overrides the configured exchange DB path for every tool
    call. Useful in tests; in production the configured path is used.
    """

    server = FastMCP(name=SERVER_NAME, instructions=SERVER_INSTRUCTIONS)

    # --- Read tools -------------------------------------------------------

    @server.tool(
        name="search_exchanges",
        description=(
            "Search captured HTTP exchanges by host, method, status, path "
            "substring, or service. Returns a list of matching exchanges "
            "with id, method, host, path, status, and size."
        ),
        annotations=_READ_ONLY,
    )
    def search_exchanges(
        host: str | None = None,
        method: str | None = None,
        status: int | None = None,
        path_contains: str | None = None,
        service: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        return _tools.search_exchanges(
            host=host,
            method=method,
            status=status,
            path_contains=path_contains,
            service=service,
            limit=limit,
            offset=offset,
            db_path=db_path,
        )

    @server.tool(
        name="show_exchange",
        description=(
            "Return the full request and response (method, URL, headers, "
            "decoded body) for a captured exchange by id. Set "
            "include_body=False to skip bodies. Bodies are capped at "
            "max_body_bytes (default 100000) and truncation is reported."
        ),
        annotations=_READ_ONLY,
    )
    def show_exchange(
        exchange_id: int,
        include_body: bool = True,
        max_body_bytes: int = _tools.DEFAULT_MAX_BODY_BYTES,
    ) -> dict[str, Any]:
        return _tools.show_exchange(
            exchange_id=exchange_id,
            include_body=include_body,
            max_body_bytes=max_body_bytes,
            db_path=db_path,
        )

    @server.tool(
        name="cat_exchange",
        description=(
            "Return only the body of a captured exchange. side='response' "
            "(default) or 'request'. Useful when the agent only needs to "
            "see the payload, not headers."
        ),
        annotations=_READ_ONLY,
    )
    def cat_exchange(
        exchange_id: int,
        side: str = "response",
        max_body_bytes: int = _tools.DEFAULT_MAX_BODY_BYTES,
    ) -> dict[str, Any]:
        return _tools.cat_exchange(
            exchange_id=exchange_id,
            side=side,
            max_body_bytes=max_body_bytes,
            db_path=db_path,
        )

    @server.tool(
        name="list_endpoints",
        description=(
            "List discovered endpoint templates (path with {id} "
            "placeholders) with observed request count and success rate. "
            "Filter by service, method, or path substring."
        ),
        annotations=_READ_ONLY,
    )
    def list_endpoints(
        service: str | None = None,
        method: str | None = None,
        path_contains: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        return _tools.list_endpoints(
            service=service,
            method=method,
            path_contains=path_contains,
            limit=limit,
            offset=offset,
            db_path=db_path,
        )

    @server.tool(
        name="list_services",
        description=(
            "List captured services (one per hostname) with exchange "
            "counts and last-seen timestamps."
        ),
        annotations=_READ_ONLY,
    )
    def list_services() -> list[dict[str, Any]]:
        return _tools.list_services(db_path=db_path)

    @server.tool(
        name="diff_exchanges",
        description=(
            "Diff two captured exchanges (by id). Returns header and body "
            "differences. Useful for comparing a replay against its "
            "baseline, or two responses to similar requests."
        ),
        annotations=_READ_ONLY,
    )
    def diff_exchanges(a_id: int, b_id: int) -> dict[str, Any]:
        return _tools.diff_exchanges_tool(a_id=a_id, b_id=b_id, db_path=db_path)

    @server.tool(
        name="around",
        description=(
            "Return exchanges captured within window_seconds of a seed "
            "exchange (same service). Use to reconstruct what happened "
            "around an interesting request."
        ),
        annotations=_READ_ONLY,
    )
    def around(
        exchange_id: int,
        window_seconds: float = 10.0,
        service: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        return _tools.around(
            exchange_id=exchange_id,
            window_seconds=window_seconds,
            service=service,
            limit=limit,
            db_path=db_path,
        )

    @server.tool(
        name="session",
        description=(
            "Reconstruct a best-effort session slice around an exchange "
            "(same service, bounded by gap_seconds idle gaps and id_window)."
        ),
        annotations=_READ_ONLY,
    )
    def session(
        exchange_id: int,
        gap_seconds: float = 120.0,
        id_window: int = 5000,
        limit: int = 500,
        filter_noise: bool = True,
    ) -> dict[str, Any]:
        return _tools.session(
            exchange_id=exchange_id,
            gap_seconds=gap_seconds,
            id_window=id_window,
            limit=limit,
            filter_noise=filter_noise,
            db_path=db_path,
        )

    @server.tool(
        name="linkfinder",
        description=(
            "Extract endpoint/URL hints from captured JavaScript bundles "
            "(LinkFinder-style). Filter by host, service, or regex."
        ),
        annotations=_READ_ONLY,
    )
    def linkfinder(
        host: str | None = None,
        service: str | None = None,
        regex: str | None = None,
        limit_assets: int = 200,
        limit: int = 500,
        beautify: bool = False,
    ) -> list[dict[str, Any]]:
        return _tools.linkfinder(
            host=host,
            service=service,
            regex=regex,
            limit_assets=limit_assets,
            limit=limit,
            beautify=beautify,
            db_path=db_path,
        )

    @server.tool(
        name="proxy_status",
        description=(
            "Report capture status: DB path, whether it exists, total "
            "exchange count, and whether mitmdump is available. Does not "
            "start or stop the proxy."
        ),
        annotations=_READ_ONLY,
    )
    def proxy_status() -> dict[str, Any]:
        return _tools.proxy_status(db_path=db_path)

    # --- Mutation tools ---------------------------------------------------

    @server.tool(
        name="replay",
        description=(
            "Replay a captured exchange. Optionally mutate via "
            "patch_json (JSON merge patch), set_headers, drop_headers, "
            "or override origin. Sends a real network request and writes "
            "the result to the DB. Returns the new replay exchange id."
        ),
        annotations=_MUTATION,
    )
    def replay(
        exchange_id: int,
        patch_json: list[Any] | dict[str, Any] | None = None,
        set_headers: dict[str, str] | None = None,
        drop_headers: list[str] | None = None,
        origin: str | None = None,
        transport: str = "httpx",
        impersonate: str | None = None,
        timeout: float = 30.0,
        http2: bool = True,
        tag: str | None = None,
    ) -> dict[str, Any]:
        return _tools.replay(
            exchange_id=exchange_id,
            patch_json=patch_json,
            set_headers=set_headers,
            drop_headers=drop_headers,
            origin=origin,
            transport=transport,
            impersonate=impersonate,
            timeout=timeout,
            http2=http2,
            tag=tag,
            db_path=db_path,
        )

    @server.tool(
        name="probe",
        description=(
            "Run a baseline replay plus N mutations (patch_json entries "
            "and drop_headers entries), diffing each run against the "
            "baseline. Used for behavioral / authorization probing."
        ),
        annotations=_MUTATION,
    )
    def probe(
        exchange_id: int,
        patch_json: list[Any] | None = None,
        drop_headers: list[str] | None = None,
        origin: str | None = None,
        transport: str = "httpx",
        impersonate: str | None = None,
        timeout: float = 30.0,
        http2: bool = True,
        tag: str | None = None,
    ) -> dict[str, Any]:
        return _tools.probe(
            exchange_id=exchange_id,
            patch_json=patch_json,
            drop_headers=drop_headers,
            origin=origin,
            transport=transport,
            impersonate=impersonate,
            timeout=timeout,
            http2=http2,
            tag=tag,
            db_path=db_path,
        )

    @server.tool(
        name="replay_sequence",
        description=(
            "Replay a list of exchanges in order. Useful for "
            "multi-step flows (login → fetch → update). Each replay "
            "result is stored to the DB."
        ),
        annotations=_MUTATION,
    )
    def replay_sequence(
        exchange_ids: list[int],
        transport: str = "httpx",
        impersonate: str | None = None,
        origin: str | None = None,
        timeout: float = 30.0,
        http2: bool = True,
        tag: str | None = None,
    ) -> list[dict[str, Any]]:
        return _tools.replay_sequence(
            exchange_ids=exchange_ids,
            transport=transport,
            impersonate=impersonate,
            origin=origin,
            timeout=timeout,
            http2=http2,
            tag=tag,
            db_path=db_path,
        )

    return server


# Names of tools exposed, in registration order. Used by tests + docs.
TOOL_NAMES: tuple[str, ...] = (
    "search_exchanges",
    "show_exchange",
    "cat_exchange",
    "list_endpoints",
    "list_services",
    "diff_exchanges",
    "around",
    "session",
    "linkfinder",
    "proxy_status",
    "replay",
    "probe",
    "replay_sequence",
)

READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "search_exchanges",
        "show_exchange",
        "cat_exchange",
        "list_endpoints",
        "list_services",
        "diff_exchanges",
        "around",
        "session",
        "linkfinder",
        "proxy_status",
    }
)

MUTATION_TOOLS: frozenset[str] = frozenset({"replay", "probe", "replay_sequence"})


def main() -> None:
    """Run the MCP server over stdio."""
    server = build_server()
    server.run("stdio")


if __name__ == "__main__":
    main()
