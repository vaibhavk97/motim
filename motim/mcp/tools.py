"""Tool implementations for the MOTIM MCP server.

Each function wraps an existing motim primitive (ExchangeDB query, replay,
diff) and returns plain JSON-serializable data. The MCP server module
(``server.py``) registers these with FastMCP and handles transport.

These functions are intentionally pure with respect to argument parsing:
they accept already-typed kwargs (matching the MCP tool schema) and let
``ExchangeDB`` raise on bad input. Errors propagate to FastMCP, which
serializes them as ``isError`` content blocks the agent can read.
"""

from __future__ import annotations

import gzip
import json
import zlib
from pathlib import Path
from typing import Any

from motim.agent_replay import diff_exchanges, replay_exchange
from motim.config import get_config
from motim.exchange_db import ExchangeDB

# Cap body payloads we return through MCP to keep results small for the
# agent. Callers can raise this via ``max_bytes`` per-call.
DEFAULT_MAX_BODY_BYTES = 100_000


def _open_db(db_path: str | None = None) -> ExchangeDB:
    cfg = get_config()
    path = db_path or cfg.capture.exchange_db_path
    return ExchangeDB(Path(path).expanduser(), max_body_bytes=cfg.capture.max_body_bytes)


def _get_header(headers: list[dict[str, str]], name: str) -> str | None:
    lower = name.lower()
    for h in headers:
        if h["name"].lower() == lower:
            return h["value"]
    return None


def _decode_body(
    body: bytes | None,
    content_type: str | None,
    content_encoding: str | None,
    *,
    max_bytes: int,
) -> dict[str, Any]:
    """Decode a captured body to a JSON-friendly representation.

    Returns ``{"text": str, "truncated": bool}`` for textual bodies,
    ``{"binary": True, "size": n}`` for binary, or ``{"missing": True}``
    when the body is None.
    """
    if body is None or (isinstance(body, bytes) and len(body) == 0):
        return {"missing": True}

    ct = (content_type or "").lower()
    is_text = any(
        t in ct
        for t in (
            "text/",
            "json",
            "xml",
            "html",
            "javascript",
            "ecmascript",
            "form-urlencoded",
        )
    )
    if not is_text:
        return {"binary": True, "size": len(body)}

    data = body
    enc = (content_encoding or "").lower()
    if enc == "gzip":
        try:
            data = gzip.decompress(data)
        except Exception:
            pass
    elif enc in ("deflate", "zlib"):
        try:
            data = zlib.decompress(data)
        except Exception:
            try:
                data = zlib.decompress(data, -zlib.MAX_WBITS)
            except Exception:
                pass

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = data.decode("latin-1")
        except Exception:
            return {"binary": True, "size": len(body)}

    truncated = False
    if len(text) > max_bytes:
        text = text[:max_bytes]
        truncated = True
    return {"text": text, "truncated": truncated}


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


def search_exchanges(
    *,
    host: str | None = None,
    method: str | None = None,
    status: int | None = None,
    path_contains: str | None = None,
    service: str | None = None,
    limit: int = 20,
    offset: int = 0,
    db_path: str | None = None,
) -> list[dict[str, Any]]:
    """Search captured exchanges. Mirrors ``motim search --json``."""
    with _open_db(db_path) as db:
        resolved_key = None
        if service:
            resolved_key = db.resolve_service_key(service) or service
        return db.search_exchanges(
            service_key=resolved_key,
            host=host,
            method=method.upper() if method else None,
            status=status,
            path_contains=path_contains,
            limit=limit,
            offset=offset,
        )


def show_exchange(
    *,
    exchange_id: int,
    include_body: bool = True,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Return the full request + response for an exchange."""
    with _open_db(db_path) as db:
        ex = db.get_exchange(exchange_id)
        req_headers = ex["headers"]["request"]
        resp_headers = ex["headers"]["response"]

        req_ct = ex.get("req_content_type") or _get_header(req_headers, "content-type") or ""
        resp_ct = ex.get("resp_content_type") or _get_header(resp_headers, "content-type") or ""
        req_enc = _get_header(req_headers, "content-encoding")
        resp_enc = _get_header(resp_headers, "content-encoding")

        request: dict[str, Any] = {
            "method": ex.get("method"),
            "url": ex.get("url"),
            "headers": {h["name"]: h["value"] for h in req_headers},
        }
        response: dict[str, Any] = {
            "status": ex.get("status"),
            "headers": {h["name"]: h["value"] for h in resp_headers},
        }
        if include_body:
            request["body"] = _decode_body(
                ex["bodies"]["request"], req_ct, req_enc, max_bytes=max_body_bytes
            )
            response["body"] = _decode_body(
                ex["bodies"]["response"], resp_ct, resp_enc, max_bytes=max_body_bytes
            )
        return {"id": exchange_id, "request": request, "response": response}


def cat_exchange(
    *,
    exchange_id: int,
    side: str = "response",
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Return only the request or response body for an exchange."""
    if side not in ("request", "response"):
        raise ValueError("side must be 'request' or 'response'")
    with _open_db(db_path) as db:
        ex = db.get_exchange(exchange_id)
        body = ex["bodies"][side]
        headers = ex["headers"][side]
        ct_field = "req_content_type" if side == "request" else "resp_content_type"
        ct = ex.get(ct_field) or _get_header(headers, "content-type") or ""
        enc = _get_header(headers, "content-encoding")
        decoded = _decode_body(body, ct, enc, max_bytes=max_body_bytes)
        return {"id": exchange_id, "side": side, "body": decoded}


def list_endpoints(
    *,
    service: str | None = None,
    method: str | None = None,
    path_contains: str | None = None,
    limit: int = 200,
    offset: int = 0,
    db_path: str | None = None,
) -> list[dict[str, Any]]:
    """Return discovered endpoint templates."""
    with _open_db(db_path) as db:
        return db.endpoint_summaries(
            service=service,
            method=method.upper() if method else None,
            path_contains=path_contains,
            limit=limit,
            offset=offset,
        )


def list_services(*, db_path: str | None = None) -> list[dict[str, Any]]:
    """Return captured services."""
    with _open_db(db_path) as db:
        return db.service_summaries()


def diff_exchanges_tool(*, a_id: int, b_id: int, db_path: str | None = None) -> dict[str, Any]:
    """Diff two stored exchanges."""
    with _open_db(db_path) as db:
        a = db.get_exchange(a_id)
        b = db.get_exchange(b_id)
        return diff_exchanges(a, b)


def around(
    *,
    exchange_id: int,
    window_seconds: float = 10.0,
    service: str | None = None,
    limit: int = 200,
    db_path: str | None = None,
) -> list[dict[str, Any]]:
    """Return a time-window slice of exchanges around a seed."""
    with _open_db(db_path) as db:
        return db.exchanges_around(
            exchange_id,
            window_seconds=window_seconds,
            service_key=service,
            limit=limit,
        )


def session(
    *,
    exchange_id: int,
    gap_seconds: float = 120.0,
    id_window: int = 5000,
    limit: int = 500,
    filter_noise: bool = True,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Reconstruct a best-effort session slice around an exchange."""
    with _open_db(db_path) as db:
        return db.session_slice(
            exchange_id,
            gap_seconds=gap_seconds,
            id_window=id_window,
            limit=limit,
            filter_noise=filter_noise,
        )


def linkfinder(
    *,
    host: str | None = None,
    service: str | None = None,
    limit_assets: int = 200,
    limit: int = 500,
    regex: str | None = None,
    beautify: bool = False,
    db_path: str | None = None,
) -> list[dict[str, Any]]:
    """Extract endpoint/URL hints from captured JS bundles."""
    with _open_db(db_path) as db:
        return db.linkfinder_js(
            host=host,
            service=service,
            limit_assets=limit_assets,
            limit=limit,
            filter_regex=regex,
            beautify=beautify,
        )


def proxy_status(*, db_path: str | None = None) -> dict[str, Any]:
    """Return basic proxy / capture status.

    Reports whether the SQLite DB exists, how many exchanges are stored,
    and whether the mitmdump binary is available on PATH. Does not start
    or stop the proxy.
    """
    import shutil

    cfg = get_config()
    path = Path(db_path or cfg.capture.exchange_db_path).expanduser()
    db_exists = path.exists()
    exchange_count = 0
    if db_exists:
        try:
            with _open_db(db_path) as db:
                summaries = db.service_summaries()
                total = 0
                for s in summaries:
                    n = s.get("exchanges") or 0
                    if isinstance(n, (int, float)):
                        total += int(n)
                exchange_count = total
        except Exception:
            exchange_count = 0
    return {
        "db_path": str(path),
        "db_exists": db_exists,
        "exchange_count": exchange_count,
        "mitmdump_available": shutil.which("mitmdump") is not None,
    }


# ---------------------------------------------------------------------------
# Mutation tools (side effects: send a real HTTP request, write to DB)
# ---------------------------------------------------------------------------


def replay(
    *,
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
    db_path: str | None = None,
) -> dict[str, Any]:
    """Replay a captured exchange, optionally mutating it first."""
    patches: list[Any] = []
    if patch_json is not None:
        patches = patch_json if isinstance(patch_json, list) else [patch_json]

    set_pairs: tuple[str, ...] = tuple(f"{k}={v}" for k, v in (set_headers or {}).items())
    drop_tuple: tuple[str, ...] = tuple(drop_headers or [])

    with _open_db(db_path) as db:
        result = replay_exchange(
            db,
            exchange_id,
            tag=tag,
            transport=transport,
            impersonate=impersonate,
            origin=origin,
            set_headers=set_pairs,
            drop_headers=drop_tuple,
            json_patches=patches,
            timeout=timeout,
            http2=http2,
        )
        return {
            "original_id": result.original_id,
            "replay_id": result.replay_id,
            "replay_record_id": result.replay_record_id,
            "status": result.status,
            "url": result.url,
            "notes": result.notes,
        }


def probe(
    *,
    exchange_id: int,
    patch_json: list[Any] | None = None,
    drop_headers: list[str] | None = None,
    origin: str | None = None,
    transport: str = "httpx",
    impersonate: str | None = None,
    timeout: float = 30.0,
    http2: bool = True,
    tag: str | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Replay baseline, then each mutation; diff each run against baseline."""
    patches = list(patch_json or [])
    dropped = list(drop_headers or [])

    with _open_db(db_path) as db:
        baseline = replay_exchange(
            db,
            exchange_id,
            tag=tag,
            transport=transport,
            impersonate=impersonate,
            origin=origin,
            timeout=timeout,
            http2=http2,
        )
        baseline_ex = db.get_exchange(baseline.replay_id)

        runs: list[dict[str, Any]] = []
        for p in patches:
            r = replay_exchange(
                db,
                exchange_id,
                tag=tag,
                transport=transport,
                impersonate=impersonate,
                origin=origin,
                json_patches=[p],
                timeout=timeout,
                http2=http2,
            )
            run_ex = db.get_exchange(r.replay_id)
            runs.append(
                {
                    "kind": "patch_json",
                    "patch": p,
                    "replay_id": r.replay_id,
                    "status": r.status,
                    "diff": diff_exchanges(baseline_ex, run_ex),
                }
            )

        for h in dropped:
            r = replay_exchange(
                db,
                exchange_id,
                tag=tag,
                transport=transport,
                impersonate=impersonate,
                origin=origin,
                drop_headers=[h],
                timeout=timeout,
                http2=http2,
            )
            run_ex = db.get_exchange(r.replay_id)
            runs.append(
                {
                    "kind": "drop_header",
                    "header": h,
                    "replay_id": r.replay_id,
                    "status": r.status,
                    "diff": diff_exchanges(baseline_ex, run_ex),
                }
            )

        return {
            "original_id": exchange_id,
            "baseline_id": baseline.replay_id,
            "baseline_status": baseline.status,
            "runs": runs,
        }


def replay_sequence(
    *,
    exchange_ids: list[int],
    transport: str = "httpx",
    impersonate: str | None = None,
    origin: str | None = None,
    timeout: float = 30.0,
    http2: bool = True,
    tag: str | None = None,
    db_path: str | None = None,
) -> list[dict[str, Any]]:
    """Replay a sequence of exchanges in order."""
    if not exchange_ids:
        raise ValueError("exchange_ids must not be empty")
    out: list[dict[str, Any]] = []
    with _open_db(db_path) as db:
        for eid in exchange_ids:
            r = replay_exchange(
                db,
                int(eid),
                tag=tag,
                transport=transport,
                impersonate=impersonate,
                origin=origin,
                timeout=timeout,
                http2=http2,
            )
            out.append(
                {
                    "original_id": int(eid),
                    "replay_id": r.replay_id,
                    "status": r.status,
                }
            )
    return out


__all__ = [
    "search_exchanges",
    "show_exchange",
    "cat_exchange",
    "list_endpoints",
    "list_services",
    "diff_exchanges_tool",
    "around",
    "session",
    "linkfinder",
    "proxy_status",
    "replay",
    "probe",
    "replay_sequence",
    "DEFAULT_MAX_BODY_BYTES",
]


# Re-export the json module so tests can use ``motim.mcp.tools.json`` if they
# need to round-trip results without a separate import. Harmless otherwise.
_json = json
